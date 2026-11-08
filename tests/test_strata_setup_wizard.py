"""Strata セットアップタブ (📦) 統合の回帰テスト。

Strata の純正 setup.sh / setup.py は対話インストーラなので、ランチャー UI は機械部分を
引き受ける:
  clone (submodule 取得済みならスキップ) → .venv → pip install -r requirements.txt
  → engine step (薄い driver strata_engine.py が純正 get_prebuilt() で既製エンジンを受信)
  → verify (import serve.server + engine/strata 実行可)。
見張る対象:
  1. spec の形 (requirements / venv_pip / engine_fetch / engine_bin)
  2. start_install の計画 — venv_pip spec は uv が在っても pip で計画する (純正 pip_install
     が .venv の -m pip を前提にするため。表示 tool と argv のズレも許さない)
  3. engine step が strata_engine.py を submodule cwd で plan に入ること、
     precheck / verify が engine/strata の実在も要求すること
  4. driver の入口ガード (cwd 違い / setup.py が純正でない) は正直に失敗すること
  5. /api/installs の installable が strata を engine_fetch / tool=pip で出すこと
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from llm_launcher import installer
from llm_launcher.installer import INSTALL_SPECS, InstallManager
from llm_launcher.store import JsonStore

ROOT = Path(__file__).resolve().parents[1]
SPEC = INSTALL_SPECS["strata"]
DRIVER = ROOT / "src" / "llm_launcher" / "strata_engine.py"


def test_spec_shape():
    assert SPEC["clone"] == "https://github.com/Niko1221/Strata"
    assert SPEC["module_path"] == "module/strata"
    # 検出 (serve.server 実 import) と同じ確認を使い、エンジン実在も verify で要求する
    assert SPEC["check"] == "serve.server"
    assert SPEC["requirements"] == "requirements.txt"
    assert SPEC["venv_pip"] is True
    assert SPEC["engine_fetch"] is True
    assert SPEC["engine_bin"] == "engine/strata"


def _manager(root: Path) -> InstallManager:
    (root / "data").mkdir(parents=True, exist_ok=True)
    return InstallManager(root, JsonStore(root / "data" / "installs.json", []))


def _plan(monkeypatch, root: Path) -> dict:
    """start_install の計画だけ取る (_run_install を殺し subprocess も venv も作らない)。"""
    got: dict[str, object] = {}

    async def fake(self, job, spec, steps):  # noqa: ANN001
        got["job"] = dict(job)
        got["steps"] = [dict(s) for s in steps]

    monkeypatch.setattr(InstallManager, "_run_install", fake)
    # uv は見えている — それでも venv_pip spec (Strata) は pip で計画しなければいけない
    monkeypatch.setattr(installer, "find_uv", lambda: "/usr/bin/uv")
    mgr = _manager(root)

    async def go() -> None:
        await mgr.start_install("strata")
        await asyncio.sleep(0)

    asyncio.run(go())
    return got


def _submodule(root: Path) -> Path:
    mod = root / "module" / "strata"
    mod.mkdir(parents=True)
    (mod / "serve").mkdir()
    (mod / "serve" / "server.py").write_text("# clone content\n", encoding="utf-8")
    return mod


def test_plan_for_existing_submodule_skips_clone(monkeypatch, tmp_path: Path):
    mod = _submodule(tmp_path)
    got = _plan(monkeypatch, tmp_path)
    steps = got["steps"]
    names = [s["name"] for s in steps]
    assert names == ["venv", "precheck", "install", "engine", "verify"]
    # 純正 setup.py の pip_install が -m pip を前提にする = uv が在っても pip で組む
    assert got["job"]["tool"] == "pip"
    assert got["job"]["uv_path"] == ""
    venvpy = str((mod / ".venv" / "bin" / "python").resolve())
    install = next(s for s in steps if s["name"] == "install")
    assert install["argv"] == [venvpy, "-m", "pip", "install", "--progress-bar", "off",
                               "-r", "requirements.txt"]
    # engine step: 薄い driver を submodule cwd で .venv python 実行
    eng = next(s for s in steps if s["name"] == "engine")
    assert eng["argv"] == [venvpy, str(DRIVER)]
    assert eng["cwd"].endswith("module/strata")
    # precheck / verify は import だけでなくエンジン実在も要求する
    for s in steps:
        if s["name"] in ("precheck", "verify"):
            assert "serve.server" in s["argv"][2]
            assert "engine/strata" in s["argv"][2]


def test_plan_without_clone_starts_with_git_clone(monkeypatch, tmp_path: Path):
    got = _plan(monkeypatch, tmp_path)
    steps = got["steps"]
    assert steps[0]["name"] == "clone"
    assert steps[0]["argv"][:3] == ["git", "clone", "--depth"]
    assert steps[0]["argv"][-2:] == [SPEC["clone"],
                                     str((tmp_path / "module" / "strata").resolve())]


def test_freetoken_plan_still_prefers_uv(monkeypatch, tmp_path: Path):
    """venv_pip は strata だけの特殊化: 他の spec (freetoken) は uv が在れば uv pip のまま。"""
    monkeypatch.setattr(installer, "find_uv", lambda: "/usr/bin/uv")
    got: dict[str, object] = {}

    async def fake(self, job, spec, steps):  # noqa: ANN001
        got["job"] = dict(job)
        got["steps"] = [dict(s) for s in steps]

    monkeypatch.setattr(InstallManager, "_run_install", fake)
    (tmp_path / "data").mkdir(parents=True)
    mgr = InstallManager(tmp_path, JsonStore(tmp_path / "data" / "installs.json", []))

    async def go() -> None:
        await mgr.start_install("freetoken")
        await asyncio.sleep(0)

    asyncio.run(go())
    assert got["job"]["tool"] == "uv"
    install = next(s for s in got["steps"] if s["name"] == "install")
    assert install["argv"][0] == "/usr/bin/uv" and install["argv"][1:3] == ["pip", "install"]


# ---------------------------------------------------------------- driver (strata_engine.py)

def test_driver_refuses_foreign_cwd(tmp_path: Path):
    r = subprocess.run([sys.executable, str(DRIVER)], cwd=tmp_path,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert "cwd" in r.stdout


def test_driver_fails_honestly_when_setup_py_is_not_stratas(tmp_path: Path):
    (tmp_path / "serve").mkdir()
    (tmp_path / "serve" / "server.py").write_text("", encoding="utf-8")
    (tmp_path / "setup.py").write_text("# 純正 setup.py ではない空ファイル\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(DRIVER)], cwd=tmp_path,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert r.stdout.startswith("strata_engine:")


# ---------------------------------------------------------------- API / ウィザード文言

def test_installs_api_exposes_strata_with_engine_fetch(tmp_path: Path):
    from fastapi.testclient import TestClient

    from llm_launcher import app as app_mod

    (tmp_path / "data").mkdir(parents=True)
    client = TestClient(app_mod.create_app(tmp_path))
    entries = client.get("/api/installs").json()["installable"]
    ent = next(e for e in entries if e["id"] == "strata")
    assert ent["name"] == "Strata"
    assert ent["requirements"] == "requirements.txt"
    assert ent["engine_fetch"] is True and ent["cmake"] is False
    # /api/installs の tool (グローバル) にかかわらず、この entry の tool は pip
    assert ent["tool"] == "pip"


def test_wizard_copy_mentions_engine_and_strata_next_step():
    import llm_launcher

    js = (Path(llm_launcher.__file__).parent / "static" / "setup.js").read_text("utf-8")
    assert "エンジンを受信" in js          # step chip 名 (engine)
    assert "engine_fetch" in js            # 確認画面の手順組み立て
    assert "INSTALL_NOTES" in js           # 完了後の setup.sh 追い書き
    assert "setup.sh" in js
