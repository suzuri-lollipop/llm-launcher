"""セットアップ画面 ② の「対応モデルから選択」の回帰テスト。

出し元は clone 内の docs/models.md (known-good 表) で、ランチャー側にモデル表を写さない
(フォークの docs を追従する)。ここで見張るのは
  1. parse_supported_models — 表から repo id を拾い、Image input 表 (HF リンクなし) や
     見出し / 区切り行を混ぜない。同一 repo の重複は 1 件に寄せる
  2. supported_models — 未 clone / 文書なしは空、mtime が動けば再読込、
     fork の大文字 clone (FreeToken) も backend_for 経由で拾う
  3. API — /api/installs の installable が supported_models を出す
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from test_auth_users import Client

from llm_launcher import app as app_mod
from llm_launcher.backends import (BACKENDS, backend_for, parse_supported_models,
                                   supported_models)

ROOT = Path(__file__).resolve().parent.parent
FORK_DOC = ROOT / "module" / "fork" / "freetoken" / "docs" / "models.md"

DOC = """# Supported models

| Model | HF checkpoints |
|---|---|
| DeepSeek-V4.1-Flash | [deepseek-ai/DeepSeek-V4.1-Flash](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash) |
| Qwen3.8-Flash-Next | [Qwen/Qwen3.8-Flash-Next-FP8](https://huggingface.co/Qwen/Qwen3.8-Flash-Next-FP8), [RadixArk/Qwen3.8-Flash-Next-NVFP4](https://huggingface.co/RadixArk/Qwen3.8-Flash-Next-NVFP4) |
| Qwen3.6 / Qwen3.5 MoE | [Qwen/Qwen3.6-35B-A3B](https://huggingface.co/Qwen/Qwen3.6-35B-A3B) ([-FP8](https://huggingface.co/Qwen/Qwen3.6-35B-A3B-FP8)) |

### Image input

| Family | Image tokens | `--image-min-tokens` / `--image-max-tokens` |
|---|---|---|
| Qwen3-VL | 256 / 1024 | 128 / 1024 |
"""


def _write_doc(root: Path, text: str, name: str = "freetoken") -> Path:
    d = root / "module" / "fork" / name / "docs"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "models.md"
    p.write_text(text, "utf-8")
    return p


# ------------------------------------------------------------------- パーサ

def test_parse_known_good_table():
    out = parse_supported_models(DOC)
    repos = [m["repo_id"] for m in out]
    assert repos[:3] == ["deepseek-ai/DeepSeek-V4.1-Flash",
                         "Qwen/Qwen3.8-Flash-Next-FP8",
                         "RadixArk/Qwen3.8-Flash-Next-NVFP4"]
    # 行内の 2 件目リンク (リンクテキストが "-FP8" でも URL から拾う)
    assert "Qwen/Qwen3.6-35B-A3B-FP8" in repos
    # Image input 表 (HF リンクなし) と見出し / 区切り行は混ざらない
    assert all("/" in m["repo_id"] and "huggingface" not in m["repo_id"] for m in out)
    assert {m["name"] for m in out if m["repo_id"].startswith("Qwen/Qwen3.6")} == {"Qwen3.6 / Qwen3.5 MoE"}


def test_parse_dedupes_and_keeps_order():
    text = ("| Model | HF checkpoints |\n|---|---|\n"
            "| A | [x/a](https://huggingface.co/x/a) |\n"
            "| B | [x/a](https://huggingface.co/X/A), [y/b](https://huggingface.co/y/b/tree/main) |\n")
    out = parse_supported_models(text)
    # 大小文字の違いも含め同一 repo は最初の 1 件だけ。/tree/... は org 名までが repo id
    assert [m["repo_id"] for m in out] == ["x/a", "y/b"]
    assert out[0]["name"] == "A"


@pytest.mark.skipif(not FORK_DOC.is_file(), reason="module/fork/freetoken が未 clone")
def test_parse_real_fork_doc():
    out = parse_supported_models(FORK_DOC.read_text("utf-8", errors="replace"))
    repos = {m["repo_id"] for m in out}
    assert "Qwen/Qwen3.8-Flash-Next-FP8" in repos
    assert "deepseek-ai/DeepSeek-V4.1-Flash" in repos
    # Image input 表の Family 行は混ざらない (その表に HF リンクはない)
    assert len(out) >= 10


# ------------------------------------------------------------ supported_models

def test_supported_models_empty_without_clone(tmp_path: Path):
    assert supported_models(tmp_path, BACKENDS["freetoken-fork"]) == []
    _write_doc(tmp_path, DOC, name="FreeToken")     # clone 既定の大文字ディレクトリ
    out = supported_models(tmp_path, backend_for(tmp_path, "freetoken-fork"))
    assert [m["repo_id"] for m in out][:1] == ["deepseek-ai/DeepSeek-V4.1-Flash"]


def test_supported_models_reflects_doc_update(tmp_path: Path):
    p = _write_doc(tmp_path, DOC)
    b = backend_for(tmp_path, "freetoken-fork")
    assert len(supported_models(tmp_path, b)) == 5
    p.write_text(DOC + "| New | [z/n](https://huggingface.co/z/n) |\n", "utf-8")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns + 10**9, st.st_mtime_ns + 10**9))   # 同一実行内の mtime 進み
    out = supported_models(tmp_path, b)
    assert out[-1] == {"name": "New", "repo_id": "z/n"}


def test_supported_models_absent_for_backend_without_key():
    assert supported_models(Path("/nonexistent"), BACKENDS["strata-fork"]) == []
    assert "model_list_doc" not in BACKENDS["strata-fork"]


# ------------------------------------------------------------------------- API

def test_api_installable_exposes_supported_models(tmp_path: Path):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    _write_doc(tmp_path, DOC)
    client = Client(app_mod.create_app(tmp_path))

    async def go() -> None:
        st, res = await client.get("/api/installs")
        assert st == 200
        fork = next(e for e in res["installable"] if e["id"] == "freetoken-fork")
        assert {"name": "DeepSeek-V4.1-Flash",
                "repo_id": "deepseek-ai/DeepSeek-V4.1-Flash"} in fork["supported_models"]
        # 未 clone の上流 FreeToken は空一覧 (キー自体は常に存在 = UI が分岐しやすい)
        upstream = next(e for e in res["installable"] if e["id"] == "freetoken")
        assert upstream["supported_models"] == []

    asyncio.run(go())
