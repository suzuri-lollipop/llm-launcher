"""FreeToken (fork) の PLE swap バックエンド回帰テスト。

fork リポジトリ (module/fork/freetoken) が `--ple-backend swap` (チェックポイント
テーブルを read-only mmap し、OS page cache がホット行を保持する) を追加したので、
launcher 側は
  1. 正表 fields/freetoken-fork.json に fork ソースの choices が反映されている
  2. プロファイル編集画面の select 選択肢に swap が出る (基底 freetoken には出ない)
  3. ON のときだけ argv に付く (OFF はサーバー既定 disk に譲る)
必要がある。
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


def test_fork_table_has_swap_choice():
    spec = backends.load_table("freetoken-fork")["--ple-backend"]
    assert "swap" in spec["choices"]
    assert spec["default"] == "disk"


def test_fork_field_offers_swap_base_does_not():
    f = _field("freetoken-fork", "ple_backend")
    assert f["type"] == "select"
    assert f["options"] == ["", "pinned", "disk", "swap"]
    assert f["section"] == "cache"
    assert f["default_on"] is False                 # auto 導出項目は既定 OFF
    base = _field("freetoken", "ple_backend")       # upstream には swap が無い
    assert "swap" not in base["options"]


def _argv(*, on: bool = True, value: str = "swap") -> list[str]:
    b = backends.BACKENDS["freetoken-fork"]
    profile = {
        "command": "",
        "values": {"model_path": "/models/x", "host": "0.0.0.0", "port": "1919",
                   "ple_backend": value},
        "on": {"model_path": True, "host": True, "port": True, "ple_backend": on},
        "extra_args": "",
    }
    return backends.render_argv(b, profile, python="py", binary="", root=ROOT)


def test_ple_backend_sent_only_when_toggled_on():
    argv = _argv(on=True)
    i = argv.index("--ple-backend")
    assert argv[i:i + 2] == ["--ple-backend", "swap"]
    assert "--ple-backend" not in _argv(on=False)


@pytest.mark.parametrize("value", ["pinned", "disk", "swap"])
def test_ple_backend_accepts_table_choices(value: str):
    argv = _argv(on=True, value=value)
    assert argv[argv.index("--ple-backend") + 1] == value
