"""UI モデルダウンロードの Freetoken-data 連携の回帰テスト。

セットアップタブのダウンロードは backend を指定すると data/ (gitignore 済み)
配下の <data>/models/<org>--<name> へ落ち、完了後に起動 JSON へ記入する。この 3 点
  1. fork_model_root: data/ 配下の既定 + FREETOKEN_DATA_DIR 上書き (setup.sh と同じ規則)
  2. record_launch_json_model: --model-path / model_name だけ更新し、他鍵や
     未導入モジュール (JSON 無し) を触らない
  3. scan_data_models: /api/hf-models がマージするデータフォルダ由来レコード
     (フォルダ名 -> repo id、重み検出、gguf 列挙)
が future proof される。ネットワークは使わない。
"""
from __future__ import annotations

import json
from pathlib import Path

from llm_launcher.app import scan_data_models
from llm_launcher.backends import BACKENDS, fork_model_root
from llm_launcher.installer import record_launch_json_model


def test_fork_model_root_default_and_env(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("FREETOKEN_DATA_DIR", raising=False)
    root = tmp_path / "root"
    spec = BACKENDS["freetoken-fork"]
    assert fork_model_root(root, spec) == (
        root / "data" / "Freetoken-data" / "models")
    monkeypatch.setenv("FREETOKEN_DATA_DIR", str(tmp_path / "data"))
    assert fork_model_root(root, spec) == tmp_path / "data" / "models"
    assert fork_model_root(root, BACKENDS["freetoken"]) is None


def test_record_launch_json_model_updates_only_model_fields(tmp_path: Path):
    mod = tmp_path / "ftmod"
    mod.mkdir()
    doc = {"python": "/v/bin/python",
           "argv": ["-m", "freetoken", "--model-path", "", "--port", "1919"],
           "cwd": str(mod), "env": {"CUDA_VISIBLE_DEVICES": "0"}, "port": 1919,
           "model_name": ""}
    path = mod / "freetoken-launch.json"
    path.write_text(json.dumps(doc), "utf-8")
    model = tmp_path / "Freetoken-data" / "models" / "org--Mini"
    assert record_launch_json_model(tmp_path, {"module_path": "ftmod"}, model)
    out = json.loads(path.read_text("utf-8"))
    assert out["argv"][3] == str(model)
    assert out["model_name"] == "Mini"
    assert out["env"] == {"CUDA_VISIBLE_DEVICES": "0"} and out["port"] == 1919


def test_record_launch_json_model_appends_missing_flag(tmp_path: Path):
    mod = tmp_path / "ftmod"
    mod.mkdir()
    path = mod / "freetoken-launch.json"
    path.write_text(json.dumps({"argv": ["-m", "freetoken"]}), "utf-8")
    model = tmp_path / "m" / "org--Mini"
    assert record_launch_json_model(tmp_path, {"module_path": "ftmod"}, model)
    assert json.loads(path.read_text("utf-8"))["argv"][-2:] == ["--model-path", str(model)]


def test_record_launch_json_model_skips_absent_or_broken(tmp_path: Path):
    assert record_launch_json_model(tmp_path, {"module_path": "nope"}, tmp_path / "m") is None
    mod = tmp_path / "ftmod"
    mod.mkdir()
    (mod / "freetoken-launch.json").write_text("{broken", "utf-8")
    note = record_launch_json_model(tmp_path, {"module_path": "ftmod"}, tmp_path / "m")
    assert note and "スキップ" in note
    assert (mod / "freetoken-launch.json").read_text("utf-8") == "{broken"


def test_scan_data_models_records(tmp_path: Path):
    assert scan_data_models(tmp_path / "missing") == []
    m = tmp_path / "org--Mini"
    m.mkdir()
    (m / "config.json").write_text("{}", "utf-8")
    (m / "model.safetensors").write_bytes(b"\0" * (2 << 20))
    (m / "f16.gguf").write_bytes(b"\0" * (3 << 20))
    empty = tmp_path / "org--Empty"
    empty.mkdir()
    out = {x["repo_id"]: x for x in scan_data_models(tmp_path)}
    mini = out["org/Mini"]
    assert mini["snapshot"] == str(m) and mini["source"] == "data"
    assert mini["has_weights"] and mini["gguf"] == [{"name": "f16.gguf", "size": 3 << 20, "parts": 1}]
    assert not out["org/Empty"]["has_weights"]


def test_scan_data_models_resolves_hf_cache_layout(tmp_path: Path):
    # hf download CLI 等がデータフォルダへ HF キャッシュ形式 (models--org--name) で
    # 置いたモデルは snapshots を scan_hf_cache と同じ規則で解決する
    # (重みは snapshots/<rev>/ の下。Inkling-2-Mini の実配置で踏んだ形)
    repo = tmp_path / "models--thinkingmachines--Inkling-2-Mini"
    snap = repo / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text('{"model_type": "inkling2_loop"}', "utf-8")
    (snap / "model.safetensors").write_bytes(b"\0" * (2 << 20))
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("abc123", "utf-8")
    # 通常の <org>--<name> 配置と混在しても両方出る
    plain = tmp_path / "org--Mini"
    plain.mkdir()
    (plain / "model.safetensors").write_bytes(b"\0" * (2 << 20))
    out = {x["repo_id"]: x for x in scan_data_models(tmp_path)}
    m = out["thinkingmachines/Inkling-2-Mini"]
    assert m["snapshot"] == str(snap) and m["source"] == "data"
    assert m["has_weights"] and m["revision"] == "main"
    assert out["org/Mini"]["snapshot"] == str(plain)


def test_scan_data_models_prepares_unsloth_gguf_layout(tmp_path: Path):
    # unsloth Mainline_Support: テキストは .gguf_file (サブフォルダ)、mmproj はルート。
    # エンジンは .gguf をテキストの隣で mmproj と共に読む -> symlink で準備して一覧化する
    repo = tmp_path / "models--unsloth--Inkling-2-Mini-GGUF"
    snap = repo / "snapshots" / "rev1"
    sub = snap / "Mainline_Support"
    sub.mkdir(parents=True)
    (snap / "mmproj-F16.gguf").write_bytes(b"\0" * (2 << 20))
    (sub / "Inkling-2-Mini-UD-Q4_K_XL.gguf_file").write_bytes(b"\0" * (3 << 20))
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("rev1", "utf-8")
    out = {x["repo_id"]: x for x in scan_data_models(tmp_path)}
    m = out["unsloth/Inkling-2-Mini-GGUF"]
    assert m["has_weights"]
    # mmproj はビジョン射影 (テキストモデルではない) なので一覧に出さない
    assert [g["name"] for g in m["gguf"]] == ["Mainline_Support/Inkling-2-Mini-UD-Q4_K_XL.gguf"]
    alias = sub / "Inkling-2-Mini-UD-Q4_K_XL.gguf"
    assert alias.is_symlink() and alias.is_file()
    assert (sub / "mmproj-F16.gguf").is_symlink()   # テキストの隣へ (エンジンは隣を探す)
    # 冪等: 再スキャンで symlink を壊さない
    scan_data_models(tmp_path)
    assert alias.is_symlink() and (sub / "mmproj-F16.gguf").is_symlink()


def test_classify_gguf_files_publisher_layouts():
    # 配信者ごとにレイアウトが違う: ルート直下 (bartowski 系) / 分割 / サブフォルダ +
    # .gguf_file (unsloth Mainline_Support) / mmproj / MTP sidecar をまとめて分類
    from llm_launcher.models import classify_gguf_files
    files = [
        {"path": "Model-Q4_K_M.gguf", "size": 4000},
        {"path": "Model-Q8_0-00001-of-00002.gguf", "size": 5000},
        {"path": "Model-Q8_0-00002-of-00002.gguf", "size": 5000},
        {"path": "mmproj-Model-f16.gguf", "size": 900},
        {"path": "Mainline_Support/Model-UD-Q4_K_XL.gguf_file", "size": 7000},
        {"path": "MTP/mtp-Model.gguf", "size": 300},
        {"path": "README.md", "size": 10},
    ]
    c = classify_gguf_files(files)
    assert [g["name"] for g in c["groups"]] == [
        "Mainline_Support/Model-UD-Q4_K_XL.gguf",   # 表示は .gguf (DL 後 symlink 準備)
        "Model-Q4_K_M.gguf",
        "Model-Q8_0-00001-of-00002.gguf",           # 分割は先頭パーツ名で 1 量子化
    ]
    q8 = next(g for g in c["groups"] if g["parts"] == 2)
    assert q8["size"] == 10000 and len(q8["paths"]) == 2
    # DL の allow_patterns は repo 内の実パス (.gguf_file のまま)
    assert c["groups"][0]["paths"] == ["Mainline_Support/Model-UD-Q4_K_XL.gguf_file"]
    assert [f["path"] for f in c["mmproj"]] == ["mmproj-Model-f16.gguf"]
    assert all("mtp-" not in g["name"] for g in c["groups"])   # drafter sidecar は出さない
    # mmproj はエンジンの型優先 (BF16 > F32 > F16) で先頭 1 つを DL すればよい
    c2 = classify_gguf_files([
        {"path": "mmproj-F16.gguf", "size": 1}, {"path": "mmproj-BF16.gguf", "size": 1},
        {"path": "mmproj-F32.gguf", "size": 1}])
    assert [f["path"] for f in c2["mmproj"]] == [
        "mmproj-BF16.gguf", "mmproj-F32.gguf", "mmproj-F16.gguf"]


def test_scan_groups_split_gguf(tmp_path: Path):
    from llm_launcher.models import group_gguf_splits
    g = group_gguf_splits([
        {"name": "M-Q8-00002-of-00002.gguf", "size": 5},
        {"name": "M-Q8-00001-of-00002.gguf", "size": 5},
        {"name": "M-Q4.gguf", "size": 4},
    ])
    assert g == [{"name": "M-Q4.gguf", "size": 4, "parts": 1},
                 {"name": "M-Q8-00001-of-00002.gguf", "size": 10, "parts": 2}]


def test_api_hf_repo_files_classifies(tmp_path, monkeypatch):
    import asyncio

    import huggingface_hub
    from test_auth_users import Client
    from llm_launcher import app as app_mod

    class _Sib:
        def __init__(self, p, s):
            self.rfilename, self.size = p, s

    class _Api:
        def model_info(self, repo, files_metadata=False):
            return type("Info", (), {"siblings": [
                _Sib("Model-Q4_K_M.gguf", 4000), _Sib("mmproj-f16.gguf", 900)]})()
    monkeypatch.setattr(huggingface_hub, "HfApi", _Api)
    client = Client(app_mod.create_app(tmp_path))

    async def go():
        st, res = await client.get("/api/hf-repo-files?repo_id=org/repo")
        assert st == 200 and res["ok"]
        assert res["groups"][0]["name"] == "Model-Q4_K_M.gguf"
        assert res["mmproj"][0]["path"] == "mmproj-f16.gguf"
        st, _ = await client.get("/api/hf-repo-files?repo_id=not-a-repo")
        assert st == 400
    asyncio.run(go())


def test_fork_spec_declares_model_data():
    assert BACKENDS["freetoken-fork"].get("model_data") == "Freetoken-data"
    assert not BACKENDS["freetoken"].get("model_data")
