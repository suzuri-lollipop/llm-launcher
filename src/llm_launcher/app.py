"""FastAPI アプリケーション: 管理 REST API と Web UI の配信。"""

from __future__ import annotations

import asyncio
import contextlib
import platform
import re
import shlex
import socket
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from . import __version__
from .backends import (BACKENDS, profile_backends, profile_config,
                       profile_default_backend, resolve_port)
from .process import ProcessManager, port_in_use
from .store import JsonStore, new_id, now

STATIC_DIR = Path(__file__).parent / "static"

DETECT_TTL = 20.0


class ProfileConfigBody(BaseModel):
    """1バックエンド分の起動設定 (特色の異なる引数はここに閉じ込める)。"""
    values: dict[str, Any] = {}
    extra_args: str = ""
    env: dict[str, str] = {}
    cwd: str = ""
    command: str = ""
    custom_only: bool = False


class ProfileBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    # v2: バックエンド選択そのものをプロファイルに含める
    configs: dict[str, ProfileConfigBody] = {}
    default_backend: str = ""
    note: str = ""
    # 互換: v1 形式 (単一バックエンド) のまま送信してきたクライアントの受け皿。
    # /api/preview では「表示したいバックエンド」の指定にも使える。
    backend: str = ""

    @model_validator(mode="before")
    @classmethod
    def _compat_v1(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("configs") and data.get("backend"):
            b = str(data["backend"])
            data = {**data,
                    "configs": {b: {k: data.get(k) or v for k, v in (
                        ("values", {}), ("extra_args", ""), ("env", {}),
                        ("cwd", ""), ("command", ""))}},
                    "default_backend": data.get("default_backend") or b}
        return data


class StartBody(BaseModel):
    force: bool = False


class SessionStartBody(StartBody):
    profile_id: str
    backend: str = ""   # 空 = プロファイルの既定バックエンド


class BackendBody(BaseModel):
    command: str | None = None
    python: str | None = None
    command_binary: str | None = None
    reset: bool = False


def lan_ip() -> str:
    try:
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())


def migrate_profiles_v1(store: JsonStore) -> None:
    """v1 プロファイル (単一バックエンド) を v2 (configs) へ起動時に一度だけ変換する。"""
    lst = store.load()
    changed = False
    for i, p in enumerate(lst):
        if isinstance(p, dict) and "configs" not in p and p.get("backend") in BACKENDS:
            bid = p["backend"]
            lst[i] = {
                "id": p["id"], "name": p.get("name", ""), "note": p.get("note", ""),
                "created_at": p.get("created_at"), "updated_at": p.get("updated_at"),
                "default_backend": bid,
                "configs": {bid: {k: p.get(k) or d for k, d in (
                    ("values", {}), ("extra_args", ""), ("env", {}),
                    ("cwd", ""), ("command", ""))}},
            }
            changed = True
    if changed:
        store.save()


