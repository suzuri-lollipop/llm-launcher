"""ワンクリック・キャリブレーション: Maya / Strata の「この PC 向け調整」を launcher から実行する。

背景:
  Maya (module/maya) と Strata (module/strata) はどちらも「この PC でモデルを最速にする設定」を
  実測で決めるキャリブレーション機能を持つが、どちらも CLI の手順を伴う:

  Maya   `maya.sh --calibrate --no-start --config <maya-<model>.json>`
         → 起動せずに設定を実測調整し、そのまま config へ書き込んで終了する (10-15 分 / DS4 は 25-40 分)。

  Strata `strata.py engine calibrate --profile <名前> --measure --exclusive --out <report.json>`
         → `--review-result <report.json>` → `--apply-result <report.json> --exclusive` の順に実行する。
         measure / apply はどちらもエンジン起動を伴い、GPU を独占していなければならない
         (保存済み profile が必須。launcher のプロファイルから同名の Strata profile を作って使う)。

  launcher 側はこれを「1 ボタン」で実行できるようにする:
    - バックエンドごとのコマンド列をここで組み立てる (UI/API は backend とプロファイルだけ指定する)
    - GPU 独占が必要なので、同じプロファイルのサーバーが稼働中なら先に停止してから実行する
    - 10-40 分の長時間処理なので、subprocess をバックグラウンドで走らせ、ログを逐行取り込み、
      進行状況を live で返す (ベンチマークと同じ fire-and-forget + poll の形)
    - 実行は 1 件ずつ (GPU 独占 / エンジン起動が衝突するため)。キャンセルはプロセスグループごと SIGTERM
    - 結果は data/calibrations.json に履歴として残す

  失敗の見分け方 (どちらも実機で確認した本物の CLI の癖):
    Maya   「エンジンが起動できなかった」場合でも **終了コードは 0** のまま (警告を出して config を
           書き換えない)。終了コードだけで失敗を見ると「成功」と偽ることになるので、調整の証拠
           (config の installer.calibration) を確認して、無ければ理由付きで失敗にする (_verify_maya_tuned)。
    Strata measure は 15-30 分のエンジン起動を伴うので、最初に読み取り専用の `--plan` 事前検査を走らせ、
           コンポーネント欠損 (例: 視覚エンコーダ vision/mmproj) を時間の無駄の前に止める (_plan_failure)。

  CLI の細部 (引数名・--output json の形・終了コード) は module 側に依存するため、
  コマンド組み立てをこのファイルに集め、tests/test_calibrate.py が本物の CLI と照合する。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
import time
from pathlib import Path
from typing import Any

from .backends import BACKENDS, backend_for
from .oom import apply_score, meminfo
from .process import pid_alive
from .store import JsonStore, new_id, now

# キャリブレーションを提供するバックエンド (fork も基底と同じ CLI を使う)
CAL_BACKENDS = {"maya": "maya", "strata": "strata", "strata-fork": "strata"}

# ログはデータベース (calibrations.json) に保持するが、長い実行では件数が増えるので
# 直近 LOG_TAIL 行だけ残す (cursor は「残っている行の添字」= UI は len から取り直す)。
LOG_TAIL = 400


class CalibrateError(Exception):
    """ユーザーに提示するキャリブレーション開始 / 実行失敗。"""


class _Cancelled(Exception):
    """キャンセル検知 (内部シグナル)。"""


# ----------------------------------------------------------------- 名前 / パス


def strata_profile_name(profile: dict[str, Any]) -> str:
    """launcher のプロファイル → Strata の保存 profile 名 (小文字 a-z0-9_- のみ)。

    Strata の profile 名は小文字始まりの a-z0-9_- のみ。launcher の id (p_xxxxxxxx) は
    そのままだと使えないため正規化する。同じ id は常に同じ名前 → 何度実行しても同じ profile を再利用可。"""
    raw = str(profile.get("id") or "")
    safe = re.sub(r"[^a-z0-9_-]", "-", raw.lower())
    safe = re.sub(r"-+", "-", safe).strip("-_")
    if not safe or not safe[0].islower():
        safe = "llm-" + (safe or "profile")
    return safe[:60]


def _resolve_config(root: Path, module_dir: Path, profile: dict[str, Any]) -> str:
    """プロファイルの values.config を実在する絶対パスに解決する。

    相対パスの基底: バックエンドは module ディレクトリを cwd に起動するので
    (process.start: profile.cwd 空 = root だが Maya/Strata は module 側の相対を読む)、
    root と module ディレクトリの両方を試す (プロファイル実例: "maya-ds4.json" =
    module/maya/maya-ds4.json)。"""
    raw = str((profile.get("values") or {}).get("config") or "").strip()
    if not raw:
        return ""
    p = Path(raw)
    cands = [p] if p.is_absolute() else [root / p, module_dir / p]
    for c in cands:
        if c.is_file():
            return str(c.resolve())
    return ""


def maya_config_path(root: Path, profile: dict[str, Any]) -> str:
    """Maya のキャリブレーション対象 config。

    プロファイルに --config がある場合はそれが実在しないと "" (開始時に明確に失敗する —
    別の config を黙って調整するとユーザーの意図したモデル設定が変わってしまうため)。
    --config 未指定 (送信 OFF) なら module/maya の最新 maya-*.json を対象にする。"""
    raw = str((profile.get("values") or {}).get("config") or "").strip()
    if raw:
        return _resolve_config(root, root / "module" / "maya", profile)
    cands = sorted((root / "module" / "maya").glob("maya-*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return str(cands[0]) if cands else ""


def strata_config_path(root: Path, profile: dict[str, Any]) -> str:
    """Strata のキャリブレーション対象 config (プロファイルの --config 必須・実在必須)。"""
    return _resolve_config(root, root / "module" / "strata", profile)


# ----------------------------------------------------------------- コマンド


def build_maya_command(root: Path, profile: dict[str, Any], python: str) -> list[str]:
    """Maya: `sh maya.sh --calibrate --no-start --config <cfg>`。

    --no-start: 調整だけしてダッシュボードを起動しない (launcher が管理するサーバーと衝突しない)。
    --config:   対象 config を明示する (対話なし, 最近使った config の選択を避ける)。
    maya.sh は module/maya/.venv を用意して maya.py を exec する (GUI .venv を pip install で汚さない)。
    python 引数は使わない (maya.sh 自身が python を探す) が、シグネチャは Strata と揃えておく。"""
    cfg = maya_config_path(root, profile)
    argv = ["sh", str(root / "module" / "maya" / "maya.sh"), "--calibrate", "--no-start"]
    if cfg:
        argv += ["--config", cfg]
    return argv


def build_strata_commands(root: Path, profile: dict[str, Any], python: str,
                          report: str, profile_name: str) -> dict[str, list[str]]:
    """Strata: profile create → measure → review → apply。

    profile create: launcher プロファイルの config から Strata の保存 profile を作る
                    (同じ id なら同じ名前なので、既に保存済みなら create は idempotent に失敗せず再利用する
                     … 実は既存なら "profile ... already saved" で失敗するので、実行前に存在確認を行う)。
    --exclusive:    measure と apply はエンジンを起動して GPU を独占する宣言。launcher が同じプロファイルの
                    サーバーを止めた状態でだけ実行するので付けてよい (ユーザーの明示操作 1 クリック = この宣言)。
    --output json:  機械可読 ({"schema_version":1,"ok":...}) で結果を受け取る。"""
    cfg = strata_config_path(root, profile)
    script = str(root / "module" / "strata" / "strata.py")
    return {
        "create": [python, script, "profile", "create", profile_name,
                   "--from-config", cfg, "--output", "json"],
        # --plan: 読み取り専用の事前検査。エンジンを起動せずに、コンポーネントの欠損
        #         (例: 視覚エンコーダ mmproj が無い) を 15-30 分の測定の前に見つける。
        "plan": [python, script, "engine", "calibrate", "--profile", profile_name,
                 "--plan", "--output", "json"],
        "measure": [python, script, "engine", "calibrate", "--profile", profile_name,
                    "--measure", "--exclusive", "--out", report, "--output", "json"],
        "review": [python, script, "engine", "calibrate", "--profile", profile_name,
                   "--review-result", report, "--output", "json"],
        "apply": [python, script, "engine", "calibrate", "--profile", profile_name,
                  "--apply-result", report, "--exclusive", "--output", "json"],
    }


# ----------------------------------------------------------------- メモリガード


# Maya の DS4 系キャリブレーションは最後に `--mmap-experts off` を試す。これはエンジンの
# 専門家 (expert) の重みのうち VRAM に載らない分を **RAM へまるごとコピーする**。
#
# 実測 (2026-10-24 03:22 / 2026-10-25 06:58, 121 GB のマシン):
#   `11724 routed experts on the CPU lane (81.0 GB, in place in the model's files)` の 81 GB を
#   そのまま RAM にコピーし、エンジン自身の重み (~9 GB) と launcher の常駐分と合わせて
#   117.2 GB に達した。このとき MemAvailable は 118.8 GB あった (ギリギリ足りる計算だった) が、
#   ユーザースライスのメモリ圧迫が 65.8% > 50% を 20 秒超えたため **systemd-oomd** が
#   launcher の cgroup を kill した (`Failed with result 'oom-kill'`)。
#
# 「空きが足りている」と実際に使えるのは同じではない (page cache / GPU のピン留め分 / 断片化)。
# 余裕を厚めに取る: コピー 81 GB + エンジン 9 GB + 余裕 20 GB = 110 GB。
DS4_MMAP_OFF_GB = 81.0     # 実測 75.5〜81.0 GB (CPU lane の専門家の総量)
DS4_ENGINE_RESIDENT_GB = 9.0   # エンジン自身の重み (CUDA0 4.48 + CUDA1 4.73 GB)
DS4_MMAP_MARGIN_GB = 20.0      # page cache / GPU ピン留め / カーネル断片化の余裕


def _is_ds4(config_path: str) -> bool:
    """Maya の DS4 (DeepSeek-V4.1-Flash-1017) 系の config かを、モデル名で判定する。"""
    try:
        cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    blob = json.dumps(cfg, ensure_ascii=False).lower()
    return "ds4" in blob or "deepseek-v4" in blob


def memory_guard(config_path: str) -> dict[str, Any]:
    """キャリブレーション開始前のメモリガード。

    DS4 系 (`--mmap-experts off`) は CPU lane の専門家 81 GB を RAM へコピーし、エンジン自身の
    重み 9 GB と合わせて 90 GB を使う。page cache / GPU ピン留め / 断片化の余裕 20 GB を見て
    **110 GB** を要求する (実測では MemAvailable 118.8 GB でもピークは 117.2 GB になり、
    systemd-oomd に launcher を kill された)。

    swap は「RAM の空き」ではなく「ページの退避先」なので MemAvailable には含まれない。
    しかし 81 GB のコピーは常駐しない (エンジンは専門家を逐次使う) ので、十分な swap があれば
    ページ退避で成立する。そこで **MemAvailable + SwapFree** で判定し、RAM だけで足りない場合は
    「swap に退避して遅くなる」と理由に明記する (黙って 40 分の遅い実行にしない)。
    他の (Maya / Strata) は従来の 8 GB 予約で足る。
    例外は投げず dict で返す (呼び出し側が CalibrateError にする)。"""
    mi = meminfo()
    avail_gb = mi.get("MemAvailable", 0) / (1 << 30)
    swapfree_gb = mi.get("SwapFree", 0) / (1 << 30)
    total_gb = avail_gb + swapfree_gb
    if _is_ds4(config_path):
        need_gb = DS4_MMAP_OFF_GB + DS4_ENGINE_RESIDENT_GB + DS4_MMAP_MARGIN_GB
        why = (f"この調整は最後に --mmap-experts off を試し、VRAM に載らない専門家 "
               f"(実測 {DS4_MMAP_OFF_GB:.0f} GB) をエンジンの重み ({DS4_ENGINE_RESIDENT_GB:.0f} GB) と"
               f"一緒に RAM へコピーします")
    else:
        need_gb = 8.0
        why = "この調整はエンジンを起動します"
    ok = total_gb >= need_gb
    note = "" if avail_gb >= need_gb else " (RAM だけでは足りず、swap へ退避するため遅くなります)"
    return {"ok": ok, "available_gb": round(avail_gb, 1), "swap_free_gb": round(swapfree_gb, 1),
            "need_gb": need_gb, "note": note,
            "reason": "" if ok else
            (f"空きメモリ {avail_gb:.1f} GB + swap {swapfree_gb:.1f} GB しかありません ({why}。"
             f"ページキャッシュ等の余裕を含め必要な空き: {need_gb:.0f} GB)。"
             "他のサーバー / Maya を止めるか、swap を増やしてから実行してください")}




# ----------------------------------------------------------------- マネージャ


class CalibrationManager:
    """1 件ずつのキャリブレーション実行 + 履歴。"""

    def __init__(self, root: Path, profiles_store: JsonStore, store: JsonStore,
                 manager: Any):
        self.root = root
        self.profiles = profiles_store
        self.store = store
        self.manager = manager            # ProcessManager (サーバー停止に使う)
        self.jobs: dict[str, dict[str, Any]] = {j["id"]: j for j in store.load()}
        self.active_id: str | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._cancel: set[str] = set()
        self._extra: set[asyncio.Task] = set()    # キャンセル猶予後の SIGKILL など短命 task

    # ------------------------------------------------------------ 再起動時の回復

    def on_startup(self) -> list[str]:
        """launcher の再起動 (OOM kill / 手動 restart) で実行中だったジョブの実体は失われている。

        asyncio の task も子プロセスも前のプロセスと一緒に死ぬので、status を "running" のまま
        残すと UI は「実行中」を永遠に表示し、active_id も再設定されず、ユーザーは完了したのか
        失敗したのか判断できなくなる (実測: systemd-oomd が launcher を kill すると Maya の
        `--calibrate` 子プロセスも巻き添えで死に、ジョブは 23:20 開始のまま "running" で残った)。
        bench.BenchManager.on_startup() と同じ形で "lost" + 理由 + finished_at を書き、ログは
        取り込まれた分を残す (ログ末尾は履歴で読める)。復旧したジョブ id の一覧を返す。"""
        changed = False
        lost: list[str] = []
        for job in self.jobs.values():
            if job.get("status") in ("queued", "running"):
                job["status"] = "lost"
                job["error"] = ("llm-launcher の再起動 (OOM kill / restart) で実行が中断されました — "
                                "調整結果は保存されていません。ログ末尾に中断直前までの出力が残っています")
                job["finished_at"] = job.get("finished_at") or now()
                self._append_log(job, f"中断: {job['error']}")
                changed = True
                lost.append(job["id"])
        # active_id は再起動後に受け継がない (前のプロセスの task が動かしていた実行だけ)。
        # ただし回復対象が無い場合は None のまま (正常)。
        self.active_id = None
        if changed:
            self._save()
        return lost

    # ------------------------------------------------------------ 公開状態

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in job.items() if not k.startswith("_")}

    def list_jobs(self) -> list[dict[str, Any]]:
        return [self._public(j) for j in sorted(self.jobs.values(),
                                                key=lambda x: x.get("started_at") or 0)]

    def get(self, jid: str) -> dict[str, Any] | None:
        job = self.jobs.get(jid)
        return self._public(job) if job else None

    def live_log(self, jid: str, cursor: int = 0) -> dict[str, Any]:
        """ジョブのログを cursor 以降で返す (cursor = 前回の start + len(lines))。"""
        job = self.jobs.get(jid)
        if not job:
            raise KeyError(jid)
        logs = job.get("logs") or []
        cursor = max(0, min(int(cursor or 0), len(logs)))
        return {"lines": logs[cursor:], "start": cursor, "total": len(logs)}

    def supports(self, backend_id: str) -> bool:
        return backend_id in CAL_BACKENDS

    # ------------------------------------------------------------ 実行

    def _save(self) -> None:
        self.store._cache = [self.jobs[j] for j in sorted(self.jobs, key=lambda x: self.jobs[x]["started_at"])]
        self.store.save()

    def _append_log(self, job: dict[str, Any], line: str) -> None:
        strip = line.rstrip("\r\n")
        if not strip:
            return
        logs = job.setdefault("logs", [])
        logs.append(strip)
        if len(logs) > LOG_TAIL:
            del logs[:len(logs) - LOG_TAIL]
        self._save()

    async def start(self, profile: dict[str, Any], backend_id: str,
                    *, restart_after: bool = False,
                    actor: dict[str, Any] | None = None) -> dict[str, Any]:
        """キャリブレーションを 1 件開始する (キューせず、同時に 1 件だけ)。"""
        if self.active_id:
            raise CalibrateError("別のキャリブレーションが実行中です。完了を待つか中断してください")
        kind = CAL_BACKENDS.get(backend_id)
        if not kind:
            raise CalibrateError(f"{BACKENDS.get(backend_id, {}).get('name', backend_id)} は"
                                 "キャリブレーションに対応していません")
        backend = backend_for(self.root, backend_id)
        module_dir = self.root / backend["module_path"]
        if not module_dir.is_dir():
            raise CalibrateError(f"{backend['module_path']} がありません (submodule を init してください)")

        python = self.manager.resolve_python(backend_id)
        report_dir = self.root / "data" / "calibrations"
        report_dir.mkdir(parents=True, exist_ok=True)
        job_id = new_id("cal")

        if kind == "maya":
            cfg = maya_config_path(self.root, profile)
            if not cfg:
                raise CalibrateError("Maya の config (maya-<model>.json) が見つかりません。"
                                     "プロファイルの --config に指定するか、先にセットアップしてください")
            steps: list[dict[str, Any]] = [
                # verify: Maya は失敗しても exit 0 なので、調整が実際に config へ書かれたかを後で確認する
                {"name": "calibrate", "argv": build_maya_command(self.root, profile, python),
                 "cwd": str(module_dir), "verify": cfg}]
        else:
            cfg = strata_config_path(self.root, profile)
            if not cfg:
                raise CalibrateError("Strata の config (strata-<model>.json) をプロファイルの --config に"
                                     "指定してください (ファイルが存在しません)")
            name = strata_profile_name(profile)
            try:
                existing = await self._strata_profiles(python, module_dir)
            except CalibrateError:
                existing = []
            report = str((report_dir / f"calibration-{job_id}.json").resolve())
            cmds = build_strata_commands(self.root, profile, python, report, name)
            # 既に保存済みで同じ config なら create は不要 (Strata は同名の保存に失敗する)
            have = next((p for p in existing if p.get("id") == name), None)
            if have is None:
                steps = [{"name": "create", "argv": cmds["create"], "cwd": str(module_dir)}]
            elif str(Path(have.get("source_config") or cfg).resolve()) != str(Path(cfg).resolve()):
                raise CalibrateError(
                    f"Strata の保存 profile {name} が別の config を指しています "
                    f"({have.get('source_config')}) — このプロファイル用の config を揃えるか、"
                    f"Strata 側で profile use-config {name} を実行してください")
            else:
                steps = []
            steps += [{"name": k, "argv": cmds[k], "cwd": str(module_dir)}
                      for k in ("plan", "measure", "review", "apply")]

        # GPU 独占: 同じプロファイルで稼働中のサーバーは全部止めてから実行する。
        # (キャリブレーションはエンジンを起動して GPU を独占するので、同居は絶対に不可)
        mine = [s for s in self.manager.sessions.values() if s["profile_id"] == profile["id"]]
        live = [s for s in mine if s["status"] in ("running", "starting")]
        for s in live:
            await self.manager.stop(s["id"])
        # stop() は「停止中 (= エンジンが arena 解放中)」を待って返す。それを過ぎても
        # 止まらない場合だけ実行しない (OS 過コミット凍結防止のため、launcher は待つ側に倒す)
        if any(s["status"] == "stopping" for s in mine):
            raise CalibrateError("サーバーの停止が完了していません (エンジン解放中)。数秒後に再試行してください")

        # メモリガード: DS4 の `--mmap-experts off` は 80 GB を要求するので、空きが無ければ
        # 40 分走らせてから OOM kill される前に、理由付きで開始を拒否する。
        # (サーバー停止後に行う — 停止して解放された RAM を数えるため)
        guard = memory_guard(cfg)
        if not guard["ok"]:
            raise CalibrateError(guard["reason"])

        job: dict[str, Any] = {
            "id": job_id,
            "profile_id": profile["id"],
            "profile_name": profile.get("name", ""),
            "backend": backend_id,
            "backend_name": backend["name"],
            "kind": kind,
            "status": "queued",
            "step": "",
            "steps": [s["name"] for s in steps],
            "report": None,
            "error": "",
            "started_at": now(),
            "finished_at": None,
            "started_by": (actor or {}).get("id", "") or "",
            "logs": [],
            "restart_after": restart_after,
        }
        self.jobs[job_id] = job
        self.active_id = job_id
        self._save()
        self._tasks[job_id] = asyncio.create_task(self._run(job, steps, profile))
        return self._public(job)

    async def _strata_profiles(self, python: str, module_dir: Path) -> list[dict[str, Any]]:
        """保存済み Strata profile の一覧 (JSON)。"""
        rc, raw = await self._exec([python, str(self.root / "module" / "strata" / "strata.py"),
                                    "profile", "list", "--output", "json"], module_dir)
        payload = self._parse_json(raw)
        if rc != 0:
            raise CalibrateError((payload or {}).get("error", {}).get("message")
                                 or f"strata profile list が失敗しました (exit {rc})")
        items = (payload or {}).get("profiles")
        return items if isinstance(items, list) else []

    def cancel(self, jid: str) -> dict[str, Any]:
        job = self.jobs.get(jid)
        if not job:
            raise KeyError(jid)
        if job["status"] not in ("queued", "running"):
            raise CalibrateError("このキャリブレーションは既に終了しています")
        self._cancel.add(jid)
        proc = self._procs.get(jid)
        if proc is not None and proc.returncode is None:
            pid = proc.pid
            self._signal_group(pid, signal.SIGTERM)
            # エンジンは SIGTERM で arena 解放に時間がかかる (launcher 自身の stop と同じ事情)。
            # 待っても終わらない場合に備えて、猶予後 SIGKILL を送る task を出す
            self._kill_later(jid, pid)
        self._save()
        return self._public(job)

    def _kill_later(self, jid: str, pid: int) -> None:
        grace = float(os.environ.get("LLM_LAUNCHER_CAL_KILL_GRACE_S", "240"))

        async def wait_kill() -> None:
            t0 = time.monotonic()
            while time.monotonic() - t0 < grace:
                if not pid_alive(pid):
                    return
                await asyncio.sleep(1.0)
            self._signal_group(pid, signal.SIGKILL)

        task = asyncio.create_task(wait_kill())
        self._extra.add(task)                     # 参照を保持 (GC されると task が消える)
        task.add_done_callback(self._extra.discard)

    def delete(self, jid: str) -> None:
        """履歴を削除する (実行中は呼び出し側が確認済みであること)。"""
        self.jobs.pop(jid, None)
        self._save()

    @staticmethod
    def _signal_group(pid: int, sig: signal.Signals) -> None:
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    async def _exec(self, argv: list[str], cwd: Path) -> tuple[int, str]:
        """1 コマンドを実行して (終了コード, stdout + stderr の末尾) を返す (ログには取り込まない)。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        except FileNotFoundError:
            return 127, ""
        except OSError:
            return 1, ""
        raw, _ = await proc.communicate()
        return proc.returncode or 0, raw.decode("utf-8", "replace")

    async def _run_step(self, job: dict[str, Any], step: dict[str, Any]) -> tuple[int, str]:
        """1 コマンドを走らせ、ログを逐行取り込み、終了コードと出力を返す。"""
        job["step"] = step["name"]
        self._append_log(job, f"===== {step['name']} {time.strftime('%F %T')} =====")
        self._append_log(job, shlex.join(step["argv"]))
        env = {**os.environ}
        try:
            proc = await asyncio.create_subprocess_exec(
                *step["argv"], cwd=step["cwd"], env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            self._append_log(job, f"コマンドが見つかりません: {e}")
            return 127, ""
        except OSError as e:
            self._append_log(job, f"起動失敗: {e}")
            return 1, ""
        self._procs[job["id"]] = proc
        # OOM 保護: キャリブレーションの子プロセス (maya.sh → maya.py → engine) は、このツリーを
        # ProcessManager.apply_oom が扱わないため **oom_score_adj が 0 のまま**になる。launcher 本体
        # だけが -700 で守られているので、systemd-oomd は launcher を kill してしまう (実測 03:22:
        # 88.3 GB ピーク → launcher 死亡)。Maya は実行中に engine を子として増やすので、1 回だけでは
        # 足りない — 定期的にツリーへ同じ score を掛け直す (apply_score は root + 全子孫を対象にする)。
        protect = asyncio.create_task(self._protect_tree(proc.pid))
        self._extra.add(protect)
        protect.add_done_callback(self._extra.discard)
        out: list[str] = []
        kept = 0
        try:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace")
                self._append_log(job, text)
                if kept < 96 * 1024:       # JSON の 1 行はこの範囲に必ず入る (行は正規の JSON 出力)
                    out.append(text)
                    kept += len(text)
            rc = proc.returncode if proc.returncode is not None else await proc.wait()
        finally:
            self._procs.pop(job["id"], None)
        return rc or 0, "".join(out)

    async def _protect_tree(self, root_pid: int, interval: float = 20.0) -> None:
        """キャリブレーションの子プロセスツリーへ oom_score_adj を定期適用する。

        Maya は実行中に engine (数十 GB を RAM へコピーする本体) を子として増やす。1 回だけの
        apply_score では後から増えた子を取りこぼすため、プロセスが消えるまで繰り返す。"""
        cfg = self.manager.oom_cfg() if hasattr(self.manager, "oom_cfg") else {"score_adj": -700}
        score = cfg.get("score_adj", -700)
        while pid_alive(root_pid):
            apply_score(root_pid, score)
            await asyncio.sleep(interval)

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any] | None:
        """CLI の --output json の行 (最後の有効な JSON オブジェクト) を返す。"""
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and "schema_version" in obj:
                    return obj
        return None

    async def _run(self, job: dict[str, Any], steps: list[dict[str, Any]],
                   profile: dict[str, Any]) -> None:
        job["status"] = "running"
        self._save()
        result: dict[str, Any] = {}
        try:
            for step in steps:
                if job["id"] in self._cancel:
                    raise _Cancelled()
                rc, raw = await self._run_step(job, step)
                if job["id"] in self._cancel:
                    raise _Cancelled()
                payload = self._parse_json(raw)
                if rc != 0:
                    err = (payload or {}).get("error") if isinstance(payload, dict) else None
                    msg = err.get("message") if isinstance(err, dict) else ""
                    if step["name"] == "plan":
                        msg = _plan_failure(payload) or msg
                    raise CalibrateError(msg or f"{step['name']} が失敗しました (exit {rc})")
                if step["name"] == "calibrate" and step.get("verify"):
                    # Maya は「エンジンが起動できなかった」場合でも exit 0 で終わる
                    # (警告を出して False を返し、config を書き換えない)。終了コードだけだと
                    # 失敗を「成功」と偽ることになるので、config が実際に調整されたかを確認する。
                    why = _verify_maya_tuned(job, step["verify"])
                    if why:
                        raise CalibrateError(why)
                if step["name"] == "review" and isinstance(payload, dict):
                    # Strata の JSON: {"schema_version":1, "ok": <applicable>, "review": {...}}
                    review = payload.get("review") if isinstance(payload.get("review"), dict) else payload
                    applicable = review.get("applicable") if "applicable" in review else payload.get("ok")
                    if applicable is not True:
                        reasons = ", ".join(review.get("reasons") or []) or "review が適用不可と判定しました"
                        raise CalibrateError(f"review: 適用できません ({reasons})")
                result[step["name"]] = payload or {}
            job["status"] = "done"
            job["report"] = _summary(job["kind"], result)
        except _Cancelled:
            job["status"] = "cancelled"
            self._append_log(job, "キャンセルされました")
        except CalibrateError as e:
            job["status"] = "failed"
            job["error"] = str(e)
            self._append_log(job, f"失敗: {e}")
        except Exception as e:  # noqa: BLE001
            job["status"] = "failed"
            job["error"] = f"{type(e).__name__}: {e}"
            self._append_log(job, f"失敗: {job['error']}")
        finally:
            job["finished_at"] = now()
            self._tasks.pop(job["id"], None)
            if self.active_id == job["id"]:
                self.active_id = None
            self._save()

        # 調整後にサーバーを起動し直す (任意・失敗してもジョブ記録は残す)
        if job.get("restart_after") and job["status"] == "done":
            try:
                await self.manager.start(profile, force=True)
            except Exception:  # noqa: BLE001
                pass


