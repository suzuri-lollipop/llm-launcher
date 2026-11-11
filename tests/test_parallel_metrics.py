"""デコード速度グラフの /api/sessions/{sid}/parallel 中継の契約。

ダッシュボードはブラウザから各サーバーの /metrics を直接叩けない (CORS / API
キー / ポート違い) ため、launcher が起動中ポートへ中継し、グラフに必要な
スロット内訳と 60 秒合算履歴だけを正規化して返す。
"""
from __future__ import annotations

import json
import asyncio
from pathlib import Path
from typing import Any

from llm_launcher import rbac
from llm_launcher.app import create_app


class FakeResponse:
    """urlopen() の context manager を、HTTP リクエストを飛ばさずに再現する。"""

    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def read(self) -> bytes:
        return self.body


async def asgi_call(app, method: str, path: str) -> tuple[int, Any]:
    payload = b""
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", b"test"), (b"content-type", b"application/json"),
                         (b"content-length", b"0")],
             "client": ("127.0.0.1", 50000), "server": ("test", 80)}
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(msg: dict) -> None:
        sent.append(msg)

    await app(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), 0)
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw.decode("utf-8", "replace")


def test_parallel_slot_metrics_proxy(tmp_path: Path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir(parents=True)
    runs = [
        {"id": "s_mock", "profile_id": "p_mock", "profile_name": "Mock Strata",
         "backend": "strata", "backend_name": "Strata", "port": 8124,
         "health_path": "/metrics", "ready": True, "status": "running",
         "pid": 1, "argv": []},
        {"id": "s_stale", "profile_id": "p_stale", "profile_name": "Stale Strata",
         "backend": "strata", "backend_name": "Strata", "port": 8125,
         "health_path": "/metrics", "ready": False, "status": "starting",
         "pid": 2, "argv": []},
    ]
    (data / "runs.json").write_text(json.dumps(runs), "utf-8")
    (data / "profiles.json").write_text("[]", "utf-8")
    metrics = {
        "engine": {"model_label": "mock-strata", "max_context": 262144},
        "live": {"state": "generating", "tok_s": 42.4, "parallel": 2, "active": 2,
                 "slots": [
                     {"slot": 0, "state": "generating", "tok_s": 21.2,
                      "prompt_tokens": 1200, "generated": 320},
                     {"slot": 1, "state": "generating", "tok_s": 21.2,
                      "prompt_tokens": 800, "generated": 170},
                 ]},
        "history": {"tok_s": [40.0] * 60},
        "time": 1793940000.0,
    }

    def fake_urlopen(req, timeout=2.0):
        return FakeResponse(json.dumps(metrics).encode("utf-8"))

    import llm_launcher.app as app_mod
    monkeypatch.setattr(app_mod.urllib.request, "urlopen", fake_urlopen)
    app = create_app(tmp_path)
    status, body = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_mock/parallel"))
    assert status == 200
    assert body["sid"] == "s_mock"
    assert body["backend"] == "strata"
    assert body["fetched_at"] == 1793940000.0
    assert body["live"]["tok_s"] == 42.4 and body["live"]["parallel"] == 2
    assert body["live"]["active"] == 2
    assert body["slots"][1]["slot"] == 1 and body["slots"][1]["tok_s"] == 21.2
    assert len(body["history"]["tok_s"]) == 60 and body["history"]["tok_s"][0] == 40.0

    status, _ = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_stale/parallel"))
    assert status == 503
    status, _ = asyncio.run(asgi_call(app, "GET", "/api/sessions/nope/parallel"))
    assert status == 404

    assert rbac.required_cap("GET", "/api/sessions/s_mock/parallel") == "dashboard"
    assert rbac.required_cap("GET", "/api/sessions/s_mock/log") == "logs"


def test_parallel_solo_request_takes_a_slot(tmp_path: Path, monkeypatch):
    """basalt の slot_table 意味づけ: 1 本のみのリクエストも最初の空きスロットを
    使う。Strata は単独経路では engine の batch スロットを占有しないため、
    outside_slots=1 の稼働リクエストをサービス側でスロット行に割り当てる。"""
    data = tmp_path / "data"
    data.mkdir(parents=True)
    runs = [{"id": "s_solo", "profile_id": "p_solo", "profile_name": "Solo Strata",
             "backend": "strata", "backend_name": "Strata", "port": 8124,
             "health_path": "/metrics", "ready": True, "status": "running",
             "pid": 1, "argv": []}]
    (data / "runs.json").write_text(json.dumps(runs), "utf-8")
    (data / "profiles.json").write_text("[]", "utf-8")
    metrics = {
        "engine": {"model_label": "mock-strata", "max_context": 262144},
        "live": {"state": "generating", "tok_s": 40.0, "parallel": 2, "running": 1,
                 "outside_slots": 1, "generated": 55, "prompt_tokens": 130,
                 "slots": [
                     {"slot": 0, "state": "idle", "held_tokens": 314},
                     {"slot": 1, "state": "idle", "held_tokens": 312},
                 ]},
        "history": {"tok_s": [None] * 60},
        "time": 1793940000.0,
    }

    def fake_urlopen(req, timeout=2.0):
        return FakeResponse(json.dumps(metrics).encode("utf-8"))

    import llm_launcher.app as app_mod
    monkeypatch.setattr(app_mod.urllib.request, "urlopen", fake_urlopen)
    app = create_app(tmp_path)
    status, body = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_solo/parallel"))
    assert status == 200
    slot0, slot1 = body["slots"]
    assert slot0["state"] == "generating" and slot0["tok_s"] == 40.0
    assert slot0["generated"] == 55 and slot0["prompt_tokens"] == 130
    assert slot1["state"] == "idle"
    # 待機行も basalt と同じく 0.0 (グラフが途切れない)
    assert slot1["tok_s"] == 0.0 and slot1["held_tokens"] == 312


def test_parallel_basalt_metrics_format(tmp_path: Path, monkeypatch):
    """basalt 形式 (/metrics の live.slots が dict、history.slot_tok_s が各スロット
    60 秒系列、行の詳細は /slots)。slot_table は 1 本でもスロットに割り当てる。"""
    data = tmp_path / "data"
    data.mkdir(parents=True)
    runs = [{"id": "s_bas", "profile_id": "p_bas", "profile_name": "Basalt",
             "backend": "strata", "backend_name": "Strata", "port": 8124,
             "health_path": "/metrics", "ready": True, "status": "running",
             "pid": 1, "argv": []}]
    (data / "runs.json").write_text(json.dumps(runs), "utf-8")
    (data / "profiles.json").write_text("[]", "utf-8")
    metrics = {
        "engine": {"model": "basalt-model", "max_context": 262144},
        "live": {"state": "generating", "tok_s": 41.2,
                 "slots": {"total": 4, "active": 1, "queue_depth": 0}, "queued": 0},
        "history": {"tok_s": [41.2] * 60,
                    "slot_tok_s": [[41.2] * 60, [0.0] * 60, [0.0] * 60, [0.0] * 60]},
        "time": 1793940000.0,
    }
    slot_rows = [
        {"id": 0, "n_ctx": 262144, "is_processing": True, "state": "decoding",
         "tok_s": 41.2, "generated": 90, "prompt_tokens": 120, "ctx_used": 210,
         "drafts_offered": 80, "drafts_accepted": 40},
        {"id": 1, "n_ctx": 262144, "is_processing": False, "state": "idle",
         "tok_s": 0.0, "generated": 0, "prompt_tokens": 0, "ctx_used": 0},
    ]

    def fake_urlopen(req, timeout=2.0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        body = slot_rows if url.endswith("/slots") else metrics
        return FakeResponse(json.dumps(body).encode("utf-8"))

    import llm_launcher.app as app_mod
    monkeypatch.setattr(app_mod.urllib.request, "urlopen", fake_urlopen)
    app = create_app(tmp_path)
    status, body = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_bas/parallel"))
    assert status == 200
    assert body["live"]["parallel"] == 4 and body["live"]["running"] == 1
    s0 = body["slots"][0]
    assert s0["state"] == "decoding" and s0["tok_s"] == 41.2
    assert s0["accept"] == 50 and s0["n_ctx"] == 262144
    assert body["slots"][1]["state"] == "idle" and body["slots"][1]["accept"] is None
    hist = body["history"]["slot_tok_s"]
    assert len(hist) == 4 and len(hist[0]) == 60 and hist[0][-1] == 41.2
