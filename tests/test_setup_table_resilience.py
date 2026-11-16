"""セットアップ表 (strata_setup / maya_setup load_options) の読み出し耐性の回帰テスト。

2026-11-16 の実害: キャリブレーションが CPU を占有している間に表 dump の subprocess が
timeout し、その失敗が 60 秒キャッシュされた — その間ウィザード ② は「一覧が読めません」
でダウンロードボタン / ジョブ検証が死に、256G のダウンロードが開始できないままだった
(ジョブが一件も作られない = ダウンロード自体が起きていない)。

見張る対象:
  1. 読み出し失敗を長くキャッシュしない: 短いバックオフで再試行し、その間は
     直近の成功表 (10 分以内) を返してウィザード / ジョブ検証を生かし続ける
  2. 成功時はキャッシュを更新し失敗記録を消す
  3. timeout は高負荷に余裕がある (>= 120s)
  4. build_argv は「ソース未取得」と「読み出し失敗 (再試行)」を区別して案内する
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from llm_launcher import maya_setup, strata_setup

GOOD = {"families": {"glm": {"title": "GLM", "models": {"m1": {"label": "M1"}}}}, "pc": {}}


def _ok_run(*a, **k):
    return subprocess.CompletedProcess(a[0], 0, stdout=json.dumps(GOOD) + "\n", stderr="")


def _timeout_run(*a, **k):
    raise subprocess.TimeoutExpired(a[0], k.get("timeout", 180))


@pytest.fixture
def mod(tmp_path, monkeypatch):
    d = tmp_path / "module" / "maya"
    d.mkdir(parents=True)
    (d / "maya.py").write_text("# stub")
    monkeypatch.setattr(maya_setup, "_CACHE", {})
    monkeypatch.setattr(maya_setup, "_FAIL", {})
    return d


def test_failure_serves_last_good_and_retries(monkeypatch, mod):
    monkeypatch.setattr(maya_setup.subprocess, "run", _ok_run)
    assert maya_setup.load_options(mod)["available"] is True
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise subprocess.TimeoutExpired(a[0], 180)

    monkeypatch.setattr(maya_setup.subprocess, "run", boom)
    # 成功キャッシュを TTL 切れにする → 失敗パスは直近の成功表を返す
    k = str(mod)
    ts, data = maya_setup._CACHE[k]
    monkeypatch.setitem(maya_setup._CACHE, k, (ts - 120, data))
    r = maya_setup.load_options(mod)
    assert r["available"] is True and r is data      # 失敗でもウィザードは死なない
    assert len(calls) == 1
    # バックオフ中は subprocess を叩かない
    r2 = maya_setup.load_options(mod)
    assert r2["available"] is True and len(calls) == 1
    assert k in maya_setup._FAIL


def test_failure_without_good_is_unavailable_not_cached(monkeypatch, mod):
    monkeypatch.setattr(maya_setup.subprocess, "run", _timeout_run)
    r = maya_setup.load_options(mod)
    assert r["available"] is False
    assert str(mod) not in maya_setup._CACHE          # 失敗を成功キャッシュに入れない
    # バックオフ明けの再試行 (今度は成功) → キャッシュ更新 / 失敗記録消去
    monkeypatch.setattr(maya_setup, "_FAIL", {})
    monkeypatch.setattr(maya_setup.subprocess, "run", _ok_run)
    assert maya_setup.load_options(mod)["available"] is True
    assert str(mod) not in maya_setup._FAIL


def test_strata_failure_serves_last_good(monkeypatch, tmp_path):
    d = tmp_path / "module" / "strata"
    d.mkdir(parents=True)
    (d / "setup.py").write_text("# stub")
    monkeypatch.setattr(strata_setup, "_CACHE", {})
    monkeypatch.setattr(strata_setup, "_FAIL", {})
    good = {"families": {"qwen": {"models": {"m": {}}}}, "contexts": [32768], "pc": {}}
    monkeypatch.setattr(strata_setup.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout=json.dumps(good) + "\n", stderr=""))
    assert strata_setup.load_options(d)["available"] is True
    k = str(d)
    ts, data = strata_setup._CACHE[k]
    monkeypatch.setitem(strata_setup._CACHE, k, (ts - 120, data))
    monkeypatch.setattr(strata_setup.subprocess, "run", _timeout_run)
    assert strata_setup.load_options(d)["available"] is True


def test_read_timeout_has_headroom():
    assert maya_setup._READ_TIMEOUT >= 120
    assert strata_setup._READ_TIMEOUT >= 120


def test_build_argv_distinguishes_missing_source_and_read_failure(monkeypatch, mod):
    monkeypatch.setattr(maya_setup.subprocess, "run", _timeout_run)
    opts = {"family": "deepseek", "model": "abliterated-256g", "context": 32768, "vision": ""}
    with pytest.raises(ValueError, match="高負荷"):
        maya_setup.build_argv(mod, opts)
    (mod / "maya.py").unlink()
    with pytest.raises(ValueError, match="未取得"):
        maya_setup.build_argv(mod, opts)


def test_wizard_hint_is_honest_about_load_failure():
    import llm_launcher

    js = (Path(llm_launcher.__file__).parent / "static" / "setup.js").read_text("utf-8")
    assert "モデル一覧を読み出せませんでした (PC が高負荷の可能性)" in js