def _verify_maya_tuned(job: dict[str, Any], cfg_path: str) -> str:
    """Maya の `--calibrate` が本当に調整したかを確かめ、失敗なら理由を返す (成功なら "")。

    maya.py は「エンジンが起動できなかった」場合に警告を出して False を返すだけで、
    終了コードは 0 のまま (config は書き換えない)。終了コードだけを見ると失敗を成功と
    取り違えるので、成功時の証拠 (config の installer.calibration) を確認し、無ければ
    ログ末尾から Maya 自身の警告 / エンジンの理由を拾って返す。"""
    try:
        cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return f"Maya の config を確認できません ({cfg_path}): {e}"
    cal = (cfg.get("installer") or {}).get("calibration") if isinstance(cfg, dict) else None
    if isinstance(cal, dict) and cal.get("date"):
        return ""
    lines = (job.get("logs") or [])[-40:]
    for line in reversed(lines):
        low = line.lower()
        if "not tuned" in low or "did not finish" in low:
            return "Maya の調整は完了しませんでした: " + line.strip()
    for line in reversed((job.get("logs") or [])[-40:]):
        if "the engine said:" in line.lower():
            return "Maya のエンジンが起動できませんでした: " + line.split(":", 1)[1].strip()
    return "Maya の調整は完了しませんでした (config に調整結果が書かれていません — ログ参照)"


