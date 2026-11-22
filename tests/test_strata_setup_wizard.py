"""Strata セットアップタブ (📦) / セットアップ画面 ② 統合の回帰テスト。

Strata の純正 setup.sh / setup.py は対話インストーラだが、機械部分とモデル構成の
両方を UI から引ける (Maya と同じ手法):
  ① clone (submodule 取得済みならスキップ) → .venv → tools step (薄い driver
     strata_tools.py が純正 pip_install(runtime_packages()) でパッケージ導入)
     → verify (import serve.server + stamp .venv/.strata-pip.json 実在)。
     エンジンは ① ではやらない — ② の純正セットアップが engine_step で行う。
  ② モデル構成: 純正 setup.py を --family/--model/--context/--vision + --yes + --no-start
     の非対話で走らせる単一 step ジョブ。表 (family/model/context) は submodule が単一の源
     (strata_setup が subprocess で読み、選択肢をホワイトリスト検証するだけ)。
見張る対象:
  1. spec の形 (tools_step / tools_driver / venv_pip / check_files)
  2. start_install の計画 — venv_pip spec は uv が在っても pip で計画する (純正 pip_install
     が .venv の -m pip を前提にするため。表示 tool と argv のズレも許さない)
  3. tools step が strata_tools.py を setup_dir cwd で plan に入ること、
     precheck / verify が導入の stamp を要求すること
  4. driver の入口ガード (cwd 違い / setup.py が純正でない) は正直に失敗すること
  5. /api/installs の installable が strata を tools_step / tool=pip で出すこと
  6. build_argv のホワイトリスト (表に無い family/model/context/vision は注入でも拒否)、
     設定 JSON 名の pure 作法 (strata-<tag+model>.json)、.venv 前条件
  7. /api/installs/strata-model の GET/POST と {jid} ルートとの衝突がないこと
"""
from __future__ import annotations

import asyncio
import json
import os
import re
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
DRIVER = ROOT / "src" / "llm_launcher" / "strata_tools.py"


def test_spec_shape():
    assert SPEC["clone"] == "https://github.com/Niko1221/Strata"
    assert SPEC["module_path"] == "module/strata"
    # Maya と同じ手法: 検出 (serve.server 実 import) + 導入の stamp で完了判定。
    # パッケージは純正 pip step (driver) が入れる — requirements パスは launcher が持たない。
    # エンジンは ② の純正セットアップの仕事 (① に engine step は無い)。
    assert SPEC["check"] == "serve.server"
    assert SPEC["requirements"] == ""
    assert SPEC["venv_pip"] is True
    assert SPEC["tools_step"] is True
    assert SPEC["tools_driver"] == "strata_tools.py"
    assert SPEC["check_files"] == [".venv/.strata-pip.json"]
    assert not SPEC.get("engine_fetch") and not SPEC.get("engine_bin")


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
    # Maya と同じ: venv → precheck → tools (純正 pip step) → verify。engine step は無い
    assert names == ["venv", "precheck", "tools", "verify"]
    # 純正 setup.py の pip_install が -m pip を前提にする = uv が在っても pip で組む
    assert got["job"]["tool"] == "pip"
    assert got["job"]["uv_path"] == ""
    venvpy = str((mod / ".venv" / "bin" / "python").resolve())
    # tools step: 薄い driver を submodule cwd で .venv python 実行
    tools = next(s for s in steps if s["name"] == "tools")
    assert tools["argv"] == [venvpy, str(DRIVER)]
    assert tools["cwd"].endswith("module/strata")
    # precheck / verify は import だけでなく導入の stamp も要求する
    for s in steps:
        if s["name"] in ("precheck", "verify"):
            assert "serve.server" in s["argv"][2]
            assert ".venv/.strata-pip.json" in s["argv"][2]


def test_plan_terminal_layout_uses_terminal_cwd(monkeypatch, tmp_path: Path):
    """0.2.16 レイアウト (terminal/setup.py): venv / tools / verify は terminal/ 基準。"""
    mod = tmp_path / "module" / "strata"
    (mod / "terminal" / "serve").mkdir(parents=True)
    (mod / "terminal" / "serve" / "server.py").write_text("# clone\n", encoding="utf-8")
    (mod / "terminal" / "setup.py").write_text("# fake\n", encoding="utf-8")
    got = _plan(monkeypatch, tmp_path)
    steps = got["steps"]
    assert [s["name"] for s in steps] == ["venv", "precheck", "tools", "verify"]
    venvpy = str((mod / "terminal" / ".venv" / "bin" / "python").resolve())
    for s in steps:
        if s["name"] in ("precheck", "tools", "verify"):
            assert s["cwd"].endswith("module/strata/terminal")
            assert s["argv"][0] == venvpy


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


# ---------------------------------------------------------------- driver (strata_tools.py)

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
    assert r.stdout.startswith("strata_tools:")


