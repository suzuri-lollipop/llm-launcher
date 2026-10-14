"""ロール (admin / maintainer / user) と機能権限のテスト。

管理者が「メンテナーに見せる情報・実行できる機能」を ON/OFF できること、
ユーザーロール (role=user) は簡易画面 API (/api/simple*) しか使えないことを API 層で検証する。
Client などの最小 ASGI クライアントと fixture は test_auth_users から流用する。
"""
from __future__ import annotations

import asyncio

from test_auth_users import (PW, Client, auth_on, fast_kdf, make_app,  # noqa: F401
                             setup_admin)

from llm_launcher import rbac

PW2 = "passw0rd-2"


async def add_user(admin: Client, username: str, role: str) -> dict:
    st, u = await admin.post("/api/users", {"username": username, "password": PW2,
                                            "display_name": username, "role": role})
    assert st == 201, u
    return u


async def login(app, username: str) -> Client:
    c = Client(app)
    st, s = await c.post("/api/auth/login", {"username": username, "password": PW2})
    assert st == 200, s
    return c


async def add_profile(admin: Client, name: str, simple: bool = False) -> dict:
    st, p = await admin.post("/api/profiles", {
        "name": name, "backend": "vllm",
        "values": {"model": "/models/test", "port": 8123},
        "simple_visible": simple})
    assert st == 201, p
    return p


async def user_id(admin: Client, username: str) -> str:
    """ユーザー管理 API から ID を引く (セッションの起動者判定に使う)。"""
    _, users = await admin.get("/api/users")
    return next(u["id"] for u in users["users"] if u["username"] == username)


def inject_session(app, profile: dict, sid: str, status: str, *, error: str = "",
                   finished_at: float | None = None, started_at: float = 100.0,
                   started_by: str = "", started_via: str = "") -> None:
    """実プロセスを起動せずにマネージャーへセッション記録だけ入れる (pid は存在しない値)。

    started_by / started_via は「誰がどの画面から起こしたインスタンスか」。既定は
    空で、簡易画面の警告対象にしない (実際の履歴でも起動者情報を持つ以前のレコードは
    空 = 誰の起動でもない扱い)。
    """
    app.state.manager.sessions[sid] = {
        "id": sid, "profile_id": profile["id"], "profile_name": profile["name"],
        "backend": profile["backend"], "status": status, "ready": status == "running",
        "pid": 2_000_000_000, "port": 8123, "base_url": "http://127.0.0.1:8123/v1",
        "argv": [], "env": {}, "cwd": "", "log": "", "detached": False,
        "started_at": started_at, "finished_at": finished_at, "exit_code": None,
        "error": error, "oom": {},
        "started_by": started_by, "started_by_name": "", "started_via": started_via,
    }


# ------------------------------------------------------------------ ロール作成

def test_maintainer_role_is_accepted_and_sorted(tmp_path, auth_on, fast_kdf):
    """maintainer が作成でき、一覧は admin → maintainer → user の順で並ぶ。"""
    admin = Client(make_app(tmp_path))

    async def go() -> None:
        await setup_admin(admin)
        await add_user(admin, "hana", "maintainer")
        await add_user(admin, "jiro", "user")
        st, s = await admin.get("/api/users")
        assert st == 200
        assert [u["role"] for u in s["users"]] == ["admin", "maintainer", "user"]
        st, bad = await admin.post("/api/users", {"username": "eve", "password": PW2,
                                                  "role": "root"})
        assert st == 400 and "maintainer" in bad["detail"]      # 選択肢は 3 つ
        st, ok = await admin.put(f"/api/users/{s['users'][1]['id']}", {"role": "user"})
        assert st == 200 and ok["role"] == "user"               # 降格も自由
    asyncio.run(go())


def test_status_exposes_role_and_perms(tmp_path, auth_on, fast_kdf):
    """/api/auth/status が role / perms / simple_only を返す。"""
    app = make_app(tmp_path)
    admin = Client(app)

    async def go() -> None:
        await setup_admin(admin)
        st, s = await admin.get("/api/auth/status")
        assert s["role"] == "admin" and s["simple_only"] is False
        assert s["perms"] == {k: True for k in rbac.CAP_KEYS}
        await add_user(admin, "hana", "maintainer")
        await add_user(admin, "jiro", "user")
        st, s = await (await login(app, "hana")).get("/api/auth/status")
        assert s["role"] == "maintainer" and s["role_label"] == "メンテナー"
        assert s["perms"] == rbac.default_perms() and s["simple_only"] is False
        st, s = await (await login(app, "jiro")).get("/api/auth/status")
        assert s["role"] == "user" and s["simple_only"] is True
        assert s["perms"] == {k: False for k in rbac.CAP_KEYS}
    asyncio.run(go())


# ------------------------------------------------------- メンテナーの既定権限

def test_maintainer_default_permissions(tmp_path, auth_on, fast_kdf):
    """既定は「閲覧系 ON / 編集系 OFF」。起動・停止などの基本操作は常に使える。"""
    app = make_app(tmp_path)
    admin = Client(app)

    async def go() -> None:
        await setup_admin(admin)
        await add_user(admin, "hana", "maintainer")
        hana = await login(app, "hana")
        for path in ("/api/state", "/api/system", "/api/oom", "/api/profiles"):
            assert (await hana.get(path))[0] == 200, path
        for method, path, body in (
                ("POST", "/api/profiles", {"name": "x", "backend": "vllm"}),
                ("POST", "/api/preview", {"name": "x", "backend": "vllm"}),
                ("PUT", "/api/oom", {"enabled": False}),
                ("PUT", "/api/backends/vllm", {"python": "/usr/bin/python3"}),
                ("POST", "/api/backends/vllm/detect", None),
                ("GET", "/api/benchmarks", None),
                ("GET", "/api/hf-models", None)):
            st, s = (await hana.get(path)) if method == "GET" \
                else (await hana.request(method, path, body))
            assert st == 403, (path, s)
            assert s["code"] == "forbidden"
        # 既定 ON のプレイグラウンドは通る (ダミー sid なので 403 ではなく 404)
        assert (await hana.get("/api/playground/sid1/models"))[0] == 404
        assert (await hana.get("/api/users"))[0] == 403          # ユーザー管理は管理者のみ
        assert (await hana.get("/api/roles"))[0] == 403          # 権限設定も管理者のみ
        st, s = await hana.put("/api/roles/maintainer", {"perms": {"bench": True}})
        assert st == 403
    asyncio.run(go())


def test_admin_toggles_change_maintainer_access(tmp_path, auth_on, fast_kdf):
    """管理者が ON/OFF すると、その場でメンテナーの API アクセスと表示が変わる。"""
    app = make_app(tmp_path)
    admin = Client(app)

    async def go() -> None:
        await setup_admin(admin)
        prof = await add_profile(admin, "test-server")
        await add_user(admin, "hana", "maintainer")
        hana = await login(app, "hana")

        st, r = await admin.get("/api/roles")
        assert st == 200 and {c["key"] for c in r["caps"]} == set(rbac.CAP_KEYS)
        assert r["maintainer"] == rbac.default_perms()

        # bench を ON / dashboard・config.read を OFF にする
        new = {**rbac.default_perms(), "bench": True, "dashboard": False,
               "config.read": False}
        st, saved = await admin.put("/api/roles/maintainer", {"perms": new})
        assert st == 200 and saved["maintainer"] == new
        st, s = await hana.get("/api/auth/status")
        assert s["perms"] == new                                  # 再ログイン不要で反映

        assert (await hana.get("/api/benchmarks"))[0] == 200      # ON にした機能は使える
        assert (await hana.get("/api/system"))[0] == 403          # OFF にした機能は使えない
        assert (await hana.post("/api/oom", {"enabled": False}))[0] == 403
        st, s = await hana.get("/api/state")                      # 起動操作は常に使える
        assert st == 200
        assert "ip" not in s["system"] and "root" not in s["system"]   # ホスト詳細を伏せる
        assert s["profiles"][0]["values"] == {}
        assert s["profiles"][0]["redacted"] is True
        assert s["profiles"][0]["name"] == "test-server"          # 名前と起動はできる

        # 既定値に戻すと元通り (未知キーは無視される)
        st, saved = await admin.put("/api/roles/maintainer", {"perms": {"unknown": True}})
        assert st == 200 and saved["maintainer"] == rbac.default_perms()
        assert (await hana.get("/api/system"))[0] == 200
        st, s = await hana.get("/api/state")
        assert s["profiles"][0]["values"] == prof["values"]
        assert "redacted" not in s["profiles"][0]
    asyncio.run(go())