def _plan_failure(payload: dict[str, Any] | None) -> str:
    """Strata `engine calibrate --plan` の失敗理由 (読み取り専用の事前検査)。

    出力に plan.component_preflight.issues / missing があれば、欠損コンポーネント名を
    列挙する (例: 視覚エンコーダ mmproj が無い → measure は 15-30 分走った末に失敗するので、
    その前に止める)。"""
    plan = (payload or {}).get("plan") if isinstance(payload, dict) else None
    if not isinstance(plan, dict):
        return ""
    pre = plan.get("component_preflight") if isinstance(plan.get("component_preflight"), dict) else {}
    issues = pre.get("issues") if isinstance(pre.get("issues"), list) else []
    if issues:
        parts = []
        for it in issues:
            if isinstance(it, dict):
                comp = it.get("component") or ""
                msg = it.get("message") or ""
                parts.append(f"{comp}: {msg}" if comp else msg)
        if parts:
            return "事前検査で不足あり: " + "; ".join(parts[:5])
    missing = [c.get("name") for c in (pre.get("components") or [])
               if isinstance(c, dict) and c.get("required") and c.get("status") != "ok"]
    if missing:
        return "必須コンポーネントが見つかりません: " + ", ".join(missing[:8])
    if plan.get("tool_available") is False:
        return "Strata の tools/calibrate.py が無い (Terminal アーカイブではエンジン calibrate 不可)"
    return ""


def _summary(kind: str, result: dict[str, Any]) -> dict[str, Any]:
    """ジョブ内の CLI 結果を UI 用の短い要約にする。"""
    if kind == "maya":
        return {"tuned": True, "backend": "maya"}
    # Strata の JSON は {"schema_version":1, "ok":true, "review": {...}} / {"... ","application": {...}}
    review = (result.get("review") or {}).get("review") or result.get("review") or {}
    apply = result.get("apply") or {}
    app = apply.get("application") or {}
    return {
        "settings": review.get("settings") or {},
        "before_args": review.get("before_args"),
        "after_args": review.get("after_args"),
        "application_record": app.get("application_record"),
        "applied": bool(apply.get("ok") is True or app),
    }
