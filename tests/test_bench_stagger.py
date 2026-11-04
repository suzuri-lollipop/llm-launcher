"""インタリーブ (stagger) ベンチマークの回帰テスト。

ユーザー要望: 「パラレル推論のベンチマークで 1 つのデコード中に他のプロンプトを
送信するベンチマークも追加。プレフィルや停止時間を棒グラフで確認・比較したい」。

  1. BenchBody: mode=stagger は 1 段 = num_prompts 本に正規化 (並列度リストは使わない)。
     gap_s は範囲検証、不正 mode は 422
  2. _stage (stagger): 送信時刻が gap 刻み / 先行本のデコード中に後着が走る (重なり) /
     停止区間 (reqs[].g) と停止合計 (st, stall_secs) が記録される
  3. フロント契約: 送信方式セレクト・間隔入力・スイムレーンの停止重畳・
     比較バーの停止セグメント
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from llm_launcher import bench as bench_mod
from llm_launcher.app import BenchBody

from test_bench_manager import PARAMS, _FakeManager, _Handler, _manager, _run_dict, server  # noqa: F401


# ------------------------------------------------------------ 1. 検証 (API 層)


def test_benchbody_stagger_normalizes_stages():
    b = BenchBody(profile_id="p", mode="stagger", num_prompts=4, concurrency=[1, 8])
    assert b.mode == "stagger"
    assert b.concurrency == [4]          # 1 段 = 本の数 (gap_s が段の条件)
    assert b.gap_s == 2.0
    b2 = BenchBody(profile_id="p", mode="stagger", num_prompts=100)
    assert b2.num_prompts == 32 and b2.concurrency == [32]   # ストリーム数に上限
    assert b2.to_params()["mode"] == "stagger"               # params へそのまま乗る


def test_benchbody_rejects_bad_mode_and_gap():
    with pytest.raises(ValidationError):
        BenchBody(profile_id="p", mode="weird")
    with pytest.raises(ValidationError):
        BenchBody(profile_id="p", mode="stagger", gap_s=0.0)     # 間隔は正秒数
    # loop は従来動作のまま (新規フィールドで既存呼び出しは壊れない)
    assert BenchBody(profile_id="p").mode == "loop"


# ------------------------------------------------------------ 2. _stage (stagger)


def test_stagger_stage_overlaps_and_records_stalls(tmp_path, server, monkeypatch):
    srv, base = server
    # 1 本 ~0.5s (10 片 × 0.05s 間隔)。閾値を 0.03s に下げて隙間を全部 停止 として記録させる
    _Handler.mode, _Handler.pieces, _Handler.delay = "stream_slow", 10, 0.05
    monkeypatch.setattr(bench_mod, "STALL_S", 0.03)
    mgr = _manager(tmp_path, _FakeManager(srv.server_address[1]))
    gap = 0.15
    params = {**PARAMS, "mode": "stagger", "gap_s": gap,
              "concurrency": [3], "num_prompts": 3, "warmup": 0}
    run = _run_dict(mgr, params)

    import asyncio

    async def go():
        return await asyncio.wait_for(
            mgr._stage(run, 0, base, "/v1/completions", "m", params, 3, 1), timeout=30)

    stage = asyncio.run(go())
    assert stage["mode"] == "stagger" and stage["gap_s"] == pytest.approx(gap)
    assert stage["ok"] == 3, f"3 本完走していない: {stage['errors']}"

    rows = sorted(stage["reqs"], key=lambda r: r["s"])
    assert len(rows) == 3
    # 送信は gap 刻み (到着待ち 50ms ポーリング + 接続オーバーヘッドのぶれを許容)
    for a, b in zip(rows, rows[1:]):
        d = b["s"] - a["s"]
        assert gap * 0.8 <= d <= gap + 0.25, f"送信間隔が gap でない: {d}"
    # デコード中の重送 = 後着が送られた時点で先行本まだ走行中 (この干渉が計測対象)
    assert rows[1]["s"] < rows[0]["e"] - 0.05, "インタリーブが実質直列になっている"

    for r in rows:
        assert isinstance(r["g"], list) and r["g"], "停止区間が記録されていない"
        assert all(len(g) == 2 and g[0] <= g[1] for g in r["g"])
        # 停止区間は待ち (s→a) より後、確定 (e) より前
        assert all(g[0] >= r["a"] - 0.01 and g[1] <= r["e"] + 0.01 for g in r["g"])
        assert r["st"] >= 0.03
    assert stage["stall_secs"] > 0
    assert stage["stall_count"] >= 3, "停止回数の集計が止まらない"
    # ライブ写しは掃除済み
    assert "reqs" not in run["progress"] and "series" not in run["progress"]


# ------------------------------------------------------------ 3. フロント契約


def test_stagger_ui_contract():
    p = Path(__file__).resolve().parents[1] / "src" / "llm_launcher" / "static" / "js" / "views" / "bench.js"
    js = p.read_text(encoding="utf-8")
    # フォーム: 送信方式セレクト + インタリーブ時のみ出る間隔入力
    assert 'id="bm-mode"' in js and 'value="stagger"' in js and 'id="bm-gap"' in js
    assert "function bmModeUI" in js and "インタリーブ" in js
    body = js[js.index("function bmFormBody"):js.index("async function bmStart")]
    assert "mode:" in body and "gap_s:" in body
    # スイムレーン: 停止区間を赤で重畳 (デコード行の上) + 凡例 + トータル秒
    stall = js[js.index("function bmStallRects"):js.index("function bmPhaseSvg")]
    assert "var(--err)" in stall and "停止" in stall
    svg = js[js.index("function bmPhaseSvg"):js.index("function bmPhaseBlock")]
    assert "bmStallRects(" in svg and "停止計" in svg
    # 比較バー: 停止セグメント (デコード内) と切り出し
    segs = js[js.index("const BM_PHASE_SEGS"):js.index("function bmPhaseAgg")]
    assert '"stall"' in segs and "停止 (デコード内)" in segs
    agg = js[js.index("function bmPhaseAgg"):js.index("function bmPhaseCmpBlock")]
    assert "r.st" in agg
    cmp_ = js[js.index("function bmPhaseCmpBlock"):js.index("function bmCmpPanel")]
    assert "hasStall" in cmp_ and "stall" in cmp_
    # 段ラベルは本数表示に切り替わる
    assert 'mode === "stagger" ? " 本" : "p"' in js
