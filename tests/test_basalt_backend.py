"""Basalt バックエンド (module/basalt) とセットアップタブ統合の回帰テスト。

Basalt は Strata 系フォークの Blackwell 向け MoE エンジンで、
  1. サーバー CLI の正表は serve/config.py の build_parser から生成 (--engine-bin は
     ラッパ専用なので落とす / ループ生成の sampling 系は gen_basalt が補う)
  2. 起動入口はビルド済み bin/basalt の serve サブコマンド ({llama_server} トークン)。
     バイナリ未解決なら render_argv が「basalt が見つかりません」で止まる
  3. セットアップタブ (📦) のインストールは FreeToken 系と違い
     clone (submodule 取得済みならスキップ) → .venv → pip install -r requirements.txt
     → cmake configure + build → verify (import serve.server + bin/basalt 実在)
の順で走る。module/basalt が未クローンの環境ではテーブル系テストを skip する。
"""
import asyncio
from pathlib import Path

import pytest

from llm_launcher import backends
from llm_launcher.installer import INSTALL_SPECS, InstallManager
from llm_launcher.store import JsonStore

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PY = ROOT / "module" / "basalt" / "serve" / "config.py"
SPEC = INSTALL_SPECS["basalt"]

pytestmark_table = pytest.mark.skipif(not CONFIG_PY.exists(), reason="module/basalt が未クローン")


# ------------------------------------------------------------------ 正表

@pytestmark_table
def test_table_is_generated_from_serve_config_argparse():
    table = backends.load_table("basalt")
    assert table["--port"]["type"] == "int"
    assert table["--lazy"]["type"] == "bool"
    assert table["--idle-unload"]["type"] == "float"
    # --engine-bin は `basalt serve` ラッパが付ける内部フラグ = GUI に出さない
    assert "--engine-bin" not in table
    # ループ生成の sampling 系は gen_basalt が静的タプルから補う
    assert table["--temperature"]["type"] == "float"
    assert table["--seed"]["type"] == "int"
    # サーバー CLI に無いエンジン本体フラグは出さない (本体設定は basalt_config.json 側)
    assert "--max-context" not in table


# ------------------------------------------------------------------ バックエンド定義

def test_basalt_meta_uses_wrapper_binary():
    b = backends.BACKENDS["basalt"]
    assert b["module_path"] == "module/basalt"
    # 入口はビルド済み bin/basalt (ラッパが .venv を選んで serve/server.py を exec する)
    assert b["default_command"] == "{llama_server} serve"
    assert "basalt" in b["binaries"]
    assert "module/basalt/bin/basalt" in b["build_candidates"]
    # 検出は Strata と同じ submodule を cwd にした実 import
    assert b["check_module"] == "serve.server" and b["check_cwd"] is True


def test_required_and_default_off_fields():
    b = backends.BACKENDS["basalt"]
    assert {f["key"] for f in b["fields"] if f.get("required")} == {"host", "port"}
    off = {f["key"] for f in b["fields"] if f.get("default_on") is False}
    # config / GPU / 並列 / アイドル系は basalt_config.json が正解なので既定送信しない
    for key in ("config", "gpu", "parallel", "idle_unload", "idle_sleep"):
        assert key in off
    # bool の割り増しフラグも既定 OFF (毎起動で付け替える類ではない)
    for key in ("slot_full_context", "lazy", "mock_engine", "debug"):
        assert key in off


def test_render_argv_builds_serve_command():
    b = backends.BACKENDS["basalt"]
    profile = {"values": {"model": "models/Q.basalt", "config": "c.json", "host": "0.0.0.0",
                          "port": "8090", "api_key": "k1"},
               "on": {"config": True, "gpu": False, "parallel": False}}
    argv = backends.render_argv(b, profile, python="py",
                                binary="/r/module/basalt/bin/basalt", root=Path("/r"))
    assert argv[:3] == ["/r/module/basalt/bin/basalt", "serve", "--model"]
    assert ["--config", "c.json"] in (argv[i:i + 2] for i in range(len(argv) - 1))
    assert argv[argv.index("--port") + 1] == "8090"
    assert "--gpu" not in argv


def test_render_argv_requires_engine_binary():
    b = backends.BACKENDS["basalt"]
    with pytest.raises(ValueError, match="basalt が見つかりません"):
        backends.render_argv(b, {"values": {"host": "0.0.0.0", "port": "8080"}},
                             python="py", binary="", root=Path("/r"))


# ------------------------------------------------------------------ セットアップタブ

def _manager(root: Path) -> InstallManager:
    (root / "data").mkdir(parents=True, exist_ok=True)
    return InstallManager(root, JsonStore(root / "data" / "installs.json", []))


def _plan(monkeypatch, root: Path) -> dict:
    """start_install の計画だけ取る (_run_install を殺して subprocess も cmake も走らせない)。"""
    got: dict[str, object] = {}

    async def fake(self, job, spec, steps):  # noqa: ANN001
        got["job"] = dict(job)
        got["steps"] = [dict(s) for s in steps]

    monkeypatch.setattr(InstallManager, "_run_install", fake)
    monkeypatch.setattr("llm_launcher.installer.shutil.which",
                        lambda name: f"/usr/bin/{name}" if name == "cmake" else None)
    mgr = _manager(root)

    async def go() -> None:
        await mgr.start_install("basalt")
        await asyncio.sleep(0)

    asyncio.run(go())
    return got


def test_plan_for_existing_submodule_skips_clone(monkeypatch, tmp_path: Path):
    mod = tmp_path / "module" / "basalt"
    mod.mkdir(parents=True)
    (mod / "README.md").write_text("clone content", encoding="utf-8")   # clone 済み = 中身あり
    got = _plan(monkeypatch, tmp_path)
    steps = got["steps"]
    names = [s["name"] for s in steps]
    assert names == ["venv", "precheck", "install", "cmake", "cmake-build", "verify"]
    install = next(s for s in steps if s["name"] == "install")
    # pyproject の無いリポジトリ: -e . ではなく requirements.txt を直接入れる
    assert install["argv"][-2:] == ["-r", "requirements.txt"]
    assert "-e" not in install["argv"]
    # cmake は submodule 内を指す (cwd 基準の相対 argv)
    conf = next(s for s in steps if s["name"] == "cmake")
    assert conf["argv"] == ["cmake", "-S", ".", "-B", "build", "-DCMAKE_BUILD_TYPE=Release"]
    assert conf["cwd"].endswith("module/basalt")
    # precheck / verify は import だけでなくエンジン実在も要求する
    for s in steps:
        if s["name"] in ("precheck", "verify"):
            assert "bin/basalt" in s["argv"][2]


def test_plan_without_clone_starts_with_git_clone(monkeypatch, tmp_path: Path):
    got = _plan(monkeypatch, tmp_path)
    steps = got["steps"]
    assert steps[0]["name"] == "clone"
    assert steps[0]["argv"][:3] == ["git", "clone", "--depth"]
    assert steps[0]["argv"][-2:] == [SPEC["clone"],
                                     str((tmp_path / "module" / "basalt").resolve())]


def test_plan_without_cmake_is_refused(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("llm_launcher.installer.shutil.which", lambda name: None)
    got: dict = {}

    async def fake(self, job, spec, steps):  # noqa: ANN001
        got["steps"] = steps

    monkeypatch.setattr(InstallManager, "_run_install", fake)
    mgr = _manager(tmp_path)

    async def go() -> None:
        await mgr.start_install("basalt")

    from llm_launcher.installer import InstallError
    with pytest.raises(InstallError, match="cmake"):
        asyncio.run(go())
