"""ベンチマークのデコード出力 (生成テキスト) 記録の回帰テスト。

「デコードが約 2 分続いた = 出力が止まらない不具合か?」を結果だけで判断できる
ように、リクエスト毎に生成テキスト (先頭 + 末尾・上限つき) と finish_reason
(stop=自然終了 / length=max_tokens 打ち切り) を記録する。ここでは:

  1. ストリーム / 非ストリーム双方でテキストと finish_reason が取れる
  2. 長い出力は先頭 + 末尾に切り詰められ、全文字数 (txn) が残る
  3. 先頭チャンクがヘッダ読み込みのバッファに引き込まれても失わない
  4. warmup (text_cap=0) はテキストを保存しない
  5. 中断で打ち切られたリクエストも「途中までの生成」が live スロットに残る
  6. _stage の reqs に tx/tx2/txn/fr が乗り、フロントがそれを描画する
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from llm_launcher import bench as bench_mod
from llm_launcher.bench import BenchManager, _one_request
from llm_launcher.store import JsonStore


def _frontend_src() -> str:
    """index.html は分割された (css/ + js/**)。アサーションは全体を結合した文字列に対して行う。"""
    base = Path(__file__).resolve().parents[1] / "src" / "llm_launcher" / "static"
    parts = [(base / "index.html").read_text(encoding="utf-8")]
    parts += [p.read_text(encoding="utf-8") for p in sorted((base / "css").glob("*.css"))]
    parts += [p.read_text(encoding="utf-8") for p in sorted((base / "js").rglob("*.js"))]
    return "\n".join(parts)


# ------------------------------------------------------------------ 偽サーバー


class _Handler(BaseHTTPRequestHandler):
    """OpenAI 互換の偽サーバー。pieces 個の content を送り finish_reason を付ける。"""
    mode = "stream"          # stream / stream_slow / json
    pieces = 6
    delay = 0.0
    finish_reason = "length"   # 既定は「長さ上限」(EOS 無視ベンチの想定)
    saw: list[str] = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = body.get("prompt") or body.get("messages", [{}])[0].get("content", "")
        self.saw.append(prompt)
        if self.mode == "json":
            out = json.dumps({"choices": [{"message": {"content": "hello there"},
                                           "finish_reason": self.finish_reason}],
                              "usage": {"completion_tokens": 2, "prompt_tokens": 3}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for i in range(self.pieces):
                if self.mode == "stream_slow":
                    waited = 0.0
                    while waited < self.delay:
                        time.sleep(min(0.02, self.delay - waited))
                        waited += 0.02
                evt = {"choices": [{"delta": {"content": f"w{i} "}}]}
                self.wfile.write(b"data: " + json.dumps(evt).encode() + b"\n\n")
                self.wfile.flush()
            final = {"choices": [{"delta": {}, "finish_reason": self.finish_reason}],
                     "usage": {"completion_tokens": self.pieces, "prompt_tokens": 5}}
            self.wfile.write(b"data: " + json.dumps(final).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass                      # 中断でクライアントが切った

    def log_message(self, *args):  # silence
        pass


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    _Handler.saw = []
    _Handler.mode, _Handler.pieces, _Handler.delay, _Handler.finish_reason = "stream", 6, 0.0, "length"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


# ------------------------------------------------------------- manager の雛形


class _FakeManager:
    def __init__(self, port: int):
        self.sessions: dict[str, dict] = {}
        self.port = port

    def _session(self, sid: str, **kw) -> dict:
        s = {"id": sid, "profile_id": "p1", "status": "running", "port": self.port,
             "health_path": "/health", "argv": [], **kw}
        self.sessions[sid] = s
        return s


PARAMS = {
    "model": "m", "endpoint": "completions", "stream": True,
    "concurrency": [1],
    "num_prompts": 2, "input_len": 16, "output_len": 8, "temperature": 0.0,
    "warmup": 1, "request_timeout": 30, "ready_timeout": 30,
    "auto_start": False, "stop_after": False,
}


def _manager(tmp_path: Path, fake: _FakeManager) -> BenchManager:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    store = JsonStore(tmp_path / "data" / "bench.json", [])
    profiles = JsonStore(tmp_path / "data" / "profiles.json", [])
    return BenchManager(store, profiles, fake)


def _run_dict(mgr: BenchManager, params: dict) -> dict:
    run = {"id": "b_text", "status": "running", "stages": [], "params": params,
           "auto_started": False, "stopped_after": False, "stop_error": "", "session_id": None,
           "progress": {"stage": 0, "stages": list(params["concurrency"]), "phase": "",
                        "done": 0, "total": params["num_prompts"], "ok": 0, "fail": 0,
                        "out_tokens": 0, "elapsed": 0}}
    mgr.store.mutate(lambda lst: lst.append(run))
    return run


# ------------------------------------------------- 1. テキストと finish_reason


def test_stream_captures_text_and_finish_reason(server):
    srv, base = server
    r = _one_request(base, "/v1/completions", {"prompt": "x", "stream": True},
                     True, 15.0, text_cap=10000)
    assert r["ok"], r
    assert r["finish_reason"] == "length"
    # 先頭チャンク (w0) が失われていないこと — ヘッダ読み込みが最初の SSE イベントを
    # バッファに引き込む高速サーバーでも全文が取れる
    assert r["tx"] == "w0 w1 w2 w3 w4 w5 ", r["tx"]
    assert "tx2" not in r              # 上限内なので中略なし
    assert r["txn"] == len(r["tx"])


def test_non_stream_captures_text_and_finish_reason(server):
    srv, base = server
    _Handler.mode, _Handler.finish_reason = "json", "stop"
    r = _one_request(base, "/v1/chat/completions",
                     {"messages": [{"role": "user", "content": "x"}], "stream": False},
                     False, 15.0, text_cap=10000)
    assert r["ok"], r
    assert r["tx"] == "hello there"
    assert r["finish_reason"] == "stop"
    assert r["txn"] == len("hello there")


# ------------------------------------------------------- 2. 切り詰め (先頭+末尾)


def test_long_output_keeps_head_and_tail_within_cap(server):
    srv, base = server
    _Handler.pieces = 300              # "w0 ... w299 " ≈ 2KB
    r = _one_request(base, "/v1/completions", {"prompt": "x", "stream": True},
                     True, 15.0, text_cap=400)
    assert r["ok"]
    assert r["txn"] > 400              # 全文字数は正直に残る
    assert len(r["tx"]) == 200 and len(r["tx2"]) == 200
    assert r["tx"].startswith("w0 w1")
    assert r["tx2"].rstrip().endswith("w299")
    assert r["finish_reason"] == "length"


def test_text_fields_cap_zero_saves_nothing():
    assert bench_mod._text_fields("hello", 0) == {}
    assert bench_mod._text_fields("", 100) == {"txn": 0, "tx": ""}


# ------------------------------------------------------------- 3. warmup は非保存


def test_warmup_does_not_capture_text(server):
    srv, base = server
    r = _one_request(base, "/v1/completions", {"prompt": "x", "stream": True}, True, 15.0)
    assert r["ok"]
    assert "tx" not in r and "txn" not in r
    assert r["finish_reason"] == "length"   # finish_reason は常に返す


# --------------------------------------------------- 4. 中断時の途中生成の保存


def test_cancelled_stream_keeps_partial_text_in_live_slot(server):
    srv, base = server
    _Handler.mode, _Handler.pieces, _Handler.delay = "stream_slow", 1000, 0.02
    stop = threading.Event()
    slot: dict = {"f": 0.0}

    def cancel_soon():
        time.sleep(0.4)
        stop.set()
    threading.Thread(target=cancel_soon, daemon=True).start()
    r = _one_request(base, "/v1/completions", {"prompt": "x", "stream": True},
                     True, 15.0, stop=stop, live=slot, text_cap=4000)
    assert r.get("cancelled") is True
    # 打ち切り時点までの生成がスロット (= 結果の reqs) に残る
    assert slot.get("tx", "").startswith("w0 w1"), slot.get("tx")
    assert slot.get("txn", 0) > 0


# ------------------------------------------------------ 5. _stage への反映 (reqs)


def test_stage_reqs_carry_text_and_finish_reason(tmp_path, server):
    srv, base = server
    fake = _FakeManager(srv.server_address[1])
    fake._session("s1")
    mgr = _manager(tmp_path, fake)
    params = {**PARAMS, "warmup": 1}
    run = _run_dict(mgr, params)
    stage = asyncio.run(mgr._stage(run, 0, base, "/v1/completions", "m", params, 1, 7))
    assert stage["ok"] == 2
    reqs = stage["reqs"]
    assert len(reqs) == 2              # warmup のスロットは混ざらない
    for q in reqs:
        assert q.get("tx") == "w0 w1 w2 w3 w4 w5 ", q
        assert q.get("fr") == "length"
        assert q.get("txn") == len(q["tx"])
        assert "tx2" not in q


def test_stage_truncates_text_when_many_requests(tmp_path, server, monkeypatch):
    srv, base = server
    _Handler.pieces = 60               # 1 本 ≈ 330 文字
    monkeypatch.setattr(bench_mod, "TEXT_REQ_MAX", 100)   # 下限 200 文字に縮む
    fake = _FakeManager(srv.server_address[1])
    fake._session("s1")
    mgr = _manager(tmp_path, fake)
    params = {**PARAMS, "warmup": 0}
    run = _run_dict(mgr, params)
    stage = asyncio.run(mgr._stage(run, 0, base, "/v1/completions", "m", params, 1, 7))
    assert stage["ok"] == 2
    for q in stage["reqs"]:
        assert q["txn"] > 200
        assert len(q["tx"]) + len(q["tx2"]) <= 200
        assert q["tx"].startswith("w0")


# ------------------------------------------------------------- 6. フロント描画


def test_frontend_renders_decoded_text_and_finish_reason():
    html = _frontend_src()
    assert "bmTextBlock" in html
    assert "デコード出力" in html
    # finish_reason の判断ラベル
    assert "自然停止" in html and "長さ上限" in html
    # reqs の tx/tx2/txn/fr を読む
    assert re.search(r"r\.tx\b", html)
    assert re.search(r"r\.tx2\b", html)
    assert re.search(r"r\.txn\b", html)
    assert re.search(r"r\.fr\b", html)
    # 結果詳細とライブカードの両方に配線されている
    assert re.search(r"bmTextBlock\(`デコード出力 \(\$\{st\.concurrency\}", html)
    assert "デコード出力 (ライブ)" in html
    # 古い履歴 (tx なし) はクラッシュせず注記を出す
    assert "デコード出力の記録がありません" in html
    # EOS 無視の注意書き (フォームと結果の両方)
    assert "bmEosNote" in html
    assert "EOS 無視 ON" in html
    # テキストは esc して XSS を防いでいる (bmTextBlock 内の esc(r.tx))
    assert re.search(r"esc\(r\.tx", html)
