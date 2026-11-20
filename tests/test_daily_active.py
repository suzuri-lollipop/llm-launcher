"""平均アクティブ数 (1 日窓) の契約: daily_active の蓄積 / 窓ロール / 永続化と、
/api/sessions/{sid}/parallel/daily (閲覧 / 手動リセット) ・
/api/parallel/daily/settings (リセット時刻) の中継。

窓はリセット時刻 (ローカル HH:MM、既定 00:00) 起点。1 分バケット [合計, 数] で
蓄積し、1日平均 = 観測できた分の平均の平均 (サンプルの無い分は含めない)。
プロファイル鍵なのでサーバー再起動 (セッション交代) を跨ぐ。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

from llm_launcher import daily_active as da
from llm_launcher import rbac
from llm_launcher.app import create_app

from test_parallel_metrics import FakeResponse, _run_session, asgi_call

# 固定基準時刻 (既存中継テストと同じ)。ローカル日付は実行 TZ で決まるが、
# 期待値も同じ localtime/mktime で組むので TZ に依存せず決定的。
BASE = 1793940000.0
_LT = time.localtime(BASE)


def at(h: int, m: int, day: int = 0) -> float:
    """基準日のローカル h:m (day ずらし可) の UNIX 秒。"""
    return time.mktime((_LT.tm_year, _LT.tm_mon, _LT.tm_mday + day, h, m, 0, 0, 0, -1))


def _fake_clock(monkeypatch, app_mod, clock: list[float]) -> None:
    monkeypatch.setattr(app_mod, "time",
                        SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0]))


# ---------------------------------------------------------------- 蓄積ユニット

def test_window_start_for_local_reset_time():
    now = at(10, 30)
    assert da.window_start_for("00:00", now) == at(0, 0)
    assert da.window_start_for("09:00", now) == at(9, 0)
    assert da.window_start_for("10:30", now) == at(10, 30)     # ちょうど今
    assert da.window_start_for("22:00", now) == at(22, 0, -1)  # まだ来ていない → 昨日
    assert da.window_start_for("bad", now) == at(0, 0)         # 不正値は既定に落とす
    assert not da.valid_reset_time("24:00")
    assert not da.valid_reset_time("6:00")
    assert not da.valid_reset_time("12:60")
    assert da.valid_reset_time("23:59")


def test_minute_buckets_and_daily_average(tmp_path: Path):
    st = da.DailyActiveStore(tmp_path / "daily.json")
    t = at(10, 0)
    st.add("p1", t + 5, 1)
    st.add("p1", t + 20, 3)          # 同じ分: 平均 2
    st.add("p1", t + 65, 4)          # 次の分: 平均 4
    snap = st.snapshot("p1", t + 70)
    assert snap["window_start"] == at(0, 0)
    assert snap["minutes"] == 2 and snap["samples"] == 3
    assert snap["avg"] == 3.0        # (2 + 4) / 2 — 分単位の時間加重
    assert snap["peak"] == 4.0 and snap["peak_at"] == int((t + 65) // 60) * 60
    assert snap["buckets"][0][0] == int(t // 60) * 60
    assert snap["buckets"][0][1] == 2.0 and snap["buckets"][0][2] == 2
    # 数値以外 / bool / 空プロファイルは無視
    st.add("p1", t + 80, None)
    st.add("p1", t + 80, True)
    st.add("", t + 80, 5)
    assert st.snapshot("p1", t + 90)["samples"] == 3


def test_peak_tie_takes_earliest_minute(tmp_path: Path):
    """ピークが同値のときは最早の分を peak_at にする。"""
    st = da.DailyActiveStore(tmp_path / "daily.json")
    t = at(10, 0)
    st.add("p1", t + 5, 4)
    st.add("p1", t + 65, 4)
    snap = st.snapshot("p1", t + 70)
    assert snap["peak"] == 4.0 and snap["peak_at"] == int(t // 60) * 60


def test_window_rolls_at_reset_time_and_drops_old_buckets(tmp_path: Path):
    st = da.DailyActiveStore(tmp_path / "daily.json")
    st.add("p1", at(10, 0), 2)
    st.add("p1", at(23, 59), 4)
    # 翌日 00:00 を跨ぐと窓が進み、前日のバケットは捨てる
    snap = st.snapshot("p1", at(0, 1, 1))
    assert snap["window_start"] == at(0, 0, 1)
    assert snap["buckets"] == [] and snap["avg"] is None
    st.add("p1", at(0, 2, 1), 6)
    snap = st.snapshot("p1", at(0, 3, 1))
    assert snap["avg"] == 6.0 and snap["minutes"] == 1


def test_reset_time_change_moves_window(tmp_path: Path):
    st = da.DailyActiveStore(tmp_path / "daily.json")
    st.add("p1", at(1, 0), 2)        # 00:00 起点の窓に 01:00 のバケット
    st.add("p1", at(3, 0), 4)
    # リセット時刻を 02:00 に変更 → 窓は 02:00 起点になり 01:00 のバケットは落ちる
    snap = st.snapshot("p1", at(10, 0), "02:00")
    assert snap["window_start"] == at(2, 0)
    assert snap["minutes"] == 1 and snap["avg"] == 4.0
    # 未来側への変更 (22:00) は窓を巻き戻さない (次の 22:00 まで現状維持)
    snap = st.snapshot("p1", at(10, 0), "22:00")
    assert snap["window_start"] == at(2, 0)


def test_manual_reset_starts_window_now(tmp_path: Path):
    st = da.DailyActiveStore(tmp_path / "daily.json")
    st.add("p1", at(10, 0), 2)
    st.reset("p1", at(10, 30))
    snap = st.snapshot("p1", at(10, 31))
    assert snap["window_start"] == at(10, 30)
    assert snap["buckets"] == [] and snap["avg"] is None
    # 窓より前の遅延サンプルは入れない
    st.add("p1", at(10, 29), 9)
    assert st.snapshot("p1", at(10, 32))["samples"] == 0
    st.add("p1", at(10, 31), 3)
    assert st.snapshot("p1", at(10, 32))["avg"] == 3.0
    # 次の自動リセット (翌 00:00) までは手動窓が維持される
    assert st.snapshot("p1", at(23, 0))["window_start"] == at(10, 30)


def test_persistence_and_prune(tmp_path: Path):
    path = tmp_path / "daily.json"
    st = da.DailyActiveStore(path)
    st.add("p1", at(10, 0), 2)
    st.add("p2", at(10, 0), 5)
    st.save()
    st2 = da.DailyActiveStore(path)          # launcher 再起動相当
    snap = st2.snapshot("p1", at(10, 30))
    assert snap["avg"] == 2.0 and snap["window_start"] == at(0, 0)
    st2.prune(["p1"])                        # 削除されたプロファイルの蓄積を捨てる
    assert st2.snapshot("p2", at(10, 30))["buckets"] == []
    assert st2.snapshot("p1", at(10, 30))["avg"] == 2.0


def test_save_if_dirty_throttled(tmp_path: Path):
    path = tmp_path / "daily.json"
    st = da.DailyActiveStore(path)
    st.add("p1", at(10, 0), 2)
    st.save()
    st.add("p1", at(10, 1), 4)
    st.save_if_dirty(now_ts=time.time() + 5)      # 30 秒未満は書かない
    doc = json.loads(path.read_text("utf-8"))
    assert len(doc["profiles"]["p1"]["buckets"]) == 1
    st.save_if_dirty(now_ts=time.time() + 40)
    doc = json.loads(path.read_text("utf-8"))
    assert len(doc["profiles"]["p1"]["buckets"]) == 2


# ---------------------------------------------------------------- API 中継

def _strata_metrics(active: int) -> dict:
    return {
        "engine": {"model_label": "m", "max_context": 8192},
        "live": {"state": "generating", "tok_s": 30.0, "parallel": 4, "active": active,
                 "slots": [{"slot": 0, "state": "generating", "tok_s": 30.0}]},
        "history": {"tok_s": [30.0] * 60},
        "time": BASE,
    }


def test_daily_endpoint_feed_reset_and_settings(tmp_path: Path, monkeypatch):
    _run_session(tmp_path, "strata", "Strata")
    clock = [at(10, 30)]
    state = {"active": 2}

    def fake_urlopen(req, timeout=2.0):
        return FakeResponse(json.dumps(_strata_metrics(state["active"])).encode("utf-8"))

    import llm_launcher.app as app_mod
    monkeypatch.setattr(app_mod.urllib.request, "urlopen", fake_urlopen)
    _fake_clock(monkeypatch, app_mod, clock)
    app = create_app(tmp_path)

    # 初期: 窓は今日 00:00 起点、まだサンプルなし
    status, body = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_new/parallel/daily"))
    assert status == 200
    assert body["reset_time"] == "00:00" and body["window_start"] == at(0, 0)
    assert body["avg"] is None and body["buckets"] == []
    assert body["peak"] is None and body["peak_at"] is None

    # 中継ポーリングが蓄積も進める (1 分バケット)
    state["active"] = 2
    status, _ = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_new/parallel"))
    assert status == 200
    clock[0] = at(10, 30, 0) + 30
    state["active"] = 4
    status, _ = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_new/parallel"))
    assert status == 200
    status, body = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_new/parallel/daily"))
    assert status == 200
    assert body["minutes"] == 1 and body["samples"] == 2 and body["avg"] == 3.0
    assert body["peak"] == 3.0 and body["peak_at"] == int(at(10, 30) // 60) * 60

    # 手動リセット: 今この時点から新しい窓
    status, body = asyncio.run(asgi_call(app, "POST", "/api/sessions/s_new/parallel/daily/reset"))
    assert status == 200
    assert body["window_start"] == clock[0] and body["buckets"] == [] and body["avg"] is None
    assert body["peak"] is None and body["peak_at"] is None

    # リセット時刻の設定: 保存 / 検証 / 窓の進め直し
    status, body = asyncio.run(asgi_call(app, "GET", "/api/parallel/daily/settings"))
    assert status == 200 and body["reset_time"] == "00:00"
    status, body = asyncio.run(_asgi_put(app, "/api/parallel/daily/settings",
                                         {"reset_time": "09:00"}))
    assert status == 200 and body["reset_time"] == "09:00"
    status, body = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_new/parallel/daily"))
    assert status == 200
    # 手動リセット窓 (10:30) は 09:00 より後なので維持される
    assert body["reset_time"] == "09:00" and body["window_start"] == clock[0]
    status, body = asyncio.run(_asgi_put(app, "/api/parallel/daily/settings",
                                         {"reset_time": "25:00"}))
    assert status == 400
    status, body = asyncio.run(_asgi_put(app, "/api/parallel/daily/settings",
                                         {"reset_time": "6:00"}))
    assert status == 400

    # 権限: 閲覧 / リセット / 設定とも dashboard 開示範囲
    assert rbac.required_cap("GET", "/api/sessions/s_new/parallel/daily") == "dashboard"
    assert rbac.required_cap("POST", "/api/sessions/s_new/parallel/daily/reset") == "dashboard"
    assert rbac.required_cap("GET", "/api/parallel/daily/settings") == "dashboard"
    assert rbac.required_cap("PUT", "/api/parallel/daily/settings") == "dashboard"

    # 存在しないセッション / 非対応バックエンド
    status, _ = asyncio.run(asgi_call(app, "GET", "/api/sessions/nope/parallel/daily"))
    assert status == 404
    _run_session(tmp_path, "unknown", "Unknown", sid="s_unk")
    app2 = create_app(tmp_path)
    status, _ = asyncio.run(asgi_call(app2, "GET", "/api/sessions/s_unk/parallel/daily"))
    assert status == 400


async def _asgi_put(app, path: str, body: dict) -> tuple[int, dict]:
    """JSON ボディ付き PUT (asgi_call はボディなし専用)。"""
    payload = json.dumps(body).encode("utf-8")
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "http_version": "1.1", "method": "PUT", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", b"test"), (b"content-type", b"application/json"),
                         (b"content-length", str(len(payload)).encode())],
             "client": ("127.0.0.1", 50000), "server": ("test", 80)}
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(msg: dict) -> None:
        sent.append(msg)

    await app(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), 0)
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(raw)


def test_daily_survives_launcher_restart_and_session_change(tmp_path: Path, monkeypatch):
    """プロファイル鍵 + ファイル永続化: launcher 再起動 / サーバー起動し直し
    (セッション ID 交代) でも同じプロファイルの日の記録は続ぐ。"""
    _run_session(tmp_path, "strata", "Strata")
    clock = [at(10, 0)]

    def fake_urlopen(req, timeout=2.0):
        return FakeResponse(json.dumps(_strata_metrics(3)).encode("utf-8"))

    import llm_launcher.app as app_mod
    monkeypatch.setattr(app_mod.urllib.request, "urlopen", fake_urlopen)
    _fake_clock(monkeypatch, app_mod, clock)
    app = create_app(tmp_path)
    status, _ = asyncio.run(asgi_call(app, "GET", "/api/sessions/s_new/parallel"))
    assert status == 200
    # 設定保存は蓄積も保存する (再起動に備えた永続化の機会)
    status, _ = asyncio.run(_asgi_put(app, "/api/parallel/daily/settings",
                                      {"reset_time": "00:00"}))
    assert status == 200

    # launcher 再起動 + 新しいセッション (同じプロファイル)
    _run_session(tmp_path, "strata", "Strata", sid="s_next")
    app2 = create_app(tmp_path)
    status, body = asyncio.run(asgi_call(app2, "GET", "/api/sessions/s_next/parallel/daily"))
    assert status == 200
    assert body["profile_id"] == "p_new"
    assert body["minutes"] == 1 and body["avg"] == 3.0
    assert body["window_start"] == at(0, 0)
