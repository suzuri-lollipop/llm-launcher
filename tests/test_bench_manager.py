"""組み込みベンチマーク (bench.BenchManager) の内部契約の回帰テスト。

test_bench_stall_metric.py が「1 リクエストの計測 (_one_request)」と表の描画だけを
見ているのに対し、ここではランナー本体の不備になりやすかった境界を押さえる:

  1. 中断: インフライトのストリームが stop で打ち切られ、成功/失敗のどちらにも
     数えられない (aborted) / 0 件でも「失敗」にしない
  2. warmup は測定本体と別のプロンプト列 (1 件目だけ prefix cache で有利にならない)
  3. total tok/s は入力トークンが全件揃ったときだけ算出 (部分欠落で過小評価しない)
  4. 非ストリームは TTFT / decode tok/s を測れない (- になる)
  5. ベンチとキャリブレーションの実走排他 (同じ GPU を 2 本で使わない)
  6. 削除・履歴整理した run は _save でディスクへ復活しない / _last_save が溜まらない
  7. on_startup は lost にし、自動起動したままのサーバーを警告として残す
  8. stop_after の停止失敗を握り潰さず stop_error に残す
  9. /api/benchmarks の並列度正規化 (先頭 8 件で切り捨てない) と num_prompts の下限
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
from llm_launcher import calibrate as calibrate_mod
from llm_launcher.bench import BenchError, BenchManager
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
    """mode で振る舞いを変える OpenAI 互換の偽サーバー。

    - "stream": SSE。pieces 個の content を送り、usage を最後に付ける
    - "stream_slow": SSE だが 1 ピース delay 秒待つ (中断テスト用)
    - "json": 非ストリーム (JSON 1 発)
    - "json_nousage": 非ストリームで usage 無し (推定へフォールバック)
    """
    mode = "stream"
    pieces = 4
    delay = 0.0
    saw: list[str] = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = body.get("prompt") or body.get("messages", [{}])[0].get("content", "")
        self.saw.append(prompt)
        if self.mode in ("json", "json_nousage"):
            out: dict = {"choices": [{"text": "hello there"}],
                         "usage": {"completion_tokens": 2, "prompt_tokens": 3}}
            if self.mode == "json_nousage":
                out.pop("usage")
            raw = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for i in range(self.pieces):
                if self.mode == "stream_slow":
                    # 0.1 秒ずつ待つ: クライアントが切ったら次の書き込みで
                    # BrokenPipe になり、20 秒ぶん GIL を握り続けない
                    waited = 0.0
                    while waited < self.delay:
                        time.sleep(min(0.02, self.delay - waited))
                        waited += 0.02
                evt = {"choices": [{"delta": {"content": f"tok{i} "}}]}
                self.wfile.write(b"data: " + json.dumps(evt).encode() + b"\n\n")
                self.wfile.flush()
            final = {"choices": [],
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
    _Handler.mode, _Handler.pieces, _Handler.delay = "stream", 4, 0.0
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _slow(n_pieces: int = 30, delay: float = 0.1) -> None:
    """1 リクエスト ~ n_pieces*delay 秒かかる大きなストリーム (中断テスト用)。"""
    _Handler.mode, _Handler.pieces, _Handler.delay = "stream_slow", n_pieces, delay


# ------------------------------------------------------------- manager の雛形


class _FakeManager:
    """ProcessManager の代わり (実プロセスは起動しない)。"""

    def __init__(self, port: int):
        self.sessions: dict[str, dict] = {}
        self.stopped: list[str] = []
        self.stop_raises = False
        self.profiles: list[dict] = []
        self.port = port

    def _session(self, sid: str, **kw) -> dict:
        s = {"id": sid, "profile_id": "p1", "status": "running", "port": self.port,
             "health_path": "/health", "argv": [], **kw}
        self.sessions[sid] = s
        return s

    async def start(self, profile, *, force=False, actor=None, via=""):
        return self._session("s_new", argv=[])

    async def stop(self, sid):
        if self.stop_raises:
            raise RuntimeError("stop failed")
        self.stopped.append(sid)
        if sid in self.sessions:
            self.sessions[sid]["status"] = "exited"


def _manager(tmp_path: Path, fake: _FakeManager) -> BenchManager:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    store = JsonStore(tmp_path / "data" / "bench.json", [])
    profiles = JsonStore(tmp_path / "data" / "profiles.json", [])
    return BenchManager(store, profiles, fake)


PARAMS = {
    "model": "m", "endpoint": "completions", "stream": True,
    "concurrency": [1],
    "num_prompts": 4, "input_len": 16, "output_len": 8, "temperature": 0.0,
    "warmup": 1, "request_timeout": 30, "ready_timeout": 30,
    "auto_start": False, "stop_after": False,
}


def _run_dict(mgr: BenchManager, params: dict) -> dict:
    run = {"id": "b_test", "status": "running", "stages": [], "params": params,
           "auto_started": False, "stopped_after": False, "stop_error": "", "session_id": None,
           "progress": {"stage": 0, "stages": list(params["concurrency"]), "phase": "",
                        "done": 0, "total": params["num_prompts"], "ok": 0, "fail": 0,
                        "out_tokens": 0, "elapsed": 0}}
    mgr.store.mutate(lambda lst: lst.append(run))
    return run


# ---------------------------------------------------------------- 1. 中断


def test_cancel_mid_stream_aborts_without_counting_ok_or_fail(tmp_path, server):
    """中断は「完了済みの 0 件」と区別し、失敗扱いにも数えない。"""
    srv, base = server
    _Handler.mode, _Handler.pieces, _Handler.delay = "stream_slow", 1000, 0.02   # 1 件 ~20 秒
    fake = _FakeManager(srv.server_address[1])
    fake._session("s1")
    mgr = _manager(tmp_path, fake)
    params = {**PARAMS, "concurrency": [2], "warmup": 0}
    run = _run_dict(mgr, params)

    async def go():
        task = asyncio.create_task(mgr._stage(run, 0, base, "/v1/completions", "m", params, 2, 1))
        # 2 本ともサーバーに届いたことを確認してから中断する (固定 sleep だと、
        # GIL の取り合いで偽サーバーの応答が先に完了して ok=1 になることがある)
        for _ in range(200):
            if len(_Handler.saw) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(_Handler.saw) >= 2
        started = time.perf_counter()
        mgr._cancel.add(run["id"])
        try:
            stage = await asyncio.wait_for(task, timeout=bench_mod.CANCEL_JOIN_S + 10)
        except asyncio.TimeoutError:
            pytest.fail("中断後も _stage が戻らない (インフライトを打ち切れていない)")
        took = time.perf_counter() - started
        assert not task.cancelled()
        return stage, took

    stage, took = asyncio.run(go())
    assert stage["aborted"] is True
    assert stage["ok"] == 0 and stage["fail"] == 0
    # stop を見て SSE の行境界で抜ける (request_timeout=30s まで待たない)
    assert took < 5.0, took


def test_cancel_during_warmup_raises_cancelled(tmp_path, server):
    srv, base = server
    _slow(n_pieces=30, delay=0.1)
    mgr = _manager(tmp_path, _FakeManager(srv.server_address[1]))
    params = {**PARAMS, "concurrency": [1], "warmup": 1}
    run = _run_dict(mgr, params)
    mgr._cancel.add(run["id"])

    with pytest.raises(bench_mod._Cancelled):
        asyncio.run(asyncio.wait_for(
            mgr._stage(run, 0, base, "/v1/completions", "m", params, 1, 1), timeout=15.0))


# ------------------------------------------------------------ 2. warmup の種


def test_warmup_prompts_differ_from_measured_prompts(tmp_path, server):
    srv, base = server
    _Handler.pieces = 2
    mgr = _manager(tmp_path, _FakeManager(srv.server_address[1]))
    params = {**PARAMS, "concurrency": [1], "num_prompts": 2, "warmup": 1}
    run = _run_dict(mgr, params)
    stage = asyncio.run(mgr._stage(run, 0, base, "/v1/completions", "m", params, 1, 123))
    prompts = list(_Handler.saw)
    assert len(prompts) == 3
    warm_prompt, first, second = prompts
    # warmup が測定 1 件目と同じだと、そのリクエストだけ prefix cache で有利になる
    assert warm_prompt != first and warm_prompt != second
    assert stage["ok"] == 2


# --------------------------------------------- 3./4. トークン合計と非ストリーム


def test_total_tps_is_none_when_prompt_tokens_missing(tmp_path, server, monkeypatch):
    srv, base = server
    _Handler.mode = "stream"
    mgr = _manager(tmp_path, _FakeManager(srv.server_address[1]))

    real = bench_mod._one_request

    def no_input_usage(*a, **kw):
        r = real(*a, **kw)
        r["in_tokens"] = None           # 入力トークンを返さないサーバー
        return r

    monkeypatch.setattr(bench_mod, "_one_request", no_input_usage)
    params = {**PARAMS, "concurrency": [1], "num_prompts": 2, "warmup": 0}
    run = _run_dict(mgr, params)
    stage = asyncio.run(mgr._stage(run, 0, base, "/v1/completions", "m", params, 1, 7))
    assert stage["ok"] == 2
    assert stage["in_tokens"] is None
    assert stage["total_tps"] is None   # 0 で埋めた過小値を出さない
    assert stage["out_tps"] is not None


def test_non_stream_has_no_ttft_or_decode(tmp_path, server):
    srv, base = server
    _Handler.mode = "json"
    mgr = _manager(tmp_path, _FakeManager(srv.server_address[1]))
    params = {**PARAMS, "concurrency": [1], "num_prompts": 1, "warmup": 0, "stream": False}
    run = _run_dict(mgr, params)
    stage = asyncio.run(mgr._stage(run, 0, base, "/v1/completions", "m", params, 1, 7))
    assert stage["ok"] == 1
    assert stage["ttft"]["avg"] is None
    assert stage["decode_tps"]["avg"] is None
    assert stage["stall_worst_s"] is None
    assert stage["total_tps"] is not None       # 入力トークンは usage から取れる


# ------------------------------------------------------------ 5. 実走の排他


def test_bench_cannot_start_while_calibration_is_active(tmp_path):
    """実行中の実走は 1 本だけ (ベンチ ⇔ キャリブレーションの相互拒否)。"""
    profiles = JsonStore(tmp_path / "profiles.json",
                         [{"id": "p1", "name": "ft", "backend": "freetoken", "values": {}}])
    fake = _FakeManager(0)
    mgr = BenchManager(JsonStore(tmp_path / "bench.json", []), profiles, fake)
    cal = calibrate_mod.CalibrationManager(tmp_path, profiles,
                                          JsonStore(tmp_path / "cal.json", []), fake)
    cal.attach_exclusive(mgr.exclusive)
    assert cal.claim_exclusive()                  # キャリブレーションが GPU を掴んでいる

    async def go():
        with pytest.raises(BenchError, match="キャリブレーション"):
            await mgr.start(profiles.load()[0], dict(PARAMS), "FreeToken")
        assert mgr.active_id is None              # 拒否された run を残さない
        cal.release_exclusive()
        assert mgr.claim_exclusive("bench:p1")
        # 逆方向: ベンチ実行中はキャリブレーションを開始できない
        with pytest.raises(calibrate_mod.CalibrateError, match="ベンチマーク"):
            await cal.start(profiles.load()[0], "freetoken")
        mgr.release_exclusive()

    asyncio.run(go())


# ------------------------------------------------------- 6. 削除/整理と保存


def test_deleted_or_trimmed_run_is_not_resurrected(tmp_path):
    mgr = _manager(tmp_path, _FakeManager(0))
    mgr.store.mutate(lambda lst: lst.extend(
        {"id": f"b{i}", "created_at": i, "status": "completed", "stages": []} for i in range(3)))
    victim = mgr.get("b1")
    mgr._last_save["b1"] = 1.0
    mgr.delete("b1")
    assert mgr.get("b1") is None
    assert "b1" not in mgr._last_save
    # 削除済み run を掴んでいる task が _save してもディスクへ戻さない
    mgr._save(victim, force=True)
    assert mgr.get("b1") is None


def test_trim_marks_evicted_runs_dead_and_drops_save_stamps(tmp_path, monkeypatch):
    monkeypatch.setattr(bench_mod, "MAX_RUNS", 2)
    mgr = _manager(tmp_path, _FakeManager(0))
    mgr.store.mutate(lambda lst: lst.extend(
        {"id": f"b{i}", "created_at": i, "status": "completed"} for i in range(4)))
    mgr._last_save.update({f"b{i}": 1.0 for i in range(4)})
    mgr._trim()
    assert {r["id"] for r in mgr.list_runs()} == {"b2", "b3"}
    assert {"b0", "b1"} <= mgr._dead
    assert set(mgr._last_save) == {"b2", "b3"}     # 消えた分の記録が溜まらない


# ------------------------------------------------------------ 7. on_startup


def test_on_startup_marks_lost_and_warns_about_leftover_server(tmp_path):
    fake = _FakeManager(0)
    mgr = _manager(tmp_path, fake)
    fake._session("s_auto")
    mgr.store.mutate(lambda lst: lst.extend([
        {"id": "b_run", "created_at": 1, "status": "running", "auto_started": True,
         "session_id": "s_auto", "stopped_after": False},
        {"id": "b_done", "created_at": 2, "status": "completed"},
    ]))
    mgr.on_startup()
    lost = mgr.get("b_run")
    assert lost["status"] == "lost"
    assert lost["finished_at"] is not None
    assert lost.get("server_left_running") is True
    assert "自動起動したサーバーが残っています" in lost["error"]
    assert mgr.get("b_done")["status"] == "completed"


# --------------------------------------------------------- 8. stop_after


def test_stop_after_failure_is_recorded(tmp_path, monkeypatch):
    fake = _FakeManager(0)
    fake.stop_raises = True
    mgr = _manager(tmp_path, fake)
    params = {**PARAMS, "stop_after": True}

    async def fake_run(run, profile, p):
        run["auto_started"] = True
        run["session_id"] = "s_auto"

    monkeypatch.setattr(mgr, "_run", fake_run)
    run = _run_dict(mgr, params)
    assert mgr.claim_exclusive("bench:p1")
    asyncio.run(mgr._job(run, {"id": "p1"}, params))
    assert run["status"] == "completed"
    assert run["stopped_after"] is False
    assert "stop failed" in run["stop_error"]      # 握り潰さない
    assert mgr.exclusive_owner == ""               # 失敗しても排他は解放される


def test_failed_auto_started_run_stops_the_server(tmp_path, monkeypatch):
    """中断/失敗で終わった run も、自動起動したサーバーを置き去りにしない。"""
    fake = _FakeManager(0)
    fake._session("s_auto")
    mgr = _manager(tmp_path, fake)
    params = {**PARAMS, "stop_after": False}

    async def failing(run, profile, p):
        run["auto_started"] = True
        run["session_id"] = "s_auto"
        raise BenchError("boom")

    monkeypatch.setattr(mgr, "_run", failing)
    run = _run_dict(mgr, params)
    assert mgr.claim_exclusive("bench:p1")
    asyncio.run(mgr._job(run, {"id": "p1"}, params))
    assert run["status"] == "failed"
    assert "s_auto" in fake.stopped and run["stopped_after"] is True


# ----------------------------------------------------- 9. BenchBody の正規化


def test_bench_body_keeps_range_ends_when_too_many_stages():
    from llm_launcher.app import BenchBody

    body = BenchBody(profile_id="p1", concurrency=list(range(1, 33)), num_prompts=64)
    conc = body.concurrency
    assert len(conc) == bench_mod.MAX_STAGES
    assert conc[0] == 1 and conc[-1] == 32          # 先頭 8 件で切り捨てない
    assert conc == sorted(conc)


def test_bench_body_rejects_num_prompts_below_max_concurrency():
    from llm_launcher.app import BenchBody

    with pytest.raises(ValueError, match="num_prompts"):
        BenchBody(profile_id="p1", concurrency=[1, 16], num_prompts=4)


def test_frontend_legend_uses_recorded_threshold_and_explains_non_stream():
    html = _frontend_src()
    # 凡例は固定文字列ではなくステージに保存された閾値を使う
    assert re.search(r"const th = [\s\S]{0,200}stall_threshold_s", html)
    assert "TTFT / 最大停止 / decode tok/s は測れません" in html
    assert "合計 tok/s は算出していません" in html