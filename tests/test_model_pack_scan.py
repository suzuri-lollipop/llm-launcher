"""モデル棚卸しの pack 単位スキャンと Strata / Maya のプロファイル専用 config の回帰テスト。

2026-11-16 の実害 2 件:
  1. Maya-data の棚卸しが org 直下 (models/<org>/) だけを見て、284GB の巨大
     エントリ 1 件になっていた。実際の重みは models/<org>/<quant>/ に置かれる
     (pack メタデータは <quant>/maya-ds4/ の中)。drafter (dspark) / vision の
     構成要素をモデルとして出さないのも含む。
  2. Strata / Maya のプロファイルが installer の共有 config (maya-ds4.json /
     strata-*.json) をそのまま参照し、編集 / キャリブレーションの書き戻しが
     他プロファイルへ漏れた。Basalt / freetoken-fork と同じく「モデル名-uuid8」
     の専用コピー (data/launch) へ張り替える (新規 / 更新 / 複製 / 起動時移行)。

見張る対象:
  - scan_pack_dirs: 重みを持つディレクトリ = モデル単位。その中は辿らない /
    構成要素は出さない / has_weights は実計算
  - 棚卸しエントリとプロファイル参照 (used_by / affected_configs) の突合
  - 専用 config: 作成 / 更新 (再利用) / 複製 (分離) / 削除 (後始末) / 起動時移行 /
    config-args の書き戻しが専用コピーだけに効く (installer は不変)
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from llm_launcher import models as mm
from test_auth_users import Client, make_app


@pytest.fixture(autouse=True)
def _isolated_stores(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.delenv("FREETOKEN_DATA_DIR", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    yield


def _layout(root: Path) -> dict:
    """Maya / Strata の最小レイアウト (installer config + 重み + pack メタデータ)。"""
    maya_mod = root / "module" / "maya"
    maya_mod.mkdir(parents=True)
    quant = root / "module" / "Maya-data" / "models" / "Org" / "Quant"
    pack = quant / "maya-ds4"
    pack.mkdir(parents=True)
    (quant / "model-00001-of-00002.gguf").write_bytes(b"x" * (2 << 20))
    (pack / "ds4.txt").write_text("pack meta")
    (pack / "tokenizer").mkdir()
    (pack / "tokenizer" / "vocab.json").write_text("{}")
    for comp, fname in (("dspark", "dspark.bin"), ("vision", "mmproj.gguf")):
        d = quant.parent / comp
        d.mkdir(parents=True)
        (d / fname).write_bytes(b"x" * (2 << 20))
    maya_cfg = maya_mod / "maya-ds4.json"
    maya_cfg.write_text(json.dumps({
        "exe": str(maya_mod / "build" / "maya-ds4"),
        "args": ["--pack", str(pack), "--max-context", "65536", "--tp", "1"],
        "log": str(maya_mod / "maya-ds4.log"),
        "model_name": "deepseek-test",
        "installer": {"family": "deepseek"},
    }), "utf-8")
    st_mod = root / "module" / "strata"
    st_mod.mkdir(parents=True)
    st_data = root / "module" / "Strata-data" / "models" / "org--name"
    st_data.mkdir(parents=True)
    (st_data / "weights.gguf").write_bytes(b"x" * (2 << 20))
    st_cfg = st_mod / "strata-x.json"
    st_cfg.write_text(json.dumps({
        "exe": str(st_mod / "engine" / "strata"),
        "args": ["--native", str(st_data / "weights.gguf"), "--max-context", "32768"],
        "log": str(st_mod / "strata-x.log"),
        "model_name": "strata-test",
    }), "utf-8")
    return {"maya_cfg": maya_cfg, "st_cfg": st_cfg, "quant": quant, "pack": pack}


# ---------------------------------------------------------------- pack 単位スキャン

def test_scan_pack_dirs_finds_quant_not_org(tmp_path):
    lay = _layout(tmp_path)
    entries = mm.scan_pack_dirs(lay["quant"].parent.parent, skip_names=frozenset(mm._MAYA_COMPONENT_DIRS))
    names = [e["name"] for e in entries]
    assert names == ["Org/Quant"], names          # org 丸ごと / pack メタデータ / 構成要素は出ない
    e = entries[0]
    assert e["has_weights"] is True
    assert [g["name"] for g in e["gguf"]] == ["model-00001-of-00002.gguf"]
    assert e["path"] == str(lay["quant"])


def test_scan_pack_dirs_skips_empty_and_components(tmp_path):
    lay = _layout(tmp_path)
    (lay["quant"].parent / "Empty").mkdir()       # 重み無しは出さない
    entries = mm.scan_pack_dirs(lay["quant"].parent.parent, skip_names=frozenset(mm._MAYA_COMPONENT_DIRS))
    assert [e["name"] for e in entries] == ["Org/Quant"]


def test_inventory_maya_entry_links_profile(tmp_path):
    lay = _layout(tmp_path)
    app = make_app(tmp_path)
    c = Client(app)

    async def go() -> None:
        st, p = await c.post("/api/profiles", {"name": "ds4", "backend": "maya",
                                               "values": {"config": str(lay["maya_cfg"])}})
        assert st == 201, p
        profs = json.loads((tmp_path / "data" / "profiles.json").read_text("utf-8"))
        inv = mm.build_inventory(tmp_path, profs, [])
        maya = [m for m in inv["models"] if m["source"] == "maya-data"]
        assert len(maya) == 1 and maya[0]["name"] == "Org/Quant"
        assert [u["name"] for u in maya[0]["used_by"]] == ["ds4"]
        assert maya[0]["affected_configs"] == ["maya-ds4.json"]
        # 削除ガード: 置き場ルート配下として解決できる
        roots = mm.allowed_delete_roots(tmp_path)
        rp = Path(maya[0]["path"]).resolve()
        assert any(a in rp.parents for a in roots)

    asyncio.run(go())


# ---------------------------------------------------------------- 専用 config (モデル名-uuid8)

def test_personal_config_created_on_profile_create(tmp_path):
    lay = _layout(tmp_path)
    app = make_app(tmp_path)
    c = Client(app)

    async def go() -> None:
        st, p = await c.post("/api/profiles", {"name": "ds4", "backend": "maya",
                                               "values": {"config": str(lay["maya_cfg"])}})
        assert st == 201, p
        cfg = p["values"]["config"]
        assert cfg.startswith(str(tmp_path / "data" / "launch" / "maya-")), cfg
        assert cfg.endswith(".json") and cfg != str(lay["maya_cfg"])
        doc = json.loads(Path(cfg).read_text("utf-8"))
        assert doc["source_config"] == "maya-ds4.json"
        assert doc["log"] != str(lay["maya_cfg"]).replace(".json", ".log")   # ログも専用名
        assert doc["args"][:2] == ["--pack", str(lay["pack"])]
        # installer 本体は触らない
        src = json.loads(lay["maya_cfg"].read_text("utf-8"))
        assert "source_config" not in src

    asyncio.run(go())


def test_two_profiles_isolated_edits(tmp_path):
    lay = _layout(tmp_path)
    app = make_app(tmp_path)
    c = Client(app)

    async def go() -> None:
        _, p1 = await c.post("/api/profiles", {"name": "a", "backend": "maya",
                                               "values": {"config": str(lay["maya_cfg"])}})
        _, p2 = await c.post("/api/profiles", {"name": "b", "backend": "maya",
                                               "values": {"config": str(lay["maya_cfg"])}})
        c1, c2 = p1["values"]["config"], p2["values"]["config"]
        assert c1 != c2                       # 共有しない
        r = await c.post(f"/api/profiles/{p1['id']}/config-args",
                         {"updates": {"--max-context": "131072"}})
        assert r[0] == 200, r[1]
        d1 = json.loads(Path(c1).read_text("utf-8"))
        d2 = json.loads(Path(c2).read_text("utf-8"))
        src = json.loads(lay["maya_cfg"].read_text("utf-8"))
        assert d1["args"][d1["args"].index("--max-context") + 1] == "131072"
        assert d2["args"][d2["args"].index("--max-context") + 1] == "65536"   # 漏れない
        assert src["args"][src["args"].index("--max-context") + 1] == "65536"
        # config-args GET は source_config (純正表 lookup 用 backlink) を返す
        st, g = await c.get(f"/api/profiles/{p1['id']}/config-args")
        assert st == 200 and g["source_config"] == "maya-ds4.json"

    asyncio.run(go())


def test_update_reuses_personal_copy(tmp_path):
    lay = _layout(tmp_path)
    app = make_app(tmp_path)
    c = Client(app)

    async def go() -> None:
        _, p = await c.post("/api/profiles", {"name": "a", "backend": "maya",
                                              "values": {"config": str(lay["maya_cfg"])}})
        cfg = p["values"]["config"]
        body = {"name": "a", "backend": "maya", "values": {"config": str(lay["maya_cfg"])}}
        st, p2 = await c.put(f"/api/profiles/{p['id']}", body)
        assert st == 200, p2
        assert p2["values"]["config"] == cfg    # uuid が増殖しない
        assert len(list((tmp_path / "data" / "launch").glob("maya-*.json"))) == 1

    asyncio.run(go())


def test_duplicate_detaches_personal_copy(tmp_path):
    lay = _layout(tmp_path)
    app = make_app(tmp_path)
    c = Client(app)

    async def go() -> None:
        _, p = await c.post("/api/profiles", {"name": "a", "backend": "maya",
                                              "values": {"config": str(lay["maya_cfg"])}})
        st, dup = await c.post("/api/profiles", {**p, "name": "a のコピー"})
        assert st == 201, dup
        assert dup["values"]["config"] != p["values"]["config"]   # 共有を引き継がない

    asyncio.run(go())


def test_delete_removes_personal_copy(tmp_path):
    lay = _layout(tmp_path)
    app = make_app(tmp_path)
    c = Client(app)

    async def go() -> None:
        _, p = await c.post("/api/profiles", {"name": "a", "backend": "maya",
                                              "values": {"config": str(lay["maya_cfg"])}})
        cfg = Path(p["values"]["config"])
        st, _ = await c.delete(f"/api/profiles/{p['id']}")
        assert st == 200
        assert not cfg.exists()                 # 孤立ファイルを残さない
        assert lay["maya_cfg"].exists()         # installer は残る

    asyncio.run(go())


def test_strata_personalization_and_isolation(tmp_path):
    lay = _layout(tmp_path)
    app = make_app(tmp_path)
    c = Client(app)

    async def go() -> None:
        _, p = await c.post("/api/profiles", {"name": "s", "backend": "strata",
                                              "values": {"config": str(lay["st_cfg"])}})
        cfg = p["values"]["config"]
        assert cfg.startswith(str(tmp_path / "data" / "launch" / "strata-")), cfg
        r = await c.post(f"/api/profiles/{p['id']}/config-args",
                         {"updates": {"--max-context": "131072"}})
        assert r[0] == 200, r[1]
        doc = json.loads(Path(cfg).read_text("utf-8"))
        assert doc["args"][doc["args"].index("--max-context") + 1] == "131072"
        src = json.loads(lay["st_cfg"].read_text("utf-8"))
        assert src["args"][src["args"].index("--max-context") + 1] == "32768"

    asyncio.run(go())


def test_migration_at_startup_idempotent(tmp_path):
    lay = _layout(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "profiles.json").write_text(json.dumps([
        {"id": "p_m1", "backend": "maya", "name": "m1",
         "values": {"config": str(lay["maya_cfg"])}},
        {"id": "p_s1", "backend": "strata", "name": "s1",
         "values": {"config": str(lay["st_cfg"])}},
    ]), "utf-8")
    make_app(tmp_path)                          # 起動時移行
    profs = json.loads((tmp_path / "data" / "profiles.json").read_text("utf-8"))
    cfgs = [p["values"]["config"] for p in profs]
    assert all(c.startswith(str(tmp_path / "data" / "launch")) for c in cfgs), cfgs
    assert all(Path(c).is_file() for c in cfgs)
    n = len(list((tmp_path / "data" / "launch").glob("*.json")))
    make_app(tmp_path)                          # 2 回目は増殖しない
    assert len(list((tmp_path / "data" / "launch").glob("*.json"))) == n


# ---------------------------------------------------------------- ウィザード側の追従

def test_wizard_uses_source_config_backlink():
    import llm_launcher

    js = (Path(llm_launcher.__file__).parent / "static" / "setup.js").read_text("utf-8")
    assert "source_config: r.source_config || \"\"" in js
    assert "String(W.cfgArgs.source_config || String(v.config || \"\").split(\"/\").pop())" in js


def test_scan_pack_dirs_strata_native(tmp_path):
    """0.2.19+ の .strata ネイティブ重み (Hy4-Flash / Inkling-2-Mini): 家系フォルダ
    (models/hy4-flash/ 等) がモデル単位として棚卸しに出る (重み拡張子に .strata)。
    tokenizer サブフォルダはモデル単位の中なので二重エントリにならない。"""
    root = tmp_path / "Strata-data" / "models"
    hy4 = root / "hy4-flash"
    hy4.mkdir(parents=True)
    (hy4 / "Hy4-Flash-SQ-64.strata").write_bytes(b"x" * (2 << 20))
    (hy4 / "Hy4-Flash-Engram-Q8.strata").write_bytes(b"x" * (2 << 20))
    ink = root / "inkling-2-mini"
    (ink / "tokenizer").mkdir(parents=True)
    (ink / "Inkling-2-Mini-SQ-20.strata").write_bytes(b"x" * (2 << 20))
    (ink / "tokenizer" / "tokenizer.json").write_text("{}")
    got = mm.scan_pack_dirs(root)
    names = {g["name"] for g in got}
    assert names == {"hy4-flash", "inkling-2-mini"}
    assert all(g["has_weights"] for g in got)
    # 汎用重みとしても数える (Strata の「ダウンロード済み」判定の根拠)
    has, _gguf = mm._dir_weights(hy4)
    assert has is True
