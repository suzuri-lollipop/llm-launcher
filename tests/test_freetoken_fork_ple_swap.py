"""FreeToken (fork) の PLE バックエンド項目の回帰テスト。

fork が一時的に持っていた `--ple-backend swap` は fork / 上流ともに削除され、
現在の choices は pinned / disk (既定 disk)。ファイル名は当時のまま残すが、
見ているのは現行仕様で
  1. 正表 fields/freetoken-fork.json が fork ソースの choices に追従している
     (swap が復活しても消えてもこのテストが赤になり gen_fields 再生成を促す)
  2. プロファイル編集画面の select 選択肢が正表と一致する
  3. ON のときだけ argv に付く (OFF はサーバー既定 disk に譲る)
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


def test_fork_table_follows_source_choices():
    spec = backends.load_table("freetoken-fork")["--ple-backend"]
    assert spec["choices"] == ["pinned", "disk"]
    assert spec["default"] == "disk"


def test_fork_field_options_match_table():
    f = _field("freetoken-fork", "ple_backend")
    assert f["type"] == "select"
    assert f["options"] == ["", "pinned", "disk"]   # 空 = 送信しない (サーバー既定 disk)
    assert f["section"] == "cache"
    assert f["default_on"] is False                 # auto 導出項目は既定 OFF
    # swap は fork / 上流ともに削除済み (復活したら正表ごと更新する)
    assert "swap" not in f["options"]
    assert "swap" not in _field("freetoken", "ple_backend")["options"]


def _argv(*, on: bool = True, value: str = "disk") -> list[str]:
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
    assert argv[i:i + 2] == ["--ple-backend", "disk"]
    assert "--ple-backend" not in _argv(on=False)


@pytest.mark.parametrize("value", ["pinned", "disk"])
def test_ple_backend_accepts_table_choices(value: str):
    argv = _argv(on=True, value=value)
    assert argv[argv.index("--ple-backend") + 1] == value
