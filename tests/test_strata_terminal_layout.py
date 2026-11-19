"""Strata 0.2.16 の terminal/ レイアウト移行の回帰テスト。

upstream 0.2.16 は Terminal 部分 (setup.py / strata.py / serve / requirements.txt /
.venv / engine / config JSON) をリポジトリ直下から terminal/ へ移した。launcher は
strata_setup.setup_dir で実 location を解決し、旧レイアウト (古い clone / fork) も
そのまま動く。見張る対象:
  1. setup_dir の解決 (新 / 旧 / terminal 自身を渡す冪等性)
  2. installed_configs / build_argv が terminal/ の config / setup.py を読む
  3. 起動コマンド (backend_for) と python 候補が terminal/ に寄る
  4. install ジョブが旧直下 .venv を terminal/ へ移す (純正 setup.sh と同じ一度きり)
  5. migrate_terminal_layout: 専用コピー / installer config / プロファイルの
     values.config の旧絶対パスを新 location へ書き換える (実在するものだけ・冪等)
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from llm_launcher import installer, strata_setup
from llm_launcher.backends import backend_for, python_candidates
from llm_launcher.installer import INSTALL_SPECS, InstallManager
from llm_launcher.store import JsonStore


def _new_layout(root: Path) -> Path:
    """0.2.16 形: terminal/ に setup.py / serve が在る。"""
    mod = root / "module" / "strata"
    term = mod / "terminal"
    (term / "serve").mkdir(parents=True)
    (term / "serve" / "server.py").write_text("# server\n", encoding="utf-8")
    (term / "setup.py").write_text("# setup\n", encoding="utf-8")
    return mod


def _old_layout(root: Path) -> Path:
    mod = root / "module" / "strata"
    (mod / "serve").mkdir(parents=True)
    (mod / "serve" / "server.py").write_text("# server\n", encoding="utf-8")
    (mod / "setup.py").write_text("# setup\n", encoding="utf-8")
    return mod


# ---------------------------------------------------------------- setup_dir

def test_setup_dir_resolves_both_layouts(tmp_path: Path):
    new = _new_layout(tmp_path)
    assert strata_setup.setup_dir(new) == new / "terminal"
    # terminal/ 自身を渡しても同じ (冪等)
    assert strata_setup.setup_dir(new / "terminal") == new / "terminal"
    old = _old_layout(tmp_path / "old")
    assert strata_setup.setup_dir(old) == old
    # 未 clone はそのまま (入口ガード側で available:False)
    assert strata_setup.setup_dir(tmp_path / "none") == tmp_path / "none"


def test_installed_configs_reads_terminal(tmp_path: Path):
    mod = _new_layout(tmp_path)
    (mod / "terminal" / "strata-q2_0.json").write_text(
        json.dumps({"exe": "e", "args": [], "model_name": "m"}), encoding="utf-8")
    # 旧直下に取り残された config は拾わない (本体は terminal/ が正)
    (mod / "strata-stale.json").write_text(
        json.dumps({"exe": "e", "args": []}), encoding="utf-8")
    names = [c["name"] for c in strata_setup.installed_configs(mod)]
    assert names == ["strata-q2_0.json"]


def test_build_argv_with_terminal_setup_py(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mod = _new_layout(tmp_path)
    tables = {"available": True,
              "families": {"qwen": {"tag": "", "multi_gpu": True,
                                    "models": {"Q2_0": {}}}},
              "contexts": [8192, 131072], "pc": {"gpus": []}}
    monkeypatch.setattr(strata_setup, "_CACHE",
                        {str(mod): (time.monotonic(), tables),
                         str(mod.resolve()): (time.monotonic(), tables)})
    argv, cfg = strata_setup.build_argv(mod, {"family": "qwen", "model": "Q2_0",
                                              "context": 131072})
    assert argv[:2] == ["setup.py", "--family"] and cfg == "strata-q2_0.json"
    # setup.py が無い (未 clone) は従来どおり拒否
    with pytest.raises(ValueError, match="未取得"):
        strata_setup.build_argv(tmp_path / "none", {"family": "qwen", "model": "Q2_0",
                                                    "context": 131072})


# ---------------------------------------------------------------- 起動コマンド / python

def test_backend_for_command_and_python_candidates(tmp_path: Path):
    mod = _new_layout(tmp_path)
    b = backend_for(tmp_path, "strata")
    assert b["default_command"].endswith("module/strata/terminal/serve/server.py")
    assert b["module_path"] == "module/strata"     # git / submodule 基準は直下のまま
    # terminal/.venv が在れば最優先候補
    vp = mod / "terminal" / ".venv" / "bin"
    vp.mkdir(parents=True)
    (vp / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    cands = python_candidates(tmp_path, "module/strata")
    assert cands[0][1] == str(vp / "python")
    # 旧レイアウトは従来どおり直下 serve / .venv
    old_root = tmp_path / "old"
    _old_layout(old_root)
    b2 = backend_for(old_root, "strata")
    assert b2["default_command"].endswith("module/strata/serve/server.py")
    assert "/terminal/" not in b2["default_command"]


# ---------------------------------------------------------------- install: .venv 移行

def test_install_moves_old_venv_into_terminal(monkeypatch, tmp_path: Path):
    """旧直下 .venv は terminal/ へ一度きり移す (純正 setup.sh と同じ)。
    venv 作成 / 各 step はスタブ — 移行と plan の cwd だけ見る。"""
    mod = _new_layout(tmp_path)
    oldbin = mod / ".venv" / "bin"
    oldbin.mkdir(parents=True)
    (oldbin / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (mod / ".venv" / "pyvenv.cfg").write_text("home = /x\n", encoding="utf-8")

    seen: dict[str, object] = {}

    def fake_venv(self, job, module_dir):  # noqa: ANN001
        seen["venv_dir"] = str(module_dir)
        return ".venv は既存です"

    async def fake_exec(self, argv, cwd):  # noqa: ANN001
        return 1, ""                      # precheck 失敗 = 導入続行

    async def fake_step(self, job, step):  # noqa: ANN001
        seen.setdefault("steps", []).append(step["name"])
        return 0, ""

    monkeypatch.setattr(InstallManager, "_venv_ensure", fake_venv)
    monkeypatch.setattr(InstallManager, "_exec", fake_exec)
    monkeypatch.setattr(InstallManager, "_run_step", fake_step)
    (tmp_path / "data").mkdir(parents=True)
    mgr = InstallManager(tmp_path, JsonStore(tmp_path / "data" / "installs.json", []))

    async def go() -> None:
        job = await mgr.start_install("strata")
        await mgr._tasks[job["id"]]

    asyncio.run(go())
    assert seen["venv_dir"] == str(mod / "terminal")
    assert not (mod / ".venv").exists()
    assert (mod / "terminal" / ".venv" / "bin" / "python").is_file()
    assert seen["steps"] == ["tools", "verify"]


# ---------------------------------------------------------------- config パス移行

def _write_cfg(p: Path, doc: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def test_migrate_terminal_layout_rewrites_stale_paths(tmp_path: Path):
    mod = _new_layout(tmp_path)
    term = mod / "terminal"
    # 新 location に実在するエンジン / データ / ログ先
    (term / "engine").mkdir()
    (term / "engine" / "strata").write_bytes(b"#!/bin/sh\n")
    (term / "data").mkdir()
    (term / "data" / "expert-profile.bin").write_bytes(b"x")
    # 旧 location は消えている (移行済み) — exe / args は書き換え対象
    old_exe = str(mod / "engine" / "strata")
    old_prof = str(mod / "data" / "expert-profile.bin")
    doc = {"exe": old_exe,
           "args": ["--expert-profile", old_prof, "--max-context", "131072"],
           "args_written": ["--expert-profile", old_prof],
           "cwd": str(mod),                       # 直下は実在するので触らない
           "log": str(mod / "strata-x.log"),      # 出力パスは未作成でも移す
           "vision": {"exe": old_exe},
           "tokenizer": "/data/tok"}              # module 外は触らない
    _write_cfg(term / "strata-x.json", doc)
    copy = tmp_path / "data" / "launch" / "strata-x-uuid8.json"
    _write_cfg(copy, dict(doc))
    profiles = [{"id": "p1", "backend": "strata",
                 "values": {"config": str(mod / "strata-x.json")}},   # 旧 installer config パス
                {"id": "p2", "backend": "strata",
                 "values": {"config": str(copy)}}]                    # 実在する専用コピーは触らない
    n_cfg, n_prof = strata_setup.migrate_terminal_layout(tmp_path, mod, profiles)
    assert n_cfg == 2 and n_prof == 1
    assert profiles[0]["values"]["config"] == str(term / "strata-x.json")
    assert profiles[1]["values"]["config"] == str(copy)
    for f in (term / "strata-x.json", copy):
        got = json.loads(f.read_text(encoding="utf-8"))
        assert got["exe"] == str(term / "engine" / "strata")
        assert got["args"][1] == str(term / "data" / "expert-profile.bin")
        assert got["args_written"][1] == str(term / "data" / "expert-profile.bin")
        assert got["args"][3] == "131072"          # 数値引数は触らない
        assert got["cwd"] == str(mod)
        assert got["log"] == str(term / "strata-x.log")
        assert got["vision"]["exe"] == str(term / "engine" / "strata")
        assert got["tokenizer"] == "/data/tok"
    # 冪等: 2 回目は無変更
    assert strata_setup.migrate_terminal_layout(tmp_path, mod, profiles) == (0, 0)


def test_migrate_terminal_layout_noop_on_old_layout(tmp_path: Path):
    mod = _old_layout(tmp_path)
    _write_cfg(mod / "strata-x.json", {"exe": str(mod / "engine" / "strata"), "args": []})
    profiles = [{"id": "p1", "backend": "strata", "values": {"config": str(mod / "strata-x.json")}}]
    assert strata_setup.migrate_terminal_layout(tmp_path, mod, profiles) == (0, 0)


def test_migrate_keeps_paths_that_still_exist(tmp_path: Path):
    """旧 location のファイルがまだ実在する (移行途中) なら書き換えない —
    実在するものだけ動かす (純正が移行済みの config を壊さない)。"""
    mod = _new_layout(tmp_path)
    term = mod / "terminal"
    (term / "engine").mkdir()
    (term / "engine" / "strata").write_bytes(b"new")
    (mod / "engine").mkdir()                    # 旧エンジンもまだ在る
    (mod / "engine" / "strata").write_bytes(b"old")
    _write_cfg(term / "strata-x.json", {"exe": str(mod / "engine" / "strata"), "args": []})
    n_cfg, _ = strata_setup.migrate_terminal_layout(tmp_path, mod, [])
    assert n_cfg == 0
    got = json.loads((term / "strata-x.json").read_text(encoding="utf-8"))
    assert got["exe"] == str(mod / "engine" / "strata")
