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


def test_fork_kv_cache_dtype_follows_source_choices():
    # fork は静的スケール fp8 (TP 可) と upstream #894 の per-token-head 形式を併存させる。
    # 正表が source に追従しないと起動 JSON がサーバーの拒否する値で書かれる
    table = backends.load_table("freetoken-fork")
    spec = table["--kv-cache-dtype"]
    assert spec["choices"] == ["auto", "fp8", "int8", "fp8_e4m3", "fp8_per_token_head", "int8_per_token_head"]
    assert spec["default"] == "auto"
    assert "--kv-cache-quant-scale" in table   # 静的 fp8 のスケール (fork 独自)


def test_fork_kv_field_options_match_table():
    f = _field("freetoken-fork", "kv_cache_dtype")
    assert f["type"] == "select"
    assert f["options"] == ["", "auto", "fp8", "int8", "fp8_e4m3", "fp8_per_token_head", "int8_per_token_head"]
    assert f["section"] == "cache"
    assert f["default_on"] is False


def test_gen_fields_resolves_kv_registry_choices():
    # 生成器が *(name for name in KV_FORMATS if name != NONE.name) を解決できること
    # (解決できないと choices が auto だけに痩せ、量子化形式が UI から消える)
    from llm_launcher.gen_fields import gen_freetoken
    tbl = gen_freetoken(ROOT, "fork/freetoken")
    assert tbl["--kv-cache-dtype"]["choices"] == ["auto", "fp8", "int8", "fp8_e4m3", "fp8_per_token_head", "int8_per_token_head"]


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
