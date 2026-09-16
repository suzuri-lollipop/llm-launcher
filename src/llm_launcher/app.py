"""FastAPI アプリケーション: 管理 REST API と Web UI の配信。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import re
import shlex
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from . import __version__
from .backends import (SECTIONS_JA, BACKENDS, python_candidates, resolve_port,
                       sections_order)
from .process import ProcessManager, port_in_use
from .store import JsonStore, new_id, now
from .bench import BenchError, BenchManager
from . import sysinfo

STATIC_DIR = Path(__file__).parent / "static"

DETECT_TTL = 20.0


class ProfileBody(BaseModel):
    """1プロファイル = 1バックエンド。バックエンドは編集フォームのプルダウンで選ぶ。"""
    name: str = Field(min_length=1, max_length=80)
    backend: str = ""
    values: dict[str, Any] = {}
    # 各フラグの送信 ON/OFF (None = 旧来動作: 値があれば送信)
    on: dict[str, bool] | None = None
    extra_args: str = ""
    env: dict[str, str] = {}
    cwd: str = ""
    command: str = ""
    custom_only: bool = False
    note: str = ""
    # 旧 v2 (configs) 形式の互換受け: default_backend か最初の1件を展開する
    configs: dict[str, Any] = {}
    default_backend: str = ""

    @model_validator(mode="before")
    @classmethod
    def _compat_v2(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("backend") and data.get("configs"):
            cfgs = data["configs"]
            if cfgs:
                bid = data.get("default_backend") if data.get("default_backend") in cfgs else next(iter(cfgs))
                c = cfgs[bid] or {}
                data = {**data, "backend": bid,
                        "values": c.get("values") or {}, "extra_args": c.get("extra_args") or "",
                        "env": c.get("env") or {}, "cwd": c.get("cwd") or "",
                        "command": c.get("command") or "", "custom_only": bool(c.get("custom_only"))}
        return data


class StartBody(BaseModel):
    force: bool = False


class SessionStartBody(StartBody):
    profile_id: str


class BackendBody(BaseModel):
    command: str | None = None
    python: str | None = None
    command_binary: str | None = None
    reset: bool = False


class BenchBody(BaseModel):
    """組み込みベンチマーク設定。profile_id のサーバー (未起動なら自動起動可) へ
    OpenAI 互換 API で負荷をかけ、並列度リストの各値を 1 ステージとして計測する。"""
    profile_id: str = Field(min_length=1)
    model: str = ""
    endpoint: str = "chat"            # chat | completions
    stream: bool = True               # ON で TTFT も計測
    concurrency: list[int] = Field(default_factory=lambda: [1, 8])
    num_prompts: int = Field(default=24, ge=1, le=10000)
    input_len: int = Field(default=128, ge=8, le=16384)    # プロンプト語数(≈語=token)
    output_len: int = Field(default=128, ge=1, le=8192)    # max_tokens
    temperature: float | None = Field(default=0.0, ge=0.0, le=2.0)
    warmup: int = Field(default=1, ge=0, le=16)
    request_timeout: int = Field(default=180, ge=10, le=3600)
    ready_timeout: int = Field(default=600, ge=10, le=3600)
    auto_start: bool = True
    stop_after: bool = False

    @model_validator(mode="before")
    @classmethod
    def _compat_int_concurrency(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("concurrency"), int):
            data = {**data, "concurrency": [data["concurrency"]]}
        return data

    @model_validator(mode="after")
    def _normalize(self) -> "BenchBody":
        if self.endpoint not in ("chat", "completions"):
            raise ValueError("endpoint は chat または completions を指定してください")
        self.concurrency = sorted({max(1, min(256, int(c))) for c in self.concurrency})[:8]
        if not self.concurrency:
            self.concurrency = [8]
        return self

    def to_params(self) -> dict[str, Any]:
        d = self.model_dump()
        d.pop("profile_id")
        d["model"] = self.model.strip()
        return d


class PlaygroundChatBody(BaseModel):
    """プレイグラウンドのチャット要求。起動中セッションの OpenAI 互換 API へ透過転送する。"""
    messages: list[dict[str, Any]] = Field(min_length=1)
    model: str = ""
    stream: bool = True
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, ge=1)


def sse_aggregate(raw: str) -> dict[str, Any]:
    """stream=false を無視して SSE を返すサーバー向けに、SSE 本文を
    OpenAI 非ストリーム応答と同等の JSON に集約する。"""
    parts: list[str] = []
    reasons: list[str] = []
    usage: dict[str, Any] = {}
    model = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            evt = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(evt, dict):
            continue
        if isinstance(evt.get("usage"), dict):
            usage = evt["usage"]
        if evt.get("model"):
            model = str(evt["model"])
        ch = (evt.get("choices") or [{}])[0] if evt.get("choices") else {}
        delta = ch.get("delta") if isinstance(ch.get("delta"), dict) else {}
        if isinstance(delta.get("content"), str):
            parts.append(delta["content"])
        rsn = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(rsn, str):
            reasons.append(rsn)
    msg: dict[str, Any] = {"role": "assistant", "content": "".join(parts)}
    if reasons:
        msg["reasoning_content"] = "".join(reasons)
    out: dict[str, Any] = {"model": model,
                           "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}]}
    if usage:
        out["usage"] = usage
    return out


def json_as_sse(obj: dict[str, Any]) -> bytes:
    """逆ケース (stream=true を無視して JSON を返すサーバー) 向けに、
    非ストリーム応答を delta 形式の SSE 1 チャンク + [DONE] に変換する。"""
    ch = (obj.get("choices") or [{}])[0] if obj.get("choices") else {}
    msg = ch.get("message") if isinstance(ch.get("message"), dict) else {}
    delta: dict[str, Any] = {}
    if msg.get("content") or ch.get("text"):
        delta["content"] = msg.get("content") or ch.get("text")
    rsn = msg.get("reasoning_content") or msg.get("reasoning")
    if rsn:
        delta["reasoning_content"] = rsn
    chunk = {"model": obj.get("model", ""), "choices": [{"index": 0, "delta": delta}]}
    if isinstance(obj.get("usage"), dict):
        chunk["usage"] = obj["usage"]
    return (f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            "data: [DONE]\n\n").encode("utf-8")


def hf_hub_cache_dir() -> Path:
    """huggingface_hub の既定キャッシュパス解決 (HF_HUB_CACHE > HF_HOME > ~/.cache)。"""
    env = os.environ.get("HF_HUB_CACHE")
    if env:
        return Path(env).expanduser()
    home = os.environ.get("HF_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".cache" / "huggingface"
    return base / "hub"


def scan_hf_cache(base: Path | None = None, max_files: int = 400) -> dict[str, Any]:
    """HF Hub キャッシュの models--org--name からモデル一覧を構築する。"""
    base = base or hf_hub_cache_dir()
    result: dict[str, Any] = {"cache_dir": str(base), "exists": base.is_dir(), "models": []}
    if not base.is_dir():
        return result
    entries: list[tuple[float, dict[str, Any]]] = []
    for d in base.iterdir():
        if not d.is_dir() or not d.name.startswith("models--"):
            continue
        repo_id = d.name[len("models--"):].replace("--", "/", 1)
        snaps_dir = d / "snapshots"
        if not snaps_dir.is_dir():
            continue
        refs: dict[str, str] = {}
        refs_dir = d / "refs"
        if refs_dir.is_dir():
            for rf in refs_dir.rglob("*"):        # refs/pr/1 のような入れ子ブランチにも対応
                try:
                    if rf.is_file():
                        refs[str(rf.relative_to(refs_dir))] = rf.read_text().strip()
                except OSError:
                    continue
        snaps = [s for s in snaps_dir.iterdir() if s.is_dir()]
        if not snaps:
            continue
        def snap_mtime(s: Path) -> float:
            try:
                return s.stat().st_mtime
            except OSError:
                return 0
        snaps.sort(key=snap_mtime, reverse=True)
        wanted = refs.get("main") or refs.get("master")
        chosen = next((s for s in snaps if wanted and s.name.startswith(wanted)), snaps[0])
        revision = next((k for k, v in refs.items() if chosen.name.startswith(v)), None) or (wanted or "")[:10] or "main"
        gguf: list[dict[str, Any]] = []
        has_weights = False
        try:
            for f in sorted(chosen.iterdir()):
                if not f.is_file():
                    continue
                size = f.stat().st_size
                suf = f.suffix.lower()
                if suf in (".safetensors", ".bin", ".pt", ".gguf") and size > 1 << 20:
                    has_weights = True
                if suf == ".gguf":
                    gguf.append({"name": f.name, "size": size})
                if len(gguf) >= max_files:
                    break
        except OSError:
            pass
        entries.append((snap_mtime(chosen), {
            "repo_id": repo_id,
            "snapshot": str(chosen),
            "revision": revision,
            "has_weights": has_weights,
            "gguf": gguf,
        }))
    entries.sort(key=lambda t: t[0], reverse=True)
    result["models"] = [m for _, m in entries]
    return result


def lan_ip() -> str:
    try:
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())


def migrate_profiles(store: JsonStore) -> None:
    """v2 プロファイル (configs マルチバックエンド) を 1プロファイル=1バックエンド へ分離する。

    複数 configs を持つプロファイルはバックエンドごとに分裂し、2件目以降には
    `[バックエンド名]` を接尾辞として付ける。v1/現行形式はそのままとする。"""
    from .store import new_id as _new_id
    lst = store.load()
    out: list[dict[str, Any]] = []
    changed = False
    for p in lst:
        if isinstance(p, dict) and "configs" in p:
            changed = True
            cfgs = [(bid, c) for bid, c in (p.get("configs") or {}).items() if bid in BACKENDS]
            for i, (bid, c) in enumerate(cfgs):
                c = c or {}
                name = p.get("name", "")
                if len(cfgs) > 1:
                    name = f"{name} [{BACKENDS[bid]['name']}]"
                out.append({
                    "id": p["id"] if i == 0 else _new_id("p"),
                    "name": name, "backend": bid,
                    "values": c.get("values") or {},
                    "extra_args": c.get("extra_args") or "",
                    "env": c.get("env") or {},
                    "cwd": c.get("cwd") or "",
                    "command": c.get("command") or "",
                    "custom_only": bool(c.get("custom_only")),
                    "note": p.get("note", ""),
                    "created_at": p.get("created_at"), "updated_at": now(),
                })
        else:
            out.append(p)
    if changed:
        store._cache = out
        store.save()


def create_app(root: Path) -> FastAPI:
    data_dir = root / "data"
    profiles_store = JsonStore(data_dir / "profiles.json", [])
    settings_store = JsonStore(data_dir / "settings.json", {"backends": {}})
    runs_store = JsonStore(data_dir / "runs.json", [])
    bench_store = JsonStore(data_dir / "bench.json", [])
    manager = ProcessManager(root, runs_store, profiles_store, settings_store)
    bench = BenchManager(bench_store, profiles_store, manager)

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
            # 候補 (module/.venv → GUI .venv → launcher) の順に実 import を試し、
            # 実際に使える python を解決して次回起動から使う
            override = (manager._backend_setting(backend_id).get("python") or "").strip()
            cands = ([("明示設定", override)] if override else []) + \
                python_candidates(root, b.get("module_path"))
            chosen = None
            tried: list[str] = []
            for label, py in cands:
                if not Path(py).exists():
                    continue
                tried.append(f"{label}: {py}")
                try:
                    proc = await asyncio.wait_for(
                        asyncio.create_subprocess_exec(
                            py, "-c",
                            "import importlib.util,sys;"
                            "sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)",
                            b["check_module"],
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        ),
                        timeout=5,
                    )
                    rc = await asyncio.wait_for(proc.wait(), timeout=20)
                except Exception as e:  # noqa: BLE001
                    tried[-1] += f" (検出失敗: {e})"
                    continue
                if rc == 0:
                    chosen = py
                    break
            if chosen:
                info["installed"] = True
                info["python"] = chosen
                manager.set_resolved_python(backend_id, chosen)
                info["detail"] = f"python: {chosen}"
            else:
                py = manager.resolve_python(backend_id)
                info["python"] = py
                info["installed"] = False
                info["detail"] = f"未検出 ({b['check_module']} import 不可)。試行: {' | '.join(tried) or '利用可能な python なし'}"
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
            "sections": [{"id": s, "label": SECTIONS_JA.get(s, s)} for s in sections_order(b)],
            "default_port": b.get("default_port", ""),
            "detection": det,
        }

    app = FastAPI(title="llm-launcher", version=__version__, docs_url="/api/docs")
    app.state.manager = manager
    app.state.root = root

    @app.on_event("startup")
    async def _startup() -> None:
        migrate_profiles(profiles_store)
        bench.on_startup()
        await manager.start_tick()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await bench.shutdown()
        await manager.shutdown(kill=False)

    # -------------------------------------------------------------- system

    @app.get("/api/system")
    async def get_system() -> dict[str, Any]:
        host = await asyncio.to_thread(sysinfo.host_snapshot, str(root))
        gpu = await sysinfo.gpu_snapshot()
        return {"host": host, **gpu}

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
        if body.backend not in BACKENDS:
            raise HTTPException(400, "バックエンドを選択してください")
        env = {}
        for k, v in (body.env or {}).items():
            k = str(k).strip()
            if k and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                raise HTTPException(400, f"環境変数名が無効です: {k}")
            if k:
                env[k] = str(v)
        togg = {f["key"] for f in BACKENDS[body.backend]["fields"] if f["type"] != "bool"}
        on = None if body.on is None else {str(k): bool(v) for k, v in body.on.items() if k in togg}
        return {
            "name": body.name.strip(),
            "backend": body.backend,
            "values": {k: v for k, v in (body.values or {}).items()},
            "on": on,
            "extra_args": body.extra_args or "",
            "env": env,
            "cwd": body.cwd or "",
            "command": body.command or "",
            "custom_only": bool(body.custom_only),
            "note": body.note or "",
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
        try:
            argv = manager.build_argv(data)
            return {"ok": True, "backend": data["backend"], "argv": argv,
                    "command": " ".join(shlex.quote(a) for a in argv)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    # ----------------------------------------------------------- sessions

    @app.post("/api/sessions", status_code=201)
    async def start_session(body: SessionStartBody) -> dict[str, Any]:
        profile = manager.find_profile(body.profile_id)
        if not profile:
            raise HTTPException(404, "プロファイルが見つかりません")
        bid = profile.get("backend")
        if bid not in BACKENDS:
            raise HTTPException(400, "このプロファイルにバックエンドが設定されていません")
        already = [s for s in manager.sessions.values()
                   if s["profile_id"] == body.profile_id
                   and s["status"] in ("running", "starting", "stopping")]
        if already:
            raise HTTPException(409, f"このプロファイルは起動済みです (session {already[0]['id']})")
        argv = manager.build_argv(profile)
        port = resolve_port(BACKENDS[bid], profile, argv)
        if port and port_in_use(port) and not body.force:
            dup = [s for s in manager.sessions.values()
                   if s.get("port") == port and s["status"] in ("running", "starting")]
            who = f"起動中のサーバー ({dup[0]['profile_name']} / {dup[0]['backend_name']})" if dup else "他プロセス"
            raise HTTPException(409, f"ポート {port} は {who} が使用中です。"
                                     "問題なければ force で起動してください。")
        try:
            session = await manager.start(profile)
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
        await manager.stop(sid)
        session = await manager.start(profile)
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

    # ------------------------------------------------------- huggingface hub

    hf_cache_state: dict[str, Any] = {"ts": 0.0, "data": None}

    @app.get("/api/hf-models")
    async def hf_models(refresh: int = 0) -> dict[str, Any]:
        if refresh or hf_cache_state["data"] is None or time.monotonic() - hf_cache_state["ts"] > 15:
            hf_cache_state["data"] = await asyncio.to_thread(scan_hf_cache)
            hf_cache_state["ts"] = time.monotonic()
        return hf_cache_state["data"]

    # ---------------------------------------------------------- playground
    # 起動中セッション (OpenAI 互換 API) への透過プロキシ。ブラウザから直接だと
    # vLLM 等は CORS ヘッダを返さずブラウザから直叩きできないため、
    # launcher 同一オリジン経由で中継する (127.0.0.1 のセッションポートへ)。

    def playground_base(sid: str) -> str:
        s = manager.sessions.get(sid)
        if not s:
            raise HTTPException(404, "セッションが見つかりません")
        port = s.get("port")
        if not port:
            raise HTTPException(400, "このセッションはポートを解決できていません (プロファイルの port を確認してください)")
        return f"http://127.0.0.1:{port}"

    def playground_open(req: urllib.request.Request):
        """接続確立までを同期実行。失敗は HTTPException に変換する。"""
        try:
            return urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as e:
            with contextlib.suppress(OSError):
                raise HTTPException(e.code, f"バックエンドエラー (HTTP {e.code}): "
                                            f"{e.read().decode('utf-8', 'replace')[:500]}")
            raise HTTPException(e.code, f"バックエンドエラー (HTTP {e.code})")
        except OSError as e:
            raise HTTPException(502, f"サーバーに接続できません (起動中ですか?): {e}")

    @app.get("/api/playground/{sid}/models")
    async def playground_models(sid: str) -> dict[str, Any]:
        base = playground_base(sid)

        def _get() -> dict[str, Any]:
            with urllib.request.urlopen(f"{base}/v1/models", timeout=8) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        try:
            return await asyncio.to_thread(_get)
        except HTTPException:
            raise
        except urllib.error.HTTPError as e:
            raise HTTPException(e.code, f"モデル一覧の取得に失敗しました (HTTP {e.code})")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"サーバーに接続できません (起動中ですか?): {e}")

    @app.post("/api/playground/{sid}/chat")
    async def playground_chat(sid: str, body: PlaygroundChatBody) -> Any:
        base = playground_base(sid)
        payload: dict[str, Any] = {"messages": body.messages, "stream": body.stream}
        if body.stream:
            # OpenAI 標準の stream_options を付与し、ストリームの最終チャンクに
            # usage (トークン数) を含めさせる。プレイグラウンドの生成速度 (tok/s)
            # 統計が正確なトークン数で描画されるようになる。
            payload["stream_options"] = {"include_usage": True}
        if body.model:
            payload["model"] = body.model
        for key in ("temperature", "top_p", "max_tokens"):
            v = getattr(body, key)
            if v is not None:
                payload[key] = v
        req = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream" if body.stream else "application/json"},
            method="POST",
        )
        if not body.stream:
            def _once() -> dict[str, Any]:
                resp = playground_open(req)
                with resp:
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    raw = resp.read().decode("utf-8", "replace")
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    if "text/event-stream" in ctype:
                        # stream=false を無視して SSE を返すサーバーのフォールバック
                        return sse_aggregate(raw)
                    raise HTTPException(502, "バックエンド応答を解析できません (JSON ではありません)")
            return await asyncio.to_thread(_once)

        resp = await asyncio.to_thread(playground_open, req)
        if "text/event-stream" not in (resp.headers.get("Content-Type") or "").lower():
            # stream=true を無視して JSON を返すサーバーは SSE に組み替えて中継する
            def _wrap() -> bytes:
                with resp:
                    raw = resp.read().decode("utf-8", "replace")
                try:
                    return json_as_sse(json.loads(raw))
                except json.JSONDecodeError:
                    raise HTTPException(502, "バックエンド応答を解析できません (SSE でも JSON でもありません)")
            data = await asyncio.to_thread(_wrap)
            return StreamingResponse(iter([data]), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache",
                                              "X-Accel-Buffering": "no"})
        read = getattr(resp, "read1", None) or resp.read

        def _chunks():
            with resp:
                while True:
                    data = read(65536)
                    if not data:
                        break
                    yield data
        return StreamingResponse(_chunks(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---------------------------------------------------------- benchmarks
    # プロファイル指定のサーバー (OpenAI 互換 API) へ launcher 内蔵の負荷ランナーで
    # 計測する。server 起動 (未起動時・自動) → health 待ち → 並列度ステージ実行。
    # 進行状況は /api/benchmarks の live progress として/ref に返す。

    @app.get("/api/benchmarks")
    async def list_benchmarks() -> dict[str, Any]:
        return {"runs": bench.list_runs(), "active": bench.active_id}

    @app.post("/api/benchmarks", status_code=201)
    async def start_benchmark(body: BenchBody) -> dict[str, Any]:
        profile = manager.find_profile(body.profile_id)
        if not profile:
            raise HTTPException(404, "プロファイルが見つかりません")
        bid = profile.get("backend")
        if bid not in BACKENDS:
            raise HTTPException(400, "このプロファイルにバックエンドが設定されていません")
        if not body.auto_start and not any(
                s["profile_id"] == body.profile_id and s["status"] in ("running", "starting")
                for s in manager.sessions.values()):
            raise HTTPException(409, "プロファイルが起動していません。"
                                     "起動タブでスタートするか、auto_start を ON にしてください")
        try:
            run = await bench.start(profile, body.to_params(), BACKENDS[bid]["name"])
        except BenchError as e:
            raise HTTPException(409, str(e))
        return run

    @app.get("/api/benchmarks/{rid}")
    async def get_benchmark(rid: str) -> dict[str, Any]:
        run = bench.get(rid)
        if not run:
            raise HTTPException(404, "ベンチマークが見つかりません")
        return run

    @app.post("/api/benchmarks/{rid}/cancel")
    async def cancel_benchmark(rid: str) -> dict[str, Any]:
        try:
            bench.cancel(rid)
        except KeyError:
            raise HTTPException(404, "ベンチマークが見つかりません")
        except BenchError as e:
            raise HTTPException(409, str(e))
        return bench.get(rid) or {"ok": True}

    @app.delete("/api/benchmarks/{rid}")
    async def delete_benchmark(rid: str) -> dict[str, Any]:
        try:
            bench.delete(rid)
        except KeyError:
            raise HTTPException(404, "ベンチマークが見つかりません")
        except BenchError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    # -------------------------------------------------------------- static

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app
