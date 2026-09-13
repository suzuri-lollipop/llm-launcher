"""バックエンドプロセスの起動・監視・停止管理。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import time
import urllib.request
from pathlib import Path
from typing import Any

from .backends import (BACKENDS, default_python, render_argv, resolve_binary,
                       resolve_port)
from .store import JsonStore, new_id, now

LOG_TAIL_LIMIT = 512 * 1024


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
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._log_handles: dict[str, Any] = {}
        self._monitors: dict[str, asyncio.Task] = {}
        self._tick_task: asyncio.Task | None = None
        self._dirty = False

    # ------------------------------------------------------------- helpers

    def _persist(self) -> None:
        self.runs._cache = sorted(self.sessions.values(), key=lambda s: s["started_at"])
        self.runs.save()

    def _backend_setting(self, backend_id: str) -> dict[str, Any]:
        return dict((self.settings.load().get("backends") or {}).get(backend_id) or {})

    def resolve_python(self, backend_id: str) -> str:
        return self._backend_setting(backend_id).get("python") or default_python(self.root)

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
        return render_argv(
            backend, {**profile, "command": command},
            python=self.resolve_python(bid),
            binary=self.resolve_binary(bid),
            root=self.root,
        )

    # --------------------------------------------------------------- start

    async def start(self, profile: dict[str, Any]) -> dict[str, Any]:
        bid = profile.get("backend")
        if bid not in BACKENDS:
            raise RuntimeError("このプロファイルにバックエンドが設定されていません")
        backend = BACKENDS[bid]
        argv = self.build_argv(profile)
        port = resolve_port(backend, profile, argv)

        env = os.environ.copy()
        for k, v in (profile.get("env") or {}).items():
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
            if s["status"] == "failed":
                s["error"] = f"exit code {rc}"
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
        self._reconcile()
        self._tick_task = asyncio.create_task(self._tick_loop())

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