# ------------------------------------------------------------- ユーザーロール

def test_user_role_is_simple_screen_only(tmp_path, auth_on, fast_kdf):
    """role=user は /api/simple* と自分のパスワード変更だけ使える。"""
    app = make_app(tmp_path)
    admin = Client(app)

    async def go() -> None:
        await setup_admin(admin)
        await add_user(admin, "jiro", "user")
        jiro = await login(app, "jiro")
        for path in ("/api/state", "/api/system", "/api/oom", "/api/profiles",
                     "/api/users", "/api/roles", "/api/benchmarks", "/api/sessions/x/log"):
            st, s = await jiro.get(path)
            assert st == 403, (path, s)
            assert "簡易画面" in s["detail"]
        st, s = await jiro.post("/api/sessions", {"profile_id": "p1"})
        assert st == 403                                          # 通常 API からの起動も不可
        st, s = await jiro.get("/api/simple")
        assert st == 200 and set(s) == {"hostname", "contact", "servers", "alerts",
                                        "server_time"}
        st, s = await jiro.post("/api/auth/password", {"current_password": PW2,
                                                       "password": "new-password-1"})
        assert st == 200                                          # 自分のパスワードは変更可
    asyncio.run(go())


def test_simple_screen_only_shows_visible_profiles(tmp_path, auth_on, fast_kdf):
    """simple_visible = ON のプロファイルだけが出る (オプトイン)。"""
    app = make_app(tmp_path)
    admin = Client(app)

    async def go() -> None:
        await setup_admin(admin)
        shown = await add_profile(admin, "しょうかいサーバー", simple=True)
        await add_profile(admin, "かくれたサーバー")
        st, r = await admin.get("/api/roles")
        assert [p["id"] for p in r["simple_profiles"]] == [shown["id"]]
        st, saved = await admin.put("/api/roles/simple",
                                    {"contact": "こまったときは 内線 1234 へ"})
        assert st == 200 and saved["simple"]["contact"] == "こまったときは 内線 1234 へ"

        await add_user(admin, "jiro", "user")
        jiro = await login(app, "jiro")
        st, s = await jiro.get("/api/simple")
        assert st == 200 and [x["profile_name"] for x in s["servers"]] == ["しょうかいサーバー"]
        assert s["servers"][0]["state"] == "stopped" and s["alerts"] == []
        assert s["contact"] == "こまったときは 内線 1234 へ"

        st, s = await jiro.post("/api/simple/start", {"profile_id": "no-such"})
        assert st == 404
        st, s = await jiro.post("/api/simple/stop", {"profile_id": "no-such"})
        assert st == 404
    asyncio.run(go())