def create_app(root: Path) -> FastAPI:
    data_dir = root / "data"
    profiles_store = JsonStore(data_dir / "profiles.json", [])
    settings_store = JsonStore(data_dir / "settings.json", {"backends": {}})
    runs_store = JsonStore(data_dir / "runs.json", [])
    manager = ProcessManager(root, runs_store, profiles_store, settings_store)

    detect_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def detect(backend_id: str, force: bool = False) -> dict[str, Any]:
        b = BACKENDS[backend_id]
        ts_cached = detect_cache.get(backend_id)
        if not force and ts_cached and time.monotonic() - ts_cached[0] < DETECT_TTL:
            return ts_cached[1]
        module_dir = root / b["module_path"]
        info: dict[str, Any] = {
            "module_exists": module_dir.is_dir() and any(module_dir.iterdir()) if module_dir.exists() else False,
            "installed": False,
            "detail": "",
        }
        if b["kind"] == "python":
            python = manager.resolve_python(backend_id)
            info["python"] = python
            try:
                proc = await asyncio.wait_for(
                    asyncio.create_subprocess_exec(
                        python, "-c",
                        "import importlib.util,sys;"
                        "sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)",
                        b["check_module"],
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    ),
                    timeout=5,
                )
                rc = await asyncio.wait_for(proc.wait(), timeout=15)
                info["installed"] = rc == 0
                info["detail"] = f"python: {python}"
            except Exception as e:  # noqa: BLE001
                info["detail"] = f"検出失敗: {e}"
        else:
            binary = manager.resolve_binary(backend_id)
            info["installed"] = bool(binary)
            info["detail"] = binary or "llama-server 未検出 (ビルド or バックエンド設定でパス指定)"
        detect_cache[backend_id] = (time.monotonic(), info)
        return info

    def backend_public(bid: str, det: dict[str, Any]) -> dict[str, Any]:
        b = BACKENDS[bid]
        st = manager._backend_setting(bid)
        return {
            "id": bid,
            "name": b["name"],
            "repo": b["repo"],
            "module_path": b["module_path"],
            "kind": b["kind"],
            "desc": b["desc"],
            "default_command": b["default_command"],
            "command": manager.base_command(bid),
            "python_override": st.get("python", ""),
            "binary_override": st.get("command_binary", ""),
            "fields": b["fields"],
            "default_port": b.get("default_port", ""),
            "detection": det,
        }

    app = FastAPI(title="llm-launcher", version=__version__, docs_url="/api/docs")
    app.state.manager = manager
    app.state.root = root

    @app.on_event("startup")
    async def _startup() -> None:
        migrate_profiles_v1(profiles_store)
        await manager.start_tick()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await manager.shutdown(kill=False)

    # ------------------------------------------------------------- state

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        dets = await asyncio.gather(*[detect(bid) for bid in BACKENDS])
        backends = [backend_public(bid, det) for bid, det in zip(BACKENDS, dets)]
        return {
            "system": {
                "hostname": socket.gethostname(),
                "ip": lan_ip(),
                "root": str(root),
                "platform": platform.platform(),
                "version": __version__,
                "server_time": now(),
            },
            "backends": backends,
            "profiles": profiles_store.load(),
            "sessions": manager.public_sessions(),
        }

    # ----------------------------------------------------------- profiles

    def validate_profile(body: ProfileBody) -> dict[str, Any]:
        if not body.configs:
            raise HTTPException(400, "バックエンドを1つ以上選択してください")
        configs: dict[str, Any] = {}
        for bid, cfg in body.configs.items():
            if bid not in BACKENDS:
                raise HTTPException(400, f"未知のバックエンドです: {bid}")
            env = {}
            for k, v in (cfg.env or {}).items():
                k = str(k).strip()
                if k and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                    raise HTTPException(400, f"[{BACKENDS[bid]['name']}] 環境変数名が無効です: {k}")
                if k:
                    env[k] = str(v)
            configs[bid] = {
                "values": {k: v for k, v in (cfg.values or {}).items()},
                "extra_args": cfg.extra_args or "",
                "env": env,
                "cwd": cfg.cwd or "",
                "command": cfg.command or "",
                "custom_only": bool(cfg.custom_only),
            }
        default = body.default_backend if body.default_backend in configs else next(iter(configs))
        return {
            "name": body.name.strip(),
            "note": body.note or "",
            "default_backend": default,
            "configs": configs,
        }

    @app.get("/api/profiles")
    async def list_profiles() -> Any:
        return profiles_store.load()

    @app.post("/api/profiles", status_code=201)
    async def create_profile(body: ProfileBody) -> dict[str, Any]:
        data = validate_profile(body)
        data["id"] = new_id("p")
        data["created_at"] = now()
        data["updated_at"] = now()
        profiles_store.mutate(lambda lst: lst.append(data))
        return data

    @app.put("/api/profiles/{profile_id}")
    async def update_profile(profile_id: str, body: ProfileBody) -> dict[str, Any]:
        data = validate_profile(body)

        def _upd(lst: list) -> dict:
            for i, p in enumerate(lst):
                if p["id"] == profile_id:
                    merged = {**p, **data, "updated_at": now()}
                    lst[i] = merged
                    return merged
            raise HTTPException(404, "プロファイルが見つかりません")
        return profiles_store.mutate(_upd)

    @app.delete("/api/profiles/{profile_id}")
    async def delete_profile(profile_id: str) -> dict[str, Any]:
        running = [s for s in manager.sessions.values()
                   if s["profile_id"] == profile_id and s["status"] in ("running", "starting", "stopping")]
        if running:
            raise HTTPException(409, "このプロファイルで起動中のサーバーがあります")

        def _del(lst: list) -> bool:
            before = len(lst)
            lst[:] = [p for p in lst if p["id"] != profile_id]
            return len(lst) < before
        if not profiles_store.mutate(_del):
            raise HTTPException(404, "プロファイルが見つかりません")
        return {"ok": True}

    @app.post("/api/preview")
    async def preview_command(body: ProfileBody) -> dict[str, Any]:
        data = validate_profile(body)
        data["id"] = "preview"
        bid = body.backend if body.backend in data["configs"] else data["default_backend"]
        try:
            argv = manager.build_argv(data, bid)
            return {"ok": True, "backend": bid, "argv": argv,
                    "command": " ".join(shlex.quote(a) for a in argv)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    # ----------------------------------------------------------- sessions

    @app.post("/api/sessions", status_code=201)
    async def start_session(body: SessionStartBody) -> dict[str, Any]:
        profile = manager.find_profile(body.profile_id)
        if not profile:
            raise HTTPException(404, "プロファイルが見つかりません")
        bid = body.backend or profile_default_backend(profile)
        if bid not in profile_backends(profile):
            raise HTTPException(400, f"このプロファイルに {BACKENDS[bid]['name'] if bid in BACKENDS else bid} の設定はありません")
        already = [s for s in manager.sessions.values()
                   if s["profile_id"] == body.profile_id and s["backend"] == bid
                   and s["status"] in ("running", "starting", "stopping")]
        if already:
            raise HTTPException(409, f"このプロファイルの {BACKENDS[bid]['name']} は起動済みです "
                                     f"(session {already[0]['id']})")
        argv = manager.build_argv(profile, bid)
        port = resolve_port(BACKENDS[bid], profile_config(profile, bid), argv)
        if port and port_in_use(port) and not body.force:
            dup = [s for s in manager.sessions.values()
                   if s.get("port") == port and s["status"] in ("running", "starting")]
            who = f"起動中のサーバー ({dup[0]['profile_name']} / {dup[0]['backend_name']})" if dup else "他プロセス"
            raise HTTPException(409, f"ポート {port} は {who} が使用中です。"
                                     "問題なければ force で起動してください。")
        try:
            session = await manager.start(profile, bid)
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        return manager.session_public(session["id"])

    @app.post("/api/sessions/{sid}/stop")
    async def stop_session(sid: str) -> dict[str, Any]:
        if sid not in manager.sessions:
            raise HTTPException(404, "セッションが見つかりません")
        await manager.stop(sid)
        return manager.session_public(sid)

    @app.post("/api/sessions/{sid}/restart", status_code=201)
    async def restart_session(sid: str) -> dict[str, Any]:
        old = manager.sessions.get(sid)
        if not old:
            raise HTTPException(404, "セッションが見つかりません")
        profile = manager.find_profile(old["profile_id"])
        if not profile:
            raise HTTPException(400, "元のプロファイルが削除されています")
        if old["backend"] not in profile_backends(profile):
            raise HTTPException(400, f"元のプロファイルから {old['backend_name']} の設定が削除されています")
        await manager.stop(sid)
        session = await manager.start(profile, old["backend"])
        return manager.session_public(session["id"])

    @app.delete("/api/sessions/{sid}")
    async def delete_session(sid: str) -> dict[str, Any]:
        s = manager.sessions.get(sid)
        if not s:
            raise HTTPException(404, "セッションが見つかりません")
        if s["status"] in ("running", "starting", "stopping"):
            raise HTTPException(409, "起動中のセッションです。停止してから削除してください")
        manager.sessions.pop(sid)
        with contextlib.suppress(OSError):
            (root / s["log"]).unlink()
        manager._persist()
        return {"ok": True}

    @app.get("/api/sessions/{sid}/log")
    async def session_log(sid: str, cursor: int = 0) -> dict[str, Any]:
        try:
            return manager.read_log(sid, cursor)
        except KeyError:
            raise HTTPException(404, "セッションが見つかりません")

    # ----------------------------------------------------------- backends

    @app.put("/api/backends/{bid}")
    async def update_backend(bid: str, body: BackendBody) -> dict[str, Any]:
        if bid not in BACKENDS:
            raise HTTPException(404, "バックエンドが見つかりません")

        def _mut(settings: dict) -> dict:
            backends = settings.setdefault("backends", {})
            entry = backends.setdefault(bid, {})
            if body.reset:
                backends[bid] = {}
            else:
                for attr in ("command", "python", "command_binary"):
                    v = getattr(body, attr)
                    if v is not None:
                        entry[attr] = v.strip()
            return dict(entry)
        st = settings_store.mutate(_mut)
        detect_cache.pop(bid, None)
        det = await detect(bid, force=True)
        return {"ok": True, "settings": st, "detection": det}

    @app.post("/api/backends/{bid}/detect")
    async def redetect_backend(bid: str) -> dict[str, Any]:
        if bid not in BACKENDS:
            raise HTTPException(404, "バックエンドが見つかりません")
        return await detect(bid, force=True)

    # -------------------------------------------------------------- static

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app
