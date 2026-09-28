"""Tests for the bench stall metric ("how many seconds did generation freeze").

_one_request must report, for streaming requests, the worst inter-chunk gap
after the first token (gap_max) and the number of gaps >= STALL_S (stalls).
TTFT (submit -> first token) is deliberately NOT part of gap_max. The stage
table in the frontend must render the new columns and tolerate old runs that
predate the metric.
"""
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from llm_launcher.bench import STALL_S, _one_request

STATIC = Path(__file__).resolve().parents[1] / "src" / "llm_launcher" / "static" / "index.html"


class _SSEHandler(BaseHTTPRequestHandler):
    # schedule per case name (matched from the prompt): list of (delay_before, text)
    CASES = {
        "stalled": [(0.0, "a"), (0.1, "b"), (0.9, "c"), (0.2, "d")],
        "smooth": [(0.0, "a"), (0.05, "b"), (0.05, "c")],
        "single": [(0.0, "only")],
    }

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        case = next((k for k in self.CASES if k in body.get("prompt", "")), "smooth")
        if not body.get("stream"):
            out = json.dumps({"choices": [{"text": "hi"}],
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
        n = 0
        for delay, piece in self.CASES[case]:
            time.sleep(delay)
            n += 1
            evt = {"choices": [{"delta": {"content": piece}}]}
            self.wfile.write(b"data: " + json.dumps(evt).encode() + b"\n\n")
            self.wfile.flush()
        final = {"choices": [], "usage": {"completion_tokens": n, "prompt_tokens": 5}}
        self.wfile.write(b"data: " + json.dumps(final).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *args):  # silence
        pass


def _server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_stalled_stream_reports_worst_gap_and_stall_count():
    srv, base = _server()
    try:
        r = _one_request(base, "/v1/completions",
                         {"prompt": "stalled case", "stream": True}, True, 15.0)
    finally:
        srv.shutdown()
    assert r["ok"], r
    # gaps: 0.1 (a->b), 0.9 (b->c, the stall), 0.2 (c->d)
    assert r["gap_max"] is not None
    assert 0.85 <= r["gap_max"] <= 1.3, r["gap_max"]
    assert r["stalls"] == 1
    assert r["ttft"] is not None and r["ttft"] < r["gap_max"]  # TTFT is not a stall
    assert r["out_tokens"] == 4


def test_smooth_stream_has_no_stalls():
    srv, base = _server()
    try:
        r = _one_request(base, "/v1/completions",
                         {"prompt": "smooth case", "stream": True}, True, 15.0)
    finally:
        srv.shutdown()
    assert r["ok"]
    assert r["gap_max"] is not None and r["gap_max"] < STALL_S
    assert r["stalls"] == 0


def test_single_piece_stream_has_no_gap():
    srv, base = _server()
    try:
        r = _one_request(base, "/v1/completions",
                         {"prompt": "single case", "stream": True}, True, 15.0)
    finally:
        srv.shutdown()
    assert r["ok"]
    assert r["gap_max"] is None  # no inter-chunk interval exists
    assert r["stalls"] == 0


def test_non_stream_reports_null_gap():
    srv, base = _server()
    try:
        r = _one_request(base, "/v1/completions",
                         {"prompt": "smooth case", "stream": False}, False, 15.0)
    finally:
        srv.shutdown()
    assert r["ok"]
    assert r["gap_max"] is None
    assert r["stalls"] == 0


def test_frontend_renders_stall_columns():
    html = STATIC.read_text(encoding="utf-8")
    assert "最大停止" in html
    assert "停止数" in html
    assert "bmStallCell" in html
    assert re.search(r"st\.stall_worst_s", html)
    assert re.search(r"st\.stall_threshold_s", html)
    # old runs without the metric must render "-", not crash
    assert re.search(r"w == null", html)