def test_simple_start_stop_uses_existing_session(tmp_path, auth_on, fast_kdf):
    """起動済みの連打はそのまま起動中を返し、停止はプロファイル名でも止められる。"""
    app = make_app(tmp_path)
    admin = Client(app)

    async def go() -> None:
        await setup_admin(admin)
        shown = await add_profile(admin, "しょうかいサーバー", simple=True)
        hidden = await add_profile(admin, "かくれたサーバー")
        inject_session(app, shown, "s-live", "running")
        await add_user(admin, "jiro", "user")
        jiro = await login(app, "jiro")

        st, s = await jiro.get("/api/simple")
        assert s["servers"][0]["state"] == "running" and s["servers"][0]["sid"] == "s-live"
        st, s = await jiro.post("/api/simple/start", {"profile_id": shown["id"]})
        assert st == 200 and s["id"] == "s-live"                  # 起動済みならそのまま返す
        st, s = await jiro.post("/api/simple/start", {"profile_id": hidden["id"]})
        assert st == 404                                          # 非表示は起動不可
        st, s = await jiro.post("/api/simple/stop", {"profile_id": hidden["id"]})
        assert st == 404                                          # 非表示は停止も不可
        st, s = await jiro.post("/api/simple/stop", {"profile_id": shown["id"]})
        assert st == 200 and app.state.manager.sessions["s-live"]["status"] == "exited"
    asyncio.run(go())


def test_simple_alerts_only_own_starts(tmp_path, auth_on, fast_kdf):
    """警告が出るのは自分が起こしたインスタンス (簡易画面 / 管理画面) が
    異常終了したときだけ。ベンチの自動起動と他人の起動は黙る。"""
    app = make_app(tmp_path)
    admin = Client(app)

    async def go() -> None:
        await setup_admin(admin)
        shown = await add_profile(admin, "しょうかいサーバー", simple=True)
        await add_profile(admin, "かくれたサーバー")
        await add_user(admin, "jiro", "user")
        admin_id, jiro_id = await user_id(admin, "admin"), await user_id(admin, "jiro")
        jiro = await login(app, "jiro")

        # 導入前の古い履歴 (起動者情報なし) は警告に出さない
        inject_session(app, shown, "s-legacy", "failed", error="導入前の異常",
                       finished_at=200.0)
        st, s = await jiro.get("/api/simple")
        assert st == 200 and s["alerts"] == []
        assert s["servers"][0]["state"] == "stopped"

        # 他人 (管理者) の管理画面起動も、本人が触っていないベンチの起動も黙る
        inject_session(app, shown, "s-admin", "failed", error="管理者の起動",
                       finished_at=300.0, started_by=admin_id, started_via="ui")
        inject_session(app, shown, "s-bench", "lost", error="ベンチの起動",
                       finished_at=350.0, started_by=jiro_id, started_via="bench")
        st, s = await jiro.get("/api/simple")
        assert s["alerts"] == []

        # 本人が管理画面から起こした分も、落ちれば自分の画面に警告が出る
        inject_session(app, shown, "s-myui", "failed", error="管理画面で起動した分",
                       finished_at=380.0, started_by=jiro_id, started_via="ui")
        st, s = await jiro.get("/api/simple")
        assert [a["sid"] for a in s["alerts"]] == ["s-myui"]

        # 本人が /simple の起動ボタンで起こした分も全画面警告になる
        # (警告は現在のインスタンスだけなので、新しい起動が古い警告を覆い隠す)
        inject_session(app, shown, "s-mine", "failed", error="CUDA out of memory",
                       finished_at=400.0, started_by=jiro_id, started_via="simple")
        st, s = await jiro.get("/api/simple")
        assert st == 200 and [a["sid"] for a in s["alerts"]] == ["s-mine"]
        a = s["alerts"][0]
        assert a["kind"] == "failed" and a["profile_name"] == "しょうかいサーバー"
        assert a["detail"] == "CUDA out of memory" and a["sid"] == "s-mine"
        # 管理者側でも simple_visible は保存されている
        st, lst = await admin.get("/api/profiles")
        assert [p.get("simple_visible") for p in lst] == [True, False]
    asyncio.run(go())


