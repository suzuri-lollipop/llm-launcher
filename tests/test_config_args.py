"""ウィザード ③ (稼働設定) の config args 書き戻し (Strata / Maya) の回帰テスト。

③ は FreeToken 基準と同じ質問 (ctx → kv → tp → par の固定順) を全バックエンドで出す。
Strata / Maya のエンジン本体ノブ (--max-context / --kv / --tp) は設定 JSON
(strata-<model>.json / maya-<model>.json) の "args" に入るため、
GET/POST /api/profiles/{id}/config-args で読み書きする (2026-11-11 の
「③ の質問がバックエンドごとに揃っていない」指摘への対応)。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_launcher import app as app_mod
from llm_launcher import rbac

ROOT = Path(__file__).resolve().parents[1]
WIZARD_JS = (ROOT / "src" / "llm_launcher" / "static" / "setup.js").read_text("utf-8")


@pytest.fixture()
def client(tmp_path: Path):
    (tmp_path / "data").mkdir(parents=True)
    (tmp_path / "module" / "strata").mkdir(parents=True)
    (tmp_path / "module" / "maya").mkdir(parents=True)
    return TestClient(app_mod.create_app(tmp_path)), tmp_path


def _config(root: Path, rel: str = "module/strata/strata-x.json",
            args: list | None = None) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"exe": "/x/engine", "args": args if args is not None else
           ["--pack", "/p", "--max-context", "524288", "--kv", "int8"],
           "args_written": ["--max-context", "524288", "--kv", "int8"],
           "port": 8080}
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _profile(client, backend: str, config: str) -> str:
    r = client.post("/api/profiles", json={"name": "t", "backend": backend,
                                           "values": {"config": config}})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _prof_cfg(client, pid: str) -> Path:
    """プロファイルが実際に参照する config (共有 installer config は専用コピーへ
    張り替わるため、編集結果の検証はそちらを読む)。"""
    profs = client.get("/api/profiles").json()
    return Path(next(p for p in profs if p["id"] == pid)["values"]["config"])


def test_get_config_args_reads_current_values(client):
    c, root = client
    _config(root)
    pid = _profile(c, "strata", "module/strata/strata-x.json")
    r = c.get(f"/api/profiles/{pid}/config-args")
    assert r.status_code == 200, r.text
    assert r.json()["args"] == {"--max-context": "524288", "--kv": "int8"}


def test_post_config_args_updates_args_and_args_written(client):
    c, root = client
    p = _config(root)
    pid = _profile(c, "strata", "module/strata/strata-x.json")
    p = _prof_cfg(c, pid)
    r = c.post(f"/api/profiles/{pid}/config-args",
               json={"updates": {"--max-context": "131072", "--kv": "fp16"}})
    assert r.status_code == 200, r.text
    assert r.json()["args"] == {"--max-context": "131072", "--kv": "fp16"}
    doc = json.loads(p.read_text("utf-8"))
    for key in ("args", "args_written"):
        a = doc[key]
        assert a[a.index("--max-context") + 1] == "131072"
        assert a[a.index("--kv") + 1] == "fp16"
    assert doc["port"] == 8080          # ノブ以外は触らない


def test_post_config_args_appends_missing_flag(client):
    c, root = client
    p = _config(root, args=["--pack", "/p"])
    pid = _profile(c, "maya", "module/strata/strata-x.json")
    # --tp 1 は常に有効 (GPU 未検出環境のカード数フォールバック = 1)。
    # 2 枚以上を要求する値はカード数検証に掛かるためここでは 1 を使う
    r = c.post(f"/api/profiles/{pid}/config-args", json={"updates": {"--tp": "1"}})
    assert r.status_code == 200, r.text
    a = json.loads(_prof_cfg(c, pid).read_text("utf-8"))["args"]
    assert a[-2:] == ["--tp", "1"]


def test_post_config_args_whitelist(client):
    c, root = client
    _config(root)
    pid = _profile(c, "strata", "module/strata/strata-x.json")
    for updates in ({"--pack": "/evil"}, {"--kv": "fp8"}, {"--max-context": "999"},
                    {"--tp": "0"}, {"--max-context": "abc"}):
        r = c.post(f"/api/profiles/{pid}/config-args", json={"updates": updates})
        assert r.status_code == 400, updates
    r = c.post(f"/api/profiles/{pid}/config-args", json={"updates": {}})
    assert r.status_code == 400


def test_config_args_path_scoped_to_module(client):
    c, root = client
    _config(root)
    outside = root / "outside.json"
    outside.write_text(json.dumps({"args": ["--kv", "int8"]}), encoding="utf-8")
    for cfg in ("outside.json", "../outside.json", "module/strata/notes.json"):
        pid = _profile(c, "strata", cfg)
        assert c.get(f"/api/profiles/{pid}/config-args").status_code == 400, cfg
        assert c.post(f"/api/profiles/{pid}/config-args",
                      json={"updates": {"--kv": "fp16"}}).status_code == 400, cfg
    assert json.loads(outside.read_text())["args"] == ["--kv", "int8"]


def test_config_args_backend_and_config_guards(client):
    c, root = client
    _config(root)
    pid = _profile(c, "vllm", "module/strata/strata-x.json")
    assert c.get(f"/api/profiles/{pid}/config-args").status_code == 400
    pid = _profile(c, "strata", "")
    assert c.get(f"/api/profiles/{pid}/config-args").status_code == 400


def test_config_args_rbac():
    assert rbac.required_cap("GET", "/api/profiles/p1/config-args") == "config.read"
    assert rbac.required_cap("POST", "/api/profiles/p1/config-args") == "profiles.edit"


# ---------------------------------------------------------------- GPU の使い方 (③ へ移動)

def _seed_gpus(monkeypatch, root: Path, multi: bool = True) -> None:
    """strata_setup.load_options が subprocess なしで PC の GPU 一覧を返すようにする。"""
    import time

    from llm_launcher import strata_setup
    mod = root / "module" / "strata"
    (mod / "setup.py").write_text("# fake\n", encoding="utf-8")  # load_options の実在ガード用
    tables = {"available": True, "contexts": [],
              "families": {"q": {"tag": "", "multi_gpu": multi, "models": {"x": {}}}},
              "pc": {"gpus": [{"index": 0, "usable": True}, {"index": 1, "usable": True}]}}
    monkeypatch.setattr(strata_setup, "_CACHE",
                        {str(mod): (time.monotonic(), tables),
                         str(mod.resolve()): (time.monotonic(), tables)})


def test_get_config_args_includes_gpu_state(client, monkeypatch):
    c, root = client
    _config(root)                      # gpu キー無し = まだ聞いていない
    _seed_gpus(monkeypatch, root)
    pid = _profile(c, "strata", "module/strata/strata-x.json")
    r = c.get(f"/api/profiles/{pid}/config-args").json()
    assert r["gpu"] == "auto" and r["gpus"] == [0, 1] and r["gpu_multi"] is True


def test_post_config_args_gpu_multi_then_single(client, monkeypatch):
    c, root = client
    p = _config(root)
    _seed_gpus(monkeypatch, root)
    pid = _profile(c, "strata", "module/strata/strata-x.json")
    p = _prof_cfg(c, pid)
    r = c.post(f"/api/profiles/{pid}/config-args", json={"updates": {"gpu": "0,1"}})
    assert r.status_code == 200, r.text
    doc = json.loads(p.read_text("utf-8"))
    assert doc["gpu"] == [0, 1] and doc["layer_split"] == "auto" and doc["gpus_asked"] is True
    assert r.json()["gpu"] == "0,1"
    # 単一カードへ戻すと layer_split は消える (純正の単一構成と同じ形)
    r = c.post(f"/api/profiles/{pid}/config-args", json={"updates": {"gpu": "1"}})
    assert r.status_code == 200, r.text
    doc = json.loads(p.read_text("utf-8"))
    assert doc["gpu"] == 1 and "layer_split" not in doc and doc["gpus_asked"] is True
    # auto は現状維持 (書き換えない)
    r = c.post(f"/api/profiles/{pid}/config-args", json={"updates": {"gpu": "auto"}})
    assert r.status_code == 200 and json.loads(p.read_text("utf-8"))["gpu"] == 1


def test_post_config_args_gpu_validation(client, monkeypatch):
    c, root = client
    _config(root)
    _seed_gpus(monkeypatch, root)
    pid = _profile(c, "strata", "module/strata/strata-x.json")
    for gv in ("5", "0,5", "0,0", "abc", "0,1,2"):
        r = c.post(f"/api/profiles/{pid}/config-args", json={"updates": {"gpu": gv}})
        assert r.status_code == 400, gv
    # 単一 GPU 宣言の家系 (config 名 strata-x.json が families の tag+model に一致) は複数拒否
    _seed_gpus(monkeypatch, root, multi=False)
    r = c.post(f"/api/profiles/{pid}/config-args", json={"updates": {"gpu": "0,1"}})
    assert r.status_code == 400
    r = c.post(f"/api/profiles/{pid}/config-args", json={"updates": {"gpu": "0"}})
    assert r.status_code == 200
    # Strata / Maya 以外のバックエンドは gpu 編集非対応
    pid = _profile(c, "vllm", "module/strata/strata-x.json")
    r = c.post(f"/api/profiles/{pid}/config-args", json={"updates": {"gpu": "0"}})
    assert r.status_code == 400


# ---------------------------------------------------------------- ウィザード側の揃え

def _runcfg_block(bid: str) -> str:
    import re
    m = re.search(r'"%s": \{(.*?)\} \},' % re.escape(bid), WIZARD_JS, re.S)
    assert m, f"RUNCFG に {bid} が無い"
    return m.group(1)


def test_wizard_strata_asks_ctx_and_kv_like_freetoken():
    st = _runcfg_block("strata")
    assert 'flag: "--max-context", config: true' in st
    assert 'flag: "--kv", config: true' in st
    assert 'regen: "config-args"' in st
    # FreeToken 基準の質問順 (ctx → kv → par) で定義されている
    assert st.index('key: "max_context"') < st.index('key: "kv"') < st.index('key: "parallel"')


def test_wizard_maya_asks_ctx_kv_tp_like_freetoken():
    my = _runcfg_block("maya")
    for flag in ('"--max-context"', '"--kv"', '"--tp"'):
        assert f"flag: {flag}, config: true" in my
    assert my.index('key: "max_context"') < my.index('key: "kv"') \
        < my.index('key: "tp"') < my.index('key: "parallel"')


def test_wizard_config_args_roundtrip_wiring():
    # ③ プリフィルは GET (args + gpu 状態)、保存は POST、④ (接続設定) は config を再生成しない
    assert "config-args`)) || {}" in WIZARD_JS
    assert "r.args || {}" in WIZARD_JS
    assert 'body: { updates }' in WIZARD_JS
    assert 'cfg.regen !== "config-args"' in WIZARD_JS
    # config 主体のプロファイルは作成時から ② の config を結びつける
    assert "initVals.config = jcfg" in WIZARD_JS
