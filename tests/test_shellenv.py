"""シェル環境変数フォールバック (shellenv) の回帰テスト。

背景 (2026-11-18 報告): 別端末でサービス運用している環境で
  1. freetoken-fork のモデルがディスクに在るのに「ダウンロード済みモデルが
     見つからない」— 端末では FREETOKEN_DATA_DIR / HF_HOME を export して
     setup.sh / HF CLI でモデルを置いたが、systemd サービスは .bashrc を
     読まないため launcher が既定の置き場をスキャンしていた。
  2. アップデート時のモジュールインストールにプロキシが効かない —
     EnvironmentFile の抽出はインストール時点だけで、古いユニット /
     後からの .bashrc 変更 / 再実行なしの restart では届かない。
 launcher 起動時に「プロセス環境に無いキーだけ」シェル設定から補完する。
明示 env (EnvironmentFile / 端末の export) が常に優先。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from llm_launcher import backends, installer, models, shellenv


@pytest.fixture(autouse=True)
def _clean_applied(monkeypatch):
    monkeypatch.setattr(shellenv, "_APPLIED", {})


def _home(tmp_path: Path, bashrc: str = "", profile: str = "",
          bash_profile: str = "") -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if bashrc:
        (home / ".bashrc").write_text(bashrc, "utf-8")
    if profile:
        (home / ".profile").write_text(profile, "utf-8")
    if bash_profile:
        (home / ".bash_profile").write_text(bash_profile, "utf-8")
    return home


# ---------------------------------------------------------------- 抽出規則

def test_extract_basic_and_case():
    text = ('export HTTP_PROXY="http://p:8080"\n'
            'export https_proxy=http://s:8080\n'
            'export HF_HOME=/data/hf\n'
            'export hf_hub_cache=/data/hf/hub\n'
            'export FREETOKEN_DATA_DIR=/mnt/models\n')
    got = shellenv.extract_exports(text)
    assert got == {"http_proxy": "http://p:8080", "https_proxy": "http://s:8080",
                   "HF_HOME": "/data/hf", "HF_HUB_CACHE": "/data/hf/hub",
                   "FREETOKEN_DATA_DIR": "/mnt/models"}


def test_extract_quotes_comments_and_unset():
    text = ("export http_proxy='http://a:1'   # コメント\n"
            'export https_proxy="http://b:2"\n'
            "export no_proxy=localhost,127.0.0.1 # 末尾コメント\n"
            "export all_proxy=\n")            # 空値 = 設定なし
    got = shellenv.extract_exports(text)
    assert got["http_proxy"] == "http://a:1"
    assert got["https_proxy"] == "http://b:2"
    assert got["no_proxy"] == "localhost,127.0.0.1"
    assert "all_proxy" not in got


def test_extract_skips_expansion_and_non_export():
    text = ('export http_proxy="http://${USER}@p:8080"\n'   # $ 展開はスキップ
            'http_proxy=http://no-export:1\n'               # export 無しは拾わない
            '# export https_proxy=http://commented:2\n'
            'export UNRELATED=1\n'
            'export ftp_proxy=http://ftp:3\n')              # 対象外キー
    assert shellenv.extract_exports(text) == {}


def test_extract_last_wins_and_empty_clears():
    text = ("export http_proxy=http://first:1\n"
            "export http_proxy=http://second:2\n"
            "export https_proxy=http://s:1\n"
            "export https_proxy=\n")
    got = shellenv.extract_exports(text)
    assert got == {"http_proxy": "http://second:2"}


def test_file_order_bashrc_wins(tmp_path):
    home = _home(tmp_path,
                 bashrc="export http_proxy=http://bashrc:1\nexport HF_HOME=/rc\n",
                 profile="export http_proxy=http://profile:1\nexport HF_HOME=/profile\n",
                 bash_profile="export http_proxy=http://bash_profile:1\n")
    got = shellenv.shell_env_fallback(home)
    assert got["http_proxy"] == "http://bashrc:1"
    assert got["HF_HOME"] == "/rc"


def test_missing_files_are_safe(tmp_path):
    assert shellenv.shell_env_fallback(tmp_path / "nope") == {}


# ---------------------------------------------------------------- 適用 (明示 env 優先)

def test_apply_fills_only_missing(tmp_path):
    home = _home(tmp_path, bashrc=(
        "export http_proxy=http://p:1\n"
        "export HF_HOME=/data/hf\n"
        "export FREETOKEN_DATA_DIR=/mnt/models\n"))
    env = {"http_proxy": "http://explicit:9", "HF_HOME": "  "}
    applied = shellenv.apply_shell_env(env, home)
    assert env["http_proxy"] == "http://explicit:9"   # 明示値は上書きしない
    assert env["HF_HOME"] == "/data/hf"               # 空白のみは未設定扱い
    assert env["FREETOKEN_DATA_DIR"] == "/mnt/models"
    assert applied == {"HF_HOME": "/data/hf", "FREETOKEN_DATA_DIR": "/mnt/models"}
    assert shellenv.applied_keys() == {}              # 明示 environ 渡しは記録しない


def test_apply_records_for_log(tmp_path, monkeypatch):
    home = _home(tmp_path, bashrc="export https_proxy=http://p:1\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    applied = shellenv.apply_shell_env()
    assert applied == {"https_proxy": "http://p:1"}
    assert shellenv.applied_keys() == {"https_proxy": "http://p:1"}


# ---------------------------------------------------------------- 実消費側への反映

def test_install_env_gets_proxy_from_bashrc(tmp_path, monkeypatch):
    """サービスにプロキシが届かなくても install_env (git/uv/pip) が効く。"""
    home = _home(tmp_path, bashrc="export http_proxy=http://user:pass@p:8080\n")
    monkeypatch.setenv("HOME", str(home))
    for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
              "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)
    shellenv.apply_shell_env()
    env = installer.install_env()
    assert env["http_proxy"] == "http://user:pass@p:8080"
    assert env["HTTP_PROXY"] == "http://user:pass@p:8080"
    line = installer.proxy_log_line()
    assert "補完" in line and "****" in line          # 出所表示 + 資格情報の伏せ字
    assert "pass" not in line


def test_model_dirs_follow_bashrc(tmp_path, monkeypatch):
    """FREETOKEN_DATA_DIR / HF_HOME の補完でモデル置き場が端末と一致する。"""
    home = _home(tmp_path, bashrc=(
        "export FREETOKEN_DATA_DIR=/mnt/ft\n"
        "export HF_HOME=/data/hf\n"))
    monkeypatch.setenv("HOME", str(home))
    for k in ("FREETOKEN_DATA_DIR", "HF_HOME", "HF_HUB_CACHE"):
        monkeypatch.delenv(k, raising=False)
    shellenv.apply_shell_env()
    root = tmp_path / "proj"
    b = backends.BACKENDS["freetoken-fork"]
    assert backends.fork_model_root(root, b) == Path("/mnt/ft/models")
    assert models.hf_hub_cache_dir() == Path("/data/hf/hub")


def test_describe_applied_masks_proxy_only():
    text = shellenv.describe_applied({
        "http_proxy": "http://user:secret@p:8080",
        "FREETOKEN_DATA_DIR": "/mnt/models"})
    assert "secret" not in text and "****" in text
    assert "/mnt/models" in text
