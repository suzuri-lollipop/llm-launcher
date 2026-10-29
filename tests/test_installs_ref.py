"""セットアップタブのブランチ選択 (ref) の回帰テスト。

FreeToken (fork) は suzuri/develop (開発版) / suzuri/product (安定版) を選べる。ここで見張るのは
  1. resolve_ref — 既定は安定版、spec の一覧 (refs) に無い名前は git argv に渡さない
  2. start_install の計画 — 未 clone は選んだブランチを clone、clone 済みは branch ステップ
  3. _switch_branch — 実 git (ローカルの裸リポジトリ = ネットワーク不要) で fetch + checkout。
     未コミットの変更では拒否 / ローカル独自コミットは壊さない / 未追跡ファイルは邪魔しない
  4. API — installable が refs / current_ref / cloned を出し、未知のブランチは 400
git は使うがネットワークは使わない。
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from test_auth_users import Client

from llm_launcher import app as app_mod
from llm_launcher.installer import (INSTALL_SPECS, InstallError, InstallManager,
                                    allowed_refs, clone_exists, current_ref, resolve_ref)
from llm_launcher.store import JsonStore

FORK = INSTALL_SPECS["freetoken-fork"]
UPSTREAM = INSTALL_SPECS["freetoken"]
GIT = ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]


def _git(*args: str, cwd: Path | None = None) -> str:
    r = subprocess.run(GIT + list(args), cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


def _origin(tmp_path: Path) -> Path:
    """suzuri/develop と suzuri/product を持つローカル origin (裸リポジトリ)。"""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", str(origin))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-b", "main", str(seed))
    (seed / "app.py").write_text("develop build\n", "utf-8")
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "develop", cwd=seed)
    _git("push", "-q", str(origin), "HEAD:refs/heads/suzuri/develop", cwd=seed)
    (seed / "app.py").write_text("product build\n", "utf-8")
    _git("commit", "-am", "product", cwd=seed)
    _git("push", "-q", str(origin), "HEAD:refs/heads/suzuri/product", cwd=seed)
    return origin


def _clone(tmp_path: Path, origin: Path, branch: str = "suzuri/develop") -> tuple[Path, Path]:
    """root/module/fork/freetoken に branch を clone して (root, module_dir) を返す。"""
    root = tmp_path / "root"
    mod = root / "module" / "fork" / "freetoken"
    mod.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "-q", "--branch", branch, str(origin), str(mod))
    return root, mod


def _manager(root: Path) -> InstallManager:
    (root / "data").mkdir(parents=True, exist_ok=True)
    return InstallManager(root, JsonStore(root / "data" / "installs.json", []))


def _plan(monkeypatch, root: Path, ref: str) -> dict:
    """start_install の計画だけ取る (_run_install を殺して subprocess も git も走らせない)。"""
    got: dict[str, object] = {}

    async def fake(self, job, spec, steps):  # noqa: ANN001
        got["job"] = dict(job)
        got["steps"] = [dict(s) for s in steps]

    monkeypatch.setattr(InstallManager, "_run_install", fake)
    mgr = _manager(root)

    async def go() -> None:
        await mgr.start_install("freetoken-fork", ref=ref)
        await asyncio.sleep(0)      # fake task を走らせて終了させる

    asyncio.run(go())
    return got


# --------------------------------------------------------- ブランチ指定の検証

def test_fork_offers_both_branches_with_stable_default():
    assert allowed_refs(FORK) == ["suzuri/develop", "suzuri/product"]
    assert FORK["ref"] == "suzuri/product"                      # 既定 = 安定版
    assert resolve_ref(FORK, "") == "suzuri/product"
    assert resolve_ref(FORK, "suzuri/develop") == "suzuri/develop"
    assert resolve_ref(FORK, "  suzuri/product  ") == "suzuri/product"


def test_unknown_or_unsafe_branch_is_rejected():
    for bad in ("main", "origin/suzuri/develop", "-b", "../x", "suzuri/develop..product", "a//b"):
        with pytest.raises(InstallError):
            resolve_ref(FORK, bad)


def test_upstream_has_no_branch_choice():
    assert allowed_refs(UPSTREAM) == []
    assert resolve_ref(UPSTREAM, "") == ""                      # 上流の既定ブランチに任せる
    with pytest.raises(InstallError):
        resolve_ref(UPSTREAM, "main")


# --------------------------------------------------------- インストール計画

def test_fresh_clone_checks_out_the_chosen_branch(monkeypatch, tmp_path: Path):
    got = _plan(monkeypatch, tmp_path / "root", "suzuri/develop")
    job, steps = got["job"], got["steps"]
    assert job["ref"] == "suzuri/develop" and job["needs_clone"] is True
    assert [s["name"] for s in steps][:2] == ["clone", "venv"]
    assert "branch" not in [s["name"] for s in steps]
    argv = steps[0]["argv"]
    assert argv[:3] == ["git", "clone", "--depth"] and "--branch" in argv
    assert argv[argv.index("--branch") + 1] == "suzuri/develop"
    assert argv[-2] == FORK["clone"]                            # shell ではなく argv 直渡し


def test_clone_without_refs_keeps_default_branch(monkeypatch, tmp_path: Path):
    steps = _plan(monkeypatch, tmp_path / "root", "")["steps"]
    argv = steps[0]["argv"]
    assert argv[argv.index("--branch") + 1] == "suzuri/product"  # 未指定 = 安定版


def test_existing_clone_switches_branch_before_venv(monkeypatch, tmp_path: Path):
    root = tmp_path / "root"
    mod = root / "module" / "fork" / "freetoken"
    mod.mkdir(parents=True)
    (mod / "app.py").write_text("x\n", "utf-8")                 # clone 済み扱い
    got = _plan(monkeypatch, root, "suzuri/product")
    names = [s["name"] for s in got["steps"]]
    assert names[0] == "branch" and "clone" not in names        # venv / pip より先に切替
    assert names[1:] == ["venv", "precheck", "install", "verify"]
    assert got["job"]["ref"] == "suzuri/product" and got["job"]["needs_clone"] is False


def test_unknown_branch_starts_nothing(monkeypatch, tmp_path: Path):
    root = tmp_path / "root"
    mgr = _manager(root)

    async def go():
        with pytest.raises(InstallError):
            await mgr.start_install("freetoken-fork", ref="suzuri/nightly")
        assert mgr.jobs == {} and mgr.active_id is None

    asyncio.run(go())


# --------------------------------------------------------- 既存 clone の切替 (実 git)

def _switch(mgr: InstallManager, mod: Path, ref: str):
    job = mgr._new_job("install", None, backend="freetoken-fork", backend_name="FreeToken (fork)",
                       module_path="module/fork/freetoken", tool="pip", extras="accel",
                       clean=False, needs_clone=False, ref=ref)
    return job, asyncio.run(mgr._switch_branch(job, mod, ref))


def test_current_ref_and_clone_exists(tmp_path: Path):
    root = tmp_path / "nothing"
    assert clone_exists(root, FORK) is False and current_ref(root, FORK) == ""
    origin = _origin(tmp_path)
    root, mod = _clone(tmp_path, origin)
    assert clone_exists(root, FORK) is True
    assert current_ref(root, FORK) == "suzuri/develop"          # UI の既定選択はここを向く
    assert current_ref(root, UPSTREAM) == ""                    # refs の無いバックエンドは対象外


def test_switch_branch_checks_out_target(tmp_path: Path):
    origin = _origin(tmp_path)
    root, mod = _clone(tmp_path, origin)
    mgr = _manager(root)
    job, switched = _switch(mgr, mod, "suzuri/product")
    assert switched is True
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=mod) == "suzuri/product"
    assert (mod / "app.py").read_text("utf-8") == "product build\n"
    assert _git("status", "--porcelain", cwd=mod) == ""          # 作業ツリーはクリーンなまま
    log = "\n".join(job["logs"])
    assert "suzuri/develop → suzuri/product" in log and "切り替え" in log


def test_switch_branch_same_branch_is_noop(tmp_path: Path):
    origin = _origin(tmp_path)
    root, mod = _clone(tmp_path, origin, "suzuri/product")
    mgr = _manager(root)
    job, switched = _switch(mgr, mod, "suzuri/product")
    assert switched is False
    assert "選択と同じ" in "\n".join(job["logs"])
    assert "fetch" not in "\n".join(job["logs"])                 # git を叩いていない


def test_switch_branch_refuses_dirty_tree(tmp_path: Path):
    origin = _origin(tmp_path)
    root, mod = _clone(tmp_path, origin)
    (mod / "app.py").write_text("私の未コミット作業\n", "utf-8")
    mgr = _manager(root)
    with pytest.raises(InstallError) as e:
        _switch(mgr, mod, "suzuri/product")
    assert "未コミット" in str(e.value)
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=mod) == "suzuri/develop"
    assert (mod / "app.py").read_text("utf-8") == "私の未コミット作業\n"   # 変更は温存


def test_switch_branch_ignores_untracked_files(tmp_path: Path):
    """freetoken-launch.json と .venv は未追跡なので切替を止めない (実環境そのもの)。"""
    origin = _origin(tmp_path)
    root, mod = _clone(tmp_path, origin)
    (mod / "freetoken-launch.json").write_text('{"argv": []}\n', "utf-8")
    (mod / ".venv" / "bin").mkdir(parents=True)
    (mod / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", "utf-8")
    mgr = _manager(root)
    _, switched = _switch(mgr, mod, "suzuri/product")
    assert switched is True
    assert (mod / "freetoken-launch.json").exists() and (mod / ".venv").exists()


def test_switch_branch_keeps_local_commits_on_same_name(tmp_path: Path):
    """ローカルに同名ブランチ + 独自コミットがあるとき: checkout はするが ff しない (壊さない)。"""
    origin = _origin(tmp_path)
    root, mod = _clone(tmp_path, origin)
    _git("checkout", "-q", "-b", "suzuri/product", cwd=mod)     # develop 起点の同名ローカル作
    (mod / "mine.py").write_text("ローカル独自コミット\n", "utf-8")
    _git("add", "-A", cwd=mod)
    _git("commit", "-m", "local only", cwd=mod)
    head = _git("rev-parse", "HEAD", cwd=mod)
    _git("checkout", "-q", "suzuri/develop", cwd=mod)           # 切替前 = develop
    mgr = _manager(root)
    job, switched = _switch(mgr, mod, "suzuri/product")
    assert switched is True and _git("rev-parse", "HEAD", cwd=mod) == head
    assert "独自コミット" in "\n".join(job["logs"])              # ff できなかったことを正直に残す
    assert (mod / "mine.py").exists()


def test_switch_branch_creates_tracking_branch(tmp_path: Path):
    origin = _origin(tmp_path)
    root, mod = _clone(tmp_path, origin)                        # 同名のローカルブランチはまだ無い
    assert _git("show-ref", "refs/remotes/origin/suzuri/product", cwd=mod)
    mgr = _manager(root)
    job, switched = _switch(mgr, mod, "suzuri/product")
    assert switched is True
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=mod) == "suzuri/product"
    assert _git("rev-parse", "suzuri/product", cwd=mod) == _git("rev-parse", "origin/suzuri/product",
                                                               cwd=mod)
    assert "tracking" in "\n".join(job["logs"]) or "checkout -b" in "\n".join(job["logs"])


def test_switch_branch_on_plain_directory_fails_cleanly(tmp_path: Path):
    root = tmp_path / "root"
    mod = root / "module" / "fork" / "freetoken"
    mod.mkdir(parents=True)
    (mod / "app.py").write_text("git でない置き場\n", "utf-8")
    mgr = _manager(root)
    with pytest.raises(InstallError) as e:
        _switch(mgr, mod, "suzuri/product")
    assert "git" in str(e.value)
    assert (mod / "app.py").read_text("utf-8") == "git でない置き場\n"


# --------------------------------------------------------- API

def test_api_exposes_branches_and_validates_ref(monkeypatch, tmp_path: Path):
    got: dict[str, object] = {}

    async def fake(self, job, spec, steps):  # noqa: ANN001
        got["steps"] = [s["name"] for s in steps]

    monkeypatch.setattr(InstallManager, "_run_install", fake)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    client = Client(app_mod.create_app(tmp_path))

    async def go() -> None:
        st, res = await client.get("/api/installs")
        assert st == 200
        fork = next(e for e in res["installable"] if e["id"] == "freetoken-fork")
        assert [r["ref"] for r in fork["refs"]] == ["suzuri/develop", "suzuri/product"]
        assert [r["label"] for r in fork["refs"]] == ["開発版", "安定版"]
        assert fork["ref"] == "suzuri/product"                   # UI の既定 = 安定版
        assert fork["current_ref"] == "" and fork["cloned"] is False
        upstream = next(e for e in res["installable"] if e["id"] == "freetoken")
        assert upstream["refs"] == []                            # 上流はブランチ選択を出さない

        st, res = await client.post("/api/installs", {"backend": "freetoken-fork", "ref": "main"})
        assert st == 400 and "ブランチ" in res["detail"]          # 一覧外は git に渡さない

        st, job = await client.post("/api/installs", {"backend": "freetoken-fork",
                                                      "ref": "suzuri/develop"})
        assert st == 201 and job["ref"] == "suzuri/develop"
        assert job["steps"][0] == "clone" and got["steps"][0] == "clone"
        st, res = await client.get(f"/api/installs/{job['id']}")
        assert st == 200 and res["ref"] == "suzuri/develop"
        await asyncio.sleep(0)

    asyncio.run(go())
