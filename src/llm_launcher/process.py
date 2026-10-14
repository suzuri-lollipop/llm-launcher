"""バックエンドプロセスの起動・監視・停止管理。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import oom
from .backends import (BACKENDS, python_candidates, render_argv, resolve_binary,
                       resolve_port)
from .oom import MemoryGuardError
from .store import JsonStore, new_id, now

LOG_TAIL_LIMIT = 512 * 1024
OOM_REAPPLY_TICKS = 4        # tick(3s) x 4 = 12 秒ごとに起動ツリーへ再適用
PRESSURE_LOG_COOLDOWN = 120.0

# セッションの起動経路 (started_via)。簡易画面の警告は "simple" と "ui" を対象に
# する ("bench" は出さない / rbac.SIMPLE_VIAS)。
VIA_SIMPLE = "simple"        # /simple の「▶ 起動する」ボタン
VIA_UI = "ui"                # 管理画面の起動 / 再起動
VIA_BENCH = "bench"          # ベンチマークの自動起動 (警告には出さない)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def port_in_use(port: int, host: str = "0.0.0.0") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


class ProcessManager:
    def __init__(self, root: Path, runs_store: JsonStore, profiles_store: JsonStore,
                 settings_store: JsonStore):
        self.root = root
        self.runs = runs_store
        self.profiles = profiles_store
        self.settings = settings_store
        self.log_dir = root / "data" / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.sessions: dict[str, dict[str, Any]] = {s["id"]: s for s in runs_store.load()}
        self._py_choice: dict[str, str] = {}   # 検出済みの実行可 python
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._log_handles: dict[str, Any] = {}
        self._monitors: dict[str, asyncio.Task] = {}
        self._tick_task: asyncio.Task | None = None
        self._dirty = False
        self.self_oom: dict[str, Any] = {}       # launcher 自身への保護適用結果
        self.last_guard: dict[str, Any] = {}     # 直近の起動前メモリガード判定
        self._tick_n = 0
        self._last_pressure_log = 0.0
        self._bg_tasks: set[asyncio.Task] = set()   # fire-and-forget task の GC 防止

    # ------------------------------------------------------------ oom guard

    def oom_cfg(self) -> dict[str, Any]:
        """settings.json の oom ブロック (既定値マージ + クリップ済み)。"""
        return oom.merged_settings((self.settings.load() or {}).get("oom"))

    def _log_note(self, sid: str, text: str) -> None:
        """セッションログに 1 行追記 (OOM 適用結果や逼迫警告を証跡として残す)。"""
        fh = self._log_handles.get(sid)
        if fh is None:
            return
        with contextlib.suppress(OSError):
            fh.write(f"[oom] {text}  # {time.strftime('%F %T')}\n".encode())

    def apply_oom(self, sid: str) -> dict[str, Any]:
        """起動済みセッションのプロセスツリー全体へ oom_score_adj を適用する。"""
        cfg = self.oom_cfg()
        s = self.sessions.get(sid)
        if not cfg["enabled"] or not s or s.get("pid", 0) <= 0:
            return {"enabled": bool(cfg["enabled"]), "skipped": True, "applied": 0,
                    "denied": 0, "via": "", "error": "", "score": cfg["score_adj"]}
        res = oom.apply_score(s["pid"], cfg["score_adj"])
        res["skipped"] = False
        if res["denied"] or res["error"]:
            hint = ("未導入 — sudo scripts/oom-protect-install.sh で導入できます"
                    if not res["helper"] else "導入済み")
            self._log_note(sid, f"oom_score_adj={cfg['score_adj']} 適用 {res['applied']} 件 / "
                                f"拒否 {res['denied']} 件 — {res['error'] or '権限不足'}"
                                f" / sudo ヘルパー: {hint}")
        else:
            self._log_note(sid, f"oom_score_adj={cfg['score_adj']} を {res['applied']} プロセス"
                                f"{' (sudo ヘルパー経由)' if res['via'] == 'helper' else ''} に適用")
        s["oom"] = {k: res[k] for k in ("applied", "denied", "via", "error", "score")}
        return res

    async def _reapply_oom(self) -> None:
        """起動後に湧いた worker を拾って再適用 (バックエンドは後から子を増やすため)。"""
        cfg = self.oom_cfg()
        if not cfg["enabled"] or not cfg["reapply"]:
            return
        if not oom.can_lower_score() and not oom.helper_available():
            return                      # 権限がない場合は /proc を毎回数分回すだけなので見送る
        live = [s for s in self.sessions.values() if s["status"] in ("running", "starting")]
        for s in live:
            res = await asyncio.to_thread(oom.apply_score, s["pid"], cfg["score_adj"])
            s["oom"] = {k: res[k] for k in ("applied", "denied", "via", "error", "score")}

    async def _check_pressure(self) -> None:
        """メモリ逼迫を監視。警告をログへ残し、swift_stop が ON なら自発的に止める。"""
        cfg = self.oom_cfg()
        if not cfg["enabled"] or cfg["min_available_gb"] <= 0:
            return
        live = [s for s in self.sessions.values() if s["status"] in ("running", "starting")]
        if not live:
            return
        p = await asyncio.to_thread(oom.pressure, cfg)
        for s in live:
            s["mem_state"] = p["state"]
            s["mem_available_gb"] = p["available_gb"]
        if p["state"] == "ok":
            return
        t = time.monotonic()
        if t - self._last_pressure_log >= PRESSURE_LOG_COOLDOWN:
            self._last_pressure_log = t
            for s in live:
                self._log_note(s["id"], f"メモリ逼迫警告: 空き {p['available_gb']:.1f}GB "
                                        f"(予約 {cfg['min_available_gb']:.1f}GB / state={p['state']})")
        if p["state"] == "crit" and cfg["swift_stop"]:
            # 逼迫の引き金を引いているのはほとんどの場合いちばん新しく起動したローダーなので、
            # 稼働中のサーバーは守って新しい方を止める (kernel OOM より軽い障害で止まる)
            candidates = [s for s in live if not s.get("oom_swift_stop")]
            if not candidates:
                return
            victim = max(candidates, key=lambda s: s.get("started_at", 0))
            victim["error"] = (f"メモリ逼迫 (空き {p['available_gb']:.1f}GB) のため launcher 側で"
                               "安全停止しました (OOM kill より軽い障害で復旧できます)")
            victim["oom_swift_stop"] = True
            self._log_note(victim["id"], victim["error"])
            self._spawn(self.stop(victim["id"], grace=8.0))

    def _spawn(self, coro: Any) -> asyncio.Task:
        """参照を保ったまま fire-and-forget (task は GC されると途中で消える)。"""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task


    # ------------------------------------------------------------- helpers

    def _persist(self) -> None:
        self.runs._cache = sorted(self.sessions.values(), key=lambda s: s["started_at"])
        self.runs.save()

    def _backend_setting(self, backend_id: str) -> dict[str, Any]:
        return dict((self.settings.load().get("backends") or {}).get(backend_id) or {})

    def resolve_python(self, backend_id: str) -> str:
        """{python} 解決: 明示設定 > 検出済み実行可 python > module .venv > GUI .venv > self。"""
        override = self._backend_setting(backend_id).get("python")
        if override:
            return override
        cached = self._py_choice.get(backend_id)
        if cached and Path(cached).exists():
            return cached
        b = BACKENDS.get(backend_id) or {}
        for _, path in python_candidates(self.root, b.get("module_path")):
            if Path(path).exists():
                return path
        return sys.executable

    def set_resolved_python(self, backend_id: str, path: str) -> None:
        """検出タスクが「実際に import できた python」を確定したらここに反映。"""
        self._py_choice[backend_id] = path

    def resolve_binary(self, backend_id: str) -> str:
        b = BACKENDS[backend_id]
        return resolve_binary(self.root, b, override=self._backend_setting(backend_id).get("command_binary") or "")

    def base_command(self, backend_id: str) -> str:
        return self._backend_setting(backend_id).get("command") or BACKENDS[backend_id]["default_command"]

    def find_profile(self, profile_id: str) -> dict[str, Any] | None:
        return next((p for p in self.profiles.load() if p["id"] == profile_id), None)

    def build_argv(self, profile: dict[str, Any]) -> list[str]:
        """1プロファイル=1バックエンド。values フォーム値から最終 argv を組み立てる。"""
        bid = profile.get("backend")
        if bid not in BACKENDS:
            raise ValueError("このプロファイルにバックエンドが設定されていません")
        backend = BACKENDS[bid]
        command = (profile.get("command") or "").strip() or self.base_command(bid)
        rendered = {**profile, "command": command}
        argv = render_argv(
            backend, rendered,
            python=self.resolve_python(bid),
            binary=self.resolve_binary(bid),
            root=self.root,
        )
        # render_argv は kernel_optimization を rendered["_resolved_env"] に解決するが、
        # rendered は浅いコピーなので、そのままでは start() が読む元の profile に
        # 伝わらない (env が黙って失われる)。呼び出し元の profile へ書き戻す。
        if "_resolved_env" in rendered:
            profile["_resolved_env"] = rendered["_resolved_env"]
        return argv

    # --------------------------------------------------------------- start

    async def start(self, profile: dict[str, Any], force: bool = False,
                    actor: dict[str, Any] | None = None, via: str = VIA_UI) -> dict[str, Any]:
        """バックエンドを起動してセッション記録を作る。

        actor / via は「誰がどの画面から起こしたインスタンスか」の記録。
        簡易画面 (/simple) は自分が起こした分だけを警告に出すため、起動の瞬間に
        しか取れないこの情報を永続化しておく (履歴レコードに無い古いセッションは
        誰の起動でもない = 警告対象にしない)。
        """
        bid = profile.get("backend")
        if bid not in BACKENDS:
            raise RuntimeError("このプロファイルにバックエンドが設定されていません")
        backend = BACKENDS[bid]
        argv = self.build_argv(profile)
        port = resolve_port(backend, profile, argv)

        # 起動前メモリガード: ここで殺し合いを仕掛けない方がマシなので、空きが予約値
        # 未満のときは基本ブロック (UI は force で再試行できる)。
        cfg = self.oom_cfg()
        g = oom.guard(cfg)
        self.last_guard = g
        if not g["ok"] and not force:
            raise MemoryGuardError(g["message"] + "。空きを空けるか、force で起動してください。")

        env = os.environ.copy()
        # venv の python を絶対パスで直接起動しても bin/ は PATH に入らない
        # (追加するのは activate スクリプトの仕事)。flashinfer JIT のように
        # venv 内ツール (ninja 等) を PATH 経由で呼ぶ処理が落ちるため補う。
        extra_paths: list[str] = []
        launch_bin = Path(argv[0]).parent
        if (launch_bin.parent / "pyvenv.cfg").exists():
            extra_paths.append(str(launch_bin))
        # nvcc が PATH のどこにも無いとき flashinfer は JIT を諦めるため、
        # PyTorch 本体と同じ /usr/local/cuda フォールバックを先回しで補う。
        if shutil.which("nvcc", path=env.get("PATH")) is None \
                and Path("/usr/local/cuda/bin/nvcc").exists():
            extra_paths.append("/usr/local/cuda/bin")
        if extra_paths:
            env["PATH"] = os.pathsep.join([*extra_paths, env.get("PATH", "")])
        for k, v in (profile.get("_resolved_env") or profile.get("env") or {}).items():
            if k.strip():
                env[k.strip()] = str(v)

        cwd = (profile.get("cwd") or "").strip() or str(self.root)
        sid = new_id("s")
        log_path = self.log_dir / f"{sid}.log"

        session = {
            "id": sid,
            "profile_id": profile["id"],
            "profile_name": profile.get("name", ""),
            "backend": bid,
            "backend_name": backend["name"],
            # 起動したユーザーと経路 (簡易画面の警告は自分が起動した分だけを出す)
            "started_by": (actor or {}).get("id", "") or "",
            "started_by_name": (actor or {}).get("username", "") or "",
            "started_via": via or VIA_UI,
            "argv": argv,
            "cwd": cwd,
            "port": port,
            "health_path": backend.get("health_path", "/health"),
            "ready": False,
            "detached": False,
            "pid": 0,
            "status": "starting",
            "started_at": now(),
            "finished_at": None,
            "exit_code": None,
            "log": str(log_path.relative_to(self.root)),
            "error": "",
            "oom": None,                                  # 保護適用結果 {applied,denied,via,error,score}
            "oom_kills0": oom.cgroup_oom_kills(),         # 起動時の cgroup oom_kill カウンタ
            "oom_suspect": False,
            "mem_state": "ok",
        }

        try:
            log_fh = open(log_path, "ab", buffering=0)
            log_fh.write(f"\n===== llm-launcher start {time.strftime('%F %T')} =====\n"
                         f"argv: {json.dumps(argv, ensure_ascii=False)}\n".encode())
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env,
                stdout=log_fh, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError:
            session["status"] = "failed"
            session["error"] = f"コマンドが見つかりません: {argv[0]}"
            session["finished_at"] = now()
            self.sessions[sid] = session
            self._persist()
            raise RuntimeError(session["error"])
        except OSError as e:
            session["status"] = "failed"
            session["error"] = str(e)
            session["finished_at"] = now()
            self.sessions[sid] = session
            self._persist()
            raise RuntimeError(f"起動に失敗しました: {e}")

        session["pid"] = proc.pid
        session["status"] = "running"
        self.sessions[sid] = session
        self._procs[sid] = proc
        self._log_handles[sid] = log_fh
        self._monitors[sid] = asyncio.create_task(self._monitor(sid, proc))
        self.apply_oom(sid)          # 起動直後にツリー全体へ oom_score_adj を適用
        self._persist()
        return session

    async def _monitor(self, sid: str, proc: asyncio.subprocess.Process) -> None:
        rc = await proc.wait()
        s = self.sessions.get(sid)
        if s:
            user_stop = s["status"] == "stopping"
            s["exit_code"] = None if user_stop else rc
            s["finished_at"] = now()
            s["status"] = "exited" if (rc == 0 or user_stop) else "failed"
            if s["status"] == "failed" and not s.get("oom_swift_stop"):
                base = s.get("oom_kills0")
                cur = await asyncio.to_thread(oom.cgroup_oom_kills)
                delta = None if (base is None or cur is None) else cur - base
                avail = oom.meminfo().get("MemAvailable", 0) / (1 << 30)
                reason, suspect = oom.death_reason(rc, delta, avail)
                s["oom_suspect"] = bool(suspect)
                s["error"] = reason or f"exit code {rc}"
            self._persist()
        fh = self._log_handles.pop(sid, None)
        if fh:
            try:
                fh.write(f"===== exited code={rc} {time.strftime('%F %T')} =====\n".encode())
                fh.close()
            except OSError:
                pass
        self._procs.pop(sid, None)
        self._monitors.pop(sid, None)

    # ---------------------------------------------------------------- stop

    async def stop(self, sid: str, grace: float = 15.0) -> None:
        s = self.sessions.get(sid)
        if not s:
            raise KeyError(sid)
        if s["status"] not in ("running", "starting"):
            return
        s["status"] = "stopping"
        self._persist()
        pid = s["pid"]
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            pgid = pid
        self._signal(pgid, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                break
            await asyncio.sleep(0.25)
        if pid_alive(pid):
            self._signal(pgid, signal.SIGKILL)
            await asyncio.sleep(0.5)
        # _monitor が finalize するのを待つ(最大2秒)。detached はここで確定。
        mon = self._monitors.get(sid)
        if mon:
            try:
                await asyncio.wait_for(asyncio.shield(mon), 2.0)
            except asyncio.TimeoutError:
                pass
        if self.sessions[sid]["status"] == "stopping":
            s = self.sessions[sid]
            s["status"] = "exited"
            s["finished_at"] = now()
            s["detached"] = False
            self._persist()

    @staticmethod
    def _signal(pgid: int, sig: signal.Signals) -> None:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pgid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    def kill_if_running(self, sid: str) -> None:  # 同期版 (reconcile 用)
        s = self.sessions.get(sid)
        if s and s["status"] in ("running", "stopping"):
            self._signal(s["pid"], signal.SIGKILL)

    # ------------------------------------------------------------ lifecycle

    async def start_tick(self) -> None:
        cfg = self.oom_cfg()
        # 先に生存確認してからscore を書きにいく (死んだ pid は pid 再利用の可能性があるので触らない)
        self._reconcile()
        if cfg["enabled"]:
            # launcher 本体が先に死んだら再起動もログも失うので自分自身を守る
            self.self_oom = await asyncio.to_thread(oom.protect_self, cfg["score_adj"])
            if not self.self_oom["applied"]:
                print(f"oom-guard: launcher 自身を保護できません ({self.self_oom['error']}) "
                      "— OOM kill 時に一番最初に kill されます。再起動・ログ管理まで巻き添えになります",
                      file=sys.stderr)
            elif cfg["score_adj"] < 0:
                print(f"oom-guard: launcher の oom_score_adj={cfg['score_adj']} "
                      f"({'sudo ヘルパー経由' if self.self_oom['via'] == 'helper' else '直接書き込み'})")
            await self._reapply_oom()      # 引き取った (detached) プロセスにも適用
        self._tick_task = asyncio.create_task(self._tick_loop())

    def oom_status(self, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
        """OOM 対策パネル用: 設定 + 権限 + メモリ状況 + kill 痕跡のスナップショット。"""
        return oom.status(cfg or self.oom_cfg(), self.self_oom)

    def apply_oom_settings(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        """設定保存時に再適用。返り値は *適用した設定* 基準のスナップショット
        (設定ストアの保存は呼び出し側 = app.py が行うため、ズレを出さない)。"""
        cfg = oom.merged_settings(raw)
        self.self_oom = (oom.protect_self(cfg["score_adj"]) if cfg["enabled"]
                         else {"enabled": False, "applied": False, "via": "", "error": "",
                               "score": cfg["score_adj"]})
        for sid in self._procs:
            self.apply_oom(sid)
        return self.oom_status(cfg)

    async def shutdown(self, kill: bool = False) -> None:
        if self._tick_task:
            self._tick_task.cancel()
        if kill:
            for sid in list(self._procs):
                await self.stop(sid)

    def _reconcile(self) -> None:
        """マネージャー再起動時に、生きていたプロセスを detached として引き取る。"""
        for s in self.sessions.values():
            if s["status"] in ("running", "starting", "stopping"):
                if pid_alive(s["pid"]):
                    s["detached"] = True
                    s["status"] = "running"
                else:
                    s["status"] = "lost"
                    s["finished_at"] = s.get("finished_at") or now()
                    s["error"] = "llm-launcher の再起動時にプロセスを検出できませんでした"
        self._persist()

    async def _tick_loop(self) -> None:
        while True:
            try:
                await self._probe_all()
                await self._check_pressure()
                self._tick_n += 1
                if self._tick_n % OOM_REAPPLY_TICKS == 0:
                    await self._reapply_oom()   # 起動後に増えた worker を拾う
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(3.0)

    async def _probe_all(self) -> None:
        targets = [s for s in self.sessions.values()
                   if s["status"] == "running" and s.get("port")]
        if not targets:
            return
        results = await asyncio.gather(*[self._probe(s) for s in targets])
        changed = False
        for s, ok in zip(targets, results):
            if s["ready"] != ok:
                s["ready"] = ok
                changed = True
        if changed:
            self._persist()

    @staticmethod
    async def _probe(s: dict[str, Any]) -> bool:
        url = f"http://127.0.0.1:{s['port']}{s.get('health_path', '/health')}"

        def _get() -> bool:
            try:
                with urllib.request.urlopen(url, timeout=1.5) as r:
                    return 200 <= r.status < 400
            except OSError:
                return False
        return await asyncio.to_thread(_get)

    # ----------------------------------------------------------------- logs

    def read_log(self, sid: str, cursor: int = 0) -> dict[str, Any]:
        s = self.sessions.get(sid)
        if not s:
            raise KeyError(sid)
        path = self.root / s["log"]
        if not path.exists():
            return {"text": "", "cursor": 0, "size": 0}
        size = path.stat().st_size
        if cursor > size:  # ローテート等
            cursor = 0
        start = max(cursor, size - LOG_TAIL_LIMIT if size - cursor > LOG_TAIL_LIMIT else cursor)
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(LOG_TAIL_LIMIT)
        return {"text": data.decode("utf-8", "replace"), "cursor": start + len(data), "size": size}

    def session_public(self, sid: str) -> dict[str, Any]:
        s = dict(self.sessions[sid])
        if s["status"] in ("running", "starting"):
            s["uptime"] = now() - s["started_at"]
        return s

    def public_sessions(self) -> list[dict[str, Any]]:
        out = []
        for s in sorted(self.sessions.values(), key=lambda x: x["started_at"], reverse=True):
            d = dict(s)
            if d["status"] in ("running", "starting"):
                d["uptime"] = now() - d["started_at"]
            out.append(d)
        return out