def test_simple_alerts_silent_while_running(tmp_path, auth_on, fast_kdf):
    """今は稼働中のインスタンスに対して、過去の異常終了を警告しにいかない。"""
    app = make_app(tmp_path)
    admin = Client(app)

    async def go() -> None:
        await setup_admin(admin)
        shown = await add_profile(admin, "しょうかいサーバー", simple=True)
        await add_user(admin, "jiro", "user")
        jiro_id = await user_id(admin, "jiro")
        inject_session(app, shown, "s-mine", "failed", error="一度落ちた",
                       finished_at=200.0, started_by=jiro_id, started_via="simple")
        inject_session(app, shown, "s-live", "running", started_at=300.0,
                       started_by=jiro_id, started_via="simple")
        jiro = await login(app, "jiro")
        st, s = await jiro.get("/api/simple")
        assert st == 200
        assert s["servers"][0]["state"] == "running"
        assert s["alerts"] == []
    asyncio.run(go())


def test_simple_alerts_ignore_other_users_instance(tmp_path, auth_on, fast_kdf):
    """他のユーザーが起動したインスタンスの異常は、自分の画面には出さない。"""
    app = make_app(tmp_path)
    admin = Client(app)

    async def go() -> None:
        await setup_admin(admin)
        shown = await add_profile(admin, "しょうかいサーバー", simple=True)
        other = await add_profile(admin, "もうひとつのサーバー", simple=True)
        await add_user(admin, "jiro", "user")
        await add_user(admin, "hanako", "user")
        hanako_id = await user_id(admin, "hanako")
        # hanako が起こした現在のインスタンス (簡易画面 / 管理画面の両方)
        inject_session(app, shown, "s-hanako", "failed", error="hanako の簡易画面起動",
                       finished_at=200.0, started_by=hanako_id, started_via="simple")
        inject_session(app, other, "s-hanako-ui", "failed", error="hanako の管理画面起動",
                       finished_at=210.0, started_by=hanako_id, started_via="ui")
        jiro = await login(app, "jiro")
        st, s = await jiro.get("/api/simple")
        assert st == 200 and s["alerts"] == []
        hanako = await login(app, "hanako")
        st, s = await hanako.get("/api/simple")
        assert [a["sid"] for a in s["alerts"]] == ["s-hanako-ui", "s-hanako"]
    asyncio.run(go())


def test_simple_memory_alert_always_shown(tmp_path, auth_on, fast_kdf, monkeypatch):
    """メモリ逼迫は起動者に依存しないライブ警告なので常に全画面で出す。"""
    app = make_app(tmp_path)
    admin = Client(app)

    async def go() -> None:
        await setup_admin(admin)
        await add_profile(admin, "しょうかいサーバー", simple=True)
        await add_user(admin, "jiro", "user")
        monkeypatch.setattr(app.state.manager, "oom_status",
                            lambda cfg=None: {"state": "crit", "available_gb": 0.4})
        jiro = await login(app, "jiro")
        st, s = await jiro.get("/api/simple")
        assert st == 200 and [a["kind"] for a in s["alerts"]] == ["memory"]
        assert "空きメモリ" in s["alerts"][0]["detail"]
    asyncio.run(go())


def test_start_records_actor_and_via(tmp_path, auth_on, fast_kdf, monkeypatch):
    """起動したユーザーと経路 (simple / ui) がセッションに記録される。"""
    app = make_app(tmp_path)
    admin = Client(app)

    async def go() -> None:
        await setup_admin(admin)
        shown = await add_profile(admin, "しょうかいサーバー", simple=True)
        await add_user(admin, "jiro", "user")
        captured: dict[str, dict] = {}

        async def fake_start(profile, force=False, actor=None, via=""):
            captured["last"] = {"actor": actor, "via": via}
            sid = "s-new"
            app.state.manager.sessions[sid] = {"id": sid, "profile_id": profile["id"],
                                               "profile_name": profile["name"],
                                               "status": "starting", "ready": False,
                                               "started_at": 1.0, "detached": False}
            return app.state.manager.sessions[sid]
        monkeypatch.setattr(app.state.manager, "start", fake_start)

        jiro = await login(app, "jiro")
        st, _ = await jiro.post("/api/simple/start", {"profile_id": shown["id"]})
        assert st == 200
        assert captured["last"]["via"] == "simple"
        assert captured["last"]["actor"]["username"] == "jiro"

        inject_session(app, shown, "s-live", "running")
        st, _ = await admin.post("/api/sessions/s-live/restart", {"force": False})
        assert st == 201
        assert captured["last"]["via"] == "ui"
        assert captured["last"]["actor"]["role"] == "admin"
    asyncio.run(go())


