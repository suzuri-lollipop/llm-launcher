"""デコード速度グラフの /api/sessions/{sid}/parallel 中継と GPU VRAM 割り当ての
/api/sessions/{sid}/meminfo 中継の契約。

ダッシュボードはブラウザから各サーバーの /metrics を直接叩けない (CORS / API
キー / ポート違い) ため、launcher が起動中ポートへ中継し、グラフに必要な
スロット内訳と 60 秒合算履歴だけを正規化して返す。

Strata / Maya / Basalt は JSON /metrics、vLLM / SGLang / llama.cpp は Prometheus
テキスト (+ llama.cpp は /slots)。FreeToken 系は新しいエンジンが basalt 形の
JSON /metrics (+ /slots) を出すので同じ経路に合流し、live の無い旧エンジンは
/v1/stats にフォールバックする。
"""
from __future__ import annotations

import json
import asyncio
import urllib.error
from pathlib import Path
from types import SimpleNamespace
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


# ---------------------------------------------------------------- 他バックエンド
# vLLM / SGLang (Prometheus) ・ llama.cpp (/slots + Prometheus) ・ FreeToken (/v1/stats)

def _run_session(tmp_path: Path, backend: str, backend_name: str, sid: str = "s_new") -> None:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    runs = [{"id": sid, "profile_id": "p_new", "profile_name": backend_name,
             "backend": backend, "backend_name": backend_name, "port": 8124,
             "health_path": "/health", "ready": True, "status": "running",
             "pid": 1, "argv": []}]
    (data / "runs.json").write_text(json.dumps(runs), "utf-8")
    (data / "profiles.json").write_text("[]", "utf-8")


def _fake_clock(monkeypatch, app_mod, clock: list[float]) -> None:
    """app.py の time 参照だけを差し替える (asyncio のループ時計は壊さない)。"""
    monkeypatch.setattr(app_mod, "time",
                        SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1793940000.0))


def test_parallel_freetoken_stats(tmp_path: Path, monkeypatch):
    """live ブロックの無い旧 FreeToken エンジン: /metrics を偵察しても live が無いので
    /v1/stats のスライディング窓 (decode_tps) にフォールバックする。リクエスト毎の
    内訳は無いので slots は空 (フロントは合算グラフだけ描く)。"""
    _run_session(tmp_path, "freetoken-fork", "FreeToken (fork)")
    stats = {"model": {"id": "qwen3.8-flash", "ctx": 262144},
             "throughput": {"decode_tps": 61.3, "prefill_tps": 900.0},
             "requests": {"active": 2, "completed": 10}}

    def fake_urlopen(req, timeout=2.0):
        if req.full_url.endswith("/metrics"):
            # 旧エンジン: bench 用の phase rows だけで live は無い
            return FakeResponse(json.dumps({"kind": "freetoken", "requests": []}).encode("utf-8"))
        assert req.full_url.endswith("/v1/stats")
        return FakeResponse(json.dumps(stats).encode("utf-8"))

    import llm_launcher.app as app_mod
    monkeypatch.setattr(app_mod.urllib.request, "urlopen", fake_urlopen)
    app = create_app(tmp_path)
    status, body = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_new/parallel"))
    assert status == 200
    assert body["backend"] == "freetoken-fork"
    assert body["live"]["tok_s"] == 61.3 and body["live"]["state"] == "generating"
    assert body["live"]["active"] == 2 and body["live"]["prefill_tok_s_mean"] == 900.0
    assert body["server"]["model"] == "qwen3.8-flash"
    assert body["server"]["max_context"] == 262144
    assert body["slots"] == []
    assert body["history"]["tok_s"] == [61.3]


def test_parallel_freetoken_metrics_unavailable_falls_back(tmp_path: Path, monkeypatch):
    """/metrics が無い (404) 旧エンジンでも偵察は黙って /v1/stats に落とす。"""
    _run_session(tmp_path, "freetoken", "FreeToken")
    stats = {"model": {"id": "m", "ctx": 8192},
             "throughput": {"decode_tps": 12.0, "prefill_tps": 0.0},
             "requests": {"active": 1}}

    def fake_urlopen(req, timeout=2.0):
        if req.full_url.endswith("/metrics"):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", None, None)
        assert req.full_url.endswith("/v1/stats")
        return FakeResponse(json.dumps(stats).encode("utf-8"))

    import llm_launcher.app as app_mod
    monkeypatch.setattr(app_mod.urllib.request, "urlopen", fake_urlopen)
    app = create_app(tmp_path)
    status, body = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_new/parallel"))
    assert status == 200
    assert body["live"]["tok_s"] == 12.0 and body["slots"] == []


