"""Strata セットアップタブ (📦) / セットアップ画面 ② 統合の回帰テスト。

Strata の純正 setup.sh / setup.py は対話インストーラだが、機械部分とモデル構成の
両方を UI から引ける:
  ① clone (submodule 取得済みならスキップ) → .venv → pip install -r requirements.txt
     → engine step (薄い driver strata_engine.py が純正 get_prebuilt() で既製エンジンを受信)
     → verify (import serve.server + engine/strata 実行可)。
  ② モデル構成: 純正 setup.py を --family/--model/--context/--vision + --yes + --no-start
     の非対話で走らせる単一 step ジョブ。表 (family/model/context) は submodule が単一の源
     (strata_setup が subprocess で読み、選択肢をホワイトリスト検証するだけ)。
見張る対象:
  1. spec の形 (requirements / venv_pip / engine_fetch / engine_bin)
  2. start_install の計画 — venv_pip spec は uv が在っても pip で計画する (純正 pip_install
     が .venv の -m pip を前提にするため。表示 tool と argv のズレも許さない)
  3. engine step が strata_engine.py を submodule cwd で plan に入ること、
     precheck / verify が engine/strata の実在も要求すること
  4. driver の入口ガード (cwd 違い / setup.py が純正でない) は正直に失敗すること
  5. /api/installs の installable が strata を engine_fetch / tool=pip で出すこと
  6. build_argv のホワイトリスト (表に無い family/model/context/vision は注入でも拒否)、
     設定 JSON 名の pure 作法 (strata-<tag+model>.json)、.venv 前条件
  7. /api/installs/strata-model の GET/POST と {jid} ルートとの衝突がないこと
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_launcher import installer, strata_setup
from llm_launcher.installer import INSTALL_SPECS, InstallError, InstallManager
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


# driver が純正名を持つ API の面 (gpus / gpu_problem / engine_dir / arch_numbers /
# get_prebuilt / pip_cuda_libs / CUDA13_MIN_ARCH / PREBUILT_URL / EXE) を最小で模す stub。
# engine/BUILD.json の source と env の切替で経路を選ぶ — 純正 get_prebuilt の
# 「source=local は黙って None (ビルド側が譲られる)」を誤読しないことをここで押さえる。
STUB_SETUP = '''
import json, os, sys
from pathlib import Path
CUDA13_MIN_ARCH = 75
PREBUILT_URL = "https://example.invalid/"
EXE = "strata"
def gpus(): return [{"index": 0, "name": "Fake PRO 4000", "arch": int(os.environ.get("STUB_GPU", "120"))}]
def gpu_problem(g): return None
def engine_dir(toolkit=13): return Path.cwd() / "engine"
def arch_numbers(v): return [int(x) for x in (v or []) if str(x).isdigit()]
def get_prebuilt(url, gpu, vision, updating=False, toolkit=13):
    if os.environ.get("STUB_TRAP"):
        raise SystemExit("get_prebuilt ran")
    if os.environ.get("STUB_NONE"):
        return None
    e = Path("engine"); e.mkdir(exist_ok=True)
    (e / EXE).write_bytes(b"#!/bin/sh\\n")
    (e / "BUILD.json").write_text(json.dumps({"source": "release", "version": "0.1.57",
                                              "archs": gpu["archs"]}))
    return e
def pip_cuda_libs(toolkit=13):
    if os.environ.get("STUB_TRAP_CUDA"):
        raise SystemExit("pip_cuda_libs ran")
    Path("cuda_stamp").write_text("done")
'''


def _stub_repo(tmp_path: Path, build_json: dict | None) -> Path:
    (tmp_path / "serve").mkdir()
    (tmp_path / "serve" / "server.py").write_text("", encoding="utf-8")
    (tmp_path / "setup.py").write_text(STUB_SETUP, encoding="utf-8")
    if build_json is not None:
        eng = tmp_path / "engine"
        eng.mkdir()
        (eng / "BUILD.json").write_text(json.dumps(build_json), encoding="utf-8")
        (eng / "strata").write_bytes(b"#!/bin/sh\n")
    return tmp_path


def _run_driver(cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    e = dict(os.environ, **env)
    return subprocess.run([sys.executable, str(DRIVER)], cwd=cwd, env=e,
                          capture_output=True, text=True, timeout=60)


def test_driver_accepts_local_engine_matching_arch(tmp_path: Path):
    """./setup.sh でビルド済みのエンジン (source=local, archs=120) は「在る」で満足 —
    get_prebuilt はこのとき呼ばれない (純正はビルド側へ譲す設計。trap で証明)。"""
    _stub_repo(tmp_path, {"source": "local", "version": "0.1.57", "archs": [120], "vision": "gpu"})
    r = _run_driver(tmp_path, {"STUB_TRAP": "1", "STUB_TRAP_CUDA": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ローカルビルド" in r.stdout
    assert "受信もビルドも不要" in r.stdout


def test_driver_names_arch_gap_of_local_engine(tmp_path: Path):
    """ローカルビルドのエンジンにこの GPU のコードが無いときは再ビルド導線 —
    「受信失敗」と誤読せず、コンパイルしない立場を正直に出す。"""
    _stub_repo(tmp_path, {"source": "local", "version": "0.1.57", "archs": [75]})
    r = _run_driver(tmp_path, {"STUB_GPU": "120", "STUB_TRAP": "1"})
    assert r.returncode == 1
    assert "sm_120" in r.stdout and "setup.sh" in r.stdout


def test_driver_fails_honestly_without_any_engine(tmp_path: Path):
    """初回 (engine/ なし) で get_prebuilt が None (リリースに無い/不通) → rc 1 + setup.sh 案内。"""
    _stub_repo(tmp_path, None)
    r = _run_driver(tmp_path, {"STUB_NONE": "1"})
    assert r.returncode == 1
    assert "受信できませんでした" in r.stdout


def test_driver_runs_pip_cuda_libs_only_for_release_engine(tmp_path: Path):
    """受信経路 (release) は純正 main と同じく CUDA ホイールを入れる。local では呼ばない。"""
    _stub_repo(tmp_path, None)
    r = _run_driver(tmp_path, {})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "cuda_stamp").is_file()
    assert "エンジン:" in r.stdout


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


def test_wizard_copy_mentions_engine_and_strata_model_step():
    import llm_launcher

    static = Path(llm_launcher.__file__).parent / "static"
    js = (static / "setup.js").read_text("utf-8")
    assert "エンジンを受信" in js          # step chip 名 (engine)
    assert "engine_fetch" in js            # 確認画面の手順組み立て
    assert "INSTALL_NOTES" in js           # 完了後の追い書き
    # ② の Strata モデル構成: 面板 / 送信先 / step 名 / 稼働設定の config 接続
    assert "モデルを準備" in js            # INS_STEPS model chip
    assert "/api/installs/strata-model" in js
    assert "md-strata" in js
    assert 'model: "config"' in js         # RUNCFG.strata → values.config
    html = (static / "setup.html").read_text("utf-8")
    assert 'id="md-strata"' in html and 'id="md-strata-exist"' in html
    tab = (static / "js" / "views" / "setup.js").read_text("utf-8")
    assert "モデル準備" in tab             # 📦 履歴の step / タイトル


# ---------------------------------------------------------------- ② モデル構成 (strata_setup)

FAKE_TABLES = {
    "available": True,
    "families": {
        "qwen": {"title": "Qwen3.8-Flash-Next", "about": "", "tag": "",
                 "models": {"Q2_0": {"about": "", "label": "", "download_gb": 66.4,
                                     "ram_gb": 48, "vision": False}}},
        "deepseek41": {"title": "DeepSeek-V4.1-Flash", "about": "",
                       "tag": "deepseek-v4.1-flash-",
                       "models": {"IST-DAS-2.00bpw": {"about": "", "label": "ISTA-DAS 2.00 bpw",
                                                      "download_gb": None, "ram_gb": 40,
                                                      "vision": False}}},
    },
    "contexts": [8192, 131072],
    "pc": {"ram_gb": 128.0, "data_dir": "/x/Strata-data", "disk_free_gb": 400.0},
}


def _seed_tables(monkeypatch, mod: Path) -> Path:
    """subprocess 実行なしで load_options が表を返すようにキャッシュへ種を打つ。
    呼び出し側が resolved / 未 resolved のどちらで渡しても.hit するよう両キー入れる。"""
    tables = dict(FAKE_TABLES)
    monkeypatch.setattr(strata_setup, "_CACHE",
                        {str(mod): (time.monotonic(), tables),
                         str(mod.resolve()): (time.monotonic(), tables)})
    return mod.resolve()


def test_build_argv_whitelist_and_rejection(monkeypatch, tmp_path: Path):
    mod = tmp_path / "module" / "strata"
    (mod / ".venv" / "bin").mkdir(parents=True)
    (mod / "setup.py").write_text("# fake\n", encoding="utf-8")
    mod = _seed_tables(monkeypatch, mod)
    argv, cfg = strata_setup.build_argv(mod, {"family": "qwen", "model": "Q2_0",
                                              "context": 131072, "vision": "gpu"})
    # --yes (全質問が既定確定 = 完全非対話) + --no-start / --no-browser は常に付ける
    assert argv == ["setup.py", "--family", "qwen", "--model", "Q2_0", "--context", "131072",
                    "--vision", "gpu", "--yes", "--no-start", "--no-browser"]
    assert cfg == "strata-q2_0.json"
    # vision "" = 渡さない (非対応パック)。tag 付き family は設定名にも効く
    argv2, cfg2 = strata_setup.build_argv(mod, {"family": "deepseek41", "model": "IST-DAS-2.00bpw",
                                                "context": 8192, "vision": ""})
    assert "--vision" not in argv2
    assert cfg2 == "strata-deepseek-v4.1-flash-ist-das-2.00bpw.json"
    # ホワイトリスト外 (注入を試みる値も含む) は全て ValueError
    for bad in ({"family": "qwen", "model": "../evil", "context": 8192, "vision": "yes"},
                {"family": "--x", "model": "Q2_0", "context": 8192, "vision": "yes"},
                {"family": "nope", "model": "Q2_0", "context": 8192, "vision": "yes"},
                {"family": "qwen", "model": "Q2_0", "context": 12345, "vision": "yes"},
                {"family": "qwen", "model": "Q2_0", "context": "131072 --yes", "vision": "yes"},
                {"family": "qwen", "model": "Q2_0", "context": 8192, "vision": "-x"}):
        with pytest.raises(ValueError):
            strata_setup.build_argv(mod, bad)


def test_build_argv_without_submodule_tables_fails(tmp_path: Path):
    """表が読めない (未 clone) は argv を作らない — 対話 setup.sh へ戻すしかない状態。"""
    mod = tmp_path / "no-strata"
    mod.mkdir()
    with pytest.raises(ValueError, match="表を読めません"):
        strata_setup.build_argv(mod, {"family": "qwen", "model": "Q2_0", "context": 8192})


def test_build_argv_against_real_submodule():
    """実 submodule が在る環境では、表読み出し (subprocess) と純正設定名作法が生きていること。"""
    mod = ROOT / "module" / "strata"
    if not (mod / "setup.py").is_file():
        pytest.skip("module/strata が未 clone")
    opts = strata_setup.load_options(mod)
    assert opts.get("available") is True
    assert opts.get("contexts"), "CONTEXTS が読めない"
    argv, cfg = strata_setup.build_argv(mod, {"family": "qwen", "model": "Q2_0",
                                              "context": 131072, "vision": "no"})
    assert argv[:2] == ["setup.py", "--family"] and argv[-3:] == ["--yes", "--no-start", "--no-browser"]
    assert cfg == "strata-q2_0.json"


# ---------------------------------------------------------------- ② ジョブ計画 (installer)

def _fake_module(tmp_path: Path, *, with_venv: bool) -> Path:
    mod = _submodule(tmp_path)
    (mod / "setup.py").write_text("# fake\n", encoding="utf-8")
    if with_venv:
        vp = mod / ".venv" / "bin"
        vp.mkdir(parents=True)
        (vp / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    return mod


def test_start_strata_model_requires_venv(monkeypatch, tmp_path: Path):
    _fake_module(tmp_path, with_venv=False)
    mgr = _manager(tmp_path)
    with pytest.raises(InstallError, match="①"):
        mgr.start_strata_model({"family": "qwen", "model": "Q2_0", "context": 131072})


def test_start_strata_model_plans_single_model_job(monkeypatch, tmp_path: Path):
    mod = _fake_module(tmp_path, with_venv=True)
    mod = _seed_tables(monkeypatch, mod)
    got: dict[str, object] = {}

    async def fake(self, job, module_dir, argv):  # noqa: ANN001
        got["job"] = dict(job)
        got["argv"] = list(argv)
        got["cwd"] = str(module_dir)

    monkeypatch.setattr(InstallManager, "_run_model", fake)
    mgr = _manager(tmp_path)

    async def go() -> dict:
        job = mgr.start_strata_model({"family": "qwen", "model": "Q2_0",
                                      "context": 131072, "vision": "gpu"})
        await asyncio.sleep(0)
        return job

    job = asyncio.run(go())
    assert job["kind"] == "install" and job["phase"] == "model"
    assert job["backend"] == "strata" and job["steps"] == ["model"]
    assert job["config_name"] == "strata-q2_0.json"
    assert job["model"] == "qwen/Q2_0"
    assert got["cwd"].endswith("module/strata")
    argv = got["argv"]
    assert str(argv[0]).endswith(str(Path(".venv/bin/python")))
    assert argv[1:] == ["setup.py", "--family", "qwen", "--model", "Q2_0",
                        "--context", "131072", "--vision", "gpu",
                        "--yes", "--no-start", "--no-browser"]
    # 実行は .venv の python — setup.py が自らの pip_install で .venv を前提にするため
    assert str(mod / ".venv" / "bin" / "python") in str(argv[0])


# ---------------------------------------------------------------- API (②)

def test_strata_model_api(tmp_path: Path):
    from fastapi.testclient import TestClient

    from llm_launcher import app as app_mod

    (tmp_path / "data").mkdir(parents=True)
    client = TestClient(app_mod.create_app(tmp_path))
    g = client.get("/api/installs/strata-model")
    assert g.status_code == 200
    body = g.json()
    # 静的パスは /api/installs/{jid} に呑まれない (登録順) + 未 clone は正直に from
    assert body["available"] is False and body["venv_ready"] is False
    assert body["configs"] == [] and "id" not in body
    p = client.post("/api/installs/strata-model",
                    json={"family": "qwen", "model": "Q2_0", "context": 131072})
    assert p.status_code == 400 and "①" in p.json()["detail"]
    # body 検証: family 空は pydantic で 422
    assert client.post("/api/installs/strata-model",
                       json={"family": "", "model": "Q2_0"}).status_code == 422


def test_strata_model_api_lists_installed_configs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from llm_launcher import app as app_mod
    from llm_launcher import strata_setup as ss

    mod = tmp_path / "module" / "strata"
    mod.mkdir(parents=True)
    (mod / "setup.py").write_text("# fake\n", encoding="utf-8")   # load_options の入口ガード
    (mod / "strata-q2_0.json").write_text('{"exe": "/x/strata", "args": [], "model_name": "qwen3.8-flash-next-q2_0"}',
                                          encoding="utf-8")
    (mod / "strata-q2_0.shared-settings.json").write_text('{"exe": "x", "args": []}', encoding="utf-8")
    (mod / "strata-not-a-config.json").write_text('{"nope": 1}', encoding="utf-8")
    # 表読み出し subprocess は環境依存なのでキャッシュで固定 (resolved / 未 resolved 両キー)
    tables = (time.monotonic(), dict(FAKE_TABLES))
    monkeypatch.setattr(ss, "_CACHE", {str(mod): tables, str(mod.resolve()): tables})
    client = TestClient(app_mod.create_app(tmp_path))
    body = client.get("/api/installs/strata-model").json()
    assert body["available"] is True
    names = [c["name"] for c in body["configs"]]
    # shared-settings と model_config 作法外 (exe/args 無し) は除外される
    assert names == ["strata-q2_0.json"]
    assert body["configs"][0]["model_name"] == "qwen3.8-flash-next-q2_0"
