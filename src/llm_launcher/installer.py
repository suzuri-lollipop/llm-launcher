"""UI からのインストール / モデルダウンロード: シェルを介さずセットアップを完結させる。

背景:
  FreeToken 系の導入は従来ターミナルが入口だった (fork の clone -> .venv -> pip install
  -e ".[accel]"、または module/fork/freetoken/setup.sh)。モデルも HF CLI か Python で
  ダウンロードする必要があった。このモジュールは両方を launcher 内の 1 ボタンにする:

    - install  : clone / .venv 作成 / pip install を argv リストで direct exec する
                 (shell=True は使わない — 起動コマンドにシェルを挟まないため)。
                 setup.sh と同じ条件式を here に持つのではなくここで再実装している:
                 動作中の .venv は絶対に消さない、pip の無い .venv は「インストールが
                 本当に必要で uv が無いときだけ」作り直す (setup.sh と同じ順序)
    - download : huggingface_hub.snapshot_download をスレッドで実行し、tqdm フックで
                 受信箱数を UI に返す。キャッシュは既定の HF_HUB_CACHE に置くので
                 /api/hf-models のスキャンがそのまま拾う (成功後に app.py がキャッシュ失効)

  実行は 1 件ずつ (インストールとダウンロードを同時に走らせても帯域と disk を奪い合う
  だけ)。キャンセルは install がプロセスグループへ SIGTERM (猶予後 SIGKILL)、download
  は transfer ループの cooperative cancel。履歴は data/installs.json に残す。
  (calibrate.CalibrationManager と同じ fire-and-forget + poll の形)
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import venv as venv_mod
from pathlib import Path
from typing import Any, Callable

from .backends import BACKENDS, fork_dir, fork_model_root
from .store import JsonStore, new_id, now

# ログはデータベース (installs.json) に保持する。pip 実行では行が増えるので
# 直近 LOG_TAIL 行だけ残す (calibrate / bench と同じ)。
LOG_TAIL = 400

# UI からインストールできるバックエンド。strata / maya は setup.sh / maya.sh 側が
# 対話プロンプトを含むため UI 化していない (導線を壊さない範囲で今後追加する)。
INSTALL_SPECS: dict[str, dict[str, Any]] = {
    "freetoken-fork": {
        "clone": "https://github.com/suzuri-lollipop/FreeToken",
        # refs が UI のブランチ選択に出る一覧、ref が未指定時の既定 (安定版)。
        # clone 済みの clone は選択ブランチへ fetch + checkout で寄せる (branch ステップ)。
        "ref": "suzuri/product",
        "refs": [
            {"ref": "suzuri/develop", "label": "開発版"},
            {"ref": "suzuri/product", "label": "安定版"},
        ],
        "fork_name": "freetoken",        # module/fork/ 下の実ディレクトリ (大小文字無視)
        "module_path": "module/fork/freetoken",
        "check": "freetoken",
        "extras": "accel",               # flashinfer + sglang-kernel 込み (README 準拠)
    },
    "freetoken": {
        "clone": "https://github.com/FlashML-org/FreeToken",
        "ref": "",                       # 上流の既定ブランチ
        "refs": [],                      # 上流はブランチ選択を出さない
        "fork_name": "",
        "module_path": "module/freetoken",
        "check": "freetoken",
        "extras": "accel",
    },
}

_EXTRAS_RE = re.compile(r"^[A-Za-z0-9._-]+(,[A-Za-z0-9._-]+)*$")
_MODULE_RE = re.compile(r"^[a-z_][a-z0-9_.]*$")
_REPO_RE = re.compile(r"^[^/\s][^\s]{0,199}$")
_REVISION_RE = re.compile(r"^[A-Za-z0-9._\-/]{1,100}$")
# ブランチ名の実在性は一覧 (refs) でしかねず、こちらは argv に渡して良い形かの防具
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,99}$")


class InstallError(Exception):
    pass


class ActiveJobError(InstallError):
    """既にほかのジョブが実行中 (app.py は 409 にマップする)。"""


class _Cancelled(Exception):
    pass


def _module_rel(root: Path, spec: dict[str, Any]) -> str:
    """モジュールの相対パスを実在ディレクトリ基準で決める (fork_dir と同じ大小文字規則)。"""
    if spec["fork_name"]:
        return fork_dir(root, spec["fork_name"]) or spec["module_path"]
    return spec["module_path"]


def allowed_refs(spec: dict[str, Any]) -> list[str]:
    """このバックエンドで選べるブランチ (UI の選択一覧 = サーバー側の許容一覧)。"""
    refs = [str(r.get("ref") or "").strip() for r in (spec.get("refs") or [])]
    refs = [r for r in refs if r]
    if refs:
        return refs
    default = str(spec.get("ref") or "").strip()
    return [default] if default else []


def resolve_ref(spec: dict[str, Any], ref: str) -> str:
    """UI が選んだブランチを既定値込みで決め、一覧に無い名前を弾く。

    ブランチ名は git の argv へ入る (先頭 `-` や `..` は引数注入 / パストラバーサルの元) ので、
    形式的な検査に加えて allowed_refs (spec 側のホワイトリスト) を通った名前だけを渡す。
    """
    want = (ref or "").strip() or str(spec.get("ref") or "").strip()
    if not want:
        return ""
    if want not in allowed_refs(spec):
        raise InstallError("このバックエンドでインストールできるブランチではありません: "
                           f"{want} (一覧: {', '.join(allowed_refs(spec)) or 'なし'})")
    if not _REF_RE.match(want) or ".." in want or "//" in want or want.endswith("/"):
        raise InstallError(f"ブランチ名が不正です: {want}")
    return want


def clone_exists(root: Path, spec: dict[str, Any]) -> bool:
    """module_path に clone があるか (UI の「clone します / 切替えます」表示と needs_clone の正)。"""
    mod = root / _module_rel(root, spec)
    return mod.is_dir() and any(mod.iterdir())


def current_ref(root: Path, spec: dict[str, Any]) -> str:
    """既存 clone が今どこで checkout されているか (UI の既定選択と切替表示用)。

    git が無い / clone が無い / bare でない等均合しないときは "" (= 不明)。
    セットアップタブの描画ごとに走るため、失敗を投げず短時間で返す。
    """
    if not allowed_refs(spec):
        return ""
    mod = root / _module_rel(root, spec)
    if not (mod / ".git").exists():
        return ""
    try:
        r = subprocess.run(["git", "-C", str(mod), "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    head = (r.stdout or "").strip()
    return head if r.returncode == 0 and head and head != "HEAD" else ""


def seed_launch_json(module_dir: Path) -> str | None:
    """fork の setup.sh と同一の起動 JSON テンプレート (Strata 方式の standalone 起動の正)。

    ユーザーが編集した JSON を絶対に壊さないよう、存在しないときだけ書く。
    中身は python -m freetoken.serve_spec と launcher の起動 JSON 主体モードが
    同じ形式 (calibrate_lookup spec) で読む。
    """
    path = module_dir / "freetoken-launch.json"
    if path.exists():
        return None
    doc = {
        "python": str(module_dir / ".venv" / "bin" / "python"),
        "argv": ["-m", "freetoken", "--model-path", "/path/to/model", "--port", "1919"],
        "cwd": str(module_dir),
        "env": {},
        "port": 1919,
        "model_name": "",
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", "utf-8")
    return ("起動 JSON テンプレート freetoken-launch.json を作成しました "
            "(--model-path を編集すれば ./setup.sh と profile の起動 JSON パスが同じ構成で起動します)")


def record_launch_json_model(root: Path, spec: dict, model_dir: Path) -> str | None:
    """model_data へ落ちたモデルを起動 JSON の --model-path / model_name だけに記入する
    (setup.sh の --model と同じ後始末)。ユーザーが編集した他の鍵は壊さないし、
    JSON が無いときにテンプレを勝手に増やさない (導入済みモジュールのみ)。"""
    module_dir = root / str(spec.get("module_path", ""))
    path = module_dir / "freetoken-launch.json"
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text("utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        return f"起動 JSON を更新できませんでした (モデル記入スキップ): {e}"
    argv = [str(a) for a in (doc.get("argv") or [])]
    idx = next((i for i, a in enumerate(argv) if a in ("--model-path", "--model")), None)
    if idx is not None and idx + 1 < len(argv):
        argv[idx + 1] = str(model_dir)
    elif idx is not None:
        argv.append(str(model_dir))
    else:
        argv += ["--model-path", str(model_dir)]
    doc["argv"] = argv
    doc["model_name"] = model_dir.name.split("--")[-1]
    try:
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", "utf-8")
    except OSError as e:
        return f"起動 JSON を書けませんでした (モデル記入スキップ): {e}"
    return f"起動 JSON の --model-path にモデルを記録しました: {model_dir}"


class InstallManager:
    """1 件ずつのインストール / ダウンロード実行 + 履歴。"""

    def __init__(self, root: Path, store: JsonStore, manager: Any = None):
        self.root = root
        self.store = store
        self.manager = manager            # ProcessManager (app が渡す。現状は参照のみ)
        self.jobs: dict[str, dict[str, Any]] = {j["id"]: j for j in store.load()}
        self.active_id: str | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._cancel: set[str] = set()
        self._dl_cancel: dict[str, threading.Event] = {}   # download は cooperative cancel
        self._extra: set[asyncio.Task] = set()
        self.on_job_done: Callable[[dict[str, Any]], None] | None = None

    # ------------------------------------------------------------ 再起動時の回復

    def on_startup(self) -> list[str]:
        """launcher 再起動で running のまま残ったジョブを "lost" にする (calibrate と同じ)。"""
        changed = False
        lost: list[str] = []
        for job in self.jobs.values():
            if job.get("status") in ("queued", "running"):
                job["status"] = "lost"
                job["error"] = ("llm-launcher の再起動でインストール / ダウンロードが中断されました。"
                                "実行済み部分をそのまま続けられます (ボタンを押し直してください)")
                job["finished_at"] = job.get("finished_at") or now()
                self._append_log(job, f"中断: {job['error']}")
                changed = True
                lost.append(job["id"])
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
        return backend_id in INSTALL_SPECS

    # ------------------------------------------------------------ 永続化 / ログ

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

    def _new_job(self, kind: str, actor: dict[str, Any] | None, **fields: Any) -> dict[str, Any]:
        job: dict[str, Any] = {
            "id": new_id("ins"),
            "kind": kind,
            "status": "queued",
            "step": "",
            "error": "",
            "started_at": now(),
            "finished_at": None,
            "started_by": (actor or {}).get("id", "") or "",
            "logs": [],
        }
        job.update(fields)
        self.jobs[job["id"]] = job
        self.active_id = job["id"]
        self._save()
        return job

    def _check_free(self) -> None:
        if self.active_id:
            raise ActiveJobError("別のインストール / ダウンロードが実行中です。完了を待つか中断してください")

    # ------------------------------------------------------------ インストール

    async def start_install(self, backend_id: str, *, extras: str | None = None,
                            clean: bool = False, ref: str = "",
                            actor: dict[str, Any] | None = None) -> dict[str, Any]:
        """clone -> .venv -> pip install -e を 1 ボタンで実行する (同時に 1 件だけ)。

        ref (ブランチ) は spec["refs"] の一覧内だけ通る。未 clone 時はそのブランチを clone し、
        clone 済み時は選択ブランチへ fetch + checkout する step を挟む。
        """
        self._check_free()
        spec = INSTALL_SPECS.get(backend_id)
        if not spec:
            raise InstallError(f"{backend_id} は UI インストールに対応していません")
        extras_str = spec["extras"] if extras is None else (extras or "").strip()
        if extras_str and not _EXTRAS_RE.match(extras_str):
            raise InstallError("extras はカンマ区切りの英数字のみです (例: accel)")
        branch = resolve_ref(spec, ref)
        check = spec["check"]
        if not _MODULE_RE.match(check):
            raise InstallError("内部設定エラー: check が不正です")
        module_rel = _module_rel(self.root, spec)
        # root が相対パスだと argv[0] が subprocess の新 cwd 基準で解決されて 127 になる
        module_dir = (self.root / module_rel).resolve()
        module_dir.parent.mkdir(parents=True, exist_ok=True)
        needs_clone = not clone_exists(self.root, spec)

        if needs_clone and not spec["clone"]:
            raise InstallError(f"{module_rel} がありません")
        tool = "uv" if shutil.which("uv") else "pip"
        venvpy = str(module_dir / ".venv" / "bin" / "python")
        verify_argv = [venvpy, "-c",
                       f"import {check}, sys; print('{check}', getattr({check}, '__version__', '?'),"
                       f" 'py', sys.version.split()[0])"]
        steps: list[dict[str, Any]] = []
        if needs_clone:
            argv = ["git", "clone", "--depth", "1"]
            if branch:
                argv += ["--branch", branch]
            argv += [spec["clone"], str(module_dir)]
            steps.append({"name": "clone", "argv": argv, "cwd": str(module_dir.parent)})
        elif branch:
            # clone 済みでも「選んだブランチ」に寄せる (venv / pip より前に置く)
            steps.append({"name": "branch"})
        steps.append({"name": "venv"})
        steps.append({"name": "precheck", "argv": verify_argv, "cwd": str(module_dir)})
        spec_arg = f".[{extras_str}]" if extras_str else "."
        if tool == "uv":
            install_argv = ["uv", "pip", "install", "--python", venvpy, "-e", spec_arg]
        else:
            install_argv = [venvpy, "-m", "pip", "install", "--progress-bar", "off",
                            "-e", spec_arg]
        steps.append({"name": "install", "argv": install_argv, "cwd": str(module_dir)})
        steps.append({"name": "verify", "argv": verify_argv, "cwd": str(module_dir)})

        job = self._new_job("install", actor, backend=backend_id,
                            backend_name=BACKENDS.get(backend_id, {}).get("name", backend_id),
                            module_path=module_rel, tool=tool,
                            extras=extras_str, clean=bool(clean), needs_clone=needs_clone,
                            ref=branch)
        job["steps"] = [s["name"] for s in steps]
        self._tasks[job["id"]] = asyncio.create_task(self._run_install(job, spec, steps))
        return self._public(job)

    def _venv_ensure(self, job: dict[str, Any], module_dir: Path) -> str:
        """スレッドで走る .venv 確保。setup.sh と同じ規則: 動く venv は絶対に消さない。"""
        venv_dir = module_dir / ".venv"
        py = venv_dir / "bin" / "python"
        has_uv = job["tool"] == "uv"
        if py.exists() and not has_uv:
            # uv が無いのに pip が無い venv (uv が作った / python3-venv 不足の Debian) は
            # インストールを実行できないので、そのときだけ作り替える
            rc = subprocess.run([str(py), "-m", "pip", "--version"],
                                capture_output=True).returncode
            if rc != 0:
                shutil.rmtree(venv_dir, ignore_errors=True)
        if not py.exists():
            if has_uv:
                rc = subprocess.run(["uv", "venv", str(venv_dir)], cwd=str(module_dir)).returncode
                if rc != 0:
                    raise InstallError("uv venv .venv が失敗しました")
            else:
                try:
                    venv_mod.EnvBuilder(with_pip=True, clear=True, symlinks=True).create(str(venv_dir))
                except Exception as e:  # noqa: BLE001
                    raise InstallError(f".venv を作れませんでした: {e} "
                                       "(Ubuntu/Debian なら sudo apt install python3-venv)")
                return ".venv を作成しました"
            return ".venv を作成しました (uv)"
        return ".venv は既存です"

    async def _switch_branch(self, job: dict[str, Any], module_dir: Path, ref: str) -> bool:
        """clone 済みの clone を選択ブランチへ fetch + checkout する (切替を起こしたら True)。

        ユーザーの作業ツリーとローカルコミットを壊さないことが先:
        追跡ファイルに未コミット変更があれば切替を失敗にしてお知らせし (未追跡の
        freetoken-launch.json / .venv は邪魔しない)、同じ名前のローカルブランチが
        既にあるときは --ff-only で進む分だけ進める (独自コミットはそのまま)。
        """
        git = ["git", "-C", str(module_dir)]
        rc, cur = await self._exec(git + ["rev-parse", "--abbrev-ref", "HEAD"], module_dir)
        if rc != 0:
            raise InstallError(
                f"{module_dir} が git リポジトリではない (または git が使えない) ため、"
                f"{ref} に切り替えられません。ブランチを選んで入れ直すにはこのフォルダを"
                "削除してからインストールし直してください")
        cur = cur.strip()
        if cur == ref:
            self._append_log(job, f"ブランチは選択と同じです ({ref})")
            return False
        rc, dirty = await self._exec(git + ["status", "--porcelain", "--untracked-files=no"],
                                     module_dir)
        if rc != 0:
            raise InstallError("git status が実行できません (clone を確認してください)")
        if dirty.strip():
            raise InstallError(
                f"未コミットの変更があるため {cur} → {ref} に切り替えられません。"
                "commit / stash するか、git clone し直し (フォルダ削除) でインストールし直してください")
        # shallow clone (--depth 1) は浅いまま追うため、そのときだけ --depth を付ける
        shallow = (module_dir / ".git" / "shallow").exists()
        fetch = git + ["fetch"] + (["--depth", "1"] if shallow else []) + [
            "origin", f"+refs/heads/{ref}:refs/remotes/origin/{ref}"]
        rc, _ = await self._run_step(job, {"name": "branch", "argv": fetch,
                                           "cwd": str(module_dir)})
        if job["id"] in self._cancel:
            raise _Cancelled()
        if rc != 0:
            raise InstallError(f"git fetch origin {ref} が失敗しました — ログ末尾を確認してください")
        rc, _ = await self._exec(git + ["show-ref", "--verify", "--quiet", f"refs/heads/{ref}"],
                                 module_dir)
        has_local = rc == 0
        argv = (git + ["checkout", ref]) if has_local else \
               (git + ["checkout", "-b", ref, "--track", f"refs/remotes/origin/{ref}"])
        rc, _ = await self._run_step(job, {"name": "branch", "argv": argv, "cwd": str(module_dir)})
        if job["id"] in self._cancel:
            raise _Cancelled()
        if rc != 0:
            raise InstallError(f"{ref} に checkout できませんでした — ログ末尾を確認してください")
        self._append_log(job, f"ブランチを {cur} → {ref} に切り替えました")
        if has_local:
            # ローカルに同じ名のブランチがあった: 速い fast-forward だけ origin まで進める
            rc, _ = await self._run_step(job, {"name": "branch", "argv": git + [
                "merge", "--ff-only", f"refs/remotes/origin/{ref}"], "cwd": str(module_dir)})
            if rc != 0:
                self._append_log(job, f"{ref} はローカルに独自コミットがあるため origin の位置へは"
                                      "進めませんでした (現在のコミットで続けます)")
        return True

    async def _run_install(self, job: dict[str, Any], spec: dict[str, Any],
                           steps: list[dict[str, Any]]) -> None:
        job["status"] = "running"
        self._save()
        module_dir = (self.root / job["module_path"]).resolve()
        switched = False   # ブランチ切替が起きたら依存を入れ直す (precheck スキップを効かせない)
        try:
            if job.get("clean") and (module_dir / ".venv").exists():
                self._append_log(job, "クリーン再インストール: .venv を削除します")
                await asyncio.to_thread(shutil.rmtree, module_dir / ".venv")
            for step in steps:
                if job["id"] in self._cancel:
                    raise _Cancelled()
                name = step["name"]
                if name == "venv":
                    job["step"] = name
                    note = await asyncio.to_thread(self._venv_ensure, job, module_dir)
                    self._append_log(job, note)
                    continue
                if name == "branch":
                    job["step"] = name
                    switched = await self._switch_branch(job, module_dir, str(job.get("ref") or ""))
                    continue
                if name == "precheck":
                    # すでに導入済みなら install をスキップ (押し直しで 2 GB を引き直さない)。
                    # ブランチを切った直後は依存セットが変わり得るのでスキップしない
                    rc, _ = await self._exec(step["argv"], Path(step["cwd"]))
                    if rc == 0 and not job.get("clean") and not switched:
                        self._append_log(job, "インストール済みです (変更なし)")
                        if spec.get("check") == "freetoken":
                            # 導入済みでも起動 JSON だけ無い環境 (setup.sh 以前から有る clone) は補う
                            note = seed_launch_json(module_dir)
                            if note:
                                self._append_log(job, note)
                        job["status"] = "done"
                        job["result"] = {"already_installed": True}
                        return
                    continue
                rc, _out = await self._run_step(job, step)
                if job["id"] in self._cancel:
                    raise _Cancelled()
                if rc != 0:
                    raise InstallError(f"{name} が失敗しました (exit {rc}) — ログ末尾を確認してください")
            if spec.get("check") == "freetoken":
                note = seed_launch_json(module_dir)
                if note:
                    self._append_log(job, note)
            job["status"] = "done"
            job["result"] = {"installed": True}
        except _Cancelled:
            job["status"] = "cancelled"
            self._append_log(job, "キャンセルされました")
        except InstallError as e:
            job["status"] = "failed"
            job["error"] = str(e)
            self._append_log(job, f"失敗: {e}")
        except Exception as e:  # noqa: BLE001
            job["status"] = "failed"
            job["error"] = f"{type(e).__name__}: {e}"
            self._append_log(job, f"失敗: {job['error']}")
        finally:
            self._finish(job)

    # ------------------------------------------------------------ モデルダウンロード

    async def start_download(self, repo_id: str, *, revision: str = "",
                             allow_patterns: list[str] | None = None,
                             local_dir: str = "", backend: str = "",
                             actor: dict[str, Any] | None = None) -> dict[str, Any]:
        """HF Hub からモデルをダウンロードする。既定は HF キャッシュ (app が成功後に
        スキャン失効)。backend に model_data 系 (freetoken-fork) を指定すると
        setup.sh と同じ <data>/models/<org>--<name> へ置き、完了後に起動 JSON へ記入する。"""
        self._check_free()
        repo = (repo_id or "").strip()
        if not repo or not _REPO_RE.match(repo) or ".." in repo:
            raise InstallError("repo_id が不正です (例: org/model-name)")
        rev = (revision or "").strip()
        if rev and (not _REVISION_RE.match(rev) or ".." in rev):
            raise InstallError("revision が不正です")
        pats = [p.strip() for p in (allow_patterns or []) if p.strip()]
        for p in pats:
            if len(p) > 100 or ".." in p or p.startswith("-"):
                raise InstallError(f"allow_patterns が不正です: {p}")
        spec = BACKENDS.get(backend) or {}
        dest = (local_dir or "").strip()
        if dest:
            p = Path(dest).expanduser()
            p = (self.root / p) if not p.is_absolute() else p
            try:
                resolved = p.resolve()
                root_r = self.root.resolve()
            except OSError:
                raise InstallError("local_dir を解決できません")
            if resolved == root_r or root_r not in resolved.parents:
                raise InstallError("local_dir は launcher の root 内の下位ディレクトリのみ指定できます")
            dest = str(resolved)
        elif spec.get("model_data"):
            # サーバー側解決のパスなので root 制約の外 (FREETOKEN_DATA_DIR 可) でも許す。
            # フォルダ名は setup.sh の sed 's|/|--|g' と同じ規約
            model_root = fork_model_root(self.root, spec)
            dest = str(model_root / repo.replace("/", "--")) if model_root else ""
        # tqdm フックが進捗と cooperative cancel の唯一の経路なので、API の提供を確認する
        try:
            from huggingface_hub import snapshot_download
            ok_hook = "tqdm_class" in inspect.signature(snapshot_download).parameters
        except Exception as e:  # noqa: BLE001
            raise InstallError(f"huggingface_hub が使えません: {e}")
        if not ok_hook:
            raise InstallError("この huggingface_hub では UI からのダウンロードは使えません")

        job = self._new_job("download", actor, backend=backend if spec.get("model_data") else "",
                            backend_name=str(spec.get("name") or "Hugging Face"),
                            repo_id=repo, revision=rev, allow_patterns=pats, local_dir=dest,
                            progress={"done": 0, "file_total": 0, "file": "",
                                      "files_done": 0, "speed_bps": 0})
        self._tasks[job["id"]] = asyncio.create_task(self._run_download(job))
        return self._public(job)

    def _make_tqdm(self, job: dict[str, Any], cancel: threading.Event) -> type:
        """snapshot_download に渡す tqdm サブクラス。update で受信 byte を数え、
        cancel を確認する (raise で転送ループそのものを止める)。
        disable=True で描画を殺す (サーバー stderr にバーを流さない)。"""
        from tqdm.auto import tqdm

        class _Hook(tqdm):  # type: ignore[misc,valid-type]
            def __init__(self, *args: Any, **kw: Any) -> None:  # noqa: D107
                # disable=True だと unit/desc が未設定になるので引数から控えておく
                self._hook_unit = kw.get("unit", "it")
                self._hook_desc = kw.get("desc") or ""
                kw["disable"] = True
                super().__init__(*args, **kw)

            def update(self, n: float = 1) -> None:  # noqa: D102
                if cancel.is_set():
                    raise _Cancelled()
                super().update(n)
                if getattr(self, "_hook_unit", "") == "B":
                    pr = job["progress"]
                    pr["done"] += int(n or 0)
                    if self.total:
                        pr["file_total"] = int(self.total)
                    pr["file"] = getattr(self, "desc", "") or self._hook_desc
                return None

            def set_description(self, desc: str = "", refresh: bool = True) -> None:  # noqa: D102
                self._hook_desc = desc or ""

            def close(self) -> None:  # noqa: D102
                if not getattr(self, "_hook_counted", False):
                    self._hook_counted = True
                    job["progress"]["files_done"] = job["progress"].get("files_done", 0) + 1
                super().close()

        return _Hook

    def _snapshot(self, job: dict[str, Any], cancel: threading.Event) -> str:
        from huggingface_hub import snapshot_download
        kwargs: dict[str, Any] = {
            "repo_id": job["repo_id"],
            "revision": job["revision"] or None,
            "allow_patterns": job["allow_patterns"] or None,
            "local_dir": job["local_dir"] or None,
            "tqdm_class": self._make_tqdm(job, cancel),
            "max_workers": 8,
        }
        return str(snapshot_download(**kwargs))

    async def _run_download(self, job: dict[str, Any]) -> None:
        job["status"] = "running"
        job["step"] = "転送開始"
        where = job["local_dir"] or "(既定の HF キャッシュ)"
        self._append_log(job, f"ダウンロード開始: {job['repo_id']}"
                         + (f" revision={job['revision']}" if job["revision"] else "")
                         + (f" patterns={job['allow_patterns']}" if job["allow_patterns"] else "")
                         + f" 保存先={where}")
        self._save()
        cancel = threading.Event()
        self._dl_cancel[job["id"]] = cancel
        tick = asyncio.create_task(self._dl_tick(job))
        try:
            path = await asyncio.to_thread(self._snapshot, job, cancel)
            pr = job["progress"]
            self._append_log(job, f"完了: {path} ({pr['done'] / 2**30:.2f} GiB,"
                             f" {pr['files_done']} ファイル)")
            if job.get("backend") and job.get("local_dir"):
                note = record_launch_json_model(self.root, BACKENDS[job["backend"]],
                                                Path(job["local_dir"]))
                if note:
                    self._append_log(job, note)
            job["status"] = "done"
            job["result"] = {"path": path, "bytes": pr["done"]}
        except _Cancelled:
            job["status"] = "cancelled"
            self._append_log(job, "キャンセルされました (受信済みの一部ファイルはキャッシュに残ります)")
        except InstallError as e:
            job["status"] = "failed"
            job["error"] = str(e)
            self._append_log(job, f"失敗: {e}")
        except Exception as e:  # noqa: BLE001
            job["status"] = "failed"
            job["error"] = f"{type(e).__name__}: {e}"
            self._append_log(job, f"失敗: {job['error']}")
        finally:
            tick.cancel()
            self._dl_cancel.pop(job["id"], None)
            self._finish(job)

    async def _dl_tick(self, job: dict[str, Any]) -> None:
        """ダウンロード中は 2 秒ごとに進捗を永続化する (byte 単位で書かない)。"""
        last_done = 0
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                await asyncio.sleep(2.0)
                pr = job["progress"]
                dt = 2.0
                pr["speed_bps"] = max(0, pr["done"] - last_done) / dt
                last_done = pr["done"]
                job["step"] = f"受信中 {pr['done'] / 2**20:.1f} MB" + (f" {pr['file']}" if pr["file"] else "")
                self._save()

    # ------------------------------------------------------------ 共通の後始末 / 制御

    def _finish(self, job: dict[str, Any]) -> None:
        job["finished_at"] = now()
        self._tasks.pop(job["id"], None)
        if self.active_id == job["id"]:
            self.active_id = None
        self._cancel.discard(job["id"])
        self._save()
        if self.on_job_done:
            with contextlib.suppress(Exception):
                self.on_job_done(self._public(job))

    def cancel(self, jid: str) -> dict[str, Any]:
        job = self.jobs.get(jid)
        if not job:
            raise KeyError(jid)
        if job["status"] not in ("queued", "running"):
            raise InstallError("このジョブは既に終了しています")
        self._cancel.add(jid)
        proc = self._procs.get(jid)
        if proc is not None and proc.returncode is None:
            pid = proc.pid
            self._signal_group(pid, signal.SIGTERM)
            self._kill_later(jid, pid)
        ev = self._dl_cancel.get(jid)
        if ev is not None:
            ev.set()
            self._append_log(job, "キャンセル要求: 転送ブロックの切れ目で停止します")
        self._save()
        return self._public(job)

    def _kill_later(self, jid: str, pid: int) -> None:
        grace = float(os.environ.get("LLM_LAUNCHER_INSTALL_KILL_GRACE_S", "60"))

        async def wait_kill() -> None:
            t0 = time.monotonic()
            while time.monotonic() - t0 < grace:
                proc = self._procs.get(jid)
                if proc is None or proc.returncode is not None:
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
        """1 コマンド direct exec (shell を挟まない) で走らせ、ログを逐行取り込む。"""
        job["step"] = step["name"]
        self._append_log(job, f"===== {step['name']} {time.strftime('%F %T')} =====")
        self._append_log(job, shlex.join(step["argv"]))
        env = {**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
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
                if kept < 96 * 1024:
                    out.append(text)
                    kept += len(text)
            rc = proc.returncode if proc.returncode is not None else await proc.wait()
        finally:
            self._procs.pop(job["id"], None)
        return rc or 0, "".join(out)