def test_parallel_freetoken_slot_metrics(tmp_path: Path, monkeypatch):
    """新しい FreeToken エンジンは basalt 形の /metrics (live.slots が dict) + /slots 行を
    出すので、Strata 系と同じ経路でスロット内訳を描く。bench 用の requests ブロックが
    同じ doc に同居していても中継は live / history / slots だけを読む。"""
    _run_session(tmp_path, "freetoken-fork", "FreeToken (fork)")
    metrics = {
        "kind": "freetoken",
        "requests": [{"time": 1793940000.0, "prompt_ms": 120.0}],   # bench 用 (中継は読まない)
        "engine": {"model": "qwen3.8-flash", "model_label": "qwen3.8-flash", "max_context": 262144},
        "live": {"state": "generating", "tok_s": 41.2, "prefill_tok_s_mean": 900.0,
                 "parallel": 4, "running": 1, "active": 1, "queued": 0,
                 "slots": {"total": 4, "active": 1, "queue_depth": 0}},
        "history": {"tok_s": [41.2] * 60,
                    "slot_tok_s": [[41.2] * 60, [0.0] * 60, [0.0] * 60, [0.0] * 60]},
        "time": 1793940000.0,
    }
    slot_rows = [
        {"id": 0, "n_ctx": 262144, "is_processing": True, "state": "decoding",
         "tok_s": 41.2, "generated": 90, "prompt_tokens": 120, "prefill_done": 120,
         "ctx_used": 210, "reused": 40, "ttft_s": 0.31, "prefill_tok_s": 258.0,
         "drafts_offered": 80, "drafts_accepted": 40},
        {"id": 1, "n_ctx": 262144, "is_processing": False, "state": "idle",
         "tok_s": 0.0, "generated": 0, "prompt_tokens": 0, "prefill_done": 0, "ctx_used": 0},
        {"id": 2, "n_ctx": 262144, "is_processing": False, "state": "idle",
         "tok_s": 0.0, "generated": 0, "prompt_tokens": 0, "prefill_done": 0, "ctx_used": 0},
        {"id": 3, "n_ctx": 262144, "is_processing": False, "state": "idle",
         "tok_s": 0.0, "generated": 0, "prompt_tokens": 0, "prefill_done": 0, "ctx_used": 0},
    ]

    def fake_urlopen(req, timeout=2.0):
        url = req.full_url
        body = slot_rows if url.endswith("/slots") else metrics
        return FakeResponse(json.dumps(body).encode("utf-8"))

    import llm_launcher.app as app_mod
    monkeypatch.setattr(app_mod.urllib.request, "urlopen", fake_urlopen)
    app = create_app(tmp_path)
    status, body = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_new/parallel"))
    assert status == 200
    assert body["backend"] == "freetoken-fork"
    assert body["fetched_at"] == 1793940000.0
    assert body["server"]["model"] == "qwen3.8-flash"
    assert body["server"]["max_context"] == 262144
    assert body["live"]["parallel"] == 4 and body["live"]["running"] == 1
    assert body["live"]["tok_s"] == 41.2
    assert len(body["slots"]) == 4
    s0 = body["slots"][0]
    assert s0["state"] == "decoding" and s0["tok_s"] == 41.2
    assert s0["prompt_read"] == 120 and s0["held_tokens"] == 210
    assert s0["accept"] == 50 and s0["n_ctx"] == 262144
    assert body["slots"][1]["state"] == "idle" and body["slots"][1]["accept"] is None
    hist = body["history"]["slot_tok_s"]
    assert len(hist) == 4 and len(hist[0]) == 60 and hist[0][-1] == 41.2


