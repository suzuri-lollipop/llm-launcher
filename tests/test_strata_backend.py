"""Strata バックエンド (module/strata) の起動引数回帰テスト。

Strata はリポジトリ直下の serve/server.py (Python) を直接起動するバックエンドで、
モデル本体の設定 (GGUF・コンテキスト長・MTP など) は setup.py が生成する
strata-<model>.json の "args" 配列側に入る。launcher 側は
  1. 正表 fields/strata.json が serve/server.py の argparse から生成されている
  2. 必須項目 (--config / --engine / --host / --port) が常に argv へ出る
  3. config 側が既に持つ項目 (--gpu / --tokenizer など) は既定 OFF = 送信しない
  4. 検出は submodule を cwd にした実 import で行う
     (find_spec だと依存の無い GUI .venv でもパス検索だけで成功してしまう)
必要がある。module/strata が未クローン (submodule 未 init) の環境では skip する。
"""
from pathlib import Path

import pytest

from llm_launcher import backends

ROOT = Path(__file__).resolve().parents[1]
SERVER_PY = ROOT / "module" / "strata" / "serve" / "server.py"

pytestmark = pytest.mark.skipif(
    not SERVER_PY.exists(),
    reason="module/strata が未クローン",
)


def _field(key: str) -> dict:
    return next(f for f in backends.BACKENDS["strata"]["fields"] if f["key"] == key)


def test_table_is_generated_from_serve_argparse():
    table = backends.load_table("strata")
    assert table["--engine"]["choices"] == ["mock", "strata"]
    assert table["--port"]["type"] == "int"
    assert table["--idle-unload"]["type"] == "float"
    assert table["--lazy"]["type"] == "bool"
    # default=None / os.environ.get(...) は「既定値なし」= 正表に default を書かない
    for flag in ("--host", "--api-key", "--replicas"):
        assert table[flag].get("default") in (None, "")
    # サーバー CLI に無いフラグは出さない (エンジン本体引数は config の args 側)
    assert "--max-context" not in table


def test_required_fields_are_always_sent():
    for key in ("config", "engine", "host", "port"):
        f = _field(key)
        assert f["required"] is True and f["default_on"] is True
    assert {f["key"] for f in backends.BACKENDS["strata"]["fields"] if f["required"]} == \
        {"config", "engine", "host", "port"}


def test_engine_defaults_to_strata_not_mock():
    f = _field("engine")
    assert f["type"] == "select"
    assert f["options"] == ["", "mock", "strata"]
    # 正表の既定は mock (serve/server.py の argparse 通り)、UI は実エンジンを既定にする
    assert f["default"] == "strata"


def test_config_side_options_default_off():
    off = backends.DEFAULT_OFF_KEYS["strata"]
    for key in ("tokenizer", "gpu", "replicas", "idle_unload", "min_free_vram_mib",
                "slot_save_path", "before_load", "mcp_config", "script"):
        assert key in off
        assert _field(key)["default_on"] is False


def _argv(on: tuple[str, ...] = (), **values) -> list[str]:
    b = backends.BACKENDS["strata"]
    profile = {
        "command": "",
        "values": {"config": "/models/strata-q2_0.json", "engine": "strata",
                   "host": "0.0.0.0", "port": "8080", **values},
        # on = GUI の項目別 ON/OFF (既定 OFF の項目はチェックして初めて送信される)
        "on": {f["key"]: f["default_on"] for f in b["fields"]} | dict.fromkeys(on, True),
        "extra_args": "",
    }
    return backends.render_argv(b, profile, python="py", binary="", root=ROOT)


def test_default_argv_launches_serve_server():
    argv = _argv()
    assert argv[:2] == ["py", str(ROOT / "module" / "strata" / "serve" / "server.py")]
    assert argv[argv.index("--config"):argv.index("--config") + 2] == \
        ["--config", "/models/strata-q2_0.json"]
    assert argv[argv.index("--engine") + 1] == "strata"
    assert argv[argv.index("--host") + 1] == "0.0.0.0"
    assert argv[argv.index("--port") + 1] == "8080"
    # 既定 OFF の項目はコマンドに一切出ない (config 側の設定を上書きしない)
    for flag in ("--gpu", "--tokenizer", "--replicas", "--idle-unload", "--lazy"):
        assert flag not in argv


def test_optional_flags_sent_when_filled():
    argv = _argv(on=("gpu", "replicas", "idle_unload"), gpu="0,2", replicas="2",
                 idle_unload="600", lazy="1")
    assert argv[argv.index("--gpu") + 1] == "0,2"
    assert argv[argv.index("--replicas") + 1] == "2"
    assert argv[argv.index("--idle-unload") + 1] == "600"
    assert "--lazy" in argv
    # 値を入れても ON にしなければ送信しない (strata-<model>.json の設定を上書きしない)
    assert "--gpu" not in _argv(gpu="0,2")


def test_port_resolution():
    b = backends.BACKENDS["strata"]
    assert b["default_port"] == "8080"
    assert backends.resolve_port(b, {"values": {}}, _argv()) == 8080


def test_detection_imports_serve_from_module_cwd():
    """serve は未 install のリポジトリ直下パッケージ → cwd+実 import でしか判定できない。"""
    b = backends.BACKENDS["strata"]
    assert b["kind"] == "python"
    assert b["check_module"] == "serve.server"
    assert b.get("check_cwd") is True
    assert b["module_path"] == "module/strata"
