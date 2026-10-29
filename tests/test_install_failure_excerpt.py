"""install step 失敗時、ジョブログ末尾に失敗出力の抜粋が複写されること。

UI はジョブログの冒頭 (プロキシ / uv / venv 行) を見せるため、原因となる
uv の `error:` 行がスクロールの向こうに埋もれていた (exit 2 レポート対応)。
失敗時に限り直前へ抜粋を置き、赤バナーの文言にも最初の error 行を入れる。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from llm_launcher.installer import InstallManager
from llm_launcher.store import JsonStore


def _manager(root: Path) -> InstallManager:
    root.mkdir(parents=True, exist_ok=True)
    return InstallManager(root, JsonStore(root / "data" / "installs.json", []))


def test_failure_tail_copied_and_message_names_error(tmp_path: Path):
    mgr = _manager(tmp_path)
    job = mgr._new_job("install", None, module_path="module/fork/freetoken",
                       clean=False, ref="", uv_path="", steps=["boom"])
    steps = [{"name": "boom", "cwd": str(tmp_path), "argv": [
        "bash", "-c",
        "echo 先頭の成功行; echo 'error: /x does not appear to be a Python project' >&2; exit 2"]}]
    asyncio.run(mgr._run_install(job, {}, steps))

    assert job["status"] == "failed"
    assert "exit 2" in job["error"]
    # 赤バナーに uv のエラー文そのものが載る (スクロール不要で原因が読める)
    assert "does not appear to be a Python project" in job["error"]
    assert "exit 2 は uv/pip が起動前に死ぬ類" in job["error"]
    # ログ末尾に抜粋セクションが複写されている
    joined = "\n".join(job["logs"])
    assert "----- boom 失敗時の出力末尾" in joined
    assert "does not appear to be a Python project" in joined


def test_failure_without_output_keeps_plain_message(tmp_path: Path):
    mgr = _manager(tmp_path)
    job = mgr._new_job("install", None, module_path="module/x", clean=False,
                       ref="", uv_path="", steps=["gone"])
    steps = [{"name": "gone", "cwd": str(tmp_path), "argv": ["/nonexistent/binary-xyz"]}]
    asyncio.run(mgr._run_install(job, {}, steps))
    assert job["status"] == "failed"
    assert "gone が失敗しました" in job["error"]
