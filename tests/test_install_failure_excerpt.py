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


def test_carriage_return_progress_streams(tmp_path: Path, monkeypatch):
    """uv / hf の \r 進捗でログが固まらないこと (\n 区切りだけでなく \r でも行をFlush)。

    実機で「Downloading ... からログが更新されず、気づいたら done」になった原因。
    readline() だと \r だけの進捗が次の \n まで 1 行として溜め込まれる。
    """
    import llm_launcher.installer as mod
    monkeypatch.setattr(mod, "PROGRESS_MIN_INTERVAL", 0.0)   # テストでは間引き禁止
    mgr = _manager(tmp_path)
    job = mgr._new_job("install", None, module_path="module/x", clean=False,
                       ref="", uv_path="", steps=["prog"])
    steps = [{"name": "prog", "cwd": str(tmp_path), "argv": [
        "bash", "-c", "printf 'Downloading a (1%%)\\rDownloading b (99%%)\\nInstalled 124 packages\\n'"]}]
    asyncio.run(mgr._run_install(job, {}, steps))
    assert job["status"] == "done"
    logs = job["logs"]
    assert "Downloading b (99%)" in logs          # 進捗の最終形が行として立っている
    assert "Installed 124 packages" in logs
    assert not any("\r" in ln for ln in logs)     # \r 混じりの巨大 1 行になっていない


def test_live_log_cursor_survives_trim(tmp_path: Path, monkeypatch):
    """前方切り詰め後もカーソルが絶対行番号なので、ログが凍らない (実機の症状)。"""
    import llm_launcher.installer as mod
    monkeypatch.setattr(mod, "LOG_TAIL", 5)
    mgr = _manager(tmp_path)
    job = mgr._new_job("install", None)
    mgr.jobs[job["id"]] = job
    for i in range(1, 9):                         # L1..L8 → 窓は L4..L8 (base=3)
        mgr._append_log(job, f"L{i}")
    r = mgr.live_log(job["id"], 0)
    assert r["start"] == 3 and r["total"] == 8 and r["lines"] == ["L4", "L5", "L6", "L7", "L8"]
    r = mgr.live_log(job["id"], 6)                # 絶対 6 行目以降
    assert r["lines"] == ["L7", "L8"] and r["start"] == 6
    r = mgr.live_log(job["id"], 8)                # 追いつき → 空 (total は増え続ける)
    assert r["lines"] == [] and r["start"] == 8 and r["total"] == 8
    mgr._append_log(job, "L9")                    # 追記後も窓が動き凍結しない
    r = mgr.live_log(job["id"], r["start"])
    assert r["lines"] == ["L9"] and r["start"] == 8 and r["total"] == 9


def test_calibrate_live_log_cursor_survives_trim(tmp_path: Path, monkeypatch):
    """calibrate 側も同じ約束 (絶対カーソル + log_base)。"""
    import llm_launcher.calibrate as mod
    monkeypatch.setattr(mod, "LOG_TAIL", 3)
    from llm_launcher.calibrate import CalibrationManager
    mgr = CalibrationManager(tmp_path, JsonStore(tmp_path / "profiles.json", []),
                             JsonStore(tmp_path / "calibrations.json", []), None)
    job = {"id": "cal1", "logs": [], "started_at": 1}
    mgr.jobs["cal1"] = job
    for i in range(1, 6):                         # L1..L5 → 窓は L3..L5 (base=2)
        mgr._append_log(job, f"L{i}")
    r = mgr.live_log("cal1", 2)
    assert r["lines"] == ["L3", "L4", "L5"] and r["start"] == 2 and r["total"] == 5
    r = mgr.live_log("cal1", r["start"] + len(r["lines"]))
    assert r["lines"] == [] and r["total"] == 5
