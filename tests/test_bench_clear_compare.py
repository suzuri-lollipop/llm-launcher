"""履歴一括クリア / 経時記録 (series) / 比較 UI / 主指標 decode / 静的キャッシュ の回帰テスト。

ベンチ画面の見やすさ改修で入れた点を押さえる:

  1. clear_all(): 完了系だけ消し、 queued / running / active_id は残す。
     消した id は _dead 入り so _save でディスクへ復活しない
  2. _stage の経時サンプリング: stage["series"] が入り、UI がライブで見られる
     よう progress["series"] にも書き、段終了後は progress から消える
     (SAMPLE_EVERY / MAX_SAMPLES は monkeypatch で短縮)
  3. DELETE /api/benchmarks: clear_all を呼んで removed 件数を返す
  4. 静的 JS は Cache-Control: no-cache + ETag 304 (旧 JS のヒューリスティック
     キャッシュで「反映されない」事故を防ぐ)
  5. フロント契約: 一括クリアボタン、比較チェックボックス / パネル、
     SVG チャート (bmSvgChart) と補助列トグル、decode 主指標表示がソースに存在する
  6. ページ内 3 タブ (実行 / 履歴・管理 / 結果・比較) の機能分離
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from llm_launcher import app as app_mod
from llm_launcher import bench as bench_mod
from llm_launcher.bench import BenchManager
from llm_launcher.store import JsonStore

from test_auth_users import Client
from test_bench_manager import PARAMS, _FakeManager, _Handler, _manager, _run_dict, server  # noqa: F401


def _seed(tmp_path: Path, *runs: dict) -> BenchManager:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    store = JsonStore(tmp_path / "data" / "bench.json", [])
    store.mutate(lambda lst: lst.extend(runs))
    mgr = BenchManager(store, JsonStore(tmp_path / "data" / "profiles.json", []), _FakeManager(0))
    return mgr


# ------------------------------------------------------------ 1. clear_all


def test_clear_all_removes_completed_and_keeps_active(tmp_path):
    mgr = _seed(tmp_path,
                {"id": "b0", "created_at": 0, "status": "completed", "stages": []},
                {"id": "b1", "created_at": 1, "status": "failed", "stages": []},
                {"id": "b2", "created_at": 2, "status": "running", "stages": []},
                {"id": "b3", "created_at": 3, "status": "queued", "stages": []},
                {"id": "b4", "created_at": 4, "status": "completed", "stages": []})
    mgr.active_id = "b4"                              # 完了でも active 扱いなら残す
    assert mgr.clear_all() == 2
    assert {r["id"] for r in mgr.list_runs()} == {"b2", "b3", "b4"}
    assert {"b0", "b1"} <= mgr._dead                  # 生き返り防止は delete と同じ
    on_disk = {r["id"] for r in json.loads(
        (tmp_path / "data" / "bench.json").read_text(encoding="utf-8"))}
    assert on_disk == {"b2", "b3", "b4"}              # 直ちにディスクへ反映


def test_clear_all_dead_runs_are_not_resurrected(tmp_path):
    mgr = _seed(tmp_path, {"id": "b0", "created_at": 0, "status": "completed", "stages": []})
    victim = mgr.get("b0")
    mgr._last_save["b0"] = 1.0
    assert mgr.clear_all() == 1
    assert "b0" not in mgr._last_save                 # 保存刻印も掃除される
    mgr._save(victim, force=True)                     # 実行中の task が最後に保存しに来る
    assert mgr.get("b0") is None


# ------------------------------------------------------------ 2. 経時記録


def test_stage_records_time_series(tmp_path, server):
    srv, base = server
    _Handler.mode, _Handler.pieces, _Handler.delay = "stream_slow", 6, 0.04   # 1 件 ~0.24s
    mgr = _manager(tmp_path, _FakeManager(srv.server_address[1]))
    # 4 件 / 2 並列: 手前 2 件が ~0.24s で完了し、その後にもサンプル窓が残る。
    # dec は「完了後」に積まる値なので、全件同時完了 (2件/2並列) だと検証が不安定になる
    params = {**PARAMS, "concurrency": [2], "num_prompts": 4, "warmup": 0}
    run = _run_dict(mgr, params)
    monkey_t = bench_mod.SAMPLE_EVERY
    try:
        bench_mod.SAMPLE_EVERY = 0.02

        async def go():
            return await asyncio.wait_for(
                mgr._stage(run, 0, base, "/v1/completions", "m", params, 2, 1), timeout=30)

        stage = asyncio.run(go())
    finally:
        bench_mod.SAMPLE_EVERY = monkey_t

    ser = stage["series"]
    assert len(ser) >= 2, "経過時間を刻むサンプルが 2 件も取れていない"
    assert all(set(("t", "done", "ok", "fail", "tok", "inf", "win", "avg", "dec", "agg")) <= set(p) for p in ser)
    assert [p["t"] for p in ser] == sorted(p["t"] for p in ser)
    toks = [p["tok"] for p in ser]
    assert toks == sorted(toks), "累積トークンは単調増加でなければならない"
    # dec (decode tok/s の完了分平均) は最初の完了以降 数値。progress の dec_sum/dec_n も積まれる
    assert any(isinstance(p["dec"], float) and p["dec"] > 0 for p in ser), "完了後 dec が入らない"
    assert run["progress"]["dec_n"] >= 1 and run["progress"]["dec_sum"] > 0
    assert stage["series_decimated"] is False
    # ライブ表示用の progress.series は段終了後に掃除 (run 全体保存を太らせない)
    assert "series" not in run["progress"]


def test_series_agg_counts_streaming_tokens_per_second(tmp_path, server, monkeypatch):
    """経時記録の意味そのもの (ユーザー指摘のデグレード): 旧実装は「完了時にまとめて
    確定する out_tokens」を窓差分していたため、グラフが完了の瞬間だけ立つスパイク
    (間は 0) になり「毎秒生成されたトークン」を見せていなかった。agg は SSE の
    content 到着を即时計数した合計の差分なので、リクエストが 1 本も完了する前から
    0 でない値が載る (done==0 なのに agg>0 が旧実装との判別条件)。"""
    srv, base = server
    _Handler.mode, _Handler.pieces, _Handler.delay = "stream_slow", 30, 0.05   # 1 件 ~1.5s
    monkeypatch.setattr(bench_mod, "SAMPLE_EVERY", 0.05)
    mgr = _manager(tmp_path, _FakeManager(srv.server_address[1]))
    params = {**PARAMS, "concurrency": [1], "num_prompts": 1, "warmup": 0}
    run = _run_dict(mgr, params)

    async def go():
        return await asyncio.wait_for(
            mgr._stage(run, 0, base, "/v1/completions", "m", params, 1, 1), timeout=30)

    stage = asyncio.run(go())
    ser = stage["series"]
    assert len(ser) >= 4, "ストリーミング中にサンプルが十分刻まれていない"
    flowing = [p for p in ser if p["done"] == 0]
    assert flowing, "完了前のサンプルが取れていない (検証になっていない)"
    assert any(p["agg"] > 0 for p in flowing), \
        "1 本も完了していない区間で agg が全 0 = 完了確定ベースに戻っている"
    # 2 並列の合算は単独の約 2 倍になるべき (「その秒に生成された全トークン」の定義)
    params2 = {**params, "concurrency": [2], "num_prompts": 2}
    run2 = _run_dict(mgr, params2)

    async def go2():
        return await asyncio.wait_for(
            mgr._stage(run2, 0, base, "/v1/completions", "m", params2, 2, 1), timeout=30)

    stage2 = asyncio.run(go2())
    mid2 = [p["agg"] for p in stage2["series"] if p["done"] == 0 and p["inf"] == 2 and p["t"] > 0.4]
    assert len(mid2) >= 4, "2 並列で走っている区間のサンプルが足りない"
    # 窓 0.05s × 1tok 粒度で個別サンプルは 0 に量子化されうるため、平均で見る
    # (想定 ~2×16 tok/s。ドリフトでも平均は 10 を大きく割らない)
    assert sum(mid2) / len(mid2) > 8, f"2 並列の合算レートが載らない (avg={sum(mid2)/len(mid2):.1f})"
    # decode tok/s Σ (段サマリ): 全並列合算 = トークン合計 ÷ 最初→最後のトークン到着窓。
    # 2 並列同時走行では「1 本あたり平均 (decode_tps.avg)」の約 2 倍になるのが定義
    assert stage["decode_tps_sum"] and stage["decode_tps_sum"] > 0
    assert stage2["decode_tps_sum"] > 1.5 * stage2["decode_tps"]["avg"], \
        (f"2 並列の decode Σ が合算になっていない "
         f"(Σ={stage2['decode_tps_sum']}, avg={stage2['decode_tps']['avg']})")


def test_reqs_phases_recorded(tmp_path, server):
    """リクエスト単位のフェーズ記録 (送信→最初のトークン→最後→確定)。
    ユーザー要望: 「プレフィルの待ちとデコード速度をリアルタイムで比較したい」。
    reqs は各段に保存され、各行 s<=a<=d<=e がストリーム順と一致すること。"""
    srv, base = server
    _Handler.mode, _Handler.pieces, _Handler.delay = "stream_slow", 8, 0.04   # 1 件 ~0.32s
    mgr = _manager(tmp_path, _FakeManager(srv.server_address[1]))
    params = {**PARAMS, "concurrency": [2], "num_prompts": 4, "warmup": 0}
    run = _run_dict(mgr, params)

    async def go():
        return await asyncio.wait_for(
            mgr._stage(run, 0, base, "/v1/completions", "m", params, 2, 1), timeout=30)

    stage = asyncio.run(go())
    reqs = stage["reqs"]
    assert stage["reqs_truncated"] is False
    assert len(reqs) == 4, "リクエスト 4 本分のフェーズ行が無い"
    for r in reqs:
        assert r["s"] is not None and r["e"] is not None
        assert r["s"] <= r["a"] <= r["d"] <= r["e"], f"フェーズ順が崩れている: {r}"
        assert (r["a"] - r["s"]) > 0.01, "待ち (プレフィル) 区間が 0 = a が届いていない"
        assert (r["d"] - r["a"]) > 0.05, "デコード区間が 0 = チャンク到着が記録されていない"
        assert r["n"] == 8, "チャンク実測トークン数が届いていない"
    # 同時実走は常に並列度以下 (ガントの行の重なりが 2 を超えないことと同じ不変条件)
    events = sorted([(r["s"], 1) for r in reqs] + [(r["e"], -1) for r in reqs])
    cur = peak = 0
    for _, d in events:
        cur += d
        peak = max(peak, cur)
    assert peak <= 2, f"並列度 2 に対し {peak} 本重なっている"
    # ライブ用の写しは段終了後に掃除される (series と同じ扱い)
    assert "reqs" not in run["progress"]
    # モックサーバーに /metrics は無い = 内訳なし (待ちブロックは従来どおり一枚)
    assert stage["phase_metrics"] is None
    assert all("arr" not in r for r in reqs)


def test_attach_phase_metrics_matches_by_time_and_tokens():
    """サーバー内訳 (strata 系 /metrics) の突合: 送信実時刻に近く入力トークン一致の
    未使用エントリを貪欲に。キュー待ち = サーバー側TTFT − prompt_ms/1000。"""
    t0_wall = 1000.0
    slots = [{"w": 1001.0, "in": 77}, {"w": 1003.0, "in": 77}, {"w": 1099.0, "in": 77}]
    snaps = [{"s": 1.0, "a": 4.0}, {"s": 3.0, "a": 6.0}, {"s": 99.0, "a": 101.0}]
    pool = [
        {"time": 500.0, "prompt_ms": 900.0, "first_engine_token_s": 2.0, "prompt_tokens": 77},   # 時刻窓外
        {"time": 1001.2, "prompt_ms": 700.0, "first_engine_token_s": 2.7, "prompt_tokens": 77},
        {"time": 1003.1, "prompt_ms": 800.0, "first_engine_token_s": 1.3, "prompt_tokens": 77},
    ]
    hit = bench_mod._attach_phase_metrics(snaps, slots, pool, t0_wall)
    assert hit == 2
    assert snaps[0]["arr"] == 1.2 and snaps[0]["p"] == 0.7 and snaps[0]["q"] == 2.0
    assert snaps[1]["arr"] == 3.1 and snaps[1]["p"] == 0.8 and snaps[1]["q"] == 0.5
    assert "arr" not in snaps[2] and "q" not in snaps[2]      # 窓外は付けない
    # ft < prompt_ms の暴値でも q は負にしない
    s2 = [{"s": 0.0, "a": 1.0}]
    bench_mod._attach_phase_metrics(s2, [{"w": 1001.0, "in": None}],
                                    [{"time": 1001.0, "prompt_ms": 5000.0, "first_engine_token_s": 1.0}], t0_wall)
    assert s2[0]["q"] == 0.0 and s2[0]["p"] == 5.0


def test_phase_ui_renders_swimlanes():
    js = _bench_js()
    assert "function bmPhaseSvg" in js and "function bmPhaseBlock" in js
    assert "フェーズ (ライブ)" in js                       # 実行中カードにライブ版
    assert 'フェーズ (${st.concurrency}${st.mode === "stagger" ? " 本" : "p"})' in js   # 結果詳細に段ごと
    assert "var(--warn)" in js and "var(--ok)" in js       # 待ち=琥珀 / デコード=緑
    assert "プレフィル" in js and "確定" in js
    # サーバー内訳が公開されている場合は 送信(青)/キュー待ち(紫)/プレフィル(琥珀) に分割、
    # 非公開のサーバーでは「待ち」一枚のまま意味を明記して正直に表示する
    assert "キュー待ち" in js and "var(--purple)" in js
    assert "このサーバーは内訳を公開しないため分割できません" in js


def test_series_decimation_bounds_samples(tmp_path, server, monkeypatch):
    srv, base = server
    _Handler.mode, _Handler.pieces, _Handler.delay = "stream_slow", 30, 0.02   # 1 件 ~0.6s
    monkeypatch.setattr(bench_mod, "SAMPLE_EVERY", 0.01)
    monkeypatch.setattr(bench_mod, "MAX_SAMPLES", 8)
    mgr = _manager(tmp_path, _FakeManager(srv.server_address[1]))
    params = {**PARAMS, "concurrency": [2], "num_prompts": 2, "warmup": 0}
    run = _run_dict(mgr, params)

    async def go():
        return await asyncio.wait_for(
            mgr._stage(run, 0, base, "/v1/completions", "m", params, 2, 1), timeout=30)

    stage = asyncio.run(go())
    assert len(stage["series"]) <= bench_mod.MAX_SAMPLES + 2   # 間引き後は上限近辺で安定
    assert stage["series_decimated"] is True                    # フロント注記の材料


# ------------------------------------------------------------ 3. API


def test_delete_all_endpoint(tmp_path):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "bench.json").write_text(json.dumps([
        {"id": "b0", "created_at": 1, "status": "completed", "stages": []},
        {"id": "b1", "created_at": 2, "status": "cancelled", "stages": []},
        {"id": "b2", "created_at": 3, "status": "completed", "stages": []},
    ]), encoding="utf-8")
    client = Client(app_mod.create_app(tmp_path))

    async def go():
        st, res = await client.delete("/api/benchmarks")
        assert st == 200 and res == {"ok": True, "removed": 3}
        st, res = await client.get("/api/benchmarks")
        assert st == 200 and res["runs"] == []
        return st

    asyncio.run(go())


# ------------------------------------------------- 3b. 静的ファイルの再検証 (キャッシュ)


def _get_raw(app, path: str, extra_headers=()) -> tuple[int, dict[str, str]]:
    """最小 ASGI GET で ステータス + レスポンスヘッダ (小文字化) を返す."""
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", b"testserver")] + [(k.lower().encode(), v.encode())
                                                      for k, v in extra_headers],
             "client": ("127.0.0.1", 50000), "server": ("test", 80)}
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg: dict) -> None:
        sent.append(msg)

    async def go():
        await app(scope, receive, send)

    asyncio.run(go())
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], {k.decode().lower(): v.decode() for k, v in start.get("headers", [])}


def test_static_js_revalidates_every_time(tmp_path):
    """JS/CSS の URL にはバージョン query が無い。既定のヒューリスティックキャッシュだと
    デプロイ後もブラウザが古い JS を使い続け「decode 列が消えた」系の幽霊不具合になる
    (実事件)。no-cache で毎回再検証させ、変わらなければ ETag 304 で安く上げる。"""
    app = app_mod.create_app(tmp_path)
    st, hdrs = _get_raw(app, "/static/js/views/bench.js")
    assert st == 200
    assert hdrs.get("cache-control") == "no-cache"
    etag = hdrs.get("etag")
    assert etag, "再検証には ETag が要る"
    st2, _ = _get_raw(app, "/static/js/views/bench.js", [("If-None-Match", etag)])
    assert st2 == 304, "未変更時は 304 で本文を再送しないこと"


# ------------------------------------------------------------ 4. フロント契約


def _bench_js() -> str:
    p = Path(__file__).resolve().parents[1] / "src" / "llm_launcher" / "static" / "js" / "views" / "bench.js"
    return p.read_text(encoding="utf-8")


def test_ui_has_clear_compare_and_charts():
    js = _bench_js()
    # 一括クリア: ボタン → 確認 → DELETE /api/benchmarks → 件数トースト
    assert 'onclick="bmClearAll()"' in js and "履歴をクリア" in js
    assert 'api("/api/benchmarks", {method: "DELETE"})' in js
    # 比較: カードのチェックボックス → cmp 状態 → パネル (チャート + 指標表)
    assert 'onchange="bmCmpToggle' in js and "bmCmpPanel" in js and "結果比較" in js
    assert js.count("bmSvgChart") >= 4                        # 定義 + 使用 (ライブ / sweep / 経時)
    assert "<svg viewBox=" in js                              # 依存なし SVG 描画そのもの
    assert "経過秒" in js or "経過" in js
    # 補助列トグル: data-x と .bm-x クラスが対になっている
    assert 'data-x="${X ? 1 : 0}"' in js and 'class="bm-x"' in js
    # decode tok/s Σ は主指標: 既定表示 (補助列でない) + 太字 + 比較指標 + ライブ行 (tpNote) も decode Σ。
    # 「1 本分だけ出して合算が見えない」デグレード戻し禁止 (ユーザー指摘: 合計を表示する)
    assert '<th class="bm-x">decode' not in js and '<td class="bm-x">${bmTps(st.decode_tps' not in js
    assert 'decode tok/s Σ</th>' in js and "${bmTps(bmDecSum(st))}</b></td>" in js   # 表は合算 + avg は title
    assert "<td>${bmTps(st.out_tps)}</td>" in js          # 出力は補助扱い (太字ではない)
    assert 'decode:{lab: "decode tok/s Σ"' in js
    assert "function bmDecSum" in js
    assert "pr.dec_n" in js and "decode Σ は最初のトークンから" in js
    # 古い履歴 (series 無し) を「説明する導線
    assert "経時記録" in js
    # 経時グラフの値は「毎秒生成トークン合計」(agg) 優先。旧データのみ win/inf 代替
    assert "p.agg != null" in js and "全並列" in js
    css = (Path(__file__).resolve().parents[1] / "src" / "llm_launcher" / "static" / "css" / "app.css").read_text(encoding="utf-8")
    assert 'table.bm[data-x="0"] .bm-x{display:none}' in css
    assert "position:sticky" in css                           # 先頭列固定


def test_bench_page_tabs_split_functions():
    """ユーザー要望: ベンチ画面をページ内 3 タブに機能分離する (メニューは「ベンチマーク」
    1 項目のまま)。実行=フォーム+ライブ / 履歴・管理=一覧と削除等 / 結果・比較=選択して
    表示・比較。役目の混線 (履歴に比較パネル、比較に削除ボタン) を戻さないための契約。"""
    js = _bench_js()
    # 3 タブのボタンとペイン
    for t in ("run", "history", "compare"):
        assert f"bmSetTab('{t}')" in js
    for p in ("bmt-p-run", "bmt-p-history", "bmt-p-compare"):
        assert f'id="{p}"' in js
    # 実行フォーム (ベンチ開始ボタン) は実行ペインの中、履歴ペインより前
    assert js.index('id="bmt-p-run"') < js.index('id="bm-start"') < js.index('id="bmt-p-history"')
    # 履歴・管理 = 管理操作のみ (比較のチェックボックス / 比較パネルを戻さない)
    hist = js[js.index("function renderBenchHistory"):js.index("function renderBenchCompare")]
    assert "bmClearAll" in hist and "bmViewRun" in hist
    assert "bmCmpPanel" not in hist and "bmCmpToggle" not in hist
    # 結果・比較 = 選択と表示のみ (管理操作ボタンを置かない)
    cmp_ = js[js.index("function renderBenchCompare"):]
    assert "bmCmpToggle" in cmp_ and "bmCmpPanel" in cmp_ and "bmPhaseBlock" in cmp_
    assert "bmDelete" not in cmp_ and "bmClearAll" not in cmp_ and "bmRerun" not in cmp_
    # タブ状態の永続化とバー描画 (件数バッジ / 実行中ドット)
    assert "function bmSetTab" in js and "function bmTabBar" in js
    assert "tab: S.bench.tab" in js
    assert 'id="bmt-run-dot"' in js and 'id="bmt-hn"' in js and 'id="bmt-cn"' in js
    css = (Path(__file__).resolve().parents[1] / "src" / "llm_launcher" / "static" / "css" / "app.css").read_text(encoding="utf-8")
    assert ".bm-tabs{" in css and ".bm-tab.on{" in css


def test_phase_compare_bars():
    """フェーズ記録 (待ち/プレフィル/デコード) は実行ごとのスイムレーンだけで、
    「どちらの実行で待いが長いか」が比較できなかった。比較パネルに同じ段のリクエスト平均を
    共通スケールで積み上げる比較バーを置く (ユーザー指摘)。"""
    js = _bench_js()
    assert "function bmPhaseAgg" in js and "function bmPhaseCmpBlock" in js
    panel = js[js.index("function bmCmpPanel"):js.index("function bmCmpToggle")]
    assert "bmPhaseCmpBlock(" in panel and "フェーズ比較" in panel and "bmPhaseAgg(" in panel
    # 色はスイムレーンと同一系 (キュー待ち=紫 / プレフィル=琥珀 / デコード=緑 / 確定=灰)
    segs = js[js.index("const BM_PHASE_SEGS"):js.index("function bmPhaseAgg")]
    assert "var(--purple)" in segs and "var(--warn)" in segs and "var(--ok)" in segs and "#94a3b8" in segs
    # 内訳非公開サーバーは「待ち」一枚 / 旧履歴は正直に記録なしと明示 (黙って消さない)
    assert '"wait"' in segs and "フェーズ記録がありません" in js
    # 平均は reqs のセカンダリ算出 (バックエンド変更不要): q/p/待ち/デコード/確定を avg
    agg = js[js.index("function bmPhaseAgg"):js.index("function bmPhaseCmpBlock")]
    assert "g.q.push" in agg and "g.wait.push" in agg and "g.dec.push" in agg


def test_api_route_declared():
    src = (Path(__file__).resolve().parents[1] / "src" / "llm_launcher" / "app.py").read_text(encoding="utf-8")
    assert '@app.delete("/api/benchmarks")' in src
    assert "bench.clear_all()" in src


def test_active_card_guards_pending_stage():
    """実事故 (デプロイ直後): 実行中カードの段バッジで pr.stages (全予定並列度) を
    回しながら run.stages (完了済み段のみ) を index 参照するため、実行中・待機の段で
    st === undefined。そこへ st.decode_tps をガード前に読むと TypeError →
    renderBench 全体が落ち、refresh() の catch が接続エラーと取り違えてランプが赤に
    =「ベンチ中にライブも履歴も消えてオフライン表示」になる。参照は if (st) の内側限定。"""
    js = _bench_js()
    m = re.search(r"st = \(run\.stages \|\| \[\]\)\[i\];\s*(?://[^\n]*\n\s*)*if \(st\)", js)
    assert m, "段バッジ: st 参照は if (st) ガードの内側に限定されなければならない"
    assert "const d = st.decode_tps && st.decode_tps.avg;\n    if (st)" not in js


def test_render_error_does_not_kill_connection_lamp():
    """refresh() の catch は「取得失敗」だけを描画と共通で扱うと、ビューの JS 例外が
    オフライン表示 (live-dot 消灯) として見える。取得 try と renderAll try を分離し、
    描画例外はコンソールへ出す (サイレント握り潰しも禁止)。"""
    core = (Path(__file__).resolve().parents[1] / "src" / "llm_launcher" / "static" / "js" / "core.js").read_text(encoding="utf-8")
    assert 'console.error("render error:"' in core
    assert re.search(r"try \{ renderAll\(!silent\); \}\s*catch", core)
    js = _bench_js()
    assert 'console.error("render error:"' in js           # bmPoll の描画も同じ
    assert "catch { /* ignore */ }" not in js              # 握り潰しの回帰禁止
