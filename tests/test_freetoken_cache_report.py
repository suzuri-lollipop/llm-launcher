"""FreeToken (fork) キャッシュレポート (--enable-cache-report) のセットアップ既定有効の回帰テスト。

背景 (2026-11-19 要望): セットアップウィザードで FreeToken (fork) のプロファイルを作成する
とき、キャッシュレポート (usage の cached_tokens / cache_read_input_tokens を返す) を
既定で有効にしたい。エンジンの既定は OFF で、ウィザードが作るプロファイルは起動 JSON
主体になるため、作成時に値を入れておかないと --enable-cache-report が JSON に焼き込まれず
OFF のままになる。launcher は

  1. ウィザードのプロファイル作成 (renderConfig) で freetoken-fork のとき
     values.enable_cache_report = "on" を種付けする
  2. その値が render_argv で --enable-cache-report として送信され、起動 JSON に焼き込まれる

必要がある。将来この種付けを外す / 他バックエンドへ漏らすと red になる。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_launcher import backends

ROOT = Path(__file__).resolve().parents[1]
SETUP_JS = (ROOT / "src" / "llm_launcher" / "static" / "setup.js").read_text("utf-8")


def _block(start: str, end: str) -> str:
    i = SETUP_JS.index(start)
    j = SETUP_JS.index(end, i)
    return SETUP_JS[i:j]


def _field(bid: str) -> dict:
    return next(f for f in backends.BACKENDS[bid]["fields"] if f["key"] == "enable_cache_report")


def test_field_is_bool_cache_report():
    f = _field("freetoken-fork")
    assert f["flag"] == "--enable-cache-report"
    assert f["type"] == "bool"


def test_wizard_seeds_cache_report_for_fork_before_creating_profile():
    """作成ブロック (POST /api/profiles) より前に freetoken-fork 限定で種付けする。"""
    fn = _block("async function renderConfig()", '$("#cf-groups").addEventListener')
    seed = 'if (W.backend === "freetoken-fork") initVals.enable_cache_report = "on";'
    assert seed in fn, "ウィザード作成時のキャッシュレポート既定有効が無い"
    # 種付けはプロファイル作成 POST より前 (initVals に載ってから送られる)
    assert fn.index(seed) < fn.index('api("/api/profiles", { method: "POST"')
    # 既存プロファイル再利用 (W.cfgProfId) の経路には足さない — 作成時だけ
    create = _block("if (!p && cfg) {", "W.cfgProfNew = true;")
    assert seed in create


def _profile(values: dict, on: dict | None = None) -> dict:
    return {"command": "", "values": values, "on": on, "extra_args": ""}


def test_render_argv_sends_flag_when_seeded():
    """ウィザードは on を持たないプロファイルを作る — bool は値が真なら送信される。"""
    b = backends.BACKENDS["freetoken-fork"]
    vals = {"model_path": "/m", "port": "1919", "enable_cache_report": "on"}
    argv = backends.render_argv(b, _profile(vals), python="py", binary="", root=ROOT)
    assert "--enable-cache-report" in argv


@pytest.mark.parametrize("val", ["", None])
def test_render_argv_omits_flag_when_unset(val):
    b = backends.BACKENDS["freetoken-fork"]
    vals = {"model_path": "/m", "port": "1919"}
    if val is not None:
        vals["enable_cache_report"] = val
    argv = backends.render_argv(b, _profile(vals), python="py", binary="", root=ROOT)
    assert "--enable-cache-report" not in argv


def test_launch_json_bakes_the_flag(tmp_path: Path):
    """export_launch_json は config を除いた全項目モードで render_argv する。
    種付け済みの値なら起動 JSON の argv に --enable-cache-report が焼き込まれる。"""
    b = backends.BACKENDS["freetoken-fork"]
    vals = {"model_path": "/m", "port": "1919", "enable_cache_report": "on"}
    argv = backends.render_argv(b, _profile(vals), python="py", binary="", root=ROOT)
    assert "--enable-cache-report" in argv
    # JSON 主体モードへ切り替えた後も JSON 側のフラグが生きる (フォームは最小項目のみ)
    cfg = tmp_path / "launch.json"
    cfg.write_text(json.dumps({"python": "py", "argv": argv[1:], "cwd": str(tmp_path),
                               "env": {}, "port": 1919}), "utf-8")
    rendered = backends.render_argv(
        b, _profile({"config": str(cfg), "port": "1919"}), python="py", binary="", root=ROOT)
    assert "--enable-cache-report" in rendered
