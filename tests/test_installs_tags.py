"""fork 専用タグ (suzuri-*) のセットアップ選択の回帰テスト。

FreeToken fork には公式タグ (v0.2.2 等) とは別に suzuri- で始まるタグがある。
セットアップ時にリモートから取得して選択できるようにし、表示 (label) は公式タグとの
識別のためだけの prefix suzuri- を外す (suzuri-v1.0.1 → v1.0.1)。git へ渡す ref は完全名。

見張る対象:
  1. fetch_tags — ls-remote の解析 (peeled ^{} 除外 / prefix 以外除外 / 自然順で新しい順)
  2. fork_tags — TTL キャッシュ / force 取り直し / 取得失敗は前回の一覧を保持
  3. allowed_refs / resolve_ref — タグはホワイトリストに入る。一覧外 / 公式タグは弾く
  4. start_install の計画 — タグ指定の clone / 既存 clone の branch ステップ / 直前 push の取り直し
  5. _switch_branch — 実 git (ローカル裸リポジトリ = ネットワーク不要) でタグへ fetch + checkout
     (detached HEAD / shallow clone / 未コミット拒否 / 同タグ noop / タグ → ブランチ)
  6. current_ref — detached HEAD をタグ名で解決 (UI の既定選択)
  7. API — installable が kind=tag + prefix なし label を出し、未知のタグは 400。refresh=1 で取り直し
  8. UI — ウィザードはタグを prefix なしで表示する (setup.js / setup.html の静的検査)
git は使うがネットワークは使わない (conftest が fetch_tags をスタブ化。ここでは上書きする)。
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from test_auth_users import Client
from test_installs_ref import FORK, _clone, _git, _manager, _origin, _plan, _switch

from llm_launcher import app as app_mod
from llm_launcher import installer
from llm_launcher.installer import (
    InstallError,
    InstallManager,
    allowed_refs,
    cached_tags,
    current_ref,
    fetch_tags,
    fork_tags,
    is_tag_ref,
    resolve_ref,
)

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "llm_launcher" / "static"
JS = (STATIC / "setup.js").read_text("utf-8")
HTML = (STATIC / "setup.html").read_text("utf-8")
CSS = (STATIC / "setup.css").read_text("utf-8")

TAGS = ["suzuri-v1.0.1", "suzuri-v1.0.0"]   # 新しい順 (fetch_tags の返り値と同じ形)


def _fake_fetch(monkeypatch, tags: list[str], calls: list | None = None):
    """fetch_tags をネットワーク無しのスタブに差し替える (conftest の既定を上書き)。"""
    def fake(url: str, prefix: str, **kw) -> list[str]:
        if calls is not None:
            calls.append((url, prefix, kw.get("force", False)))
        return [t for t in tags if t.startswith(prefix)]
    monkeypatch.setattr(installer, "fetch_tags", fake)


def _ls_remote_output() -> str:
    return ("aaa111\trefs/tags/nightly\n"
            "bbb222\trefs/tags/v0.2.2\n"
            "ccc333\trefs/tags/suzuri-v1.0.0\n"
            "ddd444\trefs/tags/suzuri-v1.0.1\n"
            "eee555\trefs/tags/suzuri-v1.0.1^{}\n"     # annotated tag の peeled 行
            "fff666\trefs/tags/suzuri-v1.0.10\n"
            "ggg777\trefs/tags/suzuri-../evil\n"       # 形式検査で落ちる名前
            "hhh888\trefs/heads/suzuri/product\n")


def _origin_tags(tmp_path: Path) -> Path:
    """ブランチ 2 本 + suzuri-* タグ 2 個 + 公式タグを持つローカル origin (裸)。

    公式タグは fork のブランチに無い独自コミットに付ける (上流と同じ形)。
    こうすると clone が公式タグを拾わず、describe の解決も fork タグだけで決まる。
    """
    origin = _origin(tmp_path)                 # seed の HEAD = product commit
    seed = tmp_path / "seed"
    dev = _git("rev-parse", "HEAD~1", cwd=seed)
    _git("tag", "suzuri-v1.0.0", dev, cwd=seed)        # develop のコミットにタグ
    _git("tag", "suzuri-v1.0.1", cwd=seed)             # product のコミットにタグ
    (seed / "official.txt").write_text("official release\n", "utf-8")
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "official", cwd=seed)
    _git("tag", "v0.2.2", cwd=seed)                    # 公式タグ (選択には出さない)
    _git("push", "-q", str(origin), "--tags", cwd=seed)
    return origin


def _clone_shallow(tmp_path: Path, origin: Path) -> tuple[Path, Path]:
    root = tmp_path / "root"
    mod = root / "module" / "fork" / "freetoken"
    mod.parent.mkdir(parents=True, exist_ok=True)
    # ローカルパスの clone は --depth が無視されるので file:// で浅い clone を強制する
    _git("clone", "-q", "--depth", "1", "--branch", "suzuri/develop",
         f"file://{origin}", str(mod))
    return root, mod


# --------------------------------------------------------- ls-remote の解析

def test_fetch_tags_filters_prefix_and_sorts_naturally(monkeypatch):
    seen: dict[str, object] = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout=_ls_remote_output(), stderr="")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    got = fetch_tags(FORK["clone"], "suzuri-")
    assert got == ["suzuri-v1.0.10", "suzuri-v1.0.1", "suzuri-v1.0.0"]
    assert seen["argv"][:3] == ["git", "ls-remote", "--tags"]
    assert seen["argv"][-1] == FORK["clone"]


def test_fetch_tags_failure_is_empty(monkeypatch):
    def fail(argv, **kw):
        return subprocess.CompletedProcess(argv, 128, stdout="", stderr="network down")
    monkeypatch.setattr(installer.subprocess, "run", fail)
    assert fetch_tags(FORK["clone"], "suzuri-") == []

    def raise_oserror(argv, **kw):
        raise OSError("no git")
    monkeypatch.setattr(installer.subprocess, "run", raise_oserror)
    assert fetch_tags(FORK["clone"], "suzuri-") == []

    def raise_timeout(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 15)
    monkeypatch.setattr(installer.subprocess, "run", raise_timeout)
    assert fetch_tags(FORK["clone"], "suzuri-") == []


# --------------------------------------------------------- TTL キャッシュ

def test_fork_tags_cache_ttl_force_and_failure(monkeypatch):
    calls: list = []
    _fake_fetch(monkeypatch, TAGS, calls)
    assert fork_tags(FORK) == TAGS and len(calls) == 1
    assert fork_tags(FORK) == TAGS and len(calls) == 1      # TTL 内は取り直さない
    assert fork_tags(FORK, force=True) == TAGS and len(calls) == 2
    # 取得失敗 ([]): 前回の一覧を保持する (一覧が消えない)
    _fake_fetch(monkeypatch, [], calls)
    assert fork_tags(FORK, force=True) == TAGS
    # TTL 切れは取り直す (ts を過去へ)
    installer._TAG_CACHE[FORK["clone"]]["ts"] -= installer.TAG_TTL + 1
    _fake_fetch(monkeypatch, ["suzuri-v1.2.0"], calls)
    assert fork_tags(FORK) == ["suzuri-v1.2.0"]
    # tag_prefix の無い spec (上流) は常に空
    assert fork_tags(installer.INSTALL_SPECS["freetoken"]) == []


def test_cached_tags_never_fetches(monkeypatch):
    def boom(url, prefix, **kw):
        raise AssertionError("cached_tags must not fetch")
    monkeypatch.setattr(installer, "fetch_tags", boom)
    assert cached_tags(FORK) == []
    _fake_fetch(monkeypatch, TAGS)
    fork_tags(FORK)
    assert cached_tags(FORK) == TAGS


# --------------------------------------------------------- ホワイトリスト

def test_tags_join_whitelist_and_resolve(monkeypatch):
    _fake_fetch(monkeypatch, TAGS)
    fork_tags(FORK)
    assert allowed_refs(FORK) == ["suzuri/develop", "suzuri/product"] + TAGS
    assert resolve_ref(FORK, "suzuri-v1.0.1") == "suzuri-v1.0.1"
    assert is_tag_ref(FORK, "suzuri-v1.0.1") is True
    assert is_tag_ref(FORK, "suzuri/product") is False
    for bad in ("suzuri-v9.9.9", "v0.2.2", "suzuri-v1.0.1^{}"):
        with pytest.raises(InstallError):
            resolve_ref(FORK, bad)


def test_tags_without_fetch_are_not_allowed(monkeypatch):
    # キャッシュが冷たい (= 未取得) ならタグはホワイトリストに無い
    assert allowed_refs(FORK) == ["suzuri/develop", "suzuri/product"]
    with pytest.raises(InstallError):
        resolve_ref(FORK, "suzuri-v1.0.1")


# --------------------------------------------------------- インストール計画

def test_fresh_clone_with_tag(monkeypatch, tmp_path: Path):
    _fake_fetch(monkeypatch, TAGS)
    got = _plan(monkeypatch, tmp_path / "root", "suzuri-v1.0.1")
    job, steps = got["job"], got["steps"]
    assert job["ref"] == "suzuri-v1.0.1" and job["needs_clone"] is True
    argv = steps[0]["argv"]
    assert steps[0]["name"] == "clone"
    assert argv[argv.index("--branch") + 1] == "suzuri-v1.0.1"   # git へは完全名


def test_existing_clone_plans_branch_step_for_tag(monkeypatch, tmp_path: Path):
    _fake_fetch(monkeypatch, TAGS)
    root = tmp_path / "root"
    mod = root / "module" / "fork" / "freetoken"
    mod.mkdir(parents=True)
    (mod / "app.py").write_text("x\n", "utf-8")
    got = _plan(monkeypatch, root, "suzuri-v1.0.0")
    assert next(s["name"] for s in got["steps"]) == "branch"
    assert got["job"]["ref"] == "suzuri-v1.0.0"


def test_unknown_tag_starts_nothing(monkeypatch, tmp_path: Path):
    _fake_fetch(monkeypatch, TAGS)
    mgr = _manager(tmp_path / "root")

    async def go():
        with pytest.raises(InstallError):
            await mgr.start_install("freetoken-fork", ref="suzuri-v9.9.9")
        assert mgr.jobs == {} and mgr.active_id is None

    asyncio.run(go())


def test_just_pushed_tag_is_refetched_once(monkeypatch, tmp_path: Path):
    """キャッシュに無いタグが指定されたら 1 回だけ取り直す (直前 push のタグを許す)。"""
    _fake_fetch(monkeypatch, ["suzuri-v1.0.0"])
    fork_tags(FORK)                                    # 古い一覧でキャッシュを温める
    calls: list = []
    _fake_fetch(monkeypatch, TAGS, calls)                # リモートには v1.0.1 が増えた
    got = _plan(monkeypatch, tmp_path / "root", "suzuri-v1.0.1")
    assert got["job"]["ref"] == "suzuri-v1.0.1"
    assert len(calls) == 1                               # force の 1 回だけ (毎回ではない)


# --------------------------------------------------------- 既存 clone の切替 (実 git)

def test_switch_to_tag_detaches_at_tag_commit(tmp_path: Path):
    origin = _origin_tags(tmp_path)
    root, mod = _clone(tmp_path, origin)                 # develop にいる
    mgr = _manager(root)
    job, switched = _switch(mgr, mod, "suzuri-v1.0.1")
    assert switched is True
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=mod) == "HEAD"   # detached
    assert _git("describe", "--tags", "--exact-match", "HEAD", cwd=mod) == "suzuri-v1.0.1"
    assert (mod / "app.py").read_text("utf-8") == "product build\n"
    assert _git("status", "--porcelain", cwd=mod) == ""
    assert current_ref(root, FORK) == "suzuri-v1.0.1"    # UI の既定選択がタグを向く
    log = "\n".join(job["logs"])
    assert "suzuri/develop → suzuri-v1.0.1" in log and "タグ" in log
    assert "refs/tags/suzuri-v1.0.1" in log              # tags を fetch している


def test_switch_same_tag_is_noop(tmp_path: Path):
    origin = _origin_tags(tmp_path)
    root, mod = _clone(tmp_path, origin)
    mgr = _manager(root)
    _switch(mgr, mod, "suzuri-v1.0.1")
    job, switched = _switch(mgr, mod, "suzuri-v1.0.1")
    assert switched is False
    assert "選択と同じ" in "\n".join(job["logs"])


def test_switch_tag_to_tag_and_back_to_branch(tmp_path: Path):
    origin = _origin_tags(tmp_path)
    root, mod = _clone(tmp_path, origin)
    mgr = _manager(root)
    _switch(mgr, mod, "suzuri-v1.0.1")
    _, switched = _switch(mgr, mod, "suzuri-v1.0.0")     # タグ → 別タグ
    assert switched is True
    assert (mod / "app.py").read_text("utf-8") == "develop build\n"
    assert current_ref(root, FORK) == "suzuri-v1.0.0"
    _, switched = _switch(mgr, mod, "suzuri/develop")    # タグ → ブランチ
    assert switched is True
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=mod) == "suzuri/develop"
    assert current_ref(root, FORK) == "suzuri/develop"


def test_switch_to_tag_on_shallow_clone(tmp_path: Path):
    """setup / UI の clone は --depth 1。浅いままタグを fetch + checkout できる。"""
    origin = _origin_tags(tmp_path)
    root, mod = _clone_shallow(tmp_path, origin)
    assert (mod / ".git" / "shallow").exists()
    mgr = _manager(root)
    _, switched = _switch(mgr, mod, "suzuri-v1.0.1")
    assert switched is True
    assert (mod / "app.py").read_text("utf-8") == "product build\n"
    assert _git("describe", "--tags", "--exact-match", "HEAD", cwd=mod) == "suzuri-v1.0.1"


def test_switch_to_tag_refuses_dirty_tree(tmp_path: Path):
    origin = _origin_tags(tmp_path)
    root, mod = _clone(tmp_path, origin)
    (mod / "app.py").write_text("私の未コミット作業\n", "utf-8")
    mgr = _manager(root)
    with pytest.raises(InstallError) as e:
        _switch(mgr, mod, "suzuri-v1.0.1")
    assert "未コミット" in str(e.value)
    assert (mod / "app.py").read_text("utf-8") == "私の未コミット作業\n"


def test_switch_to_tag_ignores_untracked_files(tmp_path: Path):
    origin = _origin_tags(tmp_path)
    root, mod = _clone(tmp_path, origin)
    (mod / "freetoken-launch.json").write_text('{"argv": []}\n', "utf-8")
    mgr = _manager(root)
    _, switched = _switch(mgr, mod, "suzuri-v1.0.1")
    assert switched is True
    assert (mod / "freetoken-launch.json").exists()


def test_official_tag_is_not_a_choice(tmp_path: Path):
    """公式タグ (v0.2.2) は fork 専用タグと識別されて選択一覧に出ない。"""
    origin = _origin_tags(tmp_path)
    root, mod = _clone(tmp_path, origin)
    mgr = _manager(root)
    with pytest.raises(InstallError):
        resolve_ref(FORK, "v0.2.2")                      # ホワイトリスト外
    head = _git("rev-parse", "HEAD", cwd=mod)
    with pytest.raises(InstallError):
        _switch(mgr, mod, "v0.2.2")                      # 直接呼んでも fetch 失敗で止まる
    assert _git("rev-parse", "HEAD", cwd=mod) == head    # 現在地は変わらない


# --------------------------------------------------------- API

def test_api_exposes_tags_without_prefix_and_validates(monkeypatch, tmp_path: Path):
    _fake_fetch(monkeypatch, TAGS)
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
        assert fork["tag_prefix"] == "suzuri-"
        refs = fork["refs"]
        assert [r["ref"] for r in refs] == ["suzuri/develop", "suzuri/product"] + TAGS
        tags = [r for r in refs if r.get("kind") == "tag"]
        assert [r["label"] for r in tags] == ["v1.0.1", "v1.0.0"]   # 表示は prefix なし
        upstream = next(e for e in res["installable"] if e["id"] == "freetoken")
        assert upstream["refs"] == [] and upstream["tag_prefix"] == ""

        st, res = await client.post("/api/installs", {"backend": "freetoken-fork",
                                                      "ref": "suzuri-v9.9.9"})
        assert st == 400                                   # 一覧外のタグは git に渡さない
        st, res = await client.post("/api/installs", {"backend": "freetoken-fork",
                                                      "ref": "v0.2.2"})
        assert st == 400                                   # 公式タグは選べない
        st, job = await client.post("/api/installs", {"backend": "freetoken-fork",
                                                      "ref": "suzuri-v1.0.1"})
        assert st == 201 and job["ref"] == "suzuri-v1.0.1"
        assert job["steps"][0] == "clone" and got["steps"][0] == "clone"
        await asyncio.sleep(0)

    asyncio.run(go())


def test_api_refresh_forces_tag_refetch(monkeypatch, tmp_path: Path):
    calls: list = []
    _fake_fetch(monkeypatch, TAGS, calls)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    client = Client(app_mod.create_app(tmp_path))

    async def go() -> None:
        st, _ = await client.get("/api/installs")
        assert st == 200 and len(calls) == 1
        st, _ = await client.get("/api/installs")
        assert st == 200 and len(calls) == 1               # TTL 内はキャッシュ
        st, res = await client.get("/api/installs?refresh=1")
        assert st == 200 and len(calls) == 2               # 一覧を更新 = 取り直し
        fork = next(e for e in res["installable"] if e["id"] == "freetoken-fork")
        assert any(r.get("kind") == "tag" for r in fork["refs"])

    asyncio.run(go())


def test_profile_setup_options_includes_tags(monkeypatch, tmp_path: Path):
    _fake_fetch(monkeypatch, TAGS)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    client = Client(app_mod.create_app(tmp_path))

    async def go() -> None:
        st, res = await client.get("/api/profiles/setup-options")
        assert st == 200
        fork = next(e for e in res["installable"] if e["id"] == "freetoken-fork")
        assert [r["label"] for r in fork["refs"] if r.get("kind") == "tag"] == ["v1.0.1", "v1.0.0"]

    asyncio.run(go())


# --------------------------------------------------------- UI (静的検査)

def test_wizard_shows_tags_without_prefix():
    # refLabel: kind=tag は label (prefix なし) を表示に使う
    assert 'r.kind === "tag" ? r.label : (ref || "")' in JS
    # 確認画面のバージョン行 / 変更予告も表示名を使う
    assert 'esc(refLabel(sp, W.ref))' in JS
    assert "refLabel(sp, sp.current_ref)} → ${refLabel(sp, W.ref)}" in JS
    # タグ一覧の手動更新 (TTL キャッシュの取り直し)
    assert 'id="br-refresh"' in HTML and 'id="br-refresh-row"' in HTML
    assert 'api("/api/installs?refresh=1")' in JS


def test_wizard_tags_are_a_dropdown():
    """タグはラジオ + プルダウン (増えても一覧が伸びない)。ブランチはラジオのまま。"""
    assert 'refs.filter(r => r.kind !== "tag")' in JS      # ラジオ一覧 = ブランチだけ
    assert 'refs.filter(r => r.kind === "tag")' in JS      # プルダウン = タグだけ
    # option の表示は prefix なし label、value は git に渡す完全名
    assert '<option value="${esc(r.ref)}">${esc(r.label)}</option>' in JS
    assert 'id="br-tags"' in HTML and 'id="br-tag-row"' in HTML
    # タグ行にもラジオ (ブランチと同じグループ name="br" で排他)
    assert 'type="radio" name="br" id="br-tag-radio"' in HTML
    assert "tagRadio.onchange" in JS and "syncTagSel();" in JS
    # 非選択 (ブランチ選択中) は灰色 + プルダウン操作不可。ラジオでタグ選択に切り替え
    assert "sel.disabled = !on;" in JS
    assert 'tagRow.classList.toggle("off", !on);' in JS
    assert ".opt.off .txt, .opt.off select { opacity: .5; }" in CSS
    assert ".opt select:disabled" in CSS
    # 導入済みタグはタグ行に「設定済み」バッジ
    assert 'id="br-tag-set"' in HTML
    assert '$("#br-tag-set").classList.toggle("hidden", !tags.some(r => r.ref === sp.current_ref))' in JS


def test_wizard_branch_step_mentions_tags():
    assert "ブランチ / タグを選んでください" in HTML
