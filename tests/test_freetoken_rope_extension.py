"""FreeToken fork: 1人あたりコンテキストが trained 超えのとき YaRN rope が入る回帰テスト。

背景 (2026-11-18 報告): セットアップで 512K を選んでも --num-tokens (KV 総量) しか
書かれず、エンジンの 1 リクエスト上限 (rope テーブル = Flash-Next は trained 262144)
が伸びなかった。fork エンジンは --hf-overrides の YaRN rope_parameters でだけ拡張でき
(model card の 1M レシピ: factor 4 / original 262144)、Strata 純正 resolve_rope も
factor = 最終 / trained を導出する。launcher は 1人あたり ctx = num_tokens / 並列数 が
trained を超えたら、checkpoint の rope キー (rope_theta / mrope / partial_rotary_factor)
を保ったまま yarn を自動で足す必要がある。この導出/マージを外すと red になる。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from test_auth_users import Client

from llm_launcher import backends

ROOT = Path(__file__).resolve().parents[1]
SETUP_JS = (ROOT / "src" / "llm_launcher" / "static" / "setup.js").read_text("utf-8")
TRAINED = 262144
# RadixArk/Qwen3.8-Flash-Next-NVFP4 の config.json と同じ形 (trained 256K / default rope)
ROPE = {"mrope_interleaved": True, "mrope_section": [11, 11, 10],
        "partial_rotary_factor": 0.25, "rope_theta": 10000000, "rope_type": "default"}


def _checkpoint(tmp_path: Path, rope: dict | None = None, *, nested: bool = True,
                rope_key: str = "rope_parameters") -> str:
    text = {"max_position_embeddings": TRAINED,
            rope_key: dict(ROPE if rope is None else rope)}
    cfg = {"model_type": "qwen4_exp",
           "architectures": ["Qwen4ExpForConditionalGeneration"],
           **({"text_config": text} if nested else text)}
    d = tmp_path / "model"
    d.mkdir(exist_ok=True)
    (d / "config.json").write_text(json.dumps(cfg), "utf-8")
    return str(d)


def _render(values: dict, on: dict | None = None, *, bid: str = "freetoken-fork",
            root: Path = ROOT, custom_only: bool = False) -> list[str]:
    prof = {"command": "", "values": values, "on": on, "extra_args": "",
            "custom_only": custom_only}
    return backends.render_argv(backends.BACKENDS[bid], prof,
                                python="py", binary="", root=root)


def _rope_of(argv: list[str]) -> dict:
    return json.loads(argv[argv.index("--hf-overrides") + 1])["text_config"]["rope_parameters"]


def _vals(mp: str, total: str, par: str = "1", **extra) -> dict:
    return {"model_path": mp, "port": "1919", "num_tokens": total,
            "max_running_requests": par, **extra}


def test_512k_per_user_adds_yarn_factor_2(tmp_path: Path):
    argv = _render(_vals(_checkpoint(tmp_path), "524288"))
    rope = _rope_of(argv)
    assert rope["rope_type"] == "yarn" and rope["factor"] == 2.0
    assert rope["original_max_position_embeddings"] == TRAINED
    # --hf-overrides は section 以外を丸ごと置換する: checkpoint の rope キーを保つ
    assert rope["rope_theta"] == 10000000
    assert rope["mrope_section"] == [11, 11, 10] and rope["mrope_interleaved"] is True
    assert rope["partial_rotary_factor"] == 0.25


def test_per_user_is_total_over_parallel(tmp_path: Path):
    mp = _checkpoint(tmp_path)
    # 1M × 4並列 = num_tokens 4M → 1人あたり 1M → factor 4
    assert _rope_of(_render(_vals(mp, "4194304", "4")))["factor"] == 4.0
    # 512K総量 ÷ 4並列 = 1人あたり 128K → trained 内なので rope 不要
    assert "--hf-overrides" not in _render(_vals(mp, "524288", "4"))


@pytest.mark.parametrize("total", ["131072", "262144"])
def test_within_trained_has_no_override(tmp_path: Path, total: str):
    assert "--hf-overrides" not in _render(_vals(_checkpoint(tmp_path), total))


def test_scaled_checkpoint_is_left_alone(tmp_path: Path):
    yarn = {**ROPE, "rope_type": "yarn", "factor": 2.0,
            "original_max_position_embeddings": TRAINED}
    mp = _checkpoint(tmp_path, yarn)
    assert "--hf-overrides" not in _render(_vals(mp, "1048576"))


def test_nope_checkpoint_has_no_override(tmp_path: Path):
    # Inkling-2-Mini (inkling2_loop): NoPE = 位置情報は学習済み相対位置バイアスが運び
    # rope が無い。config に max_position_embeddings も無いので、1M を選んでも
    # YaRN を足さない (エンジンの上限は checkpoint の model_max_length 1M)
    cfg = {"model_type": "inkling2_loop",
           "text_config": {"model_type": "inkling2_loop_text",
                           "model_max_length": 1048576, "sliding_window_size": 1024}}
    d = tmp_path / "model"
    d.mkdir(exist_ok=True)
    (d / "config.json").write_text(json.dumps(cfg), "utf-8")
    assert "--hf-overrides" not in _render(_vals(str(d), "1048576"))


def test_flat_config_writes_top_level_rope(tmp_path: Path):
    argv = _render(_vals(_checkpoint(tmp_path, nested=False), "524288"))
    payload = json.loads(argv[argv.index("--hf-overrides") + 1])
    assert "text_config" not in payload
    assert payload["rope_parameters"]["factor"] == 2.0


def test_legacy_rope_scaling_key_is_kept(tmp_path: Path):
    # transformers < 5 系の flat rope_scaling checkpoint は同じキーへ書き戻す
    legacy = {"type": "default", "rope_theta": 1000000}
    mp = _checkpoint(tmp_path, legacy, rope_key="rope_scaling")
    argv = _render(_vals(mp, "524288"))
    payload = json.loads(argv[argv.index("--hf-overrides") + 1])
    rope = payload["text_config"]["rope_scaling"]
    assert "rope_parameters" not in payload["text_config"]
    assert rope["rope_type"] == "yarn" and rope["factor"] == 2.0
    assert "type" not in rope and rope["rope_theta"] == 1000000


@pytest.mark.parametrize("mp", ["", "/nonexistent-model-dir"])
def test_missing_model_is_safe(tmp_path: Path, mp: str):
    assert "--hf-overrides" not in _render(_vals(mp, "524288"), root=tmp_path)


def test_user_overrides_merge_but_rope_intent_wins(tmp_path: Path):
    mp = _checkpoint(tmp_path)
    # rope 以外のユーザー指定は保ってマージ
    argv = _render(_vals(mp, "524288",
                         hf_overrides='{"text_config": {"head_dim": 128}}'))
    payload = json.loads(argv[argv.index("--hf-overrides") + 1])
    assert payload["text_config"]["head_dim"] == 128
    assert payload["text_config"]["rope_parameters"]["factor"] == 2.0
    # rope の明示指定はユーザーが正 (黙って上書きしない)
    explicit = ('{"text_config": {"rope_parameters": {"rope_type": "yarn", '
                '"factor": 8.0, "original_max_position_embeddings": 262144}}}')
    argv = _render(_vals(mp, "524288", hf_overrides=explicit))
    assert argv[argv.index("--hf-overrides") + 1] == explicit
    # 解析できない JSON も触らない (エンジン側のエラーに任せる)
    argv = _render(_vals(mp, "524288", hf_overrides="{bad"))
    assert argv[argv.index("--hf-overrides") + 1] == "{bad"


def test_explicit_off_and_custom_only_skip(tmp_path: Path):
    vals = _vals(_checkpoint(tmp_path), "524288")
    assert "--hf-overrides" not in _render(vals, on={"hf_overrides": False})
    assert "--hf-overrides" not in _render(vals, custom_only=True)


def test_upstream_freetoken_has_no_hf_overrides(tmp_path: Path):
    # upstream の正表に --hf-overrides は無い (fork だけの拡張)
    assert "--hf-overrides" not in _render(_vals(_checkpoint(tmp_path), "524288"),
                                           bid="freetoken")


def test_launch_json_mode_keeps_json_canonical(tmp_path: Path):
    # 起動 JSON 主体モードは JSON が正: values の num_tokens から rope を足さない
    cfg = tmp_path / "launch.json"
    cfg.write_text(json.dumps({"python": "py", "argv": ["-m", "freetoken",
                   "--model-path", "/m", "--port", "1919"], "env": {}}), "utf-8")
    vals = _vals(_checkpoint(tmp_path), "524288", config=str(cfg))
    assert "--hf-overrides" not in _render(vals)


def test_export_endpoint_bakes_yarn_into_launch_json(tmp_path: Path, monkeypatch):
    from llm_launcher import app as app_mod
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "module" / "fork" / "freetoken").mkdir(parents=True)
    mp = _checkpoint(tmp_path)
    (tmp_path / "data" / "profiles.json").write_text(json.dumps([
        {"id": "p_rope", "name": "ft rope", "backend": "freetoken-fork",
         "values": {"port": "1919", "model_path": mp, "num_tokens": "1048576",
                    "max_running_requests": "1"},
         "on": {"num_tokens": True, "max_running_requests": True}, "env": {}}]), "utf-8")
    monkeypatch.setattr(app_mod.ProcessManager, "resolve_python", lambda self, bid: "/fake/py")
    client = Client(app_mod.create_app(tmp_path))

    async def go():
        st, r = await client.post("/api/profiles/p_rope/launch-json")
        assert st == 200 and r["ok"], r
        doc = json.loads(Path(r["path"]).read_text("utf-8"))
        assert doc["argv"][doc["argv"].index("--num-tokens") + 1] == "1048576"
        rope = _rope_of(doc["argv"])
        assert rope["rope_type"] == "yarn" and rope["factor"] == 4.0
        assert rope["original_max_position_embeddings"] == TRAINED
        # 再生成 (editor の再生成ボタンも同じ endpoint) で二重に積まれない
        st, r2 = await client.post("/api/profiles/p_rope/launch-json")
        assert st == 200 and r2["ok"], r2
        doc2 = json.loads(Path(r2["path"]).read_text("utf-8"))
        assert doc2["argv"].count("--hf-overrides") == 1
        assert _rope_of(doc2["argv"])["factor"] == 4.0
    asyncio.run(go())


def test_wizard_ctx_hint_mentions_auto_yarn():
    assert "モデルの学習長を超える選択ではYaRN (rope scaling) を自動設定します" in SETUP_JS
