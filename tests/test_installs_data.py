"""UI モデルダウンロードの Freetoken-data 連携の回帰テスト。

セットアップタブのダウンロードは backend を指定すると setup.sh と同じ
<data>/models/<org>--<name> へ落ち、完了後に起動 JSON へ記入する。この 3 点
  1. fork_model_root: clone 隣接の既定 + FREETOKEN_DATA_DIR 上書き (setup.sh と同じ規則)
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
        root / "module" / "fork" / "Freetoken-data" / "models")
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
    assert mini["has_weights"] and mini["gguf"] == [{"name": "f16.gguf", "size": 3 << 20}]
    assert not out["org/Empty"]["has_weights"]


def test_fork_spec_declares_model_data():
    assert BACKENDS["freetoken-fork"].get("model_data") == "Freetoken-data"
    assert not BACKENDS["freetoken"].get("model_data")