def test_parallel_freetoken_prefill_row(tmp_path: Path, monkeypatch):
    """プレフィル中の行: state=prefill (フロントは「読み込み」)、prefill_done=None は
    進捗不明としてそのまま渡る (捏造した進捗を描かない)。"""
    _run_session(tmp_path, "freetoken-fork", "FreeToken (fork)")
    metrics = {
        "engine": {"model": "m", "max_context": 8192},
        "live": {"state": "reading", "tok_s": None,
                 "slots": {"total": 2, "active": 1, "queue_depth": 1}},
        "history": {"tok_s": [], "slot_tok_s": []},
        "time": 1793940000.0,
    }
    slot_rows = [
        {"id": 0, "n_ctx": 8192, "is_processing": True, "state": "prefill",
         "tok_s": 0.0, "generated": 0, "prompt_tokens": 4000, "prefill_done": None,
         "ctx_used": 4000, "reused": 0},
        {"id": 1, "n_ctx": 8192, "is_processing": False, "state": "idle",
         "tok_s": 0.0, "generated": 0, "prompt_tokens": 0, "prefill_done": 0, "ctx_used": 0},
    ]

    def fake_urlopen(req, timeout=2.0):
        body = slot_rows if req.full_url.endswith("/slots") else metrics
        return FakeResponse(json.dumps(body).encode("utf-8"))

    import llm_launcher.app as app_mod
    monkeypatch.setattr(app_mod.urllib.request, "urlopen", fake_urlopen)
    app = create_app(tmp_path)
    status, body = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_new/parallel"))
    assert status == 200
    assert body["live"]["state"] == "reading" and body["live"]["queued"] == 1
    s0 = body["slots"][0]
    assert s0["state"] == "prefill" and s0["prompt_read"] is None
    assert s0["prompt_tokens"] == 4000 and s0["tok_s"] == 0.0


# ---------------------------------------------------------------- GPU VRAM 割り当て中継

def test_meminfo_relay_normalizes_per_gpu(tmp_path: Path, monkeypatch):
    """/api/sessions/{sid}/meminfo: エンジンの /v1/meminfo (TP ランク合算) を GPU 単位の
    parts に分解して返す。空きは device_total − 確保合計の導出。UUID は nvidia-smi の
    カードとの突き合わせキーとしてそのまま渡る。"""
    _run_session(tmp_path, "freetoken-fork", "FreeToken (fork)")
    gib = 1024**3
    meminfo = {
        "model": "qwen3.8-flash", "tp_size": 2,
        "totals": {"device_total_bytes": 48 * gib, "weights_bytes": 8 * gib},
        "pools": [{"key": "kv", "bytes": 2 * gib}],
        "ranks": [
            {"rank": 0, "gpus": [{"index": 0, "name": "RTX-0", "uuid": "GPU-aaa",
                                  "total_bytes": 24 * gib}],
             "device_total_bytes": 24 * gib, "weights_bytes": 4 * gib,
             "nonpool_overhead_bytes": 1 * gib, "residual_bytes": 2 * gib,
             "live_bytes": 12 * gib,
             "pools": {"kv": 1 * gib, "swa": 0, "moe": 3 * gib, "mamba": 0}},
            {"rank": 1, "gpus": [{"index": 1, "name": "RTX-1", "uuid": "GPU-bbb",
                                  "total_bytes": 24 * gib}],
             "device_total_bytes": 24 * gib, "weights_bytes": 4 * gib,
             "nonpool_overhead_bytes": 1 * gib, "residual_bytes": 0,
             "live_bytes": None,
             "pools": {"kv": 1 * gib, "moe": 3 * gib}},
        ],
    }

    def fake_urlopen(req, timeout=2.0):
        assert req.full_url.endswith("/v1/meminfo")
        return FakeResponse(json.dumps(meminfo).encode("utf-8"))

    import llm_launcher.app as app_mod
    monkeypatch.setattr(app_mod.urllib.request, "urlopen", fake_urlopen)
    app = create_app(tmp_path)
    status, body = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_new/meminfo"))
    assert status == 200
    assert body["model"] == "qwen3.8-flash" and body["tp_size"] == 2
    assert len(body["gpus"]) == 2
    g0, g1 = body["gpus"]
    assert g0["uuid"] == "GPU-aaa" and g0["index"] == 0
    p0 = g0["parts"]
    assert p0["weights"] == 4 * gib and p0["kv"] == 1 * gib and p0["moe"] == 3 * gib
    assert p0["overhead"] == 1 * gib and p0["residual"] == 2 * gib
    # 空き = 24 − (4+1+3+1+2) = 13 GiB (存在しないプールは 0 で埋まらない)
    assert p0["free"] == 13 * gib and p0["swa"] == 0 and p0["mamba"] == 0
    assert sum(p0.values()) == g0["total_bytes"]
    assert g1["parts"]["free"] == 15 * gib and g1["live_bytes"] is None
    assert rbac.required_cap("GET", "/api/sessions/s_new/meminfo") == "dashboard"

    # 非対応バックエンドは 400 (グラフ中継と同じ開示範囲の判定)
    _run_session(tmp_path, "vllm", "vLLM", sid="s_vllm")
    app2 = create_app(tmp_path)
    status, _ = asyncio.run(asgi_call(app2, "GET", "/api/sessions/s_vllm/meminfo"))
    assert status == 400


