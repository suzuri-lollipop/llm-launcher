"""FreeToken (fork) の PLE バックエンド項目の回帰テスト。

fork が一時的に持っていた `--ple-backend swap` は fork / 上流ともに削除され、
現在の choices は pinned / disk / gpu (既定 disk。gpu は VRAM 常駐の fork 独自)。
ファイル名は当時のまま残すが、見ているのは現行仕様で
  1. 正表 fields/freetoken-fork.json が fork ソースの choices に追従している
     (choices が変わるとこのテストが赤くなり gen_fields 再生成を促す)
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
    assert spec["choices"] == ["pinned", "disk", "gpu"]
    assert spec["default"] == "disk"


def test_fork_kv_cache_dtype_follows_source_choices():
    # fork は静的スケール fp8 (TP 可) と upstream #894 の per-token-head 形式を併存させる。
    # 正表が source に追従しないと起動 JSON がサーバーの拒否する値で書かれる。
    # choices は canonical 名 (KV_FORMATS) が先、短縮スペル (KV_DTYPE_ALIASES) が後
    table = backends.load_table("freetoken-fork")
    spec = table["--kv-cache-dtype"]
    assert spec["choices"] == ["auto", "fp8_e4m3", "fp8_per_token_head", "int8_per_token_head",
                               "fp8", "fp8e4m3", "e4m3", "float8_e4m3fn", "int8"]
    assert spec["default"] == "auto"
    assert "--kv-cache-quant-scale" in table   # 静的 fp8 のスケール (fork 独自)


def test_fork_kv_field_options_match_table():
    f = _field("freetoken-fork", "kv_cache_dtype")
    assert f["type"] == "select"
    assert f["options"] == ["", "auto", "fp8_e4m3", "fp8_per_token_head", "int8_per_token_head",
                            "fp8", "fp8e4m3", "e4m3", "float8_e4m3fn", "int8"]
    assert f["section"] == "cache"
    assert f["default_on"] is False


def test_gen_fields_resolves_kv_registry_choices():
    # 生成器が *(name for name in KV_FORMATS if name != NONE.name) と *KV_DTYPE_ALIASES
    # の両方を解決できること (解決できないと choices が auto だけに痩せたり旧スペルが
    # UI から消えたりする)。CLI が受理する全スペル = canonical 名 + エイリアス。
    from llm_launcher.gen_fields import gen_freetoken
    tbl = gen_freetoken(ROOT, "fork/freetoken")
    assert tbl["--kv-cache-dtype"]["choices"] == [
        "auto", "fp8_e4m3", "fp8_per_token_head", "int8_per_token_head",
        "fp8", "fp8e4m3", "e4m3", "float8_e4m3fn", "int8",
    ]


def test_fork_field_options_match_table():
    f = _field("freetoken-fork", "ple_backend")
    assert f["type"] == "select"
    assert f["options"] == ["", "pinned", "disk", "gpu"]   # 空 = 送信しない (サーバー既定 disk)
    assert f["section"] == "cache"
    assert f["default_on"] is False                 # auto 導出項目は既定 OFF
    # swap は fork / 上流ともに削除済み (復活したら正表ごと更新する)
    assert "swap" not in f["options"]
    assert "swap" not in _field("freetoken", "ple_backend")["options"]
    # gpu (VRAM 常駐) は fork 独自 — upstream の選択肢には無い
    assert "gpu" not in _field("freetoken", "ple_backend")["options"]


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


@pytest.mark.parametrize("value", ["pinned", "disk", "gpu"])
def test_ple_backend_accepts_table_choices(value: str):
    argv = _argv(on=True, value=value)
    assert argv[argv.index("--ple-backend") + 1] == value


def _moe_argv(*, on: bool = True, value: str = "offload") -> list[str]:
    b = backends.BACKENDS["freetoken-fork"]
    profile = {
        "command": "",
        "values": {"model_path": "/models/x", "host": "0.0.0.0", "port": "1919",
                   "moe_strategy": value},
        "on": {"model_path": True, "host": True, "port": True, "moe_strategy": on},
        "extra_args": "",
    }
    return backends.render_argv(b, profile, python="py", binary="", root=ROOT)


def test_moe_strategy_sent_only_when_toggled_on():
    # ウィザードの「エキスパート配置」の回答も PLE と同じ送信規律 (OFF はエンジン既定 auto に譲る)
    argv = _moe_argv(on=True, value="fused")
    assert argv[argv.index("--moe-strategy") + 1] == "fused"
    assert "--moe-strategy" not in _moe_argv(on=False)


def test_launch_json_export_bakes_placement_choices(tmp_path: Path, monkeypatch):
    """ウィザード ③ の回答 (moe_strategy / ple_backend) は起動 JSON 再生成で argv に焼かれる。
    起動 JSON が起動の正なので、ここに書かれないとエンジンは既定 (auto / disk) で動く。"""
    import asyncio
    import json

    from test_auth_users import Client

    from llm_launcher import app as app_mod
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "module" / "fork" / "freetoken").mkdir(parents=True)
    (tmp_path / "data" / "profiles.json").write_text(json.dumps([
        {"id": "p_pl", "name": "ft placement", "backend": "freetoken-fork",
         "values": {"port": "1919", "model_path": "/m",
                    "moe_strategy": "fused", "ple_backend": "gpu"},
         "on": {"moe_strategy": True, "ple_backend": True}, "env": {}}]), "utf-8")
    monkeypatch.setattr(app_mod.ProcessManager, "resolve_python", lambda self, bid: "/fake/py")
    client = Client(app_mod.create_app(tmp_path))

    async def go():
        st, r = await client.post("/api/profiles/p_pl/launch-json")
        assert st == 200 and r["ok"], r
        doc = json.loads(Path(r["path"]).read_text("utf-8"))
        argv = doc["argv"]
        assert argv[argv.index("--moe-strategy") + 1] == "fused"
        assert argv[argv.index("--ple-backend") + 1] == "gpu"
    asyncio.run(go())


def test_launch_json_export_bakes_inkling_gpu_mode_and_kernel(tmp_path: Path, monkeypatch):
    """Inkling の複数 GPU 方式 / カーネル回答 (tp_size + env) も起動 JSON に焼かれる。
    layer split = --tp-size 2 + FREETOKEN_LAYER_SPLIT=1 / Vulkan = FREETOKEN_GGML_BACKEND=vulkan。"""
    import asyncio
    import json

    from test_auth_users import Client

    from llm_launcher import app as app_mod
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "module" / "fork" / "freetoken").mkdir(parents=True)
    (tmp_path / "data" / "profiles.json").write_text(json.dumps([
        {"id": "p_ik", "name": "Inkling-2-Mini-UD-Q8_K_XL", "backend": "freetoken-fork",
         "values": {"port": "1919", "model_path": "/m/model.gguf", "tp_size": "2"},
         "on": {"tp_size": True},
         "env": {"FREETOKEN_LAYER_SPLIT": "1", "FREETOKEN_GGML_BACKEND": "vulkan"}}]), "utf-8")
    monkeypatch.setattr(app_mod.ProcessManager, "resolve_python", lambda self, bid: "/fake/py")
    client = Client(app_mod.create_app(tmp_path))

    async def go():
        st, r = await client.post("/api/profiles/p_ik/launch-json")
        assert st == 200 and r["ok"], r
        doc = json.loads(Path(r["path"]).read_text("utf-8"))
        assert doc["argv"][doc["argv"].index("--tp-size") + 1] == "2"
        assert doc["env"]["FREETOKEN_LAYER_SPLIT"] == "1"
        assert doc["env"]["FREETOKEN_GGML_BACKEND"] == "vulkan"
    asyncio.run(go())
