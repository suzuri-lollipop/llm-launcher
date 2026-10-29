"""Project Maya バックエンド (module/maya) の起動引数回帰テスト。

Maya は maya.sh → maya.py (セットアップ+起動ラッパ) を起動するバックエンドで、
入口が既にインストール済みなら serve/server.py --engine strata --config maya-<model>.json
を対話なしで立てる。モデル本体の設定 (コンテキスト長・expert 設定など) は
setup が書く maya-<model>.json の args/env 側に入る。launcher 側は
  1. 正表 fields/maya.json が maya.py の argparse から生成されている
  2. 必須項目 (--host / --port) が常に argv へ出る
  3. config 側が既に持つ項目 (--config / --gpu / --parallel など) は既定 OFF = 送信しない
  4. 既定コマンドは maya.sh 経由 (module/maya/.venv のブートストラップを任せ、
     GUI .venv をセットアップの依存インストール (既定は uv pip) で汚さない)
必要がある。module/maya が未クローン (submodule 未 init) の環境では skip する。
"""
from pathlib import Path

import pytest

from llm_launcher import backends

ROOT = Path(__file__).resolve().parents[1]
MAYA_PY = ROOT / "module" / "maya" / "maya.py"

pytestmark = pytest.mark.skipif(
    not MAYA_PY.exists(),
    reason="module/maya が未クローン",
)


def _field(key: str) -> dict:
    return next(f for f in backends.BACKENDS["maya"]["fields"] if f["key"] == key)


def test_table_is_generated_from_maya_argparse():
    table = backends.load_table("maya")
    assert table["--backend"]["choices"] == ["cuda", "hip"]
    assert table["--port"]["type"] == "int"
    assert table["--parallel"]["type"] == "int"
    assert table["--calibrate"]["type"] == "bool"
    assert table["--env"]["type"] == "str"
    # default 無し (config / 実行時に決まる) は正表に default を書かない
    for flag in ("--host", "--api-key", "--config", "--gpu"):
        assert table[flag].get("default") in (None, "")


def test_required_fields_are_always_sent():
    for key in ("host", "port"):
        f = _field(key)
        assert f["required"] is True and f["default_on"] is True
    assert {f["key"] for f in backends.BACKENDS["maya"]["fields"] if f["required"]} == \
        {"host", "port"}


def test_config_side_options_default_off():
    off = backends.DEFAULT_OFF_KEYS["maya"]
    for key in ("config", "gpu", "gpus", "parallel", "vram_reserve_mib",
                "calibrate", "backend", "env"):
        assert key in off
        f = _field(key)
        if f["type"] != "bool":  # bool は値が truthy のときだけ送信 (ON/OFF チェックなし)
            assert f["default_on"] is False


def _argv(on: tuple[str, ...] = (), **values) -> list[str]:
    b = backends.BACKENDS["maya"]
    profile = {
        "command": "",
        "values": {"host": "0.0.0.0", "port": "8080", **values},
        # on = GUI の項目別 ON/OFF (既定 OFF の項目はチェックして初めて送信される)
        "on": {f["key"]: f["default_on"] for f in b["fields"]} | dict.fromkeys(on, True),
        "extra_args": "",
    }
    return backends.render_argv(b, profile, python="py", binary="", root=ROOT)


def test_default_argv_launches_maya_sh():
    argv = _argv()
    assert argv[:2] == ["sh", str(ROOT / "module" / "maya" / "maya.sh")]
    assert argv[argv.index("--host") + 1] == "0.0.0.0"
    assert argv[argv.index("--port") + 1] == "8080"
    # 既定 OFF の項目はコマンドに一切出ない (maya-<model>.json の設定を上書きしない)
    for flag in ("--config", "--gpu", "--gpus", "--parallel", "--backend",
                 "--calibrate", "--env", "--vram-reserve-mib"):
        assert flag not in argv


def test_optional_flags_sent_when_filled():
    argv = _argv(on=("config", "gpus", "parallel", "calibrate", "env"),
                 config="/models/maya-maya-m.json", gpus="0,1", parallel="4",
                 calibrate="1", env="STRATA_GLM_RAM_HEADROOM_GB=4")
    assert argv[argv.index("--config") + 1] == "/models/maya-maya-m.json"
    assert argv[argv.index("--gpus") + 1] == "0,1"
    assert argv[argv.index("--parallel") + 1] == "4"
    assert "--calibrate" in argv
    assert argv[argv.index("--env") + 1] == "STRATA_GLM_RAM_HEADROOM_GB=4"
    # 値を入れても ON にしなければ送信しない (maya-<model>.json の設定を上書きしない)
    assert "--gpus" not in _argv(gpus="0,1")


def test_port_resolution():
    b = backends.BACKENDS["maya"]
    assert b["default_port"] == "8080"
    assert backends.resolve_port(b, {"values": {}}, _argv()) == 8080


def test_detection_imports_serve_from_module_cwd():
    """serve は未 install のリポジトリ直下パッケージ → cwd+実 import でしか判定できない。"""
    b = backends.BACKENDS["maya"]
    assert b["kind"] == "python"
    assert b["check_module"] == "serve.server"
    assert b.get("check_cwd") is True
    assert b["module_path"] == "module/maya"