def test_meminfo_normalize_clamps_over_allocation():
    """確保合計が device_total を超えても空きは負にならない (残余の実測 overshoot 等)。"""
    from llm_launcher.parallel_metrics import normalize_meminfo
    doc = {"ranks": [{"rank": 0, "gpus": [], "device_total_bytes": 100,
                      "weights_bytes": 80, "nonpool_overhead_bytes": 30,
                      "residual_bytes": 10, "pools": {"kv": 20}}]}
    g = normalize_meminfo(doc)["gpus"][0]
    assert g["parts"]["free"] == 0
    assert g["uuid"] is None and g["index"] is None


def test_parallel_vllm_prometheus_counter_rate(tmp_path: Path, monkeypatch):
    """vLLM は generation_tokens_total (counter) のポーリング差分が tok/s。
    初回は差分なし (None)、2 点目から速度になる。モデル / 最大コンテキストは
    /v1/models を初回だけ引く。"""
    _run_session(tmp_path, "vllm", "vLLM")
    clock = [100.0]
    counters = {"gen": 1000.0, "prompt": 5000.0}

    def fake_urlopen(req, timeout=2.0):
        if req.full_url.endswith("/v1/models"):
            return FakeResponse(json.dumps(
                {"data": [{"id": "qwen3", "max_model_len": 32768}]}).encode("utf-8"))
        text = (
            "# HELP vllm:generation_tokens_total Number of generation tokens processed.\n"
            "# TYPE vllm:generation_tokens_total counter\n"
            f'vllm:generation_tokens_total{{model_name="qwen3"}} {counters["gen"]}\n'
            f'vllm:prompt_tokens_total{{model_name="qwen3"}} {counters["prompt"]}\n'
            'vllm:num_requests_running{model_name="qwen3"} 2\n'
            'vllm:num_requests_waiting{model_name="qwen3"} 1\n')
        return FakeResponse(text.encode("utf-8"))

    import llm_launcher.app as app_mod
    monkeypatch.setattr(app_mod.urllib.request, "urlopen", fake_urlopen)
    _fake_clock(monkeypatch, app_mod, clock)
    app = create_app(tmp_path)
    status, body = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_new/parallel"))
    assert status == 200
    assert body["live"]["tok_s"] is None            # 初回は差分が取れない
    assert body["live"]["running"] == 2 and body["live"]["queued"] == 1
    assert body["live"]["state"] == "generating"
    assert body["server"]["model"] == "qwen3" and body["server"]["max_context"] == 32768

    clock[0] += 2.0
    counters["gen"] += 84.0                          # 84 tok / 2 s = 42 tok/s
    counters["prompt"] += 200.0
    status, body = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_new/parallel"))
    assert status == 200
    assert body["live"]["tok_s"] == 42.0
    assert body["live"]["prefill_tok_s_mean"] == 100.0
    assert body["history"]["tok_s"] == [None, 42.0]


def test_parallel_sglang_gauge(tmp_path: Path, monkeypatch):
    """SGLang は gen_throughput gauge がそのまま tok/s (--enable-metrics 前提)。"""
    _run_session(tmp_path, "sglang", "SGLang")

    def fake_urlopen(req, timeout=2.0):
        if req.full_url.endswith("/v1/models"):
            return FakeResponse(json.dumps({"data": [{"id": "qwen3"}]}).encode("utf-8"))
        text = ('sglang:gen_throughput{model_name="qwen3"} 55.5\n'
                'sglang:num_running_reqs{model_name="qwen3"} 3\n'
                'sglang:num_queue_reqs{model_name="qwen3"} 2\n')
        return FakeResponse(text.encode("utf-8"))

    import llm_launcher.app as app_mod
    monkeypatch.setattr(app_mod.urllib.request, "urlopen", fake_urlopen)
    app = create_app(tmp_path)
    status, body = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_new/parallel"))
    assert status == 200
    assert body["live"]["tok_s"] == 55.5            # gauge は初回から値が出る
    assert body["live"]["running"] == 3 and body["live"]["queued"] == 2
    assert body["server"]["model"] == "qwen3"
    assert body["slots"] == []


