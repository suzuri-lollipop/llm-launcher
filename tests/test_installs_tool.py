"""セットアップタブのインストールツール (uv 既定) と ③ キャリブレーション配線の回帰テスト。

見張る対象は 3 つ
  1. install_tool() — 既定は uv、uv が無い環境だけ venv の pip (実コマンドの単一の源)
  2. start_install の計画 — job["tool"] と install step の argv がその選択と一致する
     (画面に「pip install」と書いておいて uv が走る、というズレを許さない)
  3. API と静的画面 — GET /api/installs が tool を出し、セットアップ画面は tool で文言を作る。
     あわせて ③ キャリブレーションカード (インストール後に自動計走) の配線が消えないことを見る
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from test_auth_users import Client

import llm_launcher
from llm_launcher import app as app_mod
from llm_launcher import installer
from llm_launcher.calibrate import CAL_BACKENDS
from llm_launcher.installer import INSTALL_SPECS, InstallManager, install_tool
from llm_launcher.store import JsonStore

FORK = INSTALL_SPECS["freetoken-fork"]
HTML = (Path(llm_launcher.__file__).parent / "static" / "index.html").read_text("utf-8")
SETUP = "".join((Path(llm_launcher.__file__).parent / "static" / n).read_text("utf-8")
                for n in ("setup.html", "setup.js"))


def _which(monkeypatch, *, uv: bool) -> None:
    """uv の見え方を決める: PATH 検索 (shutil.which) とフォールバック候補址の両方。"""
    monkeypatch.delenv("LLM_LAUNCHER_UV", raising=False)
    monkeypatch.setattr(installer.shutil, "which",
                        lambda n: ("/usr/local/bin/uv" if n == "uv" and uv else None))
    monkeypatch.setattr(installer, "_uv_candidates", lambda: [])


def _plan(monkeypatch, root: Path, *, uv: bool) -> dict:
    """start_install の計画だけを取る (subprocess も venv も作らない)。"""
    got: dict[str, object] = {}

    async def fake(self, job, spec, steps):  # noqa: ANN001
        got["job"] = dict(job)
        got["steps"] = [dict(s) for s in steps]

    _which(monkeypatch, uv=uv)
    monkeypatch.setattr(InstallManager, "_run_install", fake)
    (root / "data").mkdir(parents=True, exist_ok=True)
    mgr = InstallManager(root, JsonStore(root / "data" / "installs.json", []))

    async def go() -> None:
        await mgr.start_install("freetoken-fork", ref="suzuri/product")
        await asyncio.sleep(0)

    asyncio.run(go())
    return got


def _install_step(steps: list[dict]) -> dict:
    return next(s for s in steps if s["name"] == "install")


# --------------------------------------------------------- 実際に走るツール

def test_install_tool_defaults_to_uv(monkeypatch):
    _which(monkeypatch, uv=True)
    assert install_tool() == "uv"


def test_install_tool_falls_back_to_pip_only_without_uv(monkeypatch):
    _which(monkeypatch, uv=False)
    assert install_tool() == "pip"


def test_uv_is_the_single_source_of_the_choice():
    """ツール判定を増やすと表示と実行がズレる。install_tool() 以外の which("uv") を禁じる。"""
    src = (Path(llm_launcher.__file__).parent / "installer.py").read_text("utf-8")
    assert src.count('shutil.which("uv")') == 1


def test_install_step_runs_uv_pip_install(monkeypatch, tmp_path: Path):
    got = _plan(monkeypatch, tmp_path / "root", uv=True)
    assert got["job"]["tool"] == "uv"
    argv = _install_step(got["steps"])["argv"]
    # 解決した uv の 絶対パス を argv[0] に置く (子プロセスの PATH が短くても解る)
    assert argv[0] == "/usr/local/bin/uv"
    assert argv[1:3] == ["pip", "install"]                        # shell ではなく argv 直渡し
    assert argv[argv.index("--python") + 1].endswith(".venv/bin/python")
    assert argv[-2:] == ["-e", f".[{FORK['extras']}]"]
    assert got["job"]["uv_path"] == "/usr/local/bin/uv"           # venv 作成と同じバイナリを使う


def test_find_uv_uses_fallback_dirs_without_path(monkeypatch, tmp_path: Path):
    """デスクトップ / systemd 起動で PATH が minimal でも、公式インストーラの既定先の uv を使う。"""
    uv = tmp_path / "fakebin" / "uv"
    uv.parent.mkdir()
    uv.write_text("#!/bin/sh\n", "utf-8")
    uv.chmod(0o755)
    monkeypatch.delenv("LLM_LAUNCHER_UV", raising=False)
    monkeypatch.setattr(installer.shutil, "which", lambda n: None)
    monkeypatch.setattr(installer, "_uv_candidates", lambda: [uv])
    assert installer.find_uv() == str(uv)
    assert installer.install_tool() == "uv"


def test_find_uv_respects_llm_launcher_uv_override(monkeypatch, tmp_path: Path):
    uv = tmp_path / "pinned" / "uv"
    monkeypatch.setenv("LLM_LAUNCHER_UV", str(uv))
    monkeypatch.setattr(installer.shutil, "which", lambda n: "/usr/bin/uv")
    assert installer.find_uv() == str(uv)                         # 明示指定が PATH より優先


def test_install_step_falls_back_to_the_venv_pip(monkeypatch, tmp_path: Path):
    got = _plan(monkeypatch, tmp_path / "root", uv=False)
    assert got["job"]["tool"] == "pip"
    argv = _install_step(got["steps"])["argv"]
    venvpy = argv[0]
    assert venvpy.endswith(".venv/bin/python") and argv[1:3] == ["-m", "pip"]
    assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)   # argv 直渡し (shell 文字列不作成)


# --------------------------------------------------------- プロキシ (環境変数から)

def test_proxy_from_env_reads_either_case_and_skips_empty():
    prox = installer.proxy_from_env({
        "http_proxy": "http://proxy.local:3128",   # 小文字形
        "NO_PROXY": "localhost,127.0.0.1",          # 大文字形も拾う
        "https_proxy": "   ",                        # 空値は設定なし扱い
        "PATH": "/usr/bin",                          # 無関係な変数は無視
    })
    assert prox == {"http_proxy": "http://proxy.local:3128",
                    "no_proxy": "localhost,127.0.0.1"}


def test_install_env_ships_proxy_in_both_cases(monkeypatch):
    """uv / pip / git はプロキシ変数の大小文字の読み方が違う。両形の形で引き渡す。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    monkeypatch.delenv("https_proxy", raising=False)
    env = installer.install_env()
    assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://proxy.local:3128"
    assert env["PIP_DISABLE_PIP_VERSION_CHECK"] == "1"


