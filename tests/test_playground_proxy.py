"""プレイグラウンドプロキシの思考トグル転送。

Qwen3 系は既定で思考 ON: 本文より先に reasoning が流れるため、思考を切れない
プレイグラウンドは「生成開始が遅い」ように見える (実測: 挨拶でも 48 トークン中
31 が思考)。トグルはプロキシが enable_thinking を chat_template_kwargs に変換して
転送して初めて意味を持つ — 古いプロキシはこのフィールドを捨てていた。
"""
from __future__ import annotations

import asyncio
import json
import urllib.request

from test_auth_users import Client, make_app
from test_roles import inject_session


class _FakeResp:
    headers = {"Content-Type": "application/json"}

    def read(self):
        return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _chat(tmp_path, monkeypatch, body_extra):
    app = make_app(tmp_path)
    inject_session(app, {"id": "p1", "name": "ft", "backend": "freetoken-fork"},
                   "sid1", "running")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = Client(app)

    async def go():
        st, data = await client.post("/api/playground/sid1/chat", {
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False, **body_extra})
        assert st == 200, data

    asyncio.run(go())
    return captured["body"]


def test_enable_thinking_false_reaches_engine_as_template_kwarg(tmp_path, monkeypatch):
    body = _chat(tmp_path, monkeypatch, {"enable_thinking": False})
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_enable_thinking_true_is_forwarded_too(tmp_path, monkeypatch):
    body = _chat(tmp_path, monkeypatch, {"enable_thinking": True})
    assert body["chat_template_kwargs"] == {"enable_thinking": True}


def test_omitted_thinking_keeps_server_default(tmp_path, monkeypatch):
    body = _chat(tmp_path, monkeypatch, {})
    assert "chat_template_kwargs" not in body
