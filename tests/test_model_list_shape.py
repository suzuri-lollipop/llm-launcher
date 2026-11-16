"""モデル一覧の「読める形」フィルタの回帰テスト (2026-11-11 報告)。

セットアップウィザードの「ダウンロード済みのモデルを使う」と編集画面の HF ピッカーは、
選択中バックエンドが読める形のモデルだけを出すべきところ、scan_hf_cache が
.basalt pack を has_weights (汎用重み) に数えていたため、basalt pack しか中身の無い
repo (jesdga/Qwen3.8-Flash-Next-Basalt 等) が FreeToken の一覧に漏れていた。
  - has_weights は scan_data_models と同じ基準 (.safetensors/.bin/.pt/.gguf)
  - .basalt は basalt リストでのみ公開 (basalt バックエンドはそこから選ぶ)
  - ウィザード / ピッカーは汎用重みの無い repo を他バックエンドに出さない
"""
from __future__ import annotations

from pathlib import Path

from llm_launcher.app import scan_data_models, scan_hf_cache

ROOT = Path(__file__).resolve().parents[1]
WIZARD_JS = (ROOT / "src" / "llm_launcher" / "static" / "setup.js").read_text("utf-8")
EDITOR_JS = (ROOT / "src" / "llm_launcher" / "static" / "js" / "views" / "editor.js").read_text("utf-8")


def _hf_repo(tmp_path: Path, name: str, files: dict[str, int]) -> Path:
    snap = tmp_path / f"models--{name.replace('/', '--')}" / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    for fname, size in files.items():
        (snap / fname).write_bytes(b"x" * size)
    return snap


def test_basalt_pack_is_not_a_general_weight(tmp_path: Path):
    """basalt pack のみの repo は has_weights=False (他バックエンドの一覧に出ない)。
    basalt リストには出る (basalt バックエンドはそちらから選ぶ)。"""
    _hf_repo(tmp_path, "jesdga/Qwen3.8-Flash-Next-Basalt",
             {"Qwen3.8-FN-NVFP4-DQ6_K.basalt": (1 << 20) + 1})
    m = scan_hf_cache(tmp_path)["models"][0]
    assert m["has_weights"] is False
    assert [f["name"] for f in m["basalt"]] == ["Qwen3.8-FN-NVFP4-DQ6_K.basalt"]


def test_scanners_agree_on_weights_criteria(tmp_path: Path):
    """HF キャッシュとデータフォルダで has_weights の基準が一致すること
    (.basalt はどちらでも汎用重みに数えない / safetensors は数える)。"""
    data = tmp_path / "data"
    (data / "org--Pack").mkdir(parents=True)
    (data / "org--Pack" / "m.basalt").write_bytes(b"x" * (2 << 20))
    (data / "org--Real").mkdir()
    (data / "org--Real" / "model.safetensors").write_bytes(b"x" * (2 << 20))
    out = {m["repo_id"]: m for m in scan_data_models(data)}
    assert out["org/Pack"]["has_weights"] is False
    assert out["org/Real"]["has_weights"] is True

    cache = tmp_path / "cache"
    _hf_repo(cache, "org/Pack", {"m.basalt": 2 << 20})
    _hf_repo(cache, "org/Real", {"model.safetensors": 2 << 20})
    out = {m["repo_id"]: m for m in scan_hf_cache(cache)["models"]}
    assert out["org/Pack"]["has_weights"] is False
    assert out["org/Real"]["has_weights"] is True


def test_wizard_local_list_uses_loadable_shape():
    # バックエンド別の形: basalt = .basalt / llama.cpp = gguf / 他 = 汎用重み
    assert 'const shape = W.backend === "basalt" ? "basalt" : (W.backend === "llamacpp" ? "gguf" : "");' in WIZARD_JS
    assert ": m.has_weights));" in WIZARD_JS          # 他バックエンドは汎用重みのみ
    assert 'shape === "basalt" ? (m.basalt || []).length' in WIZARD_JS  # basalt は pack から
    # pack 名の suffix は basalt バックエンドのときだけ (他で basalt 名を見せない)
    assert 'W.backend === "basalt" && (m.basalt || []).length' in WIZARD_JS


def test_editor_picker_skips_repos_without_general_weights():
    # vLLM / SGLang / FreeToken 系の DIR 指定は汎用重みのある repo だけ
    assert "if (!m.has_weights) continue;" in EDITOR_JS
