"""FreeToken 最大出力トークン (max_output_tokens) のセットアップ既定値と反映の回帰テスト。

背景: エンジンの max_tokens 省略時既定は固定 32K (モデル最大 262144 ではない)。
ユーザーが「コード生成する際の基準」をセットアップ時の既定として求められるように、
launcher は
  1. プロフィールド --max-output-tokens にコード生成向け既定 32768 を持ち、既定 ON で送る
  2. セットアップウィザード ③ (稼働設定) に「最大出力トークン」質問を出す (32K 推奨)
  3. 起動 JSON 主体モードでもフォーム値で --max-output-tokens を上書きする
必要がある。将来この既定/導線を外す (OFF に戻す・質問を消す・JSON 上書きを消す) と red になる。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_launcher import backends

ROOT = Path(__file__).resolve().parents[1]
SETUP_JS = (ROOT / "src" / "llm_launcher" / "static" / "setup.js").read_text("utf-8")
CODEGEN_DEFAULT = "32768"


def _field(bid: str) -> dict:
    return next(f for f in backends.BACKENDS[bid]["fields"] if f["key"] == "max_output_tokens")


@pytest.mark.parametrize("bid", ["freetoken", "freetoken-fork"])
def test_field_has_codegen_default_and_is_on(bid: str):
    f = _field(bid)
    assert f["flag"] == "--max-output-tokens"
    assert f["default"] == CODEGEN_DEFAULT
    assert f["default_on"] is True
    assert "max_output_tokens" not in backends.DEFAULT_OFF_KEYS[bid]


def _profile(values: dict, on: dict | None) -> dict:
    return {"command": "", "values": values, "on": on, "extra_args": ""}


def test_regular_mode_sent_when_on_and_dropped_when_off():
    b = backends.BACKENDS["freetoken-fork"]
    vals = {"model_path": "/m", "host": "0.0.0.0", "port": "1919",
            "max_output_tokens": CODEGEN_DEFAULT}
    on = {"model_path": True, "host": True, "port": True, "max_output_tokens": True}
    argv = backends.render_argv(b, _profile(vals, on), python="py", binary="", root=ROOT)
    i = argv.index("--max-output-tokens")
    assert argv[i:i + 2] == ["--max-output-tokens", CODEGEN_DEFAULT]
    on["max_output_tokens"] = False
    argv = backends.render_argv(b, _profile(vals, on), python="py", binary="", root=ROOT)
    assert "--max-output-tokens" not in argv


def _launch_json(tmp_path: Path, argv: list[str]) -> str:
    p = tmp_path / "launch.json"
    p.write_text(json.dumps({"python": "py", "argv": argv, "cwd": str(tmp_path),
                             "env": {}, "port": 1919}), "utf-8")
    return str(p)


def _render_launch(cfg: str, mot: str, on: bool) -> list[str]:
    b = backends.BACKENDS["freetoken-fork"]
    vals = {"config": cfg, "model_path": "/m", "port": "1919", "max_output_tokens": mot}
    prof = _profile(vals, {"max_output_tokens": on})
    return backends.render_argv(b, prof, python="py", binary="", root=ROOT)


BASE = ["-m", "freetoken", "--model-path", "/m", "--port", "1919"]


def test_launch_json_mode_adds_when_absent(tmp_path: Path):
    argv = _render_launch(_launch_json(tmp_path, BASE), CODEGEN_DEFAULT, True)
    i = argv.index("--max-output-tokens")
    assert argv[i:i + 2] == ["--max-output-tokens", CODEGEN_DEFAULT]


def test_launch_json_mode_overrides_json_value(tmp_path: Path):
    cfg = _launch_json(tmp_path, BASE + ["--max-output-tokens", "8192"])
    argv = _render_launch(cfg, "65536", True)
    i = argv.index("--max-output-tokens")
    assert argv[i:i + 2] == ["--max-output-tokens", "65536"]


@pytest.mark.parametrize("mot,on", [("", True), ("65536", False)])
def test_launch_json_mode_defers_to_json_when_empty_or_off(tmp_path: Path, mot: str, on: bool):
    cfg = _launch_json(tmp_path, BASE + ["--max-output-tokens", "8192"])
    argv = _render_launch(cfg, mot, on)
    i = argv.index("--max-output-tokens")
    assert argv[i:i + 2] == ["--max-output-tokens", "8192"]


def test_wizard_has_output_question_with_codegen_recommendation():
    # ③ の質問セットに max_output_tokens が出ること (fork / upstream 両方)。
    assert SETUP_JS.count('out: { key: "max_output_tokens"') >= 2
    # コード生成既定 32K は「環境非依存の workload 推奨」として cfRecommend が
    # 動的に適用する (選択肢リストに固定タグは焼かない — 環境適応の推奨に移行)。
    assert '["32768", "32K", ""]' in SETUP_JS
    assert "outFit.includes(32768)" in SETUP_JS
    # 描画/保存/ヒントの3ループとも out を通すこと。
    assert SETUP_JS.count('["ctx", "out", "kv", "moe", "ple", "kern", "tp", "par", "vis"]') == 3