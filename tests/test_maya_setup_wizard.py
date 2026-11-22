"""Maya セットアップタブ (📦) / セットアップ画面 ② 統合の回帰テスト。

Maya の純正 maya.sh / maya.py は対話インストーラだが、--yes / --download-model で
完全非対話になるので、Strata と同じ立場で UI から引ける:
  ① clone (submodule 取得済みならスキップ) → .venv (標準 venv+pip — maya.sh と同じ)
     → tools step (薄い driver maya_tools.py が純正 tools_step() で PY_PACKAGES +
     llama.cpp ソース) → verify (import serve.server + 導入 stamp / llama.cpp 実在)。
     エンジンビルドは家系別 (build/maya / maya-ds4 / maya-mm3) なので ① ではやらない。
  ② モデル構成: 純正 maya.py を --model / --ds4-quant / --mm3-quant / --qwen-quant /
     --context + --yes --no-start --plain --download-model の非対話で走らせる単一 step
     ジョブ。表 (家系別 model/context) は submodule が単一の源 (maya_setup が subprocess
     で読み、選択肢をホワイトリスト検証するだけ)。エンジンビルド + pack + 速度計測も
     純正のこの中で走る。
  ③ 稼働設定は家系別: GLM / Qwen / MiniMax GGUF = --kv + gpu (layer split)、
     DeepSeek / MiniMax EXL3 = gpu + --tp (カード枚数から導出。--kv はエンジン固有で編集させない)。
見張る対象:
  1. spec の形 (venv_pip / tools_step / check_files)
  2. start_install の計画 — uv が在っても pip、install でなく tools step、
     precheck / verify が stamp + llama.cpp の実在も要求すること
  3. driver の入口ガード (cwd 違い / maya.py が純正でない) は正直に失敗すること
  4. /api/installs の installable が maya を tools_step / tool=pip で出すこと
  5. build_argv のホワイトリスト (表に無い family/model/context/vision は注入でも拒否)、
     設定 JSON 名の純正作法 (maya-<quant>.json / maya-ds4.json / config_stem)
  6. /api/installs/maya-model の GET/POST と {jid} ルートとの衝突がないこと
  7. ② ジョブの result.parallel は args の --parallel (Maya の config 形)、
     config が出来ない完了は失敗として記録すること (maya.py は 0 で返るため)
  8. ③ config-args の家系別ガード (GLM に --tp / DS4 に --kv は 400) と
     DS4 の gpu 編集が --tp をカード枚数から導出すること (min(2, 枚数)、純正同款)
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_launcher import installer, maya_setup
from llm_launcher.installer import INSTALL_SPECS, InstallError, InstallManager
from llm_launcher.store import JsonStore

ROOT = Path(__file__).resolve().parents[1]
SPEC = INSTALL_SPECS["maya"]
DRIVER = ROOT / "src" / "llm_launcher" / "maya_tools.py"


def test_spec_shape():
    assert SPEC["clone"] == "https://github.com/mw00/project-maya"
    assert SPEC["module_path"] == "module/maya"
    # 検出 (serve.server 実 import) と同じ確認。依存は遅延 import で import だけだと
    # 未導入でも通るため、導入 stamp と llama.cpp ソースの実在も要求する
    assert SPEC["check"] == "serve.server"
    assert SPEC["venv_pip"] is True
    assert SPEC["tools_step"] is True
    assert SPEC["requirements"] == ""
    assert ".venv/.strata-pip.json" in SPEC["check_files"]
    assert "third_party/llama.cpp/ggml/CMakeLists.txt" in SPEC["check_files"]
    # エンジンは家系別に ② でビルドする (① の完了条件に engine_bin は無い)
    assert not SPEC.get("engine_bin")
    assert not SPEC.get("engine_fetch")
    assert not SPEC.get("cmake")


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
    # uv は見えている — それでも venv_pip spec (Maya) は pip で計画しなければいけない
    monkeypatch.setattr(installer, "find_uv", lambda: "/usr/bin/uv")
    mgr = _manager(root)

    async def go() -> None:
        await mgr.start_install("maya")
        await asyncio.sleep(0)

    asyncio.run(go())
    return got


def _submodule(root: Path) -> Path:
    mod = root / "module" / "maya"
    (mod / "serve").mkdir(parents=True)
    (mod / "serve" / "server.py").write_text("# clone content\n", encoding="utf-8")
    (mod / "maya.py").write_text("# clone content\n", encoding="utf-8")
    return mod


def test_plan_for_existing_submodule_skips_clone(monkeypatch, tmp_path: Path):
    mod = _submodule(tmp_path)
    got = _plan(monkeypatch, tmp_path)
    steps = got["steps"]
    names = [s["name"] for s in steps]
    # -e . / -r ではなく純正 tools_step (maya.py はパッケージングでない setup.py を持つ)
    assert names == ["venv", "precheck", "tools", "verify"]
    assert got["job"]["tool"] == "pip"
    assert got["job"]["uv_path"] == ""
    venvpy = str((mod / ".venv" / "bin" / "python").resolve())
    tools = next(s for s in steps if s["name"] == "tools")
    assert tools["argv"] == [venvpy, str(DRIVER)]
    assert tools["cwd"].endswith("module/maya")
    # precheck / verify は import だけでなく導入 stamp + llama.cpp ソースも要求する
    for s in steps:
        if s["name"] in ("precheck", "verify"):
            assert "serve.server" in s["argv"][2]
            assert ".venv/.strata-pip.json" in s["argv"][2]
            assert "third_party/llama.cpp/ggml/CMakeLists.txt" in s["argv"][2]


def test_plan_without_clone_starts_with_git_clone(monkeypatch, tmp_path: Path):
    got = _plan(monkeypatch, tmp_path)
    steps = got["steps"]
    assert steps[0]["name"] == "clone"
    assert steps[0]["argv"][:3] == ["git", "clone", "--depth"]
    assert steps[0]["argv"][-2:] == [SPEC["clone"],
                                     str((tmp_path / "module" / "maya").resolve())]


def test_strata_plan_uses_same_tools_step_approach_as_maya(monkeypatch, tmp_path: Path):
    """Strata も Maya と同じ手法: 純正パッケージ導入 (tools) + stamp verify。
    エンジン受信の engine step は ① に無い (② の純正セットアップの仕事)。"""
    mod = tmp_path / "module" / "strata"
    (mod / "serve").mkdir(parents=True)
    (mod / "serve" / "server.py").write_text("# clone\n", encoding="utf-8")
    monkeypatch.setattr(installer, "find_uv", lambda: "")
    got: dict[str, object] = {}

    async def fake(self, job, spec, steps):  # noqa: ANN001
        got["steps"] = [dict(s) for s in steps]

    monkeypatch.setattr(InstallManager, "_run_install", fake)
    mgr = _manager(tmp_path)

    async def go() -> None:
        await mgr.start_install("strata")
        await asyncio.sleep(0)

    asyncio.run(go())
    names = [s["name"] for s in got["steps"]]
    assert names == ["venv", "precheck", "tools", "verify"]
    tools = next(s for s in got["steps"] if s["name"] == "tools")
    assert tools["argv"][1].endswith("strata_tools.py")


# ---------------------------------------------------------------- driver (maya_tools.py)

def test_driver_refuses_foreign_cwd(tmp_path: Path):
    r = subprocess.run([sys.executable, str(DRIVER)], cwd=tmp_path,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert "cwd" in r.stdout


def test_driver_fails_honestly_when_maya_py_is_not_mayas(tmp_path: Path):
    (tmp_path / "serve").mkdir()
    (tmp_path / "serve" / "server.py").write_text("", encoding="utf-8")
    (tmp_path / "setup.py").write_text("# 純正 setup.py ではない空ファイル\n", encoding="utf-8")
    (tmp_path / "maya.py").write_text("# tools_step の無い maya.py\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(DRIVER)], cwd=tmp_path,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert r.stdout.startswith("maya_tools:")
    assert "tools_step" in r.stdout


# 純正 tools_step の面 (yes / llama_dir を受け、llama のパスを返す) を最小で模す stub。
# --yes 相当で呼ばれていること (対話確認に到達しないこと) を trap で証明する。
STUB_MAYA = '''
from pathlib import Path
def tools_step(a):
    if not getattr(a, "yes", False):
        raise SystemExit("tools_step asked (not --yes)")
    if getattr(a, "llama_dir", None) is not None:
        raise SystemExit("llama_dir was given")
    Path("tools_ran").write_text("ok")
    return Path("third_party/llama.cpp")
'''


def test_driver_runs_stock_tools_step_unattended(tmp_path: Path):
    (tmp_path / "serve").mkdir()
    (tmp_path / "serve" / "server.py").write_text("", encoding="utf-8")
    (tmp_path / "setup.py").write_text("# stub\n", encoding="utf-8")
    (tmp_path / "maya.py").write_text(STUB_MAYA, encoding="utf-8")
    r = subprocess.run([sys.executable, str(DRIVER)], cwd=tmp_path,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "tools_ran").is_file()
    assert "tools_step" in r.stdout


# ---------------------------------------------------------------- API / ウィザード文言

def test_installs_api_exposes_maya_with_tools_step(tmp_path: Path):
    from fastapi.testclient import TestClient

    from llm_launcher import app as app_mod

    (tmp_path / "data").mkdir(parents=True)
    client = TestClient(app_mod.create_app(tmp_path))
    entries = client.get("/api/installs").json()["installable"]
    ent = next(e for e in entries if e["id"] == "maya")
    assert ent["name"] == "Project Maya"
    assert ent["tools_step"] is True
    assert not ent.get("engine_fetch") and ent["cmake"] is False
    # /api/installs の tool (グローバル) にかかわらず、この entry の tool は pip
    assert ent["tool"] == "pip"


def test_wizard_copy_mentions_maya_model_step():
    import llm_launcher

    static = Path(llm_launcher.__file__).parent / "static"
    js = (static / "setup.js").read_text("utf-8")
    assert "/api/installs/maya-model" in js
    assert "loadMaya" in js and "renderModelMaya" in js
    assert "tools_step" in js                 # 確認画面の手順組み立て
    assert "エンジンビルド" in js              # ① の確認 / ② のヒント (家系別・② で実行)
    assert "maya:" in js                      # INSTALL_NOTES の maya 追い書き
    # ② の面板は Strata と共通 (id は md-strata のまま)。家系別 ③ の出し分け
    assert "mdTableBackend" in js
    assert "kv_ok" in js and "tp_ok" in js
    html = (static / "setup.html").read_text("utf-8")
    assert 'id="md-strata"' in html and 'id="md-vision-t2"' in html
    tab = (static / "js" / "views" / "setup.js").read_text("utf-8")
    assert "モデル準備" in tab                 # 📦 履歴の step / タイトル
    assert "tools_step" in tab


# ---------------------------------------------------------------- ② モデル構成 (maya_setup)

FAKE_TABLES = {
    "available": True,
    "families": {
        "glm": {"title": "GLM-5.3-Flash / GLM-5.3 (744B)",
                "contexts": [8192, 32768, 65536, 131072, 262144, 524288, 1048576],
                "max_context": 1048576, "multi_gpu": True, "rec": "Maya-S24", "engine_ready": False,
                "models": {"Maya-S24": {"label": "GLM-5.3 · Maya-S24", "download_gb": 94.7,
                                        "vision": True, "config": "maya-maya-s24.json",
                                        "rec_context": 32768},
                           "Maya-M": {"label": "GLM-5.3 · Maya-M", "download_gb": 116.0,
                                      "vision": True, "config": "maya-maya-m.json",
                                      "rec_context": 32768},
                           "GLM-5.3-Maya-XS": {"label": "GLM-5.3 (744B) · Maya-XS",
                                               "download_gb": 170.0, "memory_gb": 176,
                                               "vision": False, "g53": True,
                                               "config": "maya-glm-5.3-maya-xs.json",
                                               "contexts": [8192, 32768, 65536, 131072],
                                               "rec_context": 32768}}},
        "deepseek": {"title": "DeepSeek-V4.1-Flash-1017",
                     "contexts": [32768, 65536, 131072, 262144, 524288, 1048576],
                     "max_context": 1048576, "multi_gpu": True, "rec": "gguf-q8",
                     "model_arg": "DeepSeek-V4.1-Flash-1017", "quant_flag": "--ds4-quant",
                     "engine_ready": True,
                     "models": {"gguf-q8": {"label": "DeepSeek V4.1 · UD-Q8_K_XL",
                                            "download_gb": 508, "vision": True,
                                            "published": True, "config": "maya-ds4.json"},
                                "maya-m": {"label": "DeepSeek V4.1 · Maya-M",
                                           "published": False, "config": "maya-ds4.json"}}},
        "minimax": {"title": "MiniMax-M3.1-Flash",
                    "contexts": [32768, 65536, 131072, 262144, 524288, 1048576],
                    "max_context": 1048576, "multi_gpu": True, "rec": "exl3",
                    "model_arg": "MiniMax-M3.1-Flash", "quant_flag": "--mm3-quant",
                    "engine_ready": False,
                    "models": {"exl3": {"label": "MiniMax M3.1 · EXL3 3.5 bpw",
                                        "vision": True, "config": "maya-mm3.json"},
                               "gguf-xs": {"label": "MiniMax M3.1 · Maya-XS",
                                           "vision": True, "config": "maya-minimax-maya-xs.json"}}},
        "qwen": {"title": "Qwen-3.8-Flash-Next (CYBER-FROST)",
                 "contexts": [32768, 65536, 131072, 262144],
                 "max_context": 262144, "multi_gpu": True, "rec": "q5_k_m",
                 "model_arg": "cyber-frost", "quant_flag": "--qwen-quant",
                 "engine_ready": True,
                 "models": {"q5_k_m": {"label": "Qwen-3.8-Flash-Next · Q5_K_M",
                                       "vision": True, "config": "maya-cyber-frost.json"},
                            "ud-iq2_xs": {"label": "Qwen-3.8-Flash-Next · unsloth UD-IQ2_XS",
                                          "vision": True, "config": "maya-qwen-ud-iq2_xs.json"}}},
        "hy4": {"title": "Hy4-Flash",
                "contexts": [4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1048576],
                "max_context": 1048576, "multi_gpu": True, "rec": "maya-xs",
                "model_arg": "Hy4-Flash", "quant_flag": "--hy4-quant",
                "engine_ready": False,
                "models": {"maya-xs": {"label": "Hy4-Flash · Maya-XS (108 GB)",
                                       "download_gb": 108.2, "memory_gb": 52.0,
                                       "vision": True, "published": True,
                                       "config": "maya-hy4-flash-maya-xs.json",
                                       "contexts": [4096, 8192, 16384, 32768, 65536, 131072],
                                       "rec_context": 32768},
                           "maya-l": {"label": "Hy4-Flash · Maya-L (153 GB)",
                                      "download_gb": 152.9, "memory_gb": 100.0,
                                      "vision": True, "published": True,
                                      "config": "maya-hy4-flash-maya-l.json",
                                      "contexts": [], "fits": False,
                                      "fit_error": "Hy4-Flash needs about 12.0 GB of free VRAM ..."},
                           "q4_k_m": {"label": "Hy4-Flash · Q4_K_M (168 GB)",
                                      "download_gb": 168.0, "memory_gb": 170.0,
                                      "vision": True, "published": True,
                                      "config": "maya-hy4-flash.json",
                                      "contexts": [32768, 65536, 131072, 262144],
                                      "rec_context": 131072}}},
        "ik2": {"title": "Inkling 2 Mini (experimental)",
                "contexts": [32768, 65536, 131072, 262144],
                "max_context": 262144, "multi_gpu": False, "rec": "ud-q4_k_xl",
                "model_arg": "Inkling-2-Mini", "engine_ready": False,
                "models": {"ud-q4_k_xl": {"label": "Inkling 2 Mini · unsloth UD-Q4_K_XL (18 GB)",
                                          "download_gb": 17.6, "vision": True,
                                          "experimental": True,
                                          "config": "maya-inkling-2-mini.json",
                                          "contexts": [32768, 65536, 131072, 262144],
                                          "rec_context": 131072, "layout": "resident"}}},
    },
    "pc": {"ram_gb": 128.0, "vram_gb": 24.0, "data_dir": "/x/Maya-data", "disk_free_gb": 400.0,
           "gpus": [{"index": 0, "name": "RTX A", "vram_gb": 24.0, "usable": True},
                    {"index": 1, "name": "RTX B", "vram_gb": 24.0, "usable": True},
                    {"index": 2, "name": "GT", "vram_gb": 8.0, "usable": False}]},
}


def _seed_tables(monkeypatch, mod: Path) -> Path:
    """subprocess 実行なしで load_options が表を返すようにキャッシュへ種を打つ。
    呼び出し側が resolved / 未 resolved のどちらで渡しても hit するよう両キー入れる。"""
    tables = dict(FAKE_TABLES)
    monkeypatch.setattr(maya_setup, "_CACHE",
                        {str(mod): (time.monotonic(), tables),
                         str(mod.resolve()): (time.monotonic(), tables)})
    return mod.resolve()


def _fake_module(tmp_path: Path, *, with_venv: bool) -> Path:
    mod = _submodule(tmp_path)
    if with_venv:
        vp = mod / ".venv" / "bin"
        vp.mkdir(parents=True)
        (vp / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    return mod


def test_build_argv_whitelist_and_rejection(monkeypatch, tmp_path: Path):
    mod = _fake_module(tmp_path, with_venv=True)
    mod = _seed_tables(monkeypatch, mod)
    argv, cfg = maya_setup.build_argv(mod, {"family": "glm", "model": "Maya-S24",
                                            "context": 32768, "vision": "off"})
    # --yes (全質問が既定確定) + --no-start + --plain (TUI でない) + --download-model は常に付ける。
    # GLM は MTP 既定で計測が起きないので --drafting は付けない
    assert argv == ["maya.py", "--model", "Maya-S24", "--context", "32768", "--no-vision",
                    "--yes", "--no-start", "--plain", "--download-model"]
    assert cfg == "maya-maya-s24.json"
    # vision "" / "on" = 渡さない (純正既定 ON)。DeepSeek は家系名 + --ds4-quant。
    # drafter 速度計測は ⑤ に集約するのでセットアップは --drafting off (計測しない)
    argv2, cfg2 = maya_setup.build_argv(mod, {"family": "deepseek", "model": "gguf-q8",
                                              "context": 262144, "vision": ""})
    assert "--no-vision" not in argv2
    assert argv2[1:5] == ["--model", "DeepSeek-V4.1-Flash-1017", "--ds4-quant", "gguf-q8"]
    assert argv2[argv2.index("--drafting") + 1] == "off"
    assert cfg2 == "maya-ds4.json"
    for fam, model, ctx in (("minimax", "gguf-xs", 65536), ("qwen", "q5_k_m", 65536)):
        av, _ = maya_setup.build_argv(mod, {"family": fam, "model": model, "context": ctx})
        assert av[av.index("--drafting") + 1] == "off", fam
    # MiniMax / Qwen の config 名は純正の config_stem 作法
    _, cfg3 = maya_setup.build_argv(mod, {"family": "minimax", "model": "gguf-xs", "context": 65536})
    assert cfg3 == "maya-minimax-maya-xs.json"
    argv4, cfg4 = maya_setup.build_argv(mod, {"family": "qwen", "model": "ud-iq2_xs",
                                              "context": 131072})
    assert argv4[1:5] == ["--model", "cyber-frost", "--qwen-quant", "ud-iq2_xs"]
    assert cfg4 == "maya-qwen-ud-iq2_xs.json"
    # GLM は trained 1M まで選択肢 (owner 方針: 対応する全てで 1M まで。純正 --context は任意値を受ける)
    argv5, _ = maya_setup.build_argv(mod, {"family": "glm", "model": "Maya-S24", "context": 1048576})
    assert argv5[argv5.index("--context") + 1] == "1048576"
    # ホワイトリスト外 (注入を試みる値も含む) は全て ValueError
    for bad in ({"family": "glm", "model": "../evil", "context": 32768},
                {"family": "--x", "model": "Maya-S24", "context": 32768},
                {"family": "nope", "model": "Maya-S24", "context": 32768},
                {"family": "glm", "model": "Maya-S24", "context": 204800},   # GLM の選択肢に無い
                {"family": "glm", "model": "Maya-S24", "context": "32768 --yes"},
                {"family": "glm", "model": "Maya-S24", "context": 32768, "vision": "-x"},
                {"family": "deepseek", "model": "Maya-S24", "context": 65536}):  # 家系違いの quant
        with pytest.raises(ValueError):
            maya_setup.build_argv(mod, bad)


def test_build_argv_parallel(monkeypatch, tmp_path: Path):
    """設定先行フロー: ③ の並列数回答はセットアップの --parallel になる (config に
    書かれ、DS4 / MM3 の推奨コンテキスト計算にも効く)。Qwen は純正 check_parallel が
    拒否するので argv に載せない (ValueError で正直に弾く)。"""
    mod = _fake_module(tmp_path, with_venv=True)
    mod = _seed_tables(monkeypatch, mod)
    argv, _ = maya_setup.build_argv(mod, {"family": "deepseek", "model": "gguf-q8",
                                          "context": 65536, "parallel": 4})
    assert argv[argv.index("--parallel") + 1] == "4"
    assert argv.index("--parallel") < argv.index("--yes")
    argv2, _ = maya_setup.build_argv(mod, {"family": "glm", "model": "Maya-S24",
                                           "context": 32768, "parallel": 2})
    assert "--parallel" in argv2
    # 未指定 (None / 空) は渡さない = 純正既定
    argv3, _ = maya_setup.build_argv(mod, {"family": "glm", "model": "Maya-S24",
                                           "context": 32768})
    assert "--parallel" not in argv3
    for bad in ({"family": "qwen", "model": "q5_k_m", "context": 65536, "parallel": 2},
                {"family": "glm", "model": "Maya-S24", "context": 32768, "parallel": 0},
                {"family": "glm", "model": "Maya-S24", "context": 32768, "parallel": 9},
                {"family": "glm", "model": "Maya-S24", "context": 32768, "parallel": "2; rm"}):
        with pytest.raises(ValueError):
            maya_setup.build_argv(mod, bad)


def test_build_argv_gpu_selection(monkeypatch, tmp_path: Path):
    """GPU 指定: 単一カード = --gpu N / 複数 = --gpus "0,1" / all = 全 usable / auto=無し。
    検出外 GPU は拒否する (Maya の --gpus は明示リストで、all キーワードは無い)。"""
    mod = _fake_module(tmp_path, with_venv=True)
    mod = _seed_tables(monkeypatch, mod)
    base = {"family": "glm", "model": "Maya-S24", "context": 32768}
    argv, _ = maya_setup.build_argv(mod, dict(base, gpus="1"))
    assert argv[argv.index("--gpu") + 1] == "1" and "--gpus" not in argv
    argv, _ = maya_setup.build_argv(mod, dict(base, gpus="0,1"))
    assert argv[argv.index("--gpus") + 1] == "0,1"
    argv, _ = maya_setup.build_argv(mod, dict(base, gpus="all"))
    assert argv[argv.index("--gpus") + 1] == "0,1"
    argv, _ = maya_setup.build_argv(mod, dict(base, gpus="auto"))
    assert "--gpus" not in argv and "--gpu" not in argv
    for bad in ("0,2", "2", "0,0", "9", "0; rm"):
        with pytest.raises(ValueError):
            maya_setup.build_argv(mod, dict(base, gpus=bad))


def test_build_argv_without_submodule_tables_fails(tmp_path: Path):
    """表が読めない (未 clone) は argv を作らない — 対話 maya.sh へ戻すしかない状態。"""
    mod = tmp_path / "no-maya"
    mod.mkdir()
    with pytest.raises(ValueError, match="未取得"):
        maya_setup.build_argv(mod, {"family": "glm", "model": "Maya-S24", "context": 32768})


def test_build_argv_against_real_submodule():
    """実 submodule が在る環境では、表読み出し (subprocess) と純正設定名作法が生きていること。"""
    mod = ROOT / "module" / "maya"
    if not (mod / "maya.py").is_file():
        pytest.skip("module/maya が未 clone")
    opts = maya_setup.load_options(mod)
    assert opts.get("available") is True
    fams = opts.get("families") or {}
    assert set(fams) == {"glm", "deepseek", "minimax", "qwen", "hy4", "ik2"}
    for fid, f in fams.items():
        assert f.get("models"), fid
        assert f.get("contexts"), fid
        assert f.get("rec") in f["models"], fid
        for m, d in f["models"].items():
            assert str(d.get("config", "")).startswith("maya-"), (fid, m)
            if "rec_context" in d:
                # モデル別 contexts (Hy4-Flash / GLM-5.3) が在ればその中、無ければ家系の中
                ctxs = d.get("contexts") or f["contexts"]
                assert d["rec_context"] in ctxs, (fid, m)
    # 推奨コンテキストは純正の式そのもの (UI の既定用)
    pc = opts.get("pc") or {}
    assert pc.get("ram_gb") and isinstance(pc.get("gpus"), list)
    argv, cfg = maya_setup.build_argv(mod, {"family": "glm", "model": "Maya-S24",
                                            "context": 32768, "vision": "off"})
    assert argv[:2] == ["maya.py", "--model"]
    assert argv[-4:] == ["--yes", "--no-start", "--plain", "--download-model"]
    assert cfg == "maya-maya-s24.json"
    # 組み立てたフラグは全て純正 maya.py の argparse に実在する (--help 相当の源码検査)
    src = (mod / "maya.py").read_text(encoding="utf-8")
    known = set(re.findall(r'add_argument\("(--[a-z0-9-]+)"', src))
    for a in argv[1:]:
        if a.startswith("--"):
            assert a in known, a
    for flag in ("--ds4-quant", "--mm3-quant", "--qwen-quant", "--hy4-quant", "--no-vision",
                 "--gpu", "--gpus", "--drafting", "--parallel"):
        assert flag in known, flag
    # Hy4-Flash (v1.1.68+): モデル別 contexts (hy4_fit) と config 名 (config_stem)
    hy4 = fams["hy4"]
    assert hy4.get("model_arg") and hy4.get("quant_flag") == "--hy4-quant"
    for m, d in hy4["models"].items():
        assert "contexts" in d, m          # hy4_fit / hy4_q4_contexts のモデル別一覧
        if d.get("fits") is False:
            assert d["contexts"] == [] and d.get("fit_error"), m
    xs = hy4["models"].get("maya-xs")
    if xs and xs.get("fits") is not False:
        argv, cfg = maya_setup.build_argv(mod, {"family": "hy4", "model": "maya-xs",
                                                "context": xs["rec_context"], "vision": "off"})
        assert argv[1:5] == ["--model", hy4["model_arg"], "--hy4-quant", "maya-xs"]
        assert argv[argv.index("--drafting") + 1] == "off"
        assert "--no-vision" in argv
        assert cfg == "maya-hy4-flash-maya-xs.json"
        # モデル別 contexts の外は拒否 (家系一覧に在っても hy4_fit が弾く値)
        outside = next((c for c in hy4["contexts"] if c not in xs["contexts"]), None)
        if outside is not None:
            with pytest.raises(ValueError):
                maya_setup.build_argv(mod, {"family": "hy4", "model": "maya-xs", "context": outside})
    # Inkling-2-Mini (v1.1.72+, ik2): 1 ファイルセット (quant フラグ無し)・単一 GPU・
    # 実験的プレビュー。fits=False (この PC に capability の合うカードが無い等) なら
    # 純正 IK2F.unsupported の理由そのまま選択不可
    ik2 = fams["ik2"]
    assert ik2.get("model_arg") == "Inkling-2-Mini" and not ik2.get("quant_flag")
    assert ik2.get("multi_gpu") is False
    assert ik2.get("rec") in ik2["models"]
    ik2m = ik2["models"][ik2["rec"]]
    assert ik2m["config"] == "maya-inkling-2-mini.json"
    assert ik2m.get("experimental") is True
    if ik2m.get("fits") is False:
        assert ik2m["contexts"] == [] and ik2m.get("fit_error")
        with pytest.raises(ValueError):
            maya_setup.build_argv(mod, {"family": "ik2", "model": ik2["rec"], "context": 32768})
    else:
        argv, cfg = maya_setup.build_argv(mod, {"family": "ik2", "model": ik2["rec"],
                                                "context": ik2m["rec_context"], "vision": "off"})
        assert argv[1:3] == ["--model", "Inkling-2-Mini"]
        assert "--drafting" not in argv    # 純正既定 auto (エンジンが初回起動時に計測)
        assert "--no-vision" in argv
        assert cfg == "maya-inkling-2-mini.json"
        for bad in ({"family": "ik2", "model": ik2["rec"], "context": 32768, "parallel": 2},
                    {"family": "ik2", "model": ik2["rec"], "context": 32768, "gpus": "0,1"},
                    {"family": "ik2", "model": ik2["rec"], "context": 1048576}):
            with pytest.raises(ValueError):
                maya_setup.build_argv(mod, bad)
    # GLM-5.3 (744B, G53_MODELS): glm 家系にモデル別 contexts で同居 (テキスト専用)
    g53 = [m for m, d in fams["glm"]["models"].items() if d.get("g53")]
    assert g53, "G53_MODELS の一覧が出ていること"
    for m in g53:
        d = fams["glm"]["models"][m]
        assert d["vision"] is False and d.get("contexts")
        argv, cfg = maya_setup.build_argv(mod, {"family": "glm", "model": m,
                                                "context": d["rec_context"]})
        assert argv[1:3] == ["--model", m]
        assert "--no-vision" not in argv    # vision 非対応モデルにフラグを付けない
        assert cfg == f"maya-{m.lower()}.json"
    # DeepSeek (v1.1.66+): 量子化別 config 名 (ds4_config_path)。既存 maya-ds4.json の
    # 量子化はそれ、他は maya-deepseek-<quant>.json の own 名
    ds4_cfgs = {m: d.get("config") for m, d in fams["deepseek"]["models"].items()}
    assert all(str(c).startswith("maya-") for c in ds4_cfgs.values())
    assert len(set(ds4_cfgs.values())) >= 1


# ---------------------------------------------------------------- ② ジョブ計画 (installer)

def test_start_maya_model_requires_venv(tmp_path: Path):
    _fake_module(tmp_path, with_venv=False)
    mgr = _manager(tmp_path)
    with pytest.raises(InstallError, match="①"):
        mgr.start_maya_model({"family": "glm", "model": "Maya-S24", "context": 32768})


def test_start_maya_model_plans_single_model_job(monkeypatch, tmp_path: Path):
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
        job = mgr.start_maya_model({"family": "deepseek", "model": "gguf-q8",
                                    "context": 262144, "vision": ""})
        await asyncio.sleep(0)
        return job

    job = asyncio.run(go())
    assert job["kind"] == "install" and job["phase"] == "model"
    assert job["backend"] == "maya" and job["steps"] == ["model"]
    assert job["config_name"] == "maya-ds4.json"
    assert job["model"] == "deepseek/gguf-q8"
    assert got["cwd"].endswith("module/maya")
    argv = got["argv"]
    assert str(argv[0]).endswith(str(Path(".venv/bin/python")))
    assert argv[1:] == ["maya.py", "--model", "DeepSeek-V4.1-Flash-1017",
                        "--ds4-quant", "gguf-q8", "--context", "262144",
                        "--drafting", "off",
                        "--yes", "--no-start", "--plain", "--download-model"]


def test_run_model_result_carries_parallel_from_args(monkeypatch, tmp_path: Path):
    """② ジョブの result.parallel — Maya の config は args の --parallel に入る
    (Strata のような top-level parallel は無い)。③ の並列数既定はこれ。"""
    mod = _fake_module(tmp_path, with_venv=True)
    mod = _seed_tables(monkeypatch, mod)
    mgr = _manager(tmp_path)

    async def fake_step(self, job, step):  # noqa: ANN001
        (Path(step["cwd"]) / job["config_name"]).write_text(
            json.dumps({"exe": "e", "args": ["--pack", "/p", "--max-context", "262144",
                                             "--parallel", "4"]}), encoding="utf-8")
        return 0, ""

    monkeypatch.setattr(InstallManager, "_run_step", fake_step)

    async def go() -> dict:
        job = mgr.start_maya_model({"family": "deepseek", "model": "gguf-q8", "context": 262144})
        await mgr._tasks[job["id"]]
        return mgr.get(job["id"]) or {}

    done = asyncio.run(go())
    assert done["status"] == "done"
    assert done["result"]["parallel"] == 4
    assert done["result"]["config"].endswith("maya-ds4.json")


def test_run_model_without_config_is_failure(monkeypatch, tmp_path: Path):
    """--download-model 付きの maya.py は config を書いてから 0 で返る。config が無い
    完了は失敗として記録する (exit 0 でも「出来た」と誤読しない — calibrate と同じ立場)。"""
    mod = _fake_module(tmp_path, with_venv=True)
    mod = _seed_tables(monkeypatch, mod)
    mgr = _manager(tmp_path)

    async def fake_step(self, job, step):  # noqa: ANN001
        return 0, "Nothing was downloaded."

    monkeypatch.setattr(InstallManager, "_run_step", fake_step)

    async def go() -> dict:
        job = mgr.start_maya_model({"family": "glm", "model": "Maya-S24", "context": 32768})
        await mgr._tasks[job["id"]]
        return mgr.get(job["id"]) or {}

    done = asyncio.run(go())
    assert done["status"] == "failed"
    assert "maya-<model>.json" in done["error"]


# ---------------------------------------------------------------- API (②)

def test_maya_model_api(tmp_path: Path):
    from fastapi.testclient import TestClient

    from llm_launcher import app as app_mod

    (tmp_path / "data").mkdir(parents=True)
    client = TestClient(app_mod.create_app(tmp_path))
    g = client.get("/api/installs/maya-model")
    assert g.status_code == 200
    body = g.json()
    # 静的パスは /api/installs/{jid} に呑まれない (登録順) + 未 clone は正直に
    assert body["available"] is False and body["venv_ready"] is False
    assert body["configs"] == [] and "id" not in body
    p = client.post("/api/installs/maya-model",
                    json={"family": "glm", "model": "Maya-S24", "context": 32768})
    assert p.status_code == 400 and "①" in p.json()["detail"]
    # body 検証: family 空は pydantic で 422
    assert client.post("/api/installs/maya-model",
                       json={"family": "", "model": "Maya-S24"}).status_code == 422


def test_maya_model_api_lists_installed_configs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from llm_launcher import app as app_mod

    mod = tmp_path / "module" / "maya"
    mod.mkdir(parents=True)
    (mod / "maya.py").write_text("# fake\n", encoding="utf-8")   # load_options の入口ガード
    (mod / "maya-ds4.json").write_text(
        '{"exe": "/x/maya-ds4", "args": ["--pack", "/p", "--parallel", "2"],'
        ' "model_name": "deepseek-v4.1-flash"}', encoding="utf-8")
    (mod / "maya-ds4.shared-settings.json").write_text('{"exe": "x", "args": []}', encoding="utf-8")
    (mod / "maya-not-a-config.json").write_text('{"nope": 1}', encoding="utf-8")
    tables = (time.monotonic(), dict(FAKE_TABLES))
    monkeypatch.setattr(maya_setup, "_CACHE", {str(mod): tables, str(mod.resolve()): tables})
    client = TestClient(app_mod.create_app(tmp_path))
    body = client.get("/api/installs/maya-model").json()
    assert body["available"] is True
    names = [c["name"] for c in body["configs"]]
    # shared-settings と maya.py configs() 作法外 (exe/args 無し) は除外される
    assert names == ["maya-ds4.json"]
    assert body["configs"][0]["model_name"] == "deepseek-v4.1-flash"
    # parallel は args の --parallel から (③ の既定になる。config 無し = None → UI は 1)
    assert body["configs"][0]["parallel"] == 2


# ---------------------------------------------------------------- config 棚卸し / 重み検証

def test_installed_configs_weights_status(tmp_path: Path):
    """「JSON が在る」だけでは起動能否は分からない — config が参照する重みの実在
    (pack の index + 実ファイル / GGUF シャード / vision / tokenizer) を検証する。"""
    mod = tmp_path / "module" / "maya"
    mod.mkdir(parents=True)
    wdir = tmp_path / "models" / "M"
    wdir.mkdir(parents=True)
    # GLM: pack は GGUF のフォルダ内 (pack/)。native_experts.txt がソース GGUF を指す
    for n in (1, 2):
        (wdir / f"M-0000{n}-of-00002.gguf").write_bytes(b"x" * (2**30 // 4))   # 0.25 GiB
    pack = wdir / "pack"
    pack.mkdir()
    (pack / "index.txt").write_text("idx", encoding="utf-8")
    (pack / "native_experts.txt").write_text(
        f"absolute offsets in {wdir / 'M-00001-of-00002.gguf'}, something", encoding="utf-8")
    tok = pack / "tokenizer"
    tok.mkdir()
    # DS4: tensors.txt の 'file N <path>' 行が実 safetensors を指す
    d4 = tmp_path / "models" / "DS4"
    d4.mkdir(parents=True)
    (d4 / "w.safetensors").write_bytes(b"x" * (2**30 // 4))
    p4 = d4 / "maya-ds4"
    p4.mkdir()
    (p4 / "tensors.txt").write_text(f"file 0 {d4 / 'w.safetensors'}\n", encoding="utf-8")
    (p4 / "ds4.txt").write_text("geom", encoding="utf-8")
    tok4 = p4 / "tokenizer"
    tok4.mkdir()

    def cfg(name: str, data: dict) -> None:
        (mod / name).write_text(json.dumps(data), encoding="utf-8")

    cfg("maya-maya-s24.json", {"exe": "e", "args": ["--glm-pack", str(pack)], "tokenizer": str(tok)})
    cfg("maya-ds4.json", {"exe": "e", "args": ["--pack", str(p4)], "tokenizer": str(tok4)})
    cfg("maya-gone.json", {"exe": "e", "args": ["--glm-pack", str(wdir / "no-pack")],
                           "tokenizer": str(tok)})
    cfg("maya-norefs.json", {"exe": "e", "args": [], "tokenizer": str(tok)})
    cfg("maya-notok.json", {"exe": "e", "args": ["--glm-pack", str(pack)],
                            "tokenizer": str(tmp_path / "no-tok")})
    # シャード欠損 (3 枚のうち 2 枚だけ)
    (wdir / "S-00001-of-00003.gguf").write_bytes(b"x")
    (wdir / "S-00002-of-00003.gguf").write_bytes(b"x")
    cfg("maya-shards.json", {"exe": "e", "args": ["--gguf", str(wdir / "S-00001-of-00003.gguf")],
                             "tokenizer": str(tok)})
    got = {c["name"]: c["weights"] for c in maya_setup.installed_configs(mod)}
    assert got["maya-maya-s24.json"]["ok"] is True
    assert got["maya-maya-s24.json"]["size_gb"] > 0
    assert got["maya-ds4.json"]["ok"] is True
    assert got["maya-gone.json"]["ok"] is False
    assert got["maya-norefs.json"]["ok"] is None       # 判断しない (UI は素通し)
    assert got["maya-notok.json"]["ok"] is False       # tokenizer ディレクトリ欠損
    assert got["maya-shards.json"]["ok"] is False
    assert "S-00003-of-00003.gguf" in got["maya-shards.json"]["missing"]


def test_merge_config_ready_ds4_shared_config_name(tmp_path: Path):
    """DS4 の量子化は maya-ds4.json を共有する — 「取得済み」バッジは installer 記録の
    量子化キー別。gguf-q8 が在るだけで maya-xs まで取得済み扱いにしない (未導入の
    量子化がダウンロード択から消える事故の回帰防止)。"""
    mod = tmp_path
    pack = mod / "maya-ds4-pack"
    pack.mkdir()
    (pack / "tensors.txt").write_text("", encoding="utf-8")
    (mod / "maya-ds4.json").write_text(json.dumps({
        "exe": "/x/maya-ds4", "args": ["--pack", str(pack)],
        "installer": {"family": "deepseek", "ds4_quant": "gguf-q8"}}), encoding="utf-8")
    data = {"families": {"deepseek": {"models": {
        "gguf-q8": {"config": "maya-ds4.json"},
        "maya-xs": {"config": "maya-ds4.json"},
    }}}}
    maya_setup._merge_config_ready(mod, data)
    ms = data["families"]["deepseek"]["models"]
    assert ms["gguf-q8"]["have"]["found"] == 1
    assert "have" not in ms["maya-xs"]
    # config 棚卸しにも家系 / 量子化キーが乗る (UI の local モードが別量子化を掴まないため)
    c = maya_setup.installed_configs(mod)[0]
    assert c["family"] == "deepseek" and c["quant_key"] == "gguf-q8"


def test_merge_config_ready_marks_models(tmp_path: Path):
    """既存 config の重み ok をモデル表へ引き写す (取得済みバッジの源)。
    ファイル名解決が既に当てたモデルは上書きしない。"""
    mod = tmp_path
    pack = mod / "pack"
    pack.mkdir()
    (pack / "index.txt").write_text("idx", encoding="utf-8")
    (mod / "maya-maya-s24.json").write_text(
        json.dumps({"exe": "e", "args": ["--glm-pack", str(pack)]}), encoding="utf-8")
    data = {"families": {"glm": {"models": {
        "Maya-S24": {"config": "maya-maya-s24.json"},
        "Maya-M": {"config": "maya-maya-m.json", "have": {"files": 3, "found": 0, "gb": 0.0}},
    }}}}
    maya_setup._merge_config_ready(mod, data)
    assert data["families"]["glm"]["models"]["Maya-S24"]["have"]["found"] == 1
    assert data["families"]["glm"]["models"]["Maya-M"]["have"]["found"] == 0  # config 無しは触らない


# ---------------------------------------------------------------- ③ 家系別 config-args

@pytest.fixture()
def client(tmp_path: Path):
    (tmp_path / "data").mkdir(parents=True)
    (tmp_path / "module" / "maya").mkdir(parents=True)
    from fastapi.testclient import TestClient

    from llm_launcher import app as app_mod
    return TestClient(app_mod.create_app(tmp_path)), tmp_path


def _maya_cfg(root: Path, name: str, doc: dict) -> Path:
    p = root / "module" / "maya" / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _profile(client, config: str) -> str:
    r = client.post("/api/profiles", json={"name": "t", "backend": "maya",
                                           "values": {"config": config}})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _prof_cfg(client, pid: str) -> Path:
    """プロファイルが実際に参照する config (共有 installer config は専用コピーへ
    張り替わるため、編集結果の検証はそちらを読む)。"""
    profs = client.get("/api/profiles").json()
    return Path(next(p for p in profs if p["id"] == pid)["values"]["config"])


def _seed_maya_gpus(monkeypatch, root: Path) -> None:
    mod = root / "module" / "maya"
    (mod / "maya.py").write_text("# fake\n", encoding="utf-8")
    tables = {"available": True, "families": {},
              "pc": {"gpus": [{"index": 0, "usable": True}, {"index": 1, "usable": True}]}}
    monkeypatch.setattr(maya_setup, "_CACHE",
                        {str(mod): (time.monotonic(), tables),
                         str(mod.resolve()): (time.monotonic(), tables)})


def test_glm_config_asks_kv_and_gpu_not_tp(client, monkeypatch):
    """GLM (maya エンジン): --kv は編集可、--tp は無い (複数 GPU は config の gpu)。"""
    c, root = client
    _seed_maya_gpus(monkeypatch, root)
    p = _maya_cfg(root, "maya-maya-s24.json",
                  {"exe": "/x/maya", "args": ["--glm-pack", "/p", "--max-context", "32768"],
                   "gpu": [0, 1]})
    pid = _profile(c, "module/maya/maya-maya-s24.json")
    p = _prof_cfg(c, pid)
    r = c.get(f"/api/profiles/{pid}/config-args").json()
    assert r["family"] == "glm" and r["kv_ok"] is True and r["tp_ok"] is False
    assert r["gpu"] == "0,1" and r["gpus"] == [0, 1]
    assert c.post(f"/api/profiles/{pid}/config-args",
                  json={"updates": {"--kv": "int8"}}).status_code == 200
    assert c.post(f"/api/profiles/{pid}/config-args",
                  json={"updates": {"--tp": "2"}}).status_code == 400
    # gpu (layer split) は編集可: 単一カードへ戻すと int になる (Strata の layer_split は付かない)
    r = c.post(f"/api/profiles/{pid}/config-args", json={"updates": {"gpu": "1"}})
    assert r.status_code == 200, r.text
    doc = json.loads(p.read_text("utf-8"))
    assert doc["gpu"] == 1 and "layer_split" not in doc and "gpus_asked" not in doc
    # 検出外 GPU は拒否
    assert c.post(f"/api/profiles/{pid}/config-args",
                  json={"updates": {"gpu": "5"}}).status_code == 400


def test_ds4_config_gpu_selection_derives_tp(client, monkeypatch):
    """DeepSeek (maya-ds4): --kv はエンジン固有 (編集不可)。GPU の使い方 (gpu) は
    全家系で編集でき、--tp は選んだカード枚数から導出される (純正 maya.py と同じ
    min(2, 枚数))。--tp だけ書いて gpu を放置すると「tp=1 なのに 2 枚 layer split」
    で起動 / 計測される (2026-11-16 実害: gpu:[0,1] + --tp 1)。"""
    c, root = client
    _seed_maya_gpus(monkeypatch, root)
    p = _maya_cfg(root, "maya-ds4.json",
                  {"exe": "/x/maya-ds4", "args": ["--pack", "/p", "--max-context", "65536", "--tp", "2"],
                   "gpu": [0, 1]})
    pid = _profile(c, "module/maya/maya-ds4.json")
    p = _prof_cfg(c, pid)
    r = c.get(f"/api/profiles/{pid}/config-args").json()
    assert r["family"] == "deepseek" and r["kv_ok"] is False and r["tp_ok"] is True
    assert r["gpu"] == "0,1" and r["gpus"] == [0, 1]
    assert c.post(f"/api/profiles/{pid}/config-args",
                  json={"updates": {"--kv": "int8"}}).status_code == 400
    # 単一カード → gpu は int + --tp は 1 に導出 (「GPU 分割数 1」はこれで実現する)
    r = c.post(f"/api/profiles/{pid}/config-args", json={"updates": {"gpu": "0"}})
    assert r.status_code == 200, r.text
    doc = json.loads(p.read_text("utf-8"))
    assert doc["gpu"] == 0 and "layer_split" not in doc
    assert doc["args"][doc["args"].index("--tp") + 1] == "1"
    # 2 枚 → --tp 2 に導出
    r = c.post(f"/api/profiles/{pid}/config-args", json={"updates": {"gpu": "0,1"}})
    assert r.status_code == 200, r.text
    doc = json.loads(p.read_text("utf-8"))
    assert doc["gpu"] == [0, 1]
    assert doc["args"][doc["args"].index("--tp") + 1] == "2"
    # カード数を超える --tp は拒否 (gpu 未設定 = 全 usable が見える)
    assert c.post(f"/api/profiles/{pid}/config-args",
                  json={"updates": {"--tp": "4"}}).status_code == 400
    assert c.post(f"/api/profiles/{pid}/config-args",
                  json={"updates": {"--tp": "2"}}).status_code == 200
    # gpu 選択と矛盾する明示 --tp は拒否 (単一カード + tp 2)
    assert c.post(f"/api/profiles/{pid}/config-args",
                  json={"updates": {"gpu": "0", "--tp": "2"}}).status_code == 400


def test_wizard_maya_gpu_question_derives_tp():
    """③ の GPU の使い方は全家系で聞く。DS4 / MM3-EXL3 は選択から --tp を導出
    (純正 min(2, 枚数)) — --tp だけ聞いて config の gpu を放置しない。"""
    import llm_launcher

    js = (Path(llm_launcher.__file__).parent / "static" / "setup.js").read_text("utf-8")
    assert "tpFromGpus: W.cfgArgs.tp_ok === true" in js
    assert 'updates["--tp"] = vals.tp;' in js
    assert "Math.min(2, n)" in js
    # DS4 でもカード選択の選択肢が組み立つ (flag === "gpu" の options ブロック)
    assert 'if (cfg.tp && cfg.tp.flag === "gpu") {' in js


def test_settings_first_flow_binds_config_after_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """設定先行フローの API 連続実行 (ウィザードの JS が踏む順序そのまま):
    ③ プロファイル作成 (config まだ無し) → ④ 保存後に ② の選択 + GPU / 並列数で
    セットアップジョブ開始 → 完了で config 紐付け + ③ の回答 (kv) 書き戻し。
    並列数 / GPU はセットアップ自体に渡り、config に最初から入る。"""
    from fastapi.testclient import TestClient

    from llm_launcher import app as app_mod

    mod = _fake_module(tmp_path, with_venv=True)
    _seed_tables(monkeypatch, mod)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    seen: dict[str, object] = {}

    async def fake_step(self, job, step):  # noqa: ANN001
        seen["argv"] = list(step["argv"])
        # 純正 maya.py が --gpu / --parallel を受けて config を書いた体
        (Path(step["cwd"]) / job["config_name"]).write_text(json.dumps({
            "exe": str(mod / "build" / "maya"),
            "args": ["--glm-pack", "/models/Maya-S24/pack", "--max-context", "32768",
                     "--parallel", "2"],
            "gpu": [0], "model_name": "glm-5.3-flash",
            "tokenizer": "/models/Maya-S24/pack/tokenizer"}), encoding="utf-8")
        return 0, "[ok] config: maya-maya-s24.json"

    monkeypatch.setattr(InstallManager, "_run_step", fake_step)
    client = TestClient(app_mod.create_app(tmp_path))
    # ③ 稼働設定: プロファイルは config なしで作られる (設定先行)
    p = client.post("/api/profiles", json={"name": "maya-s24", "backend": "maya",
                                           "values": {"parallel": "2"},
                                           "simple_visible": True}).json()
    # ④ の後: ジョブ開始 (② の選択 + ③ の GPU / 並列数回答)
    r = client.post("/api/installs/maya-model",
                    json={"family": "glm", "model": "Maya-S24", "context": 32768,
                          "vision": "", "gpus": "0", "parallel": 2})
    assert r.status_code == 201, r.text
    jid = r.json()["id"]
    for _ in range(50):
        done = client.get(f"/api/installs/{jid}").json()
        if done["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert done["status"] == "done", done
    argv = seen["argv"]
    assert argv[argv.index("--gpu") + 1] == "0"
    assert argv[argv.index("--parallel") + 1] == "2"
    # 完了後の紐付け (bindSetupResult 相当): config 接続 + 保留の kv 書き戻し
    cfg = done["result"]["config"]
    assert done["result"]["parallel"] == 2
    r = client.put(f"/api/profiles/{p['id']}",
                   json={"name": p["name"], "backend": "maya",
                         "values": {"parallel": "2", "config": cfg}})
    assert r.status_code == 200, r.text
    cfg = r.json()["values"]["config"]   # 専用コピーへ張り替わる (installer は共有しない)
    r = client.post(f"/api/profiles/{p['id']}/config-args",
                    json={"updates": {"--kv": "int8"}})
    assert r.status_code == 200, r.text
    doc = json.loads(Path(cfg).read_text("utf-8"))
    assert doc["gpu"] == [0]
    assert doc["args"][doc["args"].index("--parallel") + 1] == "2"
    assert doc["args"][doc["args"].index("--kv") + 1] == "int8"
    # ③ のプリフィル相当: 家系 / 編集可否も config から正しく読める
    ca = client.get(f"/api/profiles/{p['id']}/config-args").json()
    assert ca["family"] == "glm" and ca["kv_ok"] is True and ca["tp_ok"] is False


def test_maya_model_api_parallel_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """parallel の body 検証: 1-8 以外は 422、Qwen への --parallel は 400 (純正が拒否)。"""
    from fastapi.testclient import TestClient

    from llm_launcher import app as app_mod

    mod = _fake_module(tmp_path, with_venv=True)
    _seed_tables(monkeypatch, mod)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    client = TestClient(app_mod.create_app(tmp_path))
    for bad in (0, 9):
        assert client.post("/api/installs/maya-model",
                           json={"family": "glm", "model": "Maya-S24",
                                 "context": 32768, "parallel": bad}).status_code == 422
    r = client.post("/api/installs/maya-model",
                    json={"family": "qwen", "model": "q5_k_m",
                          "context": 65536, "parallel": 2})
    assert r.status_code == 400 and "parallel" in r.json()["detail"]


def test_qwen_and_mm3_gguf_family_detection(client, monkeypatch):
    """家系判定は installer.family → exe 名 → args の順 (maya_quants.family_of と同じ)。"""
    c, root = client
    _seed_maya_gpus(monkeypatch, root)
    _maya_cfg(root, "maya-cyber-frost.json",
              {"exe": "/x/maya", "args": ["--pack", "/p", "--native", "/g.gguf",
                                          "--ple-gguf", "/g.gguf", "--kv", "int8"]})
    pid = _profile(c, "module/maya/maya-cyber-frost.json")
    r = c.get(f"/api/profiles/{pid}/config-args").json()
    assert r["family"] == "qwen" and r["kv_ok"] is True and r["tp_ok"] is False
    # MiniMax GGUF (installer.mm3_quant) は layer split 側、EXL3 は --tp 側
    _maya_cfg(root, "maya-minimax-maya-xs.json",
              {"exe": "/x/maya-mm3", "args": ["--pack", "/p"],
               "installer": {"family": "minimax", "mm3_quant": "gguf-xs"}})
    pid = _profile(c, "module/maya/maya-minimax-maya-xs.json")
    r = c.get(f"/api/profiles/{pid}/config-args").json()
    assert r["family"] == "minimax" and r["tp_ok"] is False
    _maya_cfg(root, "maya-mm3.json",
              {"exe": "/x/maya-mm3", "args": ["--pack", "/p", "--tp", "4"],
               "installer": {"family": "minimax", "mm3_quant": "exl3"}})
    pid = _profile(c, "module/maya/maya-mm3.json")
    r = c.get(f"/api/profiles/{pid}/config-args").json()
    assert r["family"] == "minimax" and r["tp_ok"] is True and r["kv_ok"] is False


# ---------------------------------------------------------------- Hy4-Flash (v1.1.68+) / GLM-5.3 (744B)

def test_build_argv_hy4(monkeypatch, tmp_path: Path):
    """Hy4-Flash: --model Hy4-Flash --hy4-quant <key>。コンテキストはモデル別
    (hy4_fit の一覧)、--drafting off (計測は ⑤)、--parallel は拒否 (1 リクエストずつ)。
    この PC に収まらないサイズ (fits=False) は選択不可。"""
    mod = _fake_module(tmp_path, with_venv=True)
    mod = _seed_tables(monkeypatch, mod)
    argv, cfg = maya_setup.build_argv(mod, {"family": "hy4", "model": "maya-xs",
                                            "context": 65536, "vision": "off"})
    assert argv[1:5] == ["--model", "Hy4-Flash", "--hy4-quant", "maya-xs"]
    assert argv[argv.index("--context") + 1] == "65536"
    assert argv[argv.index("--drafting") + 1] == "off"
    assert "--no-vision" in argv
    assert argv[-4:] == ["--yes", "--no-start", "--plain", "--download-model"]
    assert cfg == "maya-hy4-flash-maya-xs.json"
    # Tencent Q4_K_M は config 名が maya-hy4-flash.json (纯正 config_stem)
    argv2, cfg2 = maya_setup.build_argv(mod, {"family": "hy4", "model": "q4_k_m", "context": 131072})
    assert argv2[argv2.index("--hy4-quant") + 1] == "q4_k_m"
    assert "--no-vision" not in argv2     # vision "" = 純正既定
    assert cfg2 == "maya-hy4-flash.json"
    # モデル別 contexts の外は拒否 (家系一覧に在る 1M でも maya-xs の fit 外)
    for bad in ({"family": "hy4", "model": "maya-xs", "context": 1048576},
                {"family": "hy4", "model": "maya-xs", "context": 204800},
                # fits=False (この PC の VRAM では起動できない) は context 関係なく拒否
                {"family": "hy4", "model": "maya-l", "context": 32768},
                # --parallel 非対応 (纯正 check_parallel が拒否)
                {"family": "hy4", "model": "maya-xs", "context": 32768, "parallel": 2},
                # 家系違いの quant
                {"family": "hy4", "model": "gguf-q8", "context": 32768}):
        with pytest.raises(ValueError):
            maya_setup.build_argv(mod, bad)


def test_build_argv_g53(monkeypatch, tmp_path: Path):
    """GLM-5.3 (744B): glm 家系に同居するがコンテキストはモデル別 (g53_contexts)。
    テキスト専用 (vision=False) なので --no-vision は付かない。"""
    mod = _fake_module(tmp_path, with_venv=True)
    mod = _seed_tables(monkeypatch, mod)
    argv, cfg = maya_setup.build_argv(mod, {"family": "glm", "model": "GLM-5.3-Maya-XS",
                                            "context": 65536, "vision": "off"})
    assert argv[1:3] == ["--model", "GLM-5.3-Maya-XS"]
    assert "--no-vision" not in argv      # 非対応モデルにフラグを付けない
    assert "--drafting" not in argv       # GLM は MTP 既定で計測なし
    assert cfg == "maya-glm-5.3-maya-xs.json"
    # 家系一覧の 1M は G53 のモデル別一覧に無い → 拒否
    with pytest.raises(ValueError):
        maya_setup.build_argv(mod, {"family": "glm", "model": "GLM-5.3-Maya-XS",
                                    "context": 1048576})
    # GLM-5.3 は --parallel 対応 (纯正 PARALLEL_FAMILIES に glm)
    argv2, _ = maya_setup.build_argv(mod, {"family": "glm", "model": "GLM-5.3-Maya-XS",
                                           "context": 32768, "parallel": 2})
    assert argv2[argv2.index("--parallel") + 1] == "2"


def test_hy4_config_family_and_weights(tmp_path: Path):
    """hy4 の config 判定: maya-hy4 exe / installer.family → hy4 (--pack を持つので
    DeepSeek より先)。重み検証は pack の hy4.txt + tensors.txt (file N <path> 行)。"""
    mod = tmp_path / "maya"
    mod.mkdir()
    model = tmp_path / "models" / "Hy4-Flash-Maya-XS"
    pack = model / "maya-hy4"
    (pack / "tokenizer").mkdir(parents=True)
    (model / "trunk.gguf").write_bytes(b"x" * 1024)
    (pack / "hy4.txt").write_text("model_dir .\n", encoding="utf-8")
    (pack / "tensors.txt").write_text("file 0 trunk.gguf\nt t0 Q4_K 0 0 1024 4 4\n",
                                      encoding="utf-8")
    doc = {"exe": str(mod / "build" / "maya-hy4"),
           "args": ["--pack", str(pack), "--max-context", "32768"],
           "tokenizer": str(pack / "tokenizer"),
           "installer": {"family": "hy4", "hy4_quant": "maya-xs", "layout": "standard"}}
    assert maya_setup.config_family(doc) == "hy4"
    assert maya_setup.config_quant_key(doc) == "maya-xs"
    w = maya_setup.weights_status(doc)
    assert w["ok"] is True and w["size_gb"] is not None
    # installer 記録が無くても exe 名で判定 (--pack → deepseek に落ちない)
    assert maya_setup.config_family({"exe": "/x/maya-hy4", "args": ["--pack", str(pack)]}) == "hy4"
    # 重みが無い pack は ok=False (一覧に出る = 起動できる、の根拠)
    (model / "trunk.gguf").unlink()
    assert maya_setup.weights_status(doc)["ok"] is False
    # GLM-5.3 (744B) の判定: model_name / installer.quant
    assert maya_setup.config_is_g53({"model_name": "glm-5.3"}) is True
    assert maya_setup.config_is_g53({"installer": {"quant": "GLM-5.3-Maya-XS"}}) is True
    assert maya_setup.config_is_g53({"model_name": "glm-5.3-flash",
                                     "installer": {"quant": "Maya-S24"}}) is False


def test_merge_config_ready_hy4(tmp_path: Path):
    """hy4 の取得済みバッジは installer.hy4_quant で突き合わせる (config 名はサイズ別)。"""
    mod = tmp_path / "maya"
    mod.mkdir()
    model = tmp_path / "models" / "Hy4-Flash-Maya-XS"
    pack = model / "maya-hy4"
    (pack / "tokenizer").mkdir(parents=True)
    (model / "trunk.gguf").write_bytes(b"x" * 1024)
    (pack / "hy4.txt").write_text("model_dir .\n", encoding="utf-8")
    (pack / "tensors.txt").write_text("file 0 trunk.gguf\n", encoding="utf-8")
    (mod / "maya-hy4-flash-maya-xs.json").write_text(json.dumps({
        "exe": str(mod / "build" / "maya-hy4"),
        "args": ["--pack", str(pack), "--max-context", "32768"],
        "tokenizer": str(pack / "tokenizer"),
        "installer": {"family": "hy4", "hy4_quant": "maya-xs"}}), encoding="utf-8")
    data = {"families": {"hy4": {"models": {
        "maya-xs": {"config": "maya-hy4-flash-maya-xs.json"},
        "maya-s": {"config": "maya-hy4-flash-maya-s.json"}}}}}
    maya_setup._merge_config_ready(mod, data)
    ms = data["families"]["hy4"]["models"]
    assert ms["maya-xs"]["have"]["found"] == 1
    assert "have" not in ms["maya-s"]     # 別サイズの config は無い = 触らない


def test_hy4_config_asks_tp_count_not_kv(client, monkeypatch):
    """Hy4-Flash (maya-hy4): --kv はエンジン固有 (編集不可)、--tp は auto|N。
    GPU の使い方 (gpu) は編集でき、--tp は選択カード全部の N に導出される
    (tensor parallel。auto を書くと 2 枚で layer split になり、その P2P 活性コピーが
    Blackwell + PCIe PHB でデータを壊す — 2026-11-21 実測。DS4 の min(2, N) とも違う)。"""
    c, root = client
    _seed_maya_gpus(monkeypatch, root)
    p = _maya_cfg(root, "maya-hy4-flash-maya-xs.json",
                  {"exe": "/x/maya-hy4", "args": ["--pack", "/p", "--max-context", "32768"],
                   "gpu": [0, 1],
                   "installer": {"family": "hy4", "hy4_quant": "maya-xs"}})
    pid = _profile(c, "module/maya/maya-hy4-flash-maya-xs.json")
    p = _prof_cfg(c, pid)
    r = c.get(f"/api/profiles/{pid}/config-args").json()
    assert r["family"] == "hy4" and r["kv_ok"] is False and r["tp_ok"] is True
    assert r["tp_auto"] is True
    assert c.post(f"/api/profiles/{pid}/config-args",
                  json={"updates": {"--kv": "int8"}}).status_code == 400
    # gpu 選択 → --tp はカード数 (2) に導出される (auto ではない)
    r = c.post(f"/api/profiles/{pid}/config-args", json={"updates": {"gpu": "0,1"}})
    assert r.status_code == 200, r.text
    doc = json.loads(p.read_text("utf-8"))
    assert doc["gpu"] == [0, 1]
    assert doc["args"][doc["args"].index("--tp") + 1] == "2"
    # 明示の --tp auto / N (カード数以下) は受け付ける (auto は手動選択用に残す)
    assert c.post(f"/api/profiles/{pid}/config-args",
                  json={"updates": {"--tp": "auto"}}).status_code == 200
    assert c.post(f"/api/profiles/{pid}/config-args",
                  json={"updates": {"--tp": "2"}}).status_code == 200
    # カード数を超える数値は拒否
    assert c.post(f"/api/profiles/{pid}/config-args",
                  json={"updates": {"--tp": "4"}}).status_code == 400
    # 単一カード → --tp 1 に導出
    r = c.post(f"/api/profiles/{pid}/config-args", json={"updates": {"gpu": "0"}})
    assert r.status_code == 200, r.text
    doc = json.loads(p.read_text("utf-8"))
    assert doc["gpu"] == 0
    assert doc["args"][doc["args"].index("--tp") + 1] == "1"


def test_g53_config_kv_not_editable(client, monkeypatch):
    """GLM-5.3 (744B): 家系は glm だが KV は INT8 latent 固定 → --kv 編集不可。
    --tp も無い (GLM エンジン = layer split)。"""
    c, root = client
    _seed_maya_gpus(monkeypatch, root)
    _maya_cfg(root, "maya-glm-5.3-maya-xs.json",
              {"exe": "/x/maya", "args": ["--glm-pack", "/p", "--max-context", "32768"],
               "model_name": "glm-5.3", "gpu": [0, 1],
               "installer": {"quant": "GLM-5.3-Maya-XS"}})
    pid = _profile(c, "module/maya/maya-glm-5.3-maya-xs.json")
    r = c.get(f"/api/profiles/{pid}/config-args").json()
    assert r["family"] == "glm" and r["kv_ok"] is False and r["tp_ok"] is False
    assert c.post(f"/api/profiles/{pid}/config-args",
                  json={"updates": {"--kv": "int8"}}).status_code == 400
    # gpu (layer split) は編集可
    assert c.post(f"/api/profiles/{pid}/config-args",
                  json={"updates": {"gpu": "0"}}).status_code == 200


def test_wizard_hy4_ui_wiring():
    """③ の UI 配線: hy4 は並列数の質問を出さず、--tp はカード数 N 導出 (tpAuto)。
    ② のコンテキスト一覧はモデル別 contexts を優先する。"""
    import llm_launcher

    js = (Path(llm_launcher.__file__).parent / "static" / "setup.js").read_text("utf-8")
    assert 'W.cfgArgs.family === "qwen" || W.cfgArgs.family === "hy4" || W.cfgArgs.family === "ik2") delete cfg.par' in js
    assert 'vals.tp = sec.tpAuto ? String(n) : String(Math.min(2, n));' in js
    assert "tp_auto: isMaya && W.tableReq.family === \"hy4\"" in js
    assert "(d.contexts || []).length ? d.contexts" in js
    assert 'W.tableReq.family !== "hy4"' in js     # ② ジョブへ parallel を送らない
    assert 'W.tableReq.family !== "ik2"' in js     # Inkling 2 Mini も送らない


# ---------------------------------------------------------------- Inkling-2-Mini (ik2, v1.1.72+)

def test_build_argv_ik2(monkeypatch, tmp_path: Path):
    """Inkling-2-Mini: --model Inkling-2-Mini (quant フラグ無し = 1 ファイルセット)。
    --drafting は付けない (純正既定 auto: ドラフターはセットアップで取得し、計測は
    エンジンが初回起動時に 1 回行う — セットアップ中の GPU 独占計測では無い)。
    --parallel は拒否 (1 リクエストずつ)、複数 GPU も拒否 (純正 check_pc が拒否)。"""
    mod = _fake_module(tmp_path, with_venv=True)
    mod = _seed_tables(monkeypatch, mod)
    argv, cfg = maya_setup.build_argv(mod, {"family": "ik2", "model": "ud-q4_k_xl",
                                            "context": 131072, "vision": "off"})
    assert argv[1:3] == ["--model", "Inkling-2-Mini"]
    assert "--ds4-quant" not in argv and "--hy4-quant" not in argv
    assert argv[argv.index("--context") + 1] == "131072"
    assert "--no-vision" in argv
    assert "--drafting" not in argv
    assert argv[-4:] == ["--yes", "--no-start", "--plain", "--download-model"]
    assert cfg == "maya-inkling-2-mini.json"
    # vision "" = 純正既定 (ON)
    argv2, _ = maya_setup.build_argv(mod, {"family": "ik2", "model": "ud-q4_k_xl", "context": 32768})
    assert "--no-vision" not in argv2
    # 単一カード指定は可 (--gpu N)
    argv3, _ = maya_setup.build_argv(mod, {"family": "ik2", "model": "ud-q4_k_xl",
                                           "context": 32768, "gpus": "1"})
    assert argv3[argv3.index("--gpu") + 1] == "1" and "--gpus" not in argv3
    for bad in ({"family": "ik2", "model": "ud-q4_k_xl", "context": 32768, "parallel": 2},
                {"family": "ik2", "model": "ud-q4_k_xl", "context": 32768, "gpus": "0,1"},
                {"family": "ik2", "model": "ud-q4_k_xl", "context": 32768, "gpus": "all"},
                {"family": "ik2", "model": "ud-q4_k_xl", "context": 1048576},   # 家系一覧外
                {"family": "ik2", "model": "ud-q4_k_xl", "context": 8192},      # ik2_fit.CONTEXTS 外
                {"family": "ik2", "model": "gguf-q8", "context": 32768}):       # 家系違いの quant
        with pytest.raises(ValueError):
            maya_setup.build_argv(mod, bad)


def test_build_argv_ik2_fits_false(monkeypatch, tmp_path: Path):
    """この PC で動かない (IK2F.unsupported の理由) 場合は fits=False で選択不可 —
    context 関係なく拒否する (Hy4-Flash の hy4_fit と同じ扱い)。"""
    mod = _fake_module(tmp_path, with_venv=True)
    mod = _seed_tables(monkeypatch, mod)
    tables = maya_setup._CACHE[str(mod)][1]
    m = tables["families"]["ik2"]["models"]["ud-q4_k_xl"]
    m["fits"] = False
    m["contexts"] = []
    m["fit_error"] = "Inkling 2 Mini needs an NVIDIA V100 / RTX 20 or newer GPU"
    with pytest.raises(ValueError, match="V100"):
        maya_setup.build_argv(mod, {"family": "ik2", "model": "ud-q4_k_xl", "context": 32768})


def test_ik2_config_family_and_weights(tmp_path: Path):
    """ik2 の config 判定: maya-ik2 exe / installer.family → ik2 (--model の GGUF 直読み
    で pack を持たないので、引数だけだと glm に落ちる)。重み検証は --model の GGUF。"""
    mod = tmp_path / "maya"
    mod.mkdir()
    model = tmp_path / "models" / "Inkling-2-Mini"
    model.mkdir(parents=True)
    gguf = model / "Inkling-2-Mini-UD-Q4_K_XL.gguf"
    gguf.write_bytes(b"x" * 2048)
    tok = model / "maya-ik2" / "tokenizer"
    tok.mkdir(parents=True)
    doc = {"exe": str(mod / "build" / "maya-ik2"),
           "args": ["--model", str(gguf), "--max-context", "131072", "--drafting", "auto"],
           "tokenizer": str(tok), "model_name": "inkling-2-mini",
           "installer": {"family": "ik2", "ik2_quant": "ud-q4_k_xl", "drafting": "auto"}}
    assert maya_setup.config_family(doc) == "ik2"
    assert maya_setup.config_quant_key(doc) == "ud-q4_k_xl"
    w = maya_setup.weights_status(doc)
    assert w["ok"] is True and w["size_gb"] is not None
    # installer 記録が無くても exe 名で判定 (glm に落ちない)
    assert maya_setup.config_family({"exe": "/x/maya-ik2",
                                     "args": ["--model", str(gguf)]}) == "ik2"
    assert maya_setup.config_quant_key({"exe": "/x/maya-ik2", "args": [],
                                        "installer": {"family": "ik2"}}) == "ud-q4_k_xl"
    # 重みが無い = ok=False (一覧に出る = 起動できる、の根拠)
    gguf.unlink()
    assert maya_setup.weights_status(doc)["ok"] is False


def test_merge_config_ready_ik2(tmp_path: Path):
    """ik2 の取得済みバッジは installer.ik2_quant で突き合わせる (config 名は 1 つ)。"""
    mod = tmp_path / "maya"
    mod.mkdir()
    model = tmp_path / "models" / "Inkling-2-Mini"
    model.mkdir(parents=True)
    gguf = model / "Inkling-2-Mini-UD-Q4_K_XL.gguf"
    gguf.write_bytes(b"x" * 2048)
    (mod / "maya-inkling-2-mini.json").write_text(json.dumps({
        "exe": str(mod / "build" / "maya-ik2"),
        "args": ["--model", str(gguf), "--max-context", "131072"],
        "installer": {"family": "ik2", "ik2_quant": "ud-q4_k_xl"}}), encoding="utf-8")
    data = {"families": {"ik2": {"models": {
        "ud-q4_k_xl": {"config": "maya-inkling-2-mini.json"}}}}}
    maya_setup._merge_config_ready(mod, data)
    assert data["families"]["ik2"]["models"]["ud-q4_k_xl"]["have"]["found"] == 1


def test_ik2_config_asks_gpu_only(client, monkeypatch):
    """Inkling-2-Mini (maya-ik2): --kv / --tp は無い (エンジン固有)。単一 GPU 専用
    (gpu_multi=False) なので 2 枚の選択は拒否、単一カードへの切り替えは可。"""
    c, root = client
    _seed_maya_gpus(monkeypatch, root)
    _maya_cfg(root, "maya-inkling-2-mini.json",
              {"exe": "/x/maya-ik2",
               "args": ["--model", "/m/Inkling-2-Mini-UD-Q4_K_XL.gguf",
                        "--max-context", "131072", "--drafting", "auto"],
               "gpu": [0], "installer": {"family": "ik2", "ik2_quant": "ud-q4_k_xl"}})
    pid = _profile(c, "module/maya/maya-inkling-2-mini.json")
    r = c.get(f"/api/profiles/{pid}/config-args").json()
    assert r["family"] == "ik2" and r["kv_ok"] is False and r["tp_ok"] is False
    assert r["gpu_multi"] is False
    assert c.post(f"/api/profiles/{pid}/config-args",
                  json={"updates": {"--kv": "fp16"}}).status_code == 400
    assert c.post(f"/api/profiles/{pid}/config-args",
                  json={"updates": {"--tp": "1"}}).status_code == 400
    assert c.post(f"/api/profiles/{pid}/config-args",
                  json={"updates": {"gpu": "0,1"}}).status_code == 400
    assert c.post(f"/api/profiles/{pid}/config-args",
                  json={"updates": {"gpu": "1"}}).status_code == 200
    # --max-context は編集可 (ik2_fit.CONTEXTS の範囲は ② が検証する。③ は範囲検査)
    assert c.post(f"/api/profiles/{pid}/config-args",
                  json={"updates": {"--max-context": "262144"}}).status_code == 200


def test_wizard_ik2_ui_wiring():
    """② / ③ / ⑤ の UI 配線: ik2 は並列数の質問を出さず、drafter の案内は
    「エンジンが初回起動時に計測」(セットアップでは計測しない、の他家系と違う)。"""
    import llm_launcher

    js = (Path(llm_launcher.__file__).parent / "static" / "setup.js").read_text("utf-8")
    assert 'W.mdStFam === "ik2"' in js                       # ② の drafter 案内
    assert "エンジンが初回起動時に 1 回だけ計測" in js
    assert 'd.layout === "offload"' in js                    # offload パスの注記
    assert 'W.cfgArgs.family === "ik2") delete cfg.par' in js  # ③ 並列数を出さない
    assert '/^maya-inkling/.test(cn)' in js                  # ⑤ の案内
