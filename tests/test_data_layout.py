"""実行データ置き場の data/ 一本化 (datadir) の回帰テスト。

検証するもの:
  1. 旧置き場 (module 隣接の *-data / module/fork/freetoken の起動 JSON・sh) を
     data/ へ移し、プロファイル / config / .sh の参照パスを書き換える (冪等)。
  2. root 外の置き場 (settings の Strata-data / 既定 HF キャッシュ) は実際の
     launcher checkout (module/ + pyproject.toml) でだけ移す — tmp root のテスト
     から実機のモデルを動かさない門。
  3. プロファイル削除で起動 JSON と同名 .sh の一組を削除する
     (ユーザーの手書き JSON は触らない)。
  4. 新規の起動 JSON・sh は data/launch/ に作られる。
ネットワークは使わない。HF キャッシュ / settings は tmp へ向ける。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from llm_launcher import datadir
from llm_launcher import app as app_mod

from test_auth_users import Client


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """実機の HF キャッシュ / Strata settings を見ない・動かさない。"""
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.delenv("FREETOKEN_DATA_DIR", raising=False)
    monkeypatch.setenv("STRATA_SETTINGS_FILE", str(tmp_path / "strata-settings.json"))
    yield


def _real_root(tmp_path: Path) -> Path:
    """root 外移行の門 (real_root) を通すための最小 checkout 目印。"""
    root = tmp_path / "proj"
    (root / "module").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n", "utf-8")
    return root


def _weights(d: Path) -> Path:
    d.mkdir(parents=True)
    (d / "model.safetensors").write_bytes(b"x" * (2 << 20))
    return d


def _launch_pair(mod: Path, name: str, model: Path) -> tuple[Path, Path]:
    """module/fork/freetoken に置く旧形式の起動 JSON + 同名 .sh (生成目印付き)。"""
    j = mod / f"{name}.json"
    doc = {"python": str(mod / ".venv/bin/python"),
           "argv": ["-m", "freetoken", "--model-path", str(model), "--port", "1919"],
           "cwd": str(mod), "env": {}, "port": 1919, "model_name": name}
    j.write_text(json.dumps(doc, ensure_ascii=False, indent=1), "utf-8")
    sh = j.with_suffix(".sh")
    sh.write_text("#!/bin/sh\n"
                  f"{datadir.LAUNCH_SH_MARKER} — 起動の正は同名の起動 JSON です\n"
                  f"# config: {j}\n"
                  f'cd "{mod}"\n'
                  f'exec "{mod / ".venv/bin/python"} -m freetoken.serve_spec "{j}" "$@"\n',
                  "utf-8")
    return j, sh


def test_migrate_moves_dirs_pairs_and_rewrites_refs(tmp_path):
    root = _real_root(tmp_path)
    ft_old = _weights(root / "module/fork/Freetoken-data/models/org--M")
    maya_old = _weights(root / "module/Maya-data/models/Org/Quant")
    st_old = _weights(root / "module/Strata-data/models/org--S")
    fork_mod = root / "module/fork/freetoken"
    fork_mod.mkdir(parents=True)
    j, sh = _launch_pair(fork_mod, "M-1234abcd", ft_old)
    # installer config (旧パス参照)
    maya_mod = root / "module/maya"
    maya_mod.mkdir(parents=True)
    maya_cfg = maya_mod / "maya-x.json"
    maya_cfg.write_text(json.dumps({
        "exe": str(maya_mod / "build/maya"), "args": ["--pack", str(maya_old)],
        "installer": {"models_dir": str(maya_old.parent.parent)}}), "utf-8")
    st_mod = root / "module/strata"
    st_mod.mkdir(parents=True)
    st_cfg = st_mod / "strata-x.json"
    st_cfg.write_text(json.dumps({"exe": "/bin/true",
                                  "args": ["--native", str(st_old / "model.safetensors")]}), "utf-8")
    # Strata settings (root 内なので real_root 無しでも移す)
    settings = Path(tmp_path / "strata-settings.json")
    settings.write_text(json.dumps({"data_dir": str(root / "module/Strata-data")}), "utf-8")
    # パス入りの合成キーを持つ実行状態 (vision-memory.json 形) も書き換わる
    (root / "data/launch").mkdir(parents=True, exist_ok=True)
    vm = root / "data/launch/vision-memory.json"
    vm.write_text(json.dumps({f"{maya_old}/vision/mmproj.gguf|123|gpu0": {"bytes": 1}}), "utf-8")
    profiles = [
        {"id": "p1", "name": "FT", "backend": "freetoken-fork",
         "values": {"config": str(j), "model_path": str(ft_old)},
         "env": {"FREETOKEN_DATA_DIR": str(root / "module/fork/Freetoken-data")}},
        {"id": "p2", "name": "MY", "backend": "maya", "values": {"config": str(maya_cfg)}},
    ]
    stats = datadir.migrate_data_layout(root, profiles)
    assert stats["dirs"] >= 3 and stats["pairs"] == 1
    # ディレクトリ移動 (旧は消え、新に重みが在る)
    assert not (root / "module/fork/Freetoken-data").exists()
    assert not (root / "module/Maya-data").exists()
    assert not (root / "module/Strata-data").exists()
    ft_new = root / "data/Freetoken-data/models/org--M"
    assert (ft_new / "model.safetensors").is_file()
    assert (root / "data/Maya-data/models/Org/Quant/model.safetensors").is_file()
    assert (root / "data/Strata-data/models/org--S/model.safetensors").is_file()
    # 起動 JSON・sh は data/launch/ へ (同名維持)
    nj = root / "data/launch" / j.name
    nsh = nj.with_suffix(".sh")
    assert nj.is_file() and nsh.is_file() and not j.exists() and not sh.exists()
    doc = json.loads(nj.read_text("utf-8"))
    assert doc["argv"][3] == str(ft_new)
    assert str(nj) in nsh.read_text("utf-8") and str(j) not in nsh.read_text("utf-8")
    # プロファイル参照 (config / model_path / env) の書き換え
    assert profiles[0]["values"]["config"] == str(nj)
    assert profiles[0]["values"]["model_path"] == str(ft_new)
    assert profiles[0]["env"]["FREETOKEN_DATA_DIR"] == str(root / "data/Freetoken-data")
    # installer config の書き換え (args / installer.models_dir)
    mc = json.loads(maya_cfg.read_text("utf-8"))
    assert mc["args"][1] == str(root / "data/Maya-data/models/Org/Quant")
    assert mc["installer"]["models_dir"] == str(root / "data/Maya-data/models")
    sc = json.loads(st_cfg.read_text("utf-8"))
    assert sc["args"][1] == str(root / "data/Strata-data/models/org--S/model.safetensors")
    # Strata settings の data_dir も新置き場へ
    assert json.loads(settings.read_text("utf-8"))["data_dir"] == str(root / "data/Strata-data")
    # 合成キー (辞書のキー) の旧パスも書き換わる
    vm_doc = json.loads(vm.read_text("utf-8"))
    assert list(vm_doc) == [str(root / "data/Maya-data/models/Org/Quant/vision/mmproj.gguf|123|gpu0")]
    # 冪等: 2 回目は無変更
    stats2 = datadir.migrate_data_layout(root, profiles)
    assert not any(stats2.values())


def test_migrate_leaves_user_files_alone(tmp_path):
    root = _real_root(tmp_path)
    fork_mod = root / "module/fork/freetoken"
    fork_mod.mkdir(parents=True)
    # 目印 .sh が無い / プロファイル参照も無い JSON は upstream / ユーザーのファイル
    user = fork_mod / "run-manual.json"
    user.write_text('{"argv": ["-m", "freetoken"]}', "utf-8")
    (fork_mod / "run-manual.sh").write_text("#!/bin/sh\necho hi\n", "utf-8")
    stats = datadir.migrate_data_layout(root, [])
    assert stats["pairs"] == 0
    assert user.is_file() and (fork_mod / "run-manual.sh").is_file()


def test_migrate_external_dirs_gated_by_real_root(tmp_path, monkeypatch):
    """pyproject.toml の無い tmp root からは root 外の置き場を移さない。"""
    root = tmp_path / "proj"
    (root / "module").mkdir(parents=True)
    outside = _weights(tmp_path / "outside/Strata-data/models/org--S")
    settings = Path(tmp_path / "strata-settings.json")
    settings.write_text(json.dumps({"data_dir": str(outside.parent.parent)}), "utf-8")
    stats = datadir.migrate_data_layout(root, [])
    assert (outside / "model.safetensors").is_file()   # 実機のモデル置き場は無傷
    assert not (root / "data/Strata-data").exists()
    assert json.loads(settings.read_text("utf-8"))["data_dir"] == str(outside.parent.parent)


def test_migrate_hf_cache_to_data_hf(tmp_path, monkeypatch):
    root = _real_root(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    snap = home / ".cache/huggingface/hub/models--org--Mini/snapshots/abc"
    _weights(snap)
    profiles = [{"id": "p1", "name": "V", "backend": "vllm",
                 "values": {"model": str(snap)}}]
    stats = datadir.migrate_data_layout(root, profiles)
    assert stats["dirs"] >= 1
    new_snap = root / "data/hf/hub/models--org--Mini/snapshots/abc"
    assert (new_snap / "model.safetensors").is_file()
    assert not (home / ".cache/huggingface").exists()
    assert profiles[0]["values"]["model"] == str(new_snap)
    # 明示 HF_HUB_CACHE があれば既定キャッシュは触らない
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    snap2 = home / ".cache/huggingface/hub/models--org--Other/snapshots/r"
    _weights(snap2)
    datadir.migrate_data_layout(root, [])
    assert (snap2 / "model.safetensors").is_file()


def _seed_ft_profile(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "module/fork/freetoken").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data/profiles.json").write_text(json.dumps([
        {"id": "p_x", "name": "ft x", "backend": "freetoken-fork",
         "values": {"port": "1919", "model_path": "/m"}, "on": {}, "env": {}},
    ]), "utf-8")


def test_new_launch_pair_in_data_launch_and_delete_removes_both(tmp_path, monkeypatch):
    _seed_ft_profile(tmp_path)
    monkeypatch.setattr(app_mod.ProcessManager, "resolve_python", lambda self, bid: "/fake/py")
    app = app_mod.create_app(tmp_path)
    c = Client(app)

    async def go():
        st, r = await c.post("/api/profiles/p_x/launch-json")
        assert st == 200 and r["ok"], r
        j = Path(r["path"])
        sh = Path(r["sh"])
        # 新規生成は data/launch/ (gitignore 済み) に json + 同名 .sh の一組
        assert j.parent.resolve() == (tmp_path / "data/launch").resolve()
        assert sh == j.with_suffix(".sh") and j.is_file() and sh.is_file()
        assert datadir.sh_owned(sh)
        # 再生成は同じ名前を再利用 (uuid が増殖しない)
        st, r2 = await c.post("/api/profiles/p_x/launch-json")
        assert st == 200 and Path(r2["path"]) == j
        # プロファイル削除で json / sh 両方が消える
        st, _ = await c.delete("/api/profiles/p_x")
        assert st == 200
        assert not j.exists() and not sh.exists()
    asyncio.run(go())


def test_delete_profile_keeps_shared_pair_until_last_reference(tmp_path, monkeypatch):
    """2 プロファイルが同じ起動 JSON を共有: 片方の削除では消さず、
    最後の参照が消えたときに json / sh 両方を削除する。"""
    _seed_ft_profile(tmp_path)
    monkeypatch.setattr(app_mod.ProcessManager, "resolve_python", lambda self, bid: "/fake/py")
    app = app_mod.create_app(tmp_path)
    c = Client(app)

    async def go():
        st, r = await c.post("/api/profiles/p_x/launch-json")
        assert st == 200 and r["ok"], r
        j, sh = Path(r["path"]), Path(r["sh"])
        # もう 1 プロファイル作って同じ config を共有させる
        st, p2 = await c.post("/api/profiles",
                              {"name": "ft y", "backend": "freetoken-fork",
                               "values": {"config": str(j), "model_path": "/m"},
                               "on": {}, "env": {}})
        assert st == 201, p2
        st, _ = await c.delete("/api/profiles/p_x")
        assert st == 200
        assert j.is_file() and sh.is_file()     # p2 がまだ参照している
        st, _ = await c.delete(f"/api/profiles/{p2['id']}")
        assert st == 200
        assert not j.exists() and not sh.exists()
    asyncio.run(go())


def test_delete_profile_keeps_handwritten_json(tmp_path):
    _seed_ft_profile(tmp_path)
    (tmp_path / "data/launch").mkdir(parents=True, exist_ok=True)
    hand = tmp_path / "data/launch/my-handwritten.json"
    hand.write_text('{"argv": ["-m", "freetoken", "--model-path", "/m"]}', "utf-8")
    app = app_mod.create_app(tmp_path)
    c = Client(app)

    async def go():
        st, _ = await c.put("/api/profiles/p_x",
                            {"name": "ft x", "backend": "freetoken-fork",
                             "values": {"config": str(hand), "model_path": "/m"},
                             "on": {}, "env": {}})
        assert st == 200
        st, _ = await c.delete("/api/profiles/p_x")
        assert st == 200
        assert hand.is_file()                   # 手書き JSON (目印 .sh 無し) は触らない
    asyncio.run(go())