def test_proxy_log_line_masks_credentials(monkeypatch):
    for v in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(v, raising=False)
        monkeypatch.delenv(v.upper(), raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://alice:s3cret@proxy.local:3128")
    line = installer.proxy_log_line()
    assert "s3cret" not in line and "alice:****@" in line
    monkeypatch.delenv("HTTPS_PROXY")
    assert "未設定" in installer.proxy_log_line()


# --------------------------------------------------------- API / 画面の表示

def test_api_reports_the_real_install_tool(monkeypatch, tmp_path: Path):
    async def fake(self, job, spec, steps):  # noqa: ANN001
        return None

    monkeypatch.setattr(InstallManager, "_run_install", fake)
    _which(monkeypatch, uv=True)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    client = Client(app_mod.create_app(tmp_path))

    async def go() -> None:
        st, res = await client.get("/api/installs")
        assert st == 200 and res["tool"] == "uv"                 # 画面の手順名はこれを見る
        st, job = await client.post("/api/installs", {"backend": "freetoken-fork"})
        assert st == 201 and job["tool"] == "uv"
        await asyncio.sleep(0)

    asyncio.run(go())


# --------------------------------------------------------- セットアップ画面の配線

@pytest.mark.parametrize("probe, why", [
    ("toolLabel()", "セットアップ画面: tool → 表示文言の対応 (1 箇所だけ)"),
    ('tool: inst.tool || "uv"', "refresh で API の tool を取り込む"),
    ('${toolLabel()} -e', "手順プレビューが実ツールの文言を作る"),
])
def test_setup_screen_labels_install_with_the_real_tool(probe: str, why: str):
    assert probe in SETUP, why


def test_setup_screen_does_not_hardcode_pip_install():
    """インストール文言は insToolLabel() 経由だけ。JS に pip install を直書きしない。"""
    assert 'pip install -e "' not in HTML
    assert "→ pip install" not in HTML


@pytest.mark.parametrize("probe, why", [
    ('id="s-cal"', "セットアップ画面: キャリブレーション (調整) ステップ"),
    ('id="ca-list"', "インストール後の調整を走る/スキップするの選択"),
    ('id="ca-go"', "調整開始または設定完了のボタン"),
    ('api("/api/calibrations", { method: "POST"', "調整ジョブの実配線"),
    ("calRun(pid, ", "ダッシュボード 🎯 タブと共通の実行コア"),
    ("insEp(", "履歴のログ / 削除を installs / calibrations に振り分け"),
    ("insMerged(", "履歴を 3 種 1 本の時系列に束ねる"),
])
def test_setup_screen_wires_calibration_into_the_install_flow(probe: str, why: str):
    assert probe in SETUP + HTML, why


def test_ui_default_calibration_backends_match_the_server():
    """calSupports() のローカル既定値がサーバー CAL_BACKENDS より狭いと、API 応答より前の
    最初の 1 描画で ③ カードと「完了後にキャリブレーション」が消える (fork が抜けやすい)。"""
    m = re.search(r"const CAL_FALLBACK = \[(.*?)\]", HTML)
    assert m, "CAL_FALLBACK が見つからない"
    local = sorted(x.strip().strip("\"'") for x in m.group(1).split(",") if x.strip())
    assert local == sorted(CAL_BACKENDS)