def test_parallel_llamacpp_slots_and_metrics(tmp_path: Path, monkeypatch):
    """llama.cpp は /slots の n_decoded 差分でスロット別 tok/s、合算は
    tokens_predicted_total の差分。プロンプト読み込み中は reading 表示。"""
    _run_session(tmp_path, "llamacpp", "llama.cpp")
    clock = [100.0]
    state = {"predicted": 1000.0, "slots": [
        {"id": 0, "n_ctx": 8192, "is_processing": True, "id_task": 7,
         "n_prompt_tokens": 100, "n_prompt_tokens_processed": 40,
         "next_token": [{"has_next_token": True, "n_decoded": 0}]},
        {"id": 1, "n_ctx": 8192, "is_processing": False},
    ]}

    def fake_urlopen(req, timeout=2.0):
        url = req.full_url
        if url.endswith("/slots"):
            return FakeResponse(json.dumps(state["slots"]).encode("utf-8"))
        if url.endswith("/props"):
            return FakeResponse(json.dumps(
                {"model_alias": "qwen3-gguf",
                 "default_generation_settings": {"n_ctx": 8192}}).encode("utf-8"))
        text = (f'llamacpp:tokens_predicted_total {state["predicted"]}\n'
                'llamacpp:requests_processing 1\n'
                'llamacpp:requests_deferred 0\n')
        return FakeResponse(text.encode("utf-8"))

    import llm_launcher.app as app_mod
    monkeypatch.setattr(app_mod.urllib.request, "urlopen", fake_urlopen)
    _fake_clock(monkeypatch, app_mod, clock)
    app = create_app(tmp_path)
    status, body = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_new/parallel"))
    assert status == 200
    s0, s1 = body["slots"]
    assert s0["state"] == "reading" and s0["tok_s"] == 0.0   # まだ差分なし
    assert s1["state"] == "idle"
    assert body["live"]["parallel"] == 2
    assert body["server"]["model"] == "qwen3-gguf"
    assert body["server"]["max_context"] == 8192

    clock[0] += 1.0
    state["predicted"] += 20.0
    state["slots"][0]["n_prompt_tokens_processed"] = 100
    state["slots"][0]["next_token"][0]["n_decoded"] = 20
    status, body = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_new/parallel"))
    assert status == 200
    s0, s1 = body["slots"]
    assert s0["state"] == "generating" and s0["tok_s"] == 20.0
    assert s0["generated"] == 20 and s0["n_ctx"] == 8192
    assert s1["tok_s"] == 0.0
    assert body["live"]["tok_s"] == 20.0
    assert body["history"]["tok_s"][-1] == 20.0


def test_parallel_llamacpp_metrics_only(tmp_path: Path, monkeypatch):
    """--slots なし (501) でも --metrics の gauge で合算グラフは描ける。"""
    _run_session(tmp_path, "llamacpp", "llama.cpp")

    def fake_urlopen(req, timeout=2.0):
        url = req.full_url
        if url.endswith("/slots"):
            raise urllib.error.HTTPError(url, 501, "Not Implemented", None, None)
        if url.endswith("/props"):
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        text = ('llamacpp:predicted_tokens_seconds 33.3\n'
                'llamacpp:requests_processing 1\n')
        return FakeResponse(text.encode("utf-8"))

    import llm_launcher.app as app_mod
    monkeypatch.setattr(app_mod.urllib.request, "urlopen", fake_urlopen)
    app = create_app(tmp_path)
    status, body = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_new/parallel"))
    assert status == 200
    assert body["slots"] == []
    assert body["live"]["tok_s"] == 33.3 and body["live"]["running"] == 1


def test_prometheus_parse_and_counter_reset():
    """系列はラベルを畳んで合計する。counter の巻き戻し (サーバー再起動) は
    その 1 点を None にして、次の差分から再開する。"""
    from llm_launcher.parallel_metrics import RateTracker, parse_prometheus
    sums, labels = parse_prometheus(
        '# HELP vllm:generation_tokens_total x\n'
        'vllm:generation_tokens_total{model_name="m",engine="0"} 10\n'
        'vllm:generation_tokens_total{model_name="m",engine="1"} 5\n'
        'vllm:nan_metric NaN\n')
    assert sums == {"vllm:generation_tokens_total": 15.0}
    assert labels["model_name"] == "m"
    tr = RateTracker()
    assert tr.counter_rate("c", 100.0, 10.0) is None
    assert tr.counter_rate("c", 110.0, 12.0) == 5.0
    assert tr.counter_rate("c", 1.0, 14.0) is None      # 再起動で巻き戻り
    assert tr.counter_rate("c", 3.0, 15.0) == 2.0
    assert tr.slot_rate("task-1", 10, 20.0) is None
    assert tr.slot_rate("task-1", 30, 22.0) == 10.0
    tr.prune_slots(set())
    assert tr.slot_rate("task-1", 5, 24.0) is None      # 破棄済みはまた初回扱い