# driver が純正名を持つ API の面 (pip_install / runtime_packages) を最小で模す stub。
# 呼ばれた fact を stamp ファイルに残す — 純正関数経由でだけパッケージが入ること、
# エンジンには触らないこと (② の仕事) をここで押さえる。
STUB_SETUP = '''
import json, sys
from pathlib import Path
def runtime_packages():
    return ["numpy==2.5.3", "jinja2==3.1.6"]
def pip_install(packages, what, extra=()):
    Path("pip_stamp").write_text(json.dumps({"packages": packages, "what": what}))
    stamp = Path(sys.prefix) / ".strata-pip.json"
    stamp.write_text(json.dumps(sorted(packages)))
'''


def _stub_repo(tmp_path: Path, *, terminal: bool = False) -> Path:
    d = tmp_path / "terminal" if terminal else tmp_path
    (d / "serve").mkdir(parents=True)
    (d / "serve" / "server.py").write_text("", encoding="utf-8")
    (d / "setup.py").write_text(STUB_SETUP, encoding="utf-8")
    return tmp_path


def _run_driver(cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ, **(env or {}))
    return subprocess.run([sys.executable, str(DRIVER)], cwd=cwd, env=e,
                          capture_output=True, text=True, timeout=60)


def test_driver_calls_upstream_pip_step(tmp_path: Path):
    """パッケージは純正 pip_install(runtime_packages()) 経由で入る (launcher は
    requirements のパスを持たない)。エンジンは ① では触らない。"""
    _stub_repo(tmp_path)
    r = _run_driver(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    got = json.loads((tmp_path / "pip_stamp").read_text(encoding="utf-8"))
    assert got["packages"] == ["numpy==2.5.3", "jinja2==3.1.6"]
    assert "②" in r.stdout          # エンジン / モデルは ② の仕事である案内
    assert not (tmp_path / "engine").exists()


def test_driver_resolves_terminal_layout(tmp_path: Path):
    """0.2.16: cwd がリポジトリ直下でも terminal/setup.py を見つけて実行する。"""
    _stub_repo(tmp_path, terminal=True)
    r = _run_driver(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "terminal" / "pip_stamp").is_file()


def test_driver_falls_back_to_requirements_lines(tmp_path: Path):
    """runtime_packages の無い古い版: pinned requirements.txt (requirement_lines) を使う。"""
    d = tmp_path
    (d / "serve").mkdir()
    (d / "serve" / "server.py").write_text("", encoding="utf-8")
    (d / "setup.py").write_text(
        "from pathlib import Path\n"
        "REQUIREMENTS = Path('requirements.txt')\n"
        "def requirement_lines(path=None):\n"
        "    return [l.strip() for l in REQUIREMENTS.read_text().splitlines() if l.strip()]\n"
        "def pip_install(packages, what, extra=()):\n"
        "    Path('pip_stamp').write_text(','.join(packages))\n", encoding="utf-8")
    (d / "requirements.txt").write_text("numpy==2.5.3\npsutil==7.2.2\n", encoding="utf-8")
    r = _run_driver(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "pip_stamp").read_text(encoding="utf-8") == "numpy==2.5.3,psutil==7.2.2"


# ---------------------------------------------------------------- API / ウィザード文言

def test_installs_api_exposes_strata_with_tools_step(tmp_path: Path):
    from fastapi.testclient import TestClient

    from llm_launcher import app as app_mod

    (tmp_path / "data").mkdir(parents=True)
    client = TestClient(app_mod.create_app(tmp_path))
    entries = client.get("/api/installs").json()["installable"]
    ent = next(e for e in entries if e["id"] == "strata")
    assert ent["name"] == "Strata"
    assert ent["requirements"] == ""
    assert ent["tools_step"] is True and ent["cmake"] is False
    assert ent["tools_label"]        # UI の手順名 (ジョブ実体と揃える)
    # /api/installs の tool (グローバル) にかかわらず、この entry の tool は pip
    assert ent["tool"] == "pip"


def test_wizard_copy_mentions_engine_and_strata_model_step():
    import llm_launcher

    static = Path(llm_launcher.__file__).parent / "static"
    js = (static / "setup.js").read_text("utf-8")
    assert "tools_label" in js             # 確認画面 / step chip の手順名 (純正 pip step)
    assert "INSTALL_NOTES" in js           # 完了後の追い書き
    # ② の Strata モデル構成: 面板 / 送信先 / step 名 / 稼働設定の config 接続
    assert "モデルを準備" in js            # INS_STEPS model chip
    assert "/api/installs/strata-model" in js
    assert "md-strata" in js
    assert 'model: "config"' in js         # RUNCFG.strata → values.config
    # ⑤ は ③ で作ったプロファイル (W.calPick) を調整する — 一覧先頭 (最古) を
    # 掴んで別プロファイルの config 整合チェックに弾かれる事故の回帰防止
    assert "W.calPick" in js
    assert "対象: " in js
    # Strata の --engine 既定は mock (serve/server.py)。ウィザード保存は常に実エンジンを埋める
    assert 'vals.engine = "strata"' in js
    # ③ の並列数既定 = 作られた config の parallel (全局推奨 4 を焼くと 24GB で起動失敗)
    assert "jp.parallel" in js
    # ② のコンテキスト既定 = 純正の推奨 (own_path_context / ram_ctx) + 大選択の注記
    assert "rec_context" in js and " · 推奨" in js
    assert "rec_context" in (static.parent / "strata_setup.py").read_text("utf-8")
    html = (static / "setup.html").read_text("utf-8")
    assert 'id="md-strata"' in html and 'id="md-strata-exist"' in html
    # ダウンロード済みモデル: ダウンロード択ではグレー (済み表記)、既定択は「ダウンロード済みの
    # モデルを使う」でモデル名を並べる — JSON 名は UI に出さない (2026-11-09 の指摘)
    assert "ダウンロード済み" in js
    assert "strata-<model>.json をそのまま" not in js   # JSON 選択導線の回帰禁止
    # GPU の使い方は ③ 稼働設定へ移動 (FreeToken 基準の GPU 分割質問と同じ位置)。
    # ② はモデル構成だけを担当し、GPU 質問は出さない (2026-11-11 の揃え直し)
    assert "mdStFillGpus" not in js
    assert 'id="md-gpu"' not in html
    assert 'flag: "gpu", config: true' in js
    assert "multi_gpu" in js          # 単一 GPU 家系の注記は ② に残る
    tab = (static / "js" / "views" / "setup.js").read_text("utf-8")
    assert "モデル準備" in tab             # 📦 履歴の step / タイトル


def test_wizard_profile_sends_real_engine():
    """on=null のウィザード系プロファイルでも values.engine が argv に出ること。
    出ないと serve/server.py は argparse 既定の mock (スクリプト付きテストエンジン)
    で起動する — 「起動するとモックが来る」(2026-11-09 報告) の回帰防止。"""
    from llm_launcher.backends import backend_for, render_argv

    prof = {"id": "p_x", "backend": "strata", "on": None,
            "values": {"config": "module/strata/strata-q2_0.json", "engine": "strata",
                       "host": "0.0.0.0", "port": "1919"}}
    argv = render_argv(backend_for(ROOT, "strata"), dict(prof),
                       python="py", binary="", root=ROOT)
    assert argv[argv.index("--engine") + 1] == "strata"
    del prof["values"]["engine"]
    argv2 = render_argv(backend_for(ROOT, "strata"), dict(prof),
                        python="py", binary="", root=ROOT)
    assert "--engine" not in argv2      # 埋めなければ flag 無し = サーバー側既定 mock


# ---------------------------------------------------------------- ② モデル構成 (strata_setup)

FAKE_TABLES = {
    "available": True,
    "families": {
        "qwen": {"title": "Qwen3.8-Flash-Next", "about": "", "tag": "", "multi_gpu": True,
                 "models": {"Q2_0": {"about": "", "label": "", "download_gb": 66.4,
                                     "ram_gb": 48, "vision": False}}},
        "qwen36": {"title": "Qwen3.6-35B-A3B", "about": "", "tag": "qwen36-",
                   "arch": "qwen35moe", "multi_gpu": False,
                   "models": {"UD-Q4_K_XL": {"about": "", "label": "", "download_gb": None,
                                             "ram_gb": 32, "vision": False}}},
        "hy4-flash": {"title": "Tencent Hy4-Flash", "about": "", "tag": "hy4-flash-",
                      "arch": "hy4flash", "multi_gpu": False,
                      "contexts": [8192, 32768, 65536, 131072], "max_context": 131072,
                      "models": {"SQ-64": {"about": "", "label": "", "download_gb": 130.5,
                                           "ram_gb": 62, "vision": True, "rec_context": 131072}}},
        "inkling-2-mini": {"title": "Inkling-2-Mini", "about": "", "tag": "inkling-2-mini-",
                           "arch": "inkling2_loop", "multi_gpu": True,
                           "contexts": [8192, 32768, 65536, 131072, 262144, 524288, 1048576],
                           "max_context": 1048576,
                           "models": {"SQ-20": {"about": "", "label": "", "download_gb": 20.0,
                                                "ram_gb": 20, "vision": True, "rec_context": 32768},
                                      "SQ-15": {"about": "", "label": "", "download_gb": 15.0,
                                                "ram_gb": 15, "vision": True, "rec_context": 32768}}},
        "deepseek41": {"title": "DeepSeek-V4.1-Flash", "about": "",
                       "tag": "deepseek-v4.1-flash-", "multi_gpu": True,
                       "models": {"IST-DAS-2.00bpw": {"about": "", "label": "ISTA-DAS 2.00 bpw",
                                                      "download_gb": None, "ram_gb": 40,
                                                      "vision": False}}},
    },
    "contexts": [8192, 131072],
    "pc": {"ram_gb": 128.0, "vram_gb": 24.0, "data_dir": "/x/Strata-data", "disk_free_gb": 400.0,
           "gpus": [{"index": 0, "name": "RTX A", "vram_gb": 24.0, "usable": True},
                    {"index": 1, "name": "RTX B", "vram_gb": 24.0, "usable": True},
                    {"index": 2, "name": "GT", "vram_gb": 8.0, "usable": False}]},
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


def test_build_argv_gpu_selection(monkeypatch, tmp_path: Path):
    """GPU 指定: 単一カード = --gpu N / 複数 = --gpus "0,1" / all / auto=無し。
    検出外 GPU と、単一 GPU 専一家系 (own-path) への複数指定は拒否する。"""
    mod = tmp_path / "module" / "strata"
    (mod / ".venv" / "bin").mkdir(parents=True)
    (mod / "setup.py").write_text("# fake\n", encoding="utf-8")
    mod = _seed_tables(monkeypatch, mod)
    base = {"family": "qwen", "model": "Q2_0", "context": 8192}
    argv, _ = strata_setup.build_argv(mod, dict(base, gpus="1"))
    assert argv[argv.index("--gpu") + 1] == "1" and "--gpus" not in argv
    argv, _ = strata_setup.build_argv(mod, dict(base, gpus="0,1"))
    assert argv[argv.index("--gpus") + 1] == "0,1"
    argv, _ = strata_setup.build_argv(mod, dict(base, gpus="all"))
    assert argv[argv.index("--gpus") + 1] == "all"
    argv, _ = strata_setup.build_argv(mod, dict(base, gpus="auto"))
    assert "--gpus" not in argv and "--gpu" not in argv
    for bad in ("0,2", "2", "0,0", "9", "0; rm"):
        with pytest.raises(ValueError):
            strata_setup.build_argv(mod, dict(base, gpus=bad))
    # qwen36 は multi_gpu=False (純正: runs on one GPU so far) — 単一カード指定は可、複数は拒否
    b36 = {"family": "qwen36", "model": "UD-Q4_K_XL", "context": 8192}
    argv, _ = strata_setup.build_argv(mod, dict(b36, gpus="0"))
    assert "--gpu" in argv
    for bad in ("0,1", "all"):
        with pytest.raises(ValueError, match="単一 GPU"):
            strata_setup.build_argv(mod, dict(b36, gpus=bad))


def test_build_argv_without_submodule_tables_fails(tmp_path: Path):
    """表が読めない (未 clone) は argv を作らない — 対話 setup.sh へ戻すしかない状態。"""
    mod = tmp_path / "no-strata"
    mod.mkdir()
    with pytest.raises(ValueError, match="未取得"):
        strata_setup.build_argv(mod, {"family": "qwen", "model": "Q2_0", "context": 8192})


def test_build_argv_against_real_submodule():
    """実 submodule が在る環境では、表読み出し (subprocess) と純正設定名作法が生きていること。
    0.2.16 の terminal/ レイアウトでも旧レイアウトでも同じく動く (setup_dir が解決)。"""
    mod = ROOT / "module" / "strata"
    if not (strata_setup.setup_dir(mod) / "setup.py").is_file():
        pytest.skip("module/strata が未 clone")
    opts = strata_setup.load_options(mod)
    assert opts.get("available") is True
    assert opts.get("contexts"), "CONTEXTS が読めない"
    # 推奨コンテキスト / 上限 / VRAM は純正の式・実測がそのまま乗っている (UI の既定用)
    pc = opts.get("pc") or {}
    assert pc.get("ram_gb") and pc.get("vram_gb") is not None
    q = (opts.get("families") or {}).get("qwen") or {}
    assert q.get("max_context") == max(opts["contexts"])
    # 家系別上限 (owner 方針: 対応する全てで 1M まで): Flash-Next は yarn で 1M、
    # DeepSeek-V4.1-Flash は native 1M、own-path (Qwen3.6) は rope scaling 無しで 256K
    assert q.get("max_context") == 1048576
    assert 1048576 in (q.get("contexts") or [])
    d41 = (opts.get("families") or {}).get("deepseek41") or {}
    if d41:
        assert d41.get("max_context") == 1048576
        assert (d41.get("contexts") or [])[-1] == 1048576
    # own-path (Qwen3.6): trained 262144 上限 & 単一 GPU 宣言が表に乗る
    q36 = (opts.get("families") or {}).get("qwen36") or {}
    if q36:
        assert q36.get("max_context") == 262144
        assert q36.get("multi_gpu") is False
        assert max(q36.get("contexts") or [262144]) <= 262144
        # 家系別ホワイトリスト: own-path に 256K 超は API でも拒否
        with pytest.raises(ValueError):
            strata_setup.build_argv(mod, {"family": "qwen36",
                                          "model": next(iter(q36["models"])), "context": 524288})
    # 0.2.19+ の独自エンジン家系: Hy4-Flash (単一 GPU・8K-128K) / Inkling-2-Mini
    # (1M まで・NVIDIA 2 枚 EP)。コンテキスト / 推奨は strata_control の家系定義そのまま
    fams = opts.get("families") or {}
    hy4 = fams.get("hy4-flash") or {}
    if hy4:
        assert hy4.get("arch") == "hy4flash"
        assert hy4.get("multi_gpu") is False
        assert hy4.get("contexts") == [8192, 32768, 65536, 131072]
        assert hy4.get("tag") == "hy4-flash-"
        for m, d in hy4["models"].items():
            assert d.get("rec_context") == 131072, m
            assert d.get("vision") is True, m
            assert d.get("download_gb") and d["download_gb"] > 60, m   # build + Engram + Vision
        argv, cfg = strata_setup.build_argv(mod, {"family": "hy4-flash", "model": "SQ-64",
                                                  "context": 131072, "vision": "no"})
        assert argv[:4] == ["setup.py", "--family", "hy4-flash", "--model"]
        assert cfg == "strata-hy4-flash-sq-64.json"
        with pytest.raises(ValueError):
            strata_setup.build_argv(mod, {"family": "hy4-flash", "model": "SQ-64", "context": 262144})
    ink = fams.get("inkling-2-mini") or {}
    if ink:
        assert ink.get("arch") == "inkling2_loop"
        assert ink.get("multi_gpu") is True
        assert 1048576 in ink.get("contexts") or []
        for m, d in ink["models"].items():
            assert d.get("rec_context") == 32768, m
            assert d.get("vision") is True, m
        argv, cfg = strata_setup.build_argv(mod, {"family": "inkling-2-mini", "model": "SQ-20",
                                                  "context": 1048576})
        assert cfg == "strata-inkling-2-mini-sq-20.json"
        assert argv[argv.index("--context") + 1] == "1048576"
        with pytest.raises(ValueError):
            strata_setup.build_argv(mod, {"family": "inkling-2-mini", "model": "SQ-99", "context": 32768})
    # 組み立てたフラグは全て純正 setup.py の argparse に実在する
    src = (strata_setup.setup_dir(mod) / "setup.py").read_text(encoding="utf-8")
    known = set(re.findall(r'add_argument\("(--[a-z0-9-]+)"', src))
    for fam, model in (("hy4-flash", "SQ-64"), ("inkling-2-mini", "SQ-10")):
        if fam not in fams:
            continue
        av, _ = strata_setup.build_argv(mod, {"family": fam, "model": model, "context": 32768})
        for x in av[1:]:
            if x.startswith("--"):
                assert x in known, x
    assert isinstance(opts.get("pc", {}).get("gpus"), list)
    m = next(iter((q.get("models") or {}).values()), {})
    if "rec_context" in m:
        assert m["rec_context"] in opts["contexts"]
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


def test_start_strata_model_terminal_layout_cwd(monkeypatch, tmp_path: Path):
    """0.2.16: ② のジョブは terminal/ を cwd に setup.py を実行する (venv も terminal/)。"""
    mod = tmp_path / "module" / "strata"
    term = mod / "terminal"
    (term / ".venv" / "bin").mkdir(parents=True)
    (term / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (term / "setup.py").write_text("# fake\n", encoding="utf-8")
    (term / "serve").mkdir()
    (term / "serve" / "server.py").write_text("# server\n", encoding="utf-8")
    _seed_tables(monkeypatch, mod)
    got: dict[str, object] = {}

    async def fake(self, job, module_dir, argv):  # noqa: ANN001
        got["cwd"] = str(module_dir)
        got["argv"] = list(argv)

    monkeypatch.setattr(InstallManager, "_run_model", fake)
    mgr = _manager(tmp_path)

    async def go() -> None:
        mgr.start_strata_model({"family": "qwen", "model": "Q2_0", "context": 131072})
        await asyncio.sleep(0)

    asyncio.run(go())
    assert got["cwd"].endswith("module/strata/terminal")
    assert got["argv"][0] == str(term / ".venv" / "bin" / "python")
    assert got["argv"][1] == "setup.py"


def test_strata_model_api_venv_ready_terminal_layout(tmp_path: Path):
    """venv_ready は terminal/.venv を見る (ウィザードの開始ボタンガード)。"""
    from fastapi.testclient import TestClient

    from llm_launcher import app as app_mod

    mod = tmp_path / "module" / "strata"
    term = mod / "terminal"
    (term / ".venv" / "bin").mkdir(parents=True)
    (term / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (term / "setup.py").write_text("# fake\n", encoding="utf-8")
    (tmp_path / "data").mkdir(parents=True)
    client = TestClient(app_mod.create_app(tmp_path))
    body = client.get("/api/installs/strata-model").json()
    assert body["venv_ready"] is True
    body2 = client.get("/api/profiles/setup-options").json()
    assert body2["strata"]["venv_ready"] is True


# ---------------------------------------------------------------- API (②)

def test_run_model_result_carries_parallel_from_config(monkeypatch, tmp_path: Path):
    """② ジョブの result.parallel — ③ の並列数既定はこれ (config 無し = None → 1)。
    全局推奨 4 を焼くと 24GB で 4 スロット起動失敗した回帰 (2026-11-09)。"""
    mod = _fake_module(tmp_path, with_venv=True)
    mod = _seed_tables(monkeypatch, mod)
    mgr = _manager(tmp_path)

    async def fake_step(self, job, step):  # noqa: ANN001
        (Path(step["cwd"]) / job["config_name"]).write_text(
            json.dumps({"exe": "e", "args": [], "parallel": 8}), encoding="utf-8")
        return 0, ""

    monkeypatch.setattr(InstallManager, "_run_step", fake_step)

    async def go() -> dict:
        job = mgr.start_strata_model({"family": "qwen", "model": "Q2_0", "context": 131072})
        await mgr._tasks[job["id"]]
        return mgr.get(job["id"]) or {}

    done = asyncio.run(go())
    assert done["status"] == "done"
    assert done["result"]["parallel"] == 8
    assert done["result"]["config"].endswith("strata-q2_0.json")


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


def test_merge_config_ready_marks_models(tmp_path: Path):
    """既存 config の重み ok をモデル表へ引き写す (nvfp4 のような特殊レイアウトの
    バッジ源)。ファイル名解決が既に当てたモデルは上書きしない。"""
    mod = tmp_path
    g = mod / "g.gguf"
    g.write_bytes(b"x" * 1024)
    (mod / "strata-q2_0.json").write_text(
        json.dumps({"exe": "e", "args": ["--native", str(g)]}), encoding="utf-8")
    data = {"families": {"qwen": {"tag": "", "models": {
        "Q2_0": {},
        "IQ2_XS": {"have": {"files": 2, "found": 0, "gb": 0.0}},
    }}}}
    strata_setup._merge_config_ready(mod, data)
    assert data["families"]["qwen"]["models"]["Q2_0"]["have"] == {"files": 1, "found": 1, "gb": 0.0}
    assert data["families"]["qwen"]["models"]["IQ2_XS"]["have"]["found"] == 0  # config 無しは触らない


def test_installed_configs_weights_status(tmp_path: Path):
    """「JSON が在る」だけでは起動能否は分からない — config が参照する重みの
    実在 (単一ファイル / マルチシャード / --pack ディレクトリ / tokenizer) を検証する。"""
    mod = tmp_path / "module" / "strata"
    mod.mkdir(parents=True)
    wdir = tmp_path / "data" / "models"
    wdir.mkdir(parents=True)
    (wdir / "solo.gguf").write_bytes(b"x" * (2**30 // 4))       # 0.25 GiB
    for n in (1, 2):
        (wdir / f"M-0000{n}-of-00003.gguf").write_bytes(b"x")   # 3 枚のうち 2 枚だけ
    pack = tmp_path / "data" / "packs" / "pk1"
    (pack / "sub").mkdir(parents=True)
    (pack / "sub" / "a.gguf").write_bytes(b"x")
    tok = tmp_path / "data" / "tok"
    tok.mkdir()

    def cfg(name: str, data: dict) -> None:
        (mod / name).write_text(json.dumps(data), encoding="utf-8")

    cfg("strata-solo.json", {"exe": "e", "args": ["--native", str(wdir / "solo.gguf")],
                             "tokenizer": str(tok)})
    cfg("strata-shards.json", {"exe": "e", "args": ["--native", str(wdir / "M-00001-of-00003.gguf")],
                               "tokenizer": str(tok)})
    cfg("strata-pack.json", {"exe": "e", "args": ["--pack", str(pack)], "tokenizer": str(tok)})
    cfg("strata-norefs.json", {"exe": "e", "args": [], "tokenizer": str(tok)})
    cfg("strata-gone.json", {"exe": "e", "args": ["--native", str(wdir / "gone.gguf")],
                             "tokenizer": str(tok)})
    cfg("strata-notok.json", {"exe": "e", "args": ["--native", str(wdir / "solo.gguf")],
                              "tokenizer": str(tmp_path / "data" / "no-tok")})
    got = {c["name"]: c["weights"] for c in strata_setup.installed_configs(mod)}
    assert got["strata-solo.json"]["ok"] is True
    assert got["strata-solo.json"]["size_gb"] == 0.3 or got["strata-solo.json"]["size_gb"] > 0
    assert got["strata-shards.json"]["ok"] is False
    assert "M-00003-of-00003.gguf" in got["strata-shards.json"]["missing"]
    assert got["strata-pack.json"]["ok"] is True
    assert got["strata-norefs.json"]["ok"] is None       # 判断しない (UI は素通し)
    assert got["strata-gone.json"]["ok"] is False
    assert got["strata-notok.json"]["ok"] is False       # tokenizer ディレクトリ欠損


def test_strata_model_api_lists_installed_configs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from llm_launcher import app as app_mod
    from llm_launcher import strata_setup as ss

    mod = tmp_path / "module" / "strata"
    mod.mkdir(parents=True)
    (mod / "setup.py").write_text("# fake\n", encoding="utf-8")   # load_options の入口ガード
    (mod / "strata-q2_0.json").write_text('{"exe": "/x/strata", "args": [], "model_name": "qwen3.8-flash-next-q2_0", "parallel": 4}',
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
    # parallel は ③ の既定になる (config 無し = None → UI は 1 に倒す)
    assert body["configs"][0]["parallel"] == 4


# ---------------------------------------------------------------- 0.2.19+ 独自エンジン家系 (Hy4-Flash / Inkling-2-Mini)

def test_build_argv_hy4_inkling(monkeypatch, tmp_path: Path):
    """Hy4-Flash: 単一 GPU 宣言 (複数指定は拒否)・コンテキストは純正メニュー 8K-128K。
    Inkling-2-Mini: 1M まで (up to 1M supported)・NVIDIA 2 枚 EP は可。
    config 名は純正の strata-<tag+model>.json 作法そのまま。"""
    mod = tmp_path / "module" / "strata"
    (mod / ".venv" / "bin").mkdir(parents=True)
    (mod / "setup.py").write_text("# fake\n", encoding="utf-8")
    mod = _seed_tables(monkeypatch, mod)
    argv, cfg = strata_setup.build_argv(mod, {"family": "hy4-flash", "model": "SQ-64",
                                              "context": 131072, "vision": "no"})
    assert argv == ["setup.py", "--family", "hy4-flash", "--model", "SQ-64",
                    "--context", "131072", "--vision", "no",
                    "--yes", "--no-start", "--no-browser"]
    assert cfg == "strata-hy4-flash-sq-64.json"
    # vision "" = 渡さない = 純正既定 ON (両家系とも画像既定 ON)
    argv2, cfg2 = strata_setup.build_argv(mod, {"family": "inkling-2-mini", "model": "SQ-20",
                                                "context": 1048576})
    assert "--vision" not in argv2
    assert cfg2 == "strata-inkling-2-mini-sq-20.json"
    # 単一カード指定は両家系とも可
    argv3, _ = strata_setup.build_argv(mod, {"family": "hy4-flash", "model": "SQ-64",
                                             "context": 8192, "gpus": "1"})
    assert argv3[argv3.index("--gpu") + 1] == "1"
    # Inkling は 2 枚 EP (--gpus 0,1 / all) が純正どおり通る
    argv4, _ = strata_setup.build_argv(mod, {"family": "inkling-2-mini", "model": "SQ-15",
                                             "context": 32768, "gpus": "0,1"})
    assert argv4[argv4.index("--gpus") + 1] == "0,1"
    for bad in ({"family": "hy4-flash", "model": "SQ-64", "context": 262144},   # 純正メニュー外
                {"family": "hy4-flash", "model": "SQ-64", "context": 131072, "gpus": "0,1"},
                {"family": "hy4-flash", "model": "SQ-64", "context": 131072, "gpus": "all"},
                {"family": "hy4-flash", "model": "SQ-99", "context": 131072},
                {"family": "inkling-2-mini", "model": "SQ-20", "context": 204800},  # 一覧外
                {"family": "inkling-2-mini", "model": "SQ-64", "context": 32768}):  # 家系違いの size
        with pytest.raises(ValueError):
            strata_setup.build_argv(mod, bad)


def test_weights_status_strata_native(tmp_path: Path):
    """0.2.19+ の .strata ネイティブ重み: --native / --engram / --vision の引数が
    参照するファイルの実在を検証する (Engram / Vision 欠損も ok=False)。"""
    wdir = tmp_path / "models" / "hy4-flash"
    wdir.mkdir(parents=True)
    build = wdir / "Hy4-Flash-SQ-64.strata"
    engram = wdir / "Hy4-Flash-Engram-Q8.strata"
    vision = wdir / "Hy4-Flash-Vision.strata"
    for f in (build, engram, vision):
        f.write_bytes(b"x" * 2048)
    tok = tmp_path / "tok"
    tok.mkdir()
    doc = {"exe": "/x/strata-hy4", "arch": "hy4flash",
           "args": ["--native", str(build), "--engram", str(engram),
                    "--max-context", "131072", "--vision", str(vision)],
           "tokenizer": str(tok)}
    w = strata_setup.weights_status(doc)
    assert w["ok"] is True and w["size_gb"] is not None
    engram.unlink()
    w = strata_setup.weights_status(doc)
    assert w["ok"] is False and "Hy4-Flash-Engram-Q8.strata" in w["missing"]
    # Inkling-2-Mini: --native + --vision (Vision shard)
    idir = tmp_path / "models" / "inkling-2-mini"
    idir.mkdir(parents=True)
    ib = idir / "Inkling-2-Mini-SQ-20.strata"
    iv = idir / "Inkling-2-Mini-Vision.strata"
    ib.write_bytes(b"x" * 2048)
    iv.write_bytes(b"x" * 1024)
    idoc = {"exe": "/x/strata-inkling", "arch": "inkling2_loop",
            "args": ["--native", str(ib), "--max-context", "32768", "--vision", str(iv)],
            "tokenizer": str(tok), "parallel": 1}
    assert strata_setup.weights_status(idoc)["ok"] is True
    iv.unlink()
    assert strata_setup.weights_status(idoc)["ok"] is False


def test_weights_status_boolean_vision_flag_not_a_ref(tmp_path: Path):
    """Flash-Next 系の --vision は値なしのブールフラグ (次は別のフラグ) —
    重み参照として扱わない (Hy4-Flash / Inkling-2-Mini の --vision <file> との出し分け)。
    実 config (strata-nvfp4-*.json) で次が --vram-reserve-mib だった回帰。"""
    g = tmp_path / "q.gguf"
    g.write_bytes(b"x" * 2048)
    doc = {"exe": "/x/strata",
           "args": ["--native", str(g), "--vision", "--vram-reserve-mib", "700"]}
    w = strata_setup.weights_status(doc)
    assert w["ok"] is True
    assert "--vram-reserve-mib" not in " ".join(w["missing"])


def test_config_family_arch_and_no_tuning():
    """家系判定は表の arch が単一の源 (Flash-Next 系は arch 無し = None)。
    NO_TUNING_ARCHS は純正 tune_commands の HY4_NO_TUNING / INKLING_NO_TUNING 対応。"""
    fams = FAKE_TABLES["families"]
    assert strata_setup.config_family({"arch": "hy4flash"}, fams) == "hy4-flash"
    assert strata_setup.config_family({"arch": "inkling2_loop"}, fams) == "inkling-2-mini"
    assert strata_setup.config_family({"arch": "qwen35moe"}, fams) == "qwen36"
    assert strata_setup.config_family({"args": ["--native", "/x.gguf"]}, fams) is None
    assert strata_setup.config_family({}, fams) is None
    assert set(strata_setup.NO_TUNING_ARCHS) == {"hy4flash", "inkling2_loop"}


def test_strata_config_args_family_kv_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """③ config-args: 家系 (arch) と --kv 編集可否を返す。--kv は Flash-Next 系だけ
    (own-path / Hy4-Flash / Inkling-2-Mini はエンジン固有 — 書き込みは 400)。"""
    from fastapi.testclient import TestClient

    from llm_launcher import app as app_mod
    from llm_launcher import strata_setup as ss

    mod = tmp_path / "module" / "strata"
    mod.mkdir(parents=True)
    (mod / "setup.py").write_text("# fake\n", encoding="utf-8")
    (mod / "strata-hy4-flash-sq-64.json").write_text(json.dumps({
        "exe": "/x/strata-hy4", "arch": "hy4flash",
        "args": ["--native", "/m/Hy4-Flash-SQ-64.strata", "--max-context", "131072"]}),
        encoding="utf-8")
    (mod / "strata-q2_0.json").write_text(json.dumps({
        "exe": "/x/strata", "args": ["--native", "/m/q.gguf", "--max-context", "131072"]}),
        encoding="utf-8")
    tables = (time.monotonic(), dict(FAKE_TABLES))
    monkeypatch.setattr(ss, "_CACHE", {str(mod): tables, str(mod.resolve()): tables})
    client = TestClient(app_mod.create_app(tmp_path))

    def prof(cfg: str) -> str:
        r = client.post("/api/profiles", json={"name": "t", "backend": "strata",
                                               "values": {"config": cfg, "engine": "strata"}})
        assert r.status_code in (200, 201), r.text
        return r.json()["id"]

    pid = prof("module/strata/strata-hy4-flash-sq-64.json")
    r = client.get(f"/api/profiles/{pid}/config-args").json()
    assert r["family"] == "hy4-flash" and r["kv_ok"] is False
    assert client.post(f"/api/profiles/{pid}/config-args",
                       json={"updates": {"--kv": "int8"}}).status_code == 400
    assert client.post(f"/api/profiles/{pid}/config-args",
                       json={"updates": {"--max-context": "65536"}}).status_code == 200
    # Flash-Next 系 (arch 無し) は --kv 編集可のまま
    pid2 = prof("module/strata/strata-q2_0.json")
    r2 = client.get(f"/api/profiles/{pid2}/config-args").json()
    assert r2["family"] is None and r2["kv_ok"] is True
    assert client.post(f"/api/profiles/{pid2}/config-args",
                       json={"updates": {"--kv": "int8"}}).status_code == 200


def test_wizard_strata_new_families_ui_wiring():
    """③ の UI 配線: Strata の --kv は arch 持ち家系で出し分かれ、Hy4-Flash は
    並列数の質問を出さない。⑤ は調整不要の案内を出す。"""
    import llm_launcher

    js = (Path(llm_launcher.__file__).parent / "static" / "setup.js").read_text("utf-8")
    assert 'if (W.backend === "strata" && W.cfgArgs.family) {' in js
    assert 'if (W.cfgArgs.family === "hy4-flash") delete cfg.par;' in js
    assert "|| W.tableReq.family === \"qwen\") : !fam.arch," in js
    assert '/^strata-(hy4-flash|inkling-2-mini)/.test(cn)' in js