def test_start_maps_backend_error_to_400(tmp_path, auth_on, fast_kdf, monkeypatch):
    """バックエンド未導入などの起動前エラー (ValueError) は 500 にせず 400 に落とす。"""
    app = make_app(tmp_path)
    admin = Client(app)

    def boom_argv(profile):
        raise ValueError("llama-server が見つかりません。バックエンド設定でパスを指定してください")

    async def boom_start(profile, force=False, actor=None, via=""):
        raise ValueError("llama-server が見つかりません。バックエンド設定でパスを指定してください")

    async def go() -> None:
        await setup_admin(admin)
        shown = await add_profile(admin, "しょうかいサーバー", simple=True)
        monkeypatch.setattr(app.state.manager, "build_argv", boom_argv)
        st, s = await admin.post("/api/sessions", {"profile_id": shown["id"]})
        assert st == 400 and "llama-server" in s["detail"]          # 管理画面は理由を見られる

        monkeypatch.setattr(app.state.manager, "start", boom_start)
        await add_user(admin, "jiro", "user")
        jiro = await login(app, "jiro")
        st, s = await jiro.post("/api/simple/start", {"profile_id": shown["id"]})
        assert st == 400 and "管理者" in s["detail"] and "llama-server" not in s["detail"]
    asyncio.run(go())


def test_simple_page_delegates_login_to_root(tmp_path, auth_on, fast_kdf):
    """簡易画面は自前のログインフォームを持たない (認証は管理画面と同じ / に委譲)。"""
    client = Client(make_app(tmp_path))

    async def go() -> None:
        st, body = await client.get("/simple")
        assert st == 200 and b"/api/simple" in body
        assert b"<form" not in body                       # /simple 専用のログイン画面は廃止
        assert b"/api/auth/login" not in body
        assert b'location.replace("/")' in body           # 未ログイン / ログアウトは / へ
        st, root = await client.get("/")                  # 共通のログイン画面側
        assert st == 200 and b"login-note" in root and b"/simple" in root
    asyncio.run(go())


# ------------------------------------------------------------------ 設定保存

def test_role_settings_persist_and_merge(tmp_path, auth_on, fast_kdf):
    """settings.json に保存し、アプリを再起動しても設定が維持される。"""
    admin = Client(make_app(tmp_path))

    async def go() -> None:
        await setup_admin(admin)
        await admin.put("/api/roles/maintainer", {"perms": {"logs": False}})
        await admin.put("/api/roles/simple", {"contact": "内線 1234"})
        raw = (tmp_path / "data" / "settings.json").read_text("utf-8")
        assert '"maintainer"' in raw and '"logs": false' in raw and "内線 1234" in raw

        again = Client(make_app(tmp_path))                        # 同一 data を開き直す
        st, _ = await again.post("/api/auth/login", {"username": "admin", "password": PW})
        assert st == 200
        st, r = await again.get("/api/roles")
        assert r["maintainer"]["logs"] is False and r["simple"]["contact"] == "内線 1234"
        st, s = await again.put("/api/roles/simple", {"contact": "あ" * 121})
        assert st == 400                                          # 長さ上限
    asyncio.run(go())



