"""全バックエンド横断のモデル管理 (/api/models) のテスト。

棚卸し (HF キャッシュ / model_data / Strata-data / Maya-data) が 1 つに束ねられること、
各エントリの「読めるバックエンド / 参照プロファイル / 稼働中」判定、そして削除ガード
(稼働中 409 / 参照あり force / 置き場ルート外拒否) を検証する。
起動 config JSON (strata-*.json / maya-*.json) はプロファイル側の設定なので
棚卸しエントリにしない (削除ガードの参照解決にだけ使う)。
ネットワークは使わない (HF キャッシュは tmp へ向ける)。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from llm_launcher import models as mm
from llm_launcher import rbac
from llm_launcher.app import scan_data_models, scan_hf_cache  # 再エクスポート互換

from test_auth_users import Client, auth_on, fast_kdf, make_app, setup_admin  # noqa: F401
from test_roles import add_user, inject_session, login


@pytest.fixture(autouse=True)
def _isolated_stores(monkeypatch, tmp_path):
    """実機の HF キャッシュ / Freetoken-data を見ない (棚卸しは tmp だけで完結)。"""
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.delenv("FREETOKEN_DATA_DIR", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    yield


def _hf_repo(base: Path, repo: str, files: dict[str, int]) -> Path:
    snap = base / f"models--{repo.replace('/', '--')}" / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    for fname, size in files.items():
        (snap / fname).write_bytes(b"x" * size)
    return snap


def _data_model(root: Path, name: str, files: dict[str, int]) -> Path:
    d = root / "data" / "Freetoken-data" / "models" / name
    d.mkdir(parents=True)
    for fname, size in files.items():
        (d / fname).write_bytes(b"x" * size)
    return d


def _strata_config(root: Path, name: str, pack: Path, model_name: str = "") -> Path:
    mod = root / "module" / "strata"
    mod.mkdir(parents=True, exist_ok=True)
    p = mod / f"strata-{name}.json"
    p.write_text(json.dumps({
        "exe": "/bin/true", "args": ["--pack", str(pack)],
        "model_name": model_name or name, "parallel": 4}), "utf-8")
    return p


# ------------------------------------------------------------------ スキャナ互換


def test_scanners_still_exported_from_app_and_carry_size(tmp_path):
    snap = _hf_repo(tmp_path / "hub", "org/Mini", {"model.safetensors": 2 << 20})
    m = scan_hf_cache(tmp_path / "hub")["models"][0]
    assert m["repo_id"] == "org/Mini" and m["has_weights"]
    assert m["size_bytes"] >= 2 << 20 and m["mtime"] > 0
    data = _data_model(tmp_path, "org--Data", {"model.safetensors": 2 << 20})
    d = scan_data_models(data.parent)[0]
    assert d["size_bytes"] >= 2 << 20 and d["source"] == "data"
    assert snap.is_dir()


# ------------------------------------------------------------------ 棚卸し


def test_inventory_covers_all_storage_kinds(tmp_path):
    root = tmp_path
    _hf_repo(root / "hub", "org/Mini", {"model.safetensors": 2 << 20})
    _hf_repo(root / "hub", "org/GGUF", {"m.gguf": 2 << 20})
    _hf_repo(root / "hub", "jesdga/Qwen3.8-Flash-Next-Basalt",
             {"Qwen3.8-FN-NVFP4-DQ6_K.basalt": 2 << 20})
    _data_model(root, "org--Data", {"model.safetensors": 2 << 20})
    pack = root / "data" / "Strata-data" / "packs" / "testpack"
    pack.mkdir(parents=True)
    (pack / "w.gguf").write_bytes(b"x" * (2 << 20))
    cfg = _strata_config(root, "test", pack, model_name="test-model")
    maya_weights = root / "data" / "Maya-data" / "models" / "ds4"
    maya_weights.mkdir(parents=True)
    (maya_weights / "w.safetensors").write_bytes(b"x" * (2 << 20))
    maya_mod = root / "module" / "maya"
    maya_mod.mkdir(parents=True, exist_ok=True)
    (maya_mod / "maya-ds4.json").write_text(json.dumps({
        "exe": "/bin/true", "args": ["--model", str(maya_weights / "w.safetensors")],
        "model_name": "ds4"}), "utf-8")

    inv = mm.build_inventory(root, [], [])
    by_id = {m["id"]: m for m in inv["models"]}

    mini = by_id["cache:org/Mini"]
    assert mini["kind"] == "repo" and mini["size_bytes"] >= 2 << 20
    assert {"vllm", "sglang", "freetoken", "freetoken-fork"} <= set(mini["backends"])
    assert "basalt" not in mini["backends"]          # 汎用重みを basalt は読めない

    gguf = by_id["cache:org/GGUF"]
    assert {"llamacpp", "strata", "strata-fork"} <= set(gguf["backends"])

    bas = by_id["cache:jesdga/Qwen3.8-Flash-Next-Basalt"]
    assert bas["backends"] == ["basalt"] and not bas["has_weights"]

    data = next(m for m in inv["models"] if m["source"] == "data")
    assert data["repo_id"] == "org/Data" and data["owner"] == "freetoken-fork"

    scfg = by_id.get(f"cfg:{cfg}")
    assert scfg is None                        # config JSON はプロファイル側 = 棚卸しに出ない
    assert not any(m["kind"] == "config" for m in inv["models"])
    assert not any(m["source"] in ("strata", "maya") for m in inv["models"])

    spack = by_id[f"strata-data:{pack}"]
    assert spack["affected_configs"] == ["strata-test.json"]

    assert f"cfg:{maya_mod / 'maya-ds4.json'}" not in by_id
    assert f"maya-data:{maya_weights}" in by_id

    # 対応モデル表: basalt の静的 model_list は取得済み判定が立つ
    bas_cat = next(b for b in inv["backends"] if b["id"] == "basalt")
    ent = next(s for s in bas_cat["supported"]
               if s["repo_id"] == "jesdga/Qwen3.8-Flash-Next-Basalt")
    assert ent["downloaded"] is True and ent["entry_id"] == bas["id"]
    # 全 9 バックエンドが並ぶ
    assert len(inv["backends"]) == 9
    assert {b["id"] for b in inv["backends"]} == {
        "vllm", "sglang", "llamacpp", "freetoken", "freetoken-fork",
        "strata", "strata-fork", "maya", "basalt"}


def test_inventory_used_by_and_running(tmp_path):
    root = tmp_path
    snap = _hf_repo(root / "hub", "org/Mini", {"model.safetensors": 2 << 20})
    prof = {"id": "p1", "name": "Mini サーバー", "backend": "vllm",
            "values": {"model": str(snap)}}
    inv = mm.build_inventory(root, [prof], [])
    m = inv["models"][0]
    assert m["used_by"] == [{"id": "p1", "name": "Mini サーバー", "backend": "vllm"}]
    assert m["running"] is False
    sess = {"id": "s1", "profile_id": "p1", "status": "running", "argv": []}
    inv = mm.build_inventory(root, [prof], [sess])
    assert inv["models"][0]["running"] is True


def test_inventory_used_by_via_launch_json_and_config(tmp_path):
    root = tmp_path
    data = _data_model(root, "org--Data", {"model.safetensors": 2 << 20})
    # freetoken 系: 起動 JSON の --model-path 経由
    launch = root / "data" / "launch" / "ft.json"
    launch.parent.mkdir(parents=True)
    launch.write_text(json.dumps({"argv": ["-m", "freetoken", "--model-path", str(data)]}), "utf-8")
    prof = {"id": "p1", "name": "FT", "backend": "freetoken-fork",
            "values": {"config": str(launch)}}
    # strata: config JSON の --pack 経由
    pack = root / "data" / "Strata-data" / "packs" / "pk"
    pack.mkdir(parents=True)
    (pack / "w.gguf").write_bytes(b"x" * (2 << 20))
    cfg = _strata_config(root, "pk", pack)
    prof2 = {"id": "p2", "name": "ST", "backend": "strata", "values": {"config": str(cfg)}}
    inv = mm.build_inventory(root, [prof, prof2], [])
    by_id = {m["id"]: m for m in inv["models"]}
    assert [u["id"] for u in by_id[f"data:{data}"]["used_by"]] == ["p1"]
    # config JSON 自体はエントリにならないが、config 経由の参照は重み側に立つ
    assert f"cfg:{cfg}" not in by_id
    assert [u["id"] for u in by_id[f"strata-data:{pack}"]["used_by"]] == ["p2"]


def test_inventory_catalog_downloaded_from_docs_table(tmp_path):
    root = tmp_path
    docs = root / "module" / "fork" / "freetoken" / "docs"
    docs.mkdir(parents=True)
    (docs / "models.md").write_text(
        "| モデル | リンク |\n| --- | --- |\n"
        "| Mini | [org/Mini](https://huggingface.co/org/Mini) |\n", "utf-8")
    _data_model(root, "org--Mini", {"model.safetensors": 2 << 20})
    inv = mm.build_inventory(root, [], [])
    ft = next(b for b in inv["backends"] if b["id"] == "freetoken-fork")
    ent = next(s for s in ft["supported"] if s["repo_id"] == "org/Mini")
    assert ent["downloaded"] is True
    # 掲載 repo は形に関係なくそのバックエンドの互換リストに入る
    m = next(x for x in inv["models"] if x["repo_id"] == "org/Mini")
    assert "freetoken-fork" in m["backends"]


# ------------------------------------------------------------------ 削除ガード


def test_delete_unknown_id_is_404(tmp_path):
    with pytest.raises(mm.ModelError) as e:
        mm.delete_entry(tmp_path, "cache:../../etc/passwd", force=True,
                        profiles=[], sessions=[])
    assert e.value.status == 404


def test_delete_cache_repo_frees_space(tmp_path):
    root = tmp_path
    _hf_repo(root / "hub", "org/Mini", {"model.safetensors": 2 << 20})
    repo_dir = root / "hub" / "models--org--Mini"
    out = mm.delete_entry(root, "cache:org/Mini", force=False, profiles=[], sessions=[])
    assert out["ok"] and out["freed_bytes"] >= 2 << 20
    assert not repo_dir.exists()
    assert (root / "hub").is_dir()          # 置き場ルート自体は消えない


def test_delete_profile_guard_and_force(tmp_path):
    root = tmp_path
    snap = _hf_repo(root / "hub", "org/Mini", {"model.safetensors": 2 << 20})
    prof = {"id": "p1", "name": "P", "backend": "vllm", "values": {"model": str(snap)}}
    with pytest.raises(mm.ModelError) as e:
        mm.delete_entry(root, "cache:org/Mini", force=False, profiles=[prof], sessions=[])
    assert e.value.status == 409 and "force" in e.value.message
    out = mm.delete_entry(root, "cache:org/Mini", force=True, profiles=[prof], sessions=[])
    assert out["ok"] and not snap.exists()


def test_delete_running_guard_beats_force(tmp_path):
    root = tmp_path
    snap = _hf_repo(root / "hub", "org/Mini", {"model.safetensors": 2 << 20})
    prof = {"id": "p1", "name": "P", "backend": "vllm", "values": {"model": str(snap)}}
    sess = {"id": "s1", "profile_id": "p1", "status": "running", "argv": []}
    with pytest.raises(mm.ModelError) as e:
        mm.delete_entry(root, "cache:org/Mini", force=True, profiles=[prof], sessions=[sess])
    assert e.value.status == 409 and snap.exists()
    # argv が直接モデルパスを指す場合も同じ (プロファイル参照が無くても)
    sess2 = {"id": "s2", "profile_id": "other", "status": "running",
             "argv": ["python", "-m", "vllm", str(snap)]}
    with pytest.raises(mm.ModelError) as e:
        mm.delete_entry(root, "cache:org/Mini", force=True, profiles=[], sessions=[sess2])
    assert e.value.status == 409


def test_delete_busy_download_dir_is_409(tmp_path):
    root = tmp_path
    _hf_repo(root / "hub", "org/Mini", {"model.safetensors": 2 << 20})
    busy = str(root / "hub" / "models--org--Mini")
    with pytest.raises(mm.ModelError) as e:
        mm.delete_entry(root, "cache:org/Mini", force=True, profiles=[], sessions=[],
                        busy_dirs=[busy])
    assert e.value.status == 409


def test_config_json_is_not_managed_by_models_api(tmp_path):
    """config JSON はプロファイル側の設定: 棚卸しに出ず、モデル API からも削除できない。
    重みの削除ガードは config 経由のプロファイル参照を引き続き守る。"""
    root = tmp_path
    pack = root / "data" / "Strata-data" / "packs" / "pk"
    pack.mkdir(parents=True)
    (pack / "w.gguf").write_bytes(b"x" * (2 << 20))
    cfg = _strata_config(root, "pk", pack)
    inv = mm.build_inventory(root, [], [])
    assert f"cfg:{cfg}" not in {m["id"] for m in inv["models"]}
    with pytest.raises(mm.ModelError) as e:
        mm.delete_entry(root, f"cfg:{cfg}", force=True, profiles=[], sessions=[])
    assert e.value.status == 404 and cfg.exists()
    # 重みディレクトリの削除は参照 config のプロファイルを守る
    prof = {"id": "p2", "name": "ST", "backend": "strata", "values": {"config": str(cfg)}}
    with pytest.raises(mm.ModelError) as e:
        mm.delete_entry(root, f"strata-data:{pack}", force=False, profiles=[prof], sessions=[])
    assert e.value.status == 409
    out = mm.delete_entry(root, f"strata-data:{pack}", force=True, profiles=[prof], sessions=[])
    assert out["ok"] and not pack.exists()
    assert cfg.exists()                        # config はプロファイル管理側のまま残る


def test_delete_never_touches_storage_roots(tmp_path):
    # 置き場ルートそのものを指す細工は棚卸しに現れない (エントリは必ずその配下)
    roots = mm.allowed_delete_roots(tmp_path)
    assert (tmp_path / "hub").resolve() in roots
    assert (tmp_path / "data" / "Maya-data").resolve() in roots
    # config JSON の置き場 (module 直下) は削除を許さない = プロファイル側
    assert (tmp_path / "module" / "strata").resolve() not in roots
    assert (tmp_path / "module" / "maya").resolve() not in roots
    for r in roots:
        assert r != tmp_path.resolve()      # root 自体は許可ルートに含まれない


# ------------------------------------------------------------------ API / 権限


def test_api_models_inventory_and_delete(tmp_path):
    app = make_app(tmp_path)
    _hf_repo(tmp_path / "hub", "org/Mini", {"model.safetensors": 2 << 20})
    c = Client(app)

    async def go() -> None:
        st, inv = await c.get("/api/models")
        assert st == 200
        m = next(x for x in inv["models"] if x["repo_id"] == "org/Mini")
        assert m["backends"] and m["size_bytes"] > 0
        # 参照プロファイル付き → force 無しは 409、force ありで削除
        st, p = await c.post("/api/profiles", {"name": "Mini", "backend": "vllm",
                                               "values": {"model": m["path"]}})
        assert st == 201
        st, r = await c.request("DELETE", "/api/models", {"id": m["id"]})
        assert st == 409 and "force" in r["detail"]
        st, r = await c.request("DELETE", "/api/models", {"id": m["id"], "force": True})
        assert st == 200 and r["ok"] and r["freed_bytes"] > 0
        st, inv = await c.get("/api/models")
        assert not any(x["repo_id"] == "org/Mini" for x in inv["models"])
        st, r = await c.request("DELETE", "/api/models", {"id": m["id"]})
        assert st == 404
    asyncio.run(go())


def test_api_models_running_session_blocks_delete(tmp_path):
    app = make_app(tmp_path)
    snap = _hf_repo(tmp_path / "hub", "org/Mini", {"model.safetensors": 2 << 20})
    c = Client(app)

    async def go() -> None:
        st, p = await c.post("/api/profiles", {"name": "Mini", "backend": "vllm",
                                               "values": {"model": str(snap)}})
        assert st == 201
        inject_session(app, p, "s1", "running")
        st, inv = await c.get("/api/models")
        m = next(x for x in inv["models"] if x["repo_id"] == "org/Mini")
        assert m["running"] is True
        st, r = await c.request("DELETE", "/api/models", {"id": m["id"], "force": True})
        assert st == 409 and snap.exists()
    asyncio.run(go())


def test_api_models_requires_install_cap(tmp_path, auth_on, fast_kdf):
    app = make_app(tmp_path)
    admin = Client(app)

    async def go() -> None:
        await setup_admin(admin)
        await add_user(admin, "hana", "maintainer")
        hana = await login(app, "hana")
        st, r = await hana.get("/api/models")
        assert st == 403 and r["code"] == "forbidden"
        st, r = await hana.request("DELETE", "/api/models", {"id": "cache:x"})
        assert st == 403
        st, _ = await admin.get("/api/models")
        assert st == 200
        # install を開示するとメンテナーも使える
        st, _ = await admin.put("/api/roles/maintainer", {"perms": {"install": True}})
        assert st == 200
        st, _ = await hana.get("/api/models")
        assert st == 200
    asyncio.run(go())


def test_rbac_maps_models_to_install():
    assert rbac.required_cap("GET", "/api/models") == "install"
    assert rbac.required_cap("DELETE", "/api/models") == "install"


# ------------------------------------------------------------------ UI 配線


def _frontend_src() -> str:
    base = Path(__file__).resolve().parents[1] / "src" / "llm_launcher" / "static"
    parts = [(base / "index.html").read_text(encoding="utf-8")]
    parts += [p.read_text(encoding="utf-8") for p in sorted((base / "js").rglob("*.js"))]
    return "\n".join(parts)


def test_frontend_models_tab_wiring():
    src = _frontend_src()
    assert 'data-tab="models" id="mi-models"' in src
    assert '<script src="/static/js/views/models.js"></script>' in src
    assert '"users","playground","bench","coding","calibrate","setup","models"]' in src
    assert 'models: "install"' in src                       # TAB_CAP
    assert '["#mi-models", "install"]' in src               # メニュー開示
    assert 'if (S.tab === "models") renderModels();' in src


def test_frontend_models_escapes_and_confirms():
    src = _frontend_src()
    # モデル名 / パスは esc を通す (エンジン / repo 由来の文字列)
    assert 'title="${esc(m.name)}">${esc(m.name)}</b>' in src
    assert 'title="${esc(m.path)}">${esc(shortPath(m.path))}' in src
    # 削除は確認ダイアログ + 参照プロファイルには force
    assert "const force = (m.used_by || []).length > 0;" in src
    assert 'body: {id, force}' in src
    assert "if (m.running) return toast(" in src


def test_download_form_catalog_greys_downloaded():
    """モデル管理のダウンロードフォーム「対応モデルから選択」も取得済みは選択不可。"""
    import llm_launcher

    js = (Path(llm_launcher.__file__).parent / "static" / "js" / "views" / "models.js").read_text("utf-8")
    assert '${s.downloaded ? " disabled" : ""}' in js
    assert "(取得済み — 再利用は棚卸しから)" in js
