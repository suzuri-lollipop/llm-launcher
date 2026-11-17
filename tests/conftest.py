"""テスト全体の共通設定。

既存テストは /api/* を認証なしで直接叩くため、既定で認証を無効化してアプリを作る
(実行時と同じ `--no-auth` / `LLM_LAUNCHER_AUTH=0` の状態)。
認証そのものを検証する tests/test_auth_users.py は `auth_on` fixture で有効に戻す。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _auth_disabled(monkeypatch):
    """全テストで認証をオフにし、環境変数による管理者ブートストラップも無効化する。"""
    monkeypatch.setenv("LLM_LAUNCHER_AUTH", "0")
    monkeypatch.delenv("LLM_LAUNCHER_ADMIN_USER", raising=False)
    monkeypatch.delenv("LLM_LAUNCHER_ADMIN_PASSWORD", raising=False)
    yield


@pytest.fixture(autouse=True)
def _no_network_tags(monkeypatch):
    """fork 専用タグの取得 (git ls-remote) でテストがネットワークを叩かないようにする。

    既定は「タグ無し」を返すスタブ + テストごとに空のキャッシュ。タグを検証する
    テスト (test_installs_tags) は fetch_tags を自前のスタブで上書きする。
    """
    from llm_launcher import installer
    monkeypatch.setattr(installer, "_TAG_CACHE", {})
    monkeypatch.setattr(installer, "fetch_tags", lambda url, prefix, **kw: [])
    yield
