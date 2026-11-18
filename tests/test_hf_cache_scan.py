"""HF キャッシュスキャンの取りこぼし修正の回帰テスト。

背景 (2026-11-18 報告): 稼働中プロファイルが .cache (HF キャッシュ) から
モデルをロードして動いているのに、プロファイル作成 / セットアップの
「ダウンロード済みモデル」一覧に出てこない。実証された取りこぼし:

  B. refs/main が空 / config だけの snapshot を指し、別 rev に重みがある
     (プロファイルは旧 rev で稼働中) → 旧スキャナは refs/main だけ見ていた
  C. 重みが 1 階層下のサブフォルダ (GGUF / シャードディレクトリ)
  D. refs 無しで mtime 最新の snapshot が空
  E. local_dir 配置等 models--* / snapshots 規約外の置き場
     → プロファイル参照からの保険エントリで一覧に出す

hf 2.x の共有 blob (snapshot が hub/blobs への symlink) も判定できること。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from test_auth_users import Client

from llm_launcher import app as app_mod
from llm_launcher import models

W = 2 << 20          # _WEIGHT_MIN (1 MiB) 超の重みファイルサイズ


def _repo(hub: Path, name: str, snaps: dict[str, dict[str, int]],
          refs: dict[str, str] | None = None) -> Path:
    d = hub / name
    for rev, files in snaps.items():
        s = d / "snapshots" / rev
        s.mkdir(parents=True)
        for fn, sz in files.items():
            f = s / fn
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x" * sz)
    if refs:
        (d / "refs").mkdir(exist_ok=True)
        for k, v in refs.items():
            (d / "refs" / k).write_text(v)
    return d


def _by_repo(res: dict) -> dict[str, dict]:
    return {m["repo_id"]: m for m in res["models"]}


def test_refs_main_empty_but_other_snapshot_has_weights(tmp_path):
    """B: プロファイルが旧 rev で稼働中 — 重みのある snapshot を選ぶ。"""
    hub = tmp_path / "hub"
    _repo(hub, "models--Org--M",
          {"new": {"config.json": 100},
           "old": {"config.json": 100, "model.safetensors": W}},
          {"main": "new"})
    m = _by_repo(models.scan_hf_cache(hub))["Org/M"]
    assert m["has_weights"] is True
    assert m["snapshot"].endswith("/old")


def test_preferred_snapshot_wins_when_it_has_weights(tmp_path):
    """refs/main が重みありなら、より新しい別 snapshot より優先する。"""
    hub = tmp_path / "hub"
    d = _repo(hub, "models--Org--M",
              {"aaa": {"config.json": 100, "model.safetensors": W},
               "bbb": {"config.json": 100, "model.safetensors": W}},
              {"main": "aaa"})
    os.utime(d / "snapshots" / "bbb", (time.time() + 10, time.time() + 10))
    m = _by_repo(models.scan_hf_cache(hub))["Org/M"]
    assert m["snapshot"].endswith("/aaa") and m["revision"] == "main"


def test_nested_weights_one_level(tmp_path):
    """C: 重みがサブフォルダ — 検出し、名前は snapshot からの相対パス。"""
    hub = tmp_path / "hub"
    _repo(hub, "models--Org--G",
          {"rev": {"config.json": 100, "gguf/model.gguf": W}}, {"main": "rev"})
    m = _by_repo(models.scan_hf_cache(hub))["Org/G"]
    assert m["has_weights"] is True
    assert m["gguf"] == [{"name": "gguf/model.gguf", "size": W}]
    # ウィザードの pack パス結合 (snapshot + "/" + name) が実ファイルを指す
    assert (Path(m["snapshot"]) / m["gguf"][0]["name"]).is_file()


def test_no_refs_latest_mtime_empty(tmp_path):
    """D: refs 無し / mtime 最新が空でも重みのある snapshot を選ぶ。"""
    hub = tmp_path / "hub"
    d = _repo(hub, "models--Org--M",
              {"good": {"config.json": 100, "model.safetensors": W},
               "empty": {"config.json": 100}})
    os.utime(d / "snapshots" / "empty", (time.time() + 10, time.time() + 10))
    m = _by_repo(models.scan_hf_cache(hub))["Org/M"]
    assert m["has_weights"] is True and m["snapshot"].endswith("/good")


def test_shared_blob_symlinks_are_followed(tmp_path):
    """hf 2.x 共有 blob: snapshot の symlink 先 (hub/blobs) を辿って判定。"""
    hub = tmp_path / "hub"
    d = _repo(hub, "models--Org--M", {"rev": {"config.json": 100}}, {"main": "rev"})
    blob = hub / "blobs" / "ab" / ("ab" * 32)
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"x" * W)
    (d / "snapshots" / "rev" / "model.safetensors").symlink_to(blob)
    m = _by_repo(models.scan_hf_cache(hub))["Org/M"]
    assert m["has_weights"] is True


def test_config_only_repo_still_listed(tmp_path):
    """重みがどこにも無い repo も一覧には残す (has_weights=False)。"""
    hub = tmp_path / "hub"
    _repo(hub, "models--Org--C", {"rev": {"config.json": 100}}, {"main": "rev"})
    m = _by_repo(models.scan_hf_cache(hub))["Org/C"]
    assert m["has_weights"] is False


def test_basalt_pack_not_generic_weights(tmp_path):
    """.basalt だけは has_weights にしない (FreeToken 等への漏れ防止)。"""
    hub = tmp_path / "hub"
    _repo(hub, "models--Org--B", {"rev": {"config.json": 100, "pack.basalt": W}},
          {"main": "rev"})
    m = _by_repo(models.scan_hf_cache(hub))["Org/B"]
    assert m["has_weights"] is False and len(m["basalt"]) == 1


# ---------------------------------------------------------------- プロファイル参照の保険

def _local_model(hub: Path) -> Path:
    ld = hub / "Org" / "LocalModel"
    ld.mkdir(parents=True)
    (ld / "config.json").write_text("{}", "utf-8")
    (ld / "model.safetensors").write_bytes(b"x" * W)
    return ld


def test_profile_cache_misses_local_dir(tmp_path):
    """E: models-- 規約外の local_dir 配置でもプロファイル参照から一覧化。"""
    hub = tmp_path / "hub"
    ld = _local_model(hub)
    known = models.scan_hf_cache(hub)["models"]
    prof = {"backend": "freetoken-fork", "values": {"model_path": str(ld)}}
    got = models.profile_cache_misses(tmp_path, [prof], hub, known)
    assert [(m["repo_id"], m["has_weights"]) for m in got] == [("Org/LocalModel", True)]


def test_profile_cache_misses_via_launch_json(tmp_path):
    """起動 JSON の --model-path 参照も拾う (JSON 主体モード)。"""
    hub = tmp_path / "hub"
    ld = _local_model(hub)
    lj = tmp_path / "launch.json"
    lj.write_text(json.dumps({"argv": ["-m", "freetoken", "--model-path", str(ld)]}), "utf-8")
    prof = {"backend": "freetoken-fork", "values": {"config": str(lj)}}
    got = models.profile_cache_misses(tmp_path, [prof], hub, [])
    assert [m["repo_id"] for m in got] == ["Org/LocalModel"]


def test_profile_cache_misses_dedup_and_safety(tmp_path):
    """既知 repo / キャッシュ外 / 重み無しディレクトリは出さない。"""
    hub = tmp_path / "hub"
    ld = _local_model(hub)
    empty = hub / "Org" / "Empty"
    empty.mkdir(parents=True)
    (empty / "config.json").write_text("{}", "utf-8")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "model.safetensors").write_bytes(b"x" * W)
    known = [{"repo_id": "Org/LocalModel", "snapshot": str(ld)}]
    profs = [{"backend": "freetoken-fork",
              "values": {"model_path": str(ld)}},
             {"backend": "freetoken-fork", "values": {"model_path": str(empty)}},
             {"backend": "freetoken-fork", "values": {"model_path": str(outside)}}]
    assert models.profile_cache_misses(tmp_path, profs, hub, known) == []


def test_api_hf_models_includes_profile_referenced_cache_model(tmp_path, monkeypatch):
    """/api/hf-models (ウィザード / エディタの源) が保険エントリを返す。"""
    hub = tmp_path / "hub"
    ld = _local_model(hub)
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.delenv("FREETOKEN_DATA_DIR", raising=False)
    (tmp_path / "data").mkdir()
    app_mod.ProcessManager.resolve_python = lambda self, bid: "/fake/py"
    c = Client(app_mod.create_app(tmp_path))

    async def go():
        st, p = await c.post("/api/profiles", {
            "name": "ft", "backend": "freetoken-fork",
            "values": {"model_path": str(ld), "port": "1919"}})
        assert st == 201, p
        st, r = await c.get("/api/hf-models")
        assert st == 200
        hit = [m for m in r["models"] if m["repo_id"] == "Org/LocalModel"]
        assert hit and hit[0]["has_weights"] and hit[0]["snapshot"] == str(ld)
    asyncio.run(go())
