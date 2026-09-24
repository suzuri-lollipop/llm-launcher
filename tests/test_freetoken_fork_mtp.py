"""FreeToken (fork) バックエンドのプロファイル項目 — MTP 投機的デコード回帰テスト。

fork リポジトリ (module/fork/freetoken) が `--speculative mtp` (MTP head による
先読みデコード) を追加したので、launcher 側は
  1. 正表 fields/freetoken-fork.json に fork ソースの定義が反映されている
  2. プロファイル編集画面の項目 (select / exec セクション / 既定 OFF) に出る
  3. ON のときだけ argv に付く (OFF はサーバー既定に譲る)
必要がある。基底 FreeToken (upstream) には存在しないフラグであることも確認する。
"""
from pathlib import Path

import pytest

from llm_launcher import backends

ROOT = Path(__file__).resolve().parents[1]
FORK = ROOT / "module" / "fork" / "freetoken"

pytestmark = pytest.mark.skipif(
    not (FORK / "python" / "freetoken" / "server" / "args.py").exists(),
    reason="module/fork/freetoken が未クローン",
)


def _field(bid: str, key: str) -> dict:
    return next(f for f in backends.BACKENDS[bid]["fields"] if f["key"] == key)


def test_fork_table_has_speculative_from_source():
    table = backends.load_table("freetoken-fork")
    spec = table["--speculative"]
    assert spec["choices"] == ["none", "mtp"]
    assert spec["default"] == "none"


def test_speculative_field_exposed_only_on_fork():
    f = _field("freetoken-fork", "speculative")
    assert f["flag"] == "--speculative"
    assert f["type"] == "select"
    assert f["options"] == ["", "none", "mtp"]     # 空 = 送信しない (サーバー既定 none)
    assert f["section"] == "exec"
    assert f["default_on"] is False                 # auto 導出項目は既定 OFF
    assert "speculative" in backends.DEFAULT_OFF_KEYS["freetoken-fork"]
    # upstream FreeToken にはまだ無い項目
    assert "--speculative" not in backends.load_table("freetoken")
    assert all(x["key"] != "speculative" for x in backends.BACKENDS["freetoken"]["fields"])


def _argv(*, on: bool = True, value: str = "mtp") -> list[str]:
    b = backends.BACKENDS["freetoken-fork"]
    profile = {
        "command": "",
        "values": {"model_path": "/models/x", "host": "0.0.0.0", "port": "1919",
                   "speculative": value},
        "on": {"model_path": True, "host": True, "port": True, "speculative": on},
        "extra_args": "",
    }
    return backends.render_argv(b, profile, python="py", binary="", root=ROOT)


def test_speculative_sent_only_when_toggled_on():
    argv = _argv(on=True)
    i = argv.index("--speculative")
    assert argv[i:i + 2] == ["--speculative", "mtp"]
    assert "--speculative" not in _argv(on=False)


@pytest.mark.parametrize("value", ["none", "mtp"])
def test_speculative_accepts_table_choices(value: str):
    argv = _argv(on=True, value=value)
    assert argv[argv.index("--speculative") + 1] == value
