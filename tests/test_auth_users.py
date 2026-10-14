"""認証・ユーザー管理 API のテスト (初回セットアップ / ログイン / ロール制限 / API キー)。

httpx 追加依存を避けるため tests/test_oom_guard.py と同じ最小 ASGI クライアントを使い、
Cookie と Authorization ヘッダーは手で受け渡す。PBKDF2 の反復回数は fixture で下げて
実行時間を抑える (検証時は保存された iters を読むので正しく動く)。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from llm_launcher import app as app_mod
from llm_launcher import auth as auth_mod

PW = "passw0rd-1"


@pytest.fixture
def auth_on(monkeypatch, _auth_disabled):
    """conftest の autouse (認証オフ) の後から認証を有効へ戻す。"""
    monkeypatch.setenv("LLM_LAUNCHER_AUTH", "1")


@pytest.fixture
def fast_kdf(monkeypatch):
    monkeypatch.setattr(auth_mod, "PBKDF2_ITERS", 1000)


class Client:
    """Cookie / Bearer を扱う最小 ASGI クライアント。"""

    def __init__(self, app):
        self.app = app
        self.cookie = ""

    async def request(self, method: str, path: str, body: dict | None = None, *,
                      use_cookie: bool = True, api_key: str = ""):
        payload = b"" if body is None else json.dumps(body).encode()
        headers = [(b"host", b"testserver"), (b"content-type", b"application/json"),
                   (b"content-length", str(len(payload)).encode()),
                   (b"user-agent", b"pytest")]
        if use_cookie and self.cookie:
            headers.append((b"cookie", self.cookie.encode()))
        if api_key:
            headers.append((b"authorization", f"Bearer {api_key}".encode()))
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "method": method, "scheme": "http",
                 "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
                 "headers": headers, "client": ("127.0.0.1", 50000), "server": ("test", 80)}
        sent: list[dict] = []

        async def receive() -> dict:
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(msg: dict) -> None:
            sent.append(msg)

        await self.app(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        for k, v in start.get("headers", []):
            if k == b"set-cookie":
                self.cookie = v.decode("utf-8").split(";")[0]
        data = json.loads(raw) if raw[:1] in (b"{", b"[") else raw
        return start["status"], data

    async def get(self, path, **kw):
        return await self.request("GET", path, **kw)

    async def post(self, path, body=None, **kw):
        return await self.request("POST", path, body, **kw)

    async def put(self, path, body=None, **kw):
        return await self.request("PUT", path, body, **kw)

    async def delete(self, path, **kw):
        return await self.request("DELETE", path, **kw)


def make_app(tmp_path):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return app_mod.create_app(tmp_path)


async def setup_admin(client: Client, username: str = "admin", password: str = PW) -> dict:
    """初回セットアップを実行し、作成された管理者 (ログイン済み状態) を返す。"""
    st, res = await client.post("/api/auth/setup",
                                {"username": username, "password": password})
    assert st == 201, res
    return res["user"]


# --------------------------------------------------------- 初回セットアップ / ログイン


def test_setup_login_logout_flow(tmp_path, auth_on, fast_kdf):
    client = Client(make_app(tmp_path))

    async def go() -> None:
        st, s = await client.get("/api/auth/status")
        assert st == 200 and s["auth_enabled"] is True and s["needs_setup"] is True
        assert s["authenticated"] is False and s["user"] is None and s["is_admin"] is False
        st, s = await client.get("/api/state")                     # 未ログインの通常 API
        assert st == 401 and s["code"] == "unauthorized"

        user = await setup_admin(client)                           # 作成 = 自動ログイン
        assert user["role"] == "admin" and user["username"] == "admin"
        assert user["display_name"] == "admin"      # 未指定の表示名はユーザー名にフォールバック
        st, s = await client.get("/api/auth/status")
        assert st == 200 and s["needs_setup"] is False and s["authenticated"] is True
        assert s["is_admin"] is True and s["user"]["username"] == "admin"
        st, s = await client.get("/api/state")
        assert st == 200 and "profiles" in s

        st, s = await client.post("/api/auth/setup",                # 2 回目のセットアップは不可
                                  {"username": "evil", "password": "passw0rd-2"})
        assert st == 403

        st, s = await client.post("/api/auth/logout")
        assert st == 200
        st, s = await client.get("/api/state")
        assert st == 401

        st, s = await client.post("/api/auth/login",                # 誤パスワード
                                  {"username": "admin", "password": "wrong-password"})
        assert st == 401 and "detail" in s
        st, s = await client.post("/api/auth/login",                # 大文字小文字は正規化して一致
                                  {"username": "ADMIN", "password": PW})
        assert st == 200 and s["user"]["username"] == "admin"
        st, s = await client.get("/api/state")
        assert st == 200
    asyncio.run(go())


def test_setup_display_name_kept_when_given(tmp_path, auth_on, fast_kdf):
    """セットアップで表示名を渡した場合はそのまま保存する (自動上書きしない)。"""
    client = Client(make_app(tmp_path))

    async def go() -> None:
        st, res = await client.post("/api/auth/setup", {"username": "admin", "password": PW,
                                                        "display_name": "運用チーム"})
        assert st == 201 and res["user"]["display_name"] == "運用チーム"
        st, s = await client.get("/api/auth/status")
        assert st == 200 and s["user"]["display_name"] == "運用チーム"
    asyncio.run(go())


def test_login_throttling(tmp_path, auth_on, fast_kdf):
    client = Client(make_app(tmp_path))

    async def go() -> None:
        await setup_admin(client)
        await client.post("/api/auth/logout")
        for _ in range(5):
            st, _ = await client.post("/api/auth/login",
                                      {"username": "admin", "password": "wrong-password"})
            assert st == 401
        st, s = await client.post("/api/auth/login",                # 5 回失敗でロック
                                  {"username": "admin", "password": "wrong-password"})
        assert st == 429 and "30" in s["detail"]
        st, _ = await client.post("/api/auth/login",                # ロック中は正解でも通さない
                                  {"username": "admin", "password": PW})
        assert st == 429
    asyncio.run(go())


def test_session_survives_restart(tmp_path, auth_on, fast_kdf):
    first = Client(make_app(tmp_path))

    async def go() -> None:
        await setup_admin(first)
        assert (await first.get("/api/state"))[0] == 200
        second = Client(make_app(tmp_path))                        # 同一 data を開き直す
        second.cookie = first.cookie
        st, s = await second.get("/api/auth/status")
        assert st == 200 and s["authenticated"] is True            # セッションは永続化される
    asyncio.run(go())


def test_index_served_without_login(tmp_path, auth_on, fast_kdf):
    client = Client(make_app(tmp_path))

    async def go() -> None:
        st, body = await client.get("/")
        assert st == 200 and b'id="login"' in body and b"function boot()" in body
    asyncio.run(go())


def test_secrets_are_hashed_on_disk(tmp_path, auth_on, fast_kdf):
    client = Client(make_app(tmp_path))

    async def go() -> None:
        me = await setup_admin(client, password="sup3r-secret-pw")
        st, d = await client.post(f"/api/users/{me['id']}/apikey")
        assert st == 200 and d["api_key"].startswith("llm_")
        users_txt = (tmp_path / "data" / "users.json").read_text("utf-8")
        assert "sup3r-secret-pw" not in users_txt
        assert d["api_key"] not in users_txt and "pbkdf2_sha256$" in users_txt
        sessions_txt = (tmp_path / "data" / "sessions.json").read_text("utf-8")
        assert "sup3r-secret-pw" not in sessions_txt
    asyncio.run(go())


# ------------------------------------------------------------ ユーザー管理とロール制限


def test_user_crud_and_role_restriction(tmp_path, auth_on, fast_kdf):
    app = make_app(tmp_path)
    admin = Client(app)

    async def go() -> None:
        await setup_admin(admin)
        st, bob = await admin.post("/api/users", {"username": "Bob", "password": "passw0rd-2",
                                                  "display_name": "Bob", "role": "user"})
        assert st == 201 and bob["username"] == "bob" and bob["role"] == "user"
        assert bob["has_api_key"] is False and "password" not in bob
        st, _ = await admin.post("/api/users", {"username": "bob", "password": "passw0rd-2"})
        assert st == 409                                           # 名前のかぶせ
        st, _ = await admin.post("/api/users", {"username": "eve", "password": "passw0rd-2",
                                                "role": "root"})
        assert st == 400                                           # 未知のロール
        st, _ = await admin.post("/api/users", {"username": "eve", "password": "short"})
        assert st == 422                                           # Pydantic の min_length

        user = Client(app)
        st, _ = await user.post("/api/auth/login",
                                {"username": "bob", "password": "passw0rd-2"})
        assert st == 200
        st, s = await user.get("/api/users")                       # 一般ユーザーは閲覧不可
        assert st == 403 and s["code"] == "forbidden"
        st, _ = await user.post("/api/users", {"username": "mallory", "password": "passw0rd-3"})
        assert st == 403                                           # 作成も不可
        st, s = await user.get("/api/state")                       # ユーザーロールは通常 API を使えない
        assert st == 403 and s["code"] == "forbidden"
        st, s = await user.get("/api/simple")                      # 簡易画面の API だけ通る
        assert st == 200 and set(s) >= {"servers", "alerts"}
        st, s = await user.get("/api/auth/status")
        assert (st == 200 and s["is_admin"] is False and s["user"]["role"] == "user"
                and s["simple_only"] is True)

        st, s = await admin.get("/api/users")
        assert st == 200 and len(s["users"]) == 2
        assert s["users"][0]["username"] == "admin"                # 管理者が先頭
        st, up = await admin.put(f"/api/users/{bob['id']}", {"display_name": "Bob 改",
                                                             "role": "admin"})
        assert st == 200 and up["display_name"] == "Bob 改" and up["role"] == "admin"
        st, up = await admin.put(f"/api/users/{bob['id']}", {"enabled": False})
        assert st == 200 and up["enabled"] is False
        st, _ = await user.get("/api/state")                       # 無効化は既存セッションにも効く
        assert st == 401
        st, s = await user.post("/api/auth/login",
                                {"username": "bob", "password": "passw0rd-2"})
        assert st == 403 and "無効" in s["detail"]
        st, _ = await admin.put(f"/api/users/{bob['id']}", {"enabled": True})
        assert st == 200
        st, _ = await user.post("/api/auth/login",
                                {"username": "bob", "password": "passw0rd-2"})
        assert st == 200
        st, _ = await admin.delete(f"/api/users/{bob['id']}")
        assert st == 200
        st, s = await admin.get("/api/users")
        assert len(s["users"]) == 1
        st, _ = await admin.delete(f"/api/users/{bob['id']}")
        assert st == 404
    asyncio.run(go())


def test_last_admin_guards(tmp_path, auth_on, fast_kdf):
    admin = Client(make_app(tmp_path))

    async def go() -> None:
        me = await setup_admin(admin)
        st, s = await admin.put(f"/api/users/{me['id']}", {"enabled": False})
        assert st == 409 and "最後" in s["detail"]
        st, s = await admin.put(f"/api/users/{me['id']}", {"role": "user"})
        assert st == 409
        st, s = await admin.delete(f"/api/users/{me['id']}")
        assert st == 409 and "自分自身" in s["detail"]
        st, second = await admin.post("/api/users", {"username": "second",
                                                     "password": "passw0rd-2", "role": "admin"})
        assert st == 201
        st, _ = await admin.delete(f"/api/users/{second['id']}")    # 別の管理者がいれば削除可
        assert st == 200
        st, _ = await admin.put(f"/api/users/{me['id']}", {"enabled": False})
        assert st == 409                                            # 再び最後の管理者に戻る
    asyncio.run(go())


def test_password_changes_revoke_sessions(tmp_path, auth_on, fast_kdf):
    app = make_app(tmp_path)
    admin = Client(app)

    async def go() -> None:
        me = await setup_admin(admin)
        st, bob = await admin.post("/api/users", {"username": "bob", "password": "passw0rd-2"})
        assert st == 201
        user = Client(app)
        st, _ = await user.post("/api/auth/login",
                                {"username": "bob", "password": "passw0rd-2"})
        assert st == 200
        st, s = await user.post("/api/auth/password", {"current_password": "wrong-password",
                                                       "password": "new-password-1"})
        assert st == 401 and "現在" in s["detail"]
        st, s = await user.post("/api/auth/password", {"current_password": "passw0rd-2",
                                                       "password": "new-password-1"})
        assert st == 200 and s["relogin"] is True
        st, _ = await user.get("/api/state")                        # 自分のセッションも失効
        assert st == 401
        st, _ = await user.post("/api/auth/login",
                                {"username": "bob", "password": "new-password-1"})
        assert st == 200
        st, _ = await admin.post(f"/api/users/{bob['id']}/password", {"password": "reset-pass-1"})
        assert st == 200
        st, _ = await user.get("/api/state")                        # 管理者リセットでも失効
        assert st == 401
        st, _ = await user.post("/api/auth/login",
                                {"username": "bob", "password": "new-password-1"})
        assert st == 401
        st, _ = await user.post("/api/auth/login",
                                {"username": "bob", "password": "reset-pass-1"})
        assert st == 200
        st, _ = await admin.put(f"/api/users/{me['id']}", {"password": "admin-pass-9"})
        assert st == 200
        st, _ = await admin.get("/api/state")                       # 管理者自身も張り直しが必要
        assert st == 401
    asyncio.run(go())


# ------------------------------------------------------------------ API キー


def test_api_key_auth_and_rotation(tmp_path, auth_on, fast_kdf):
    app = make_app(tmp_path)
    admin = Client(app)

    async def go() -> None:
        me = await setup_admin(admin)
        st, u = await admin.post("/api/users", {"username": "script", "password": "passw0rd-4",
                                                "role": "maintainer"})
        assert st == 201
        st, d = await admin.post(f"/api/users/{u['id']}/apikey")
        assert st == 200 and d["api_key"].startswith("llm_")
        key = d["api_key"]
        anon = Client(app)
        st, _ = await anon.get("/api/state", use_cookie=False, api_key=key)
        assert st == 200                                           # Cookie 無しでも API キーで通る
        st, s = await anon.get("/api/auth/status", use_cookie=False, api_key=key)
        assert s["authenticated"] is True and s["user"]["username"] == "script"
        assert s["is_admin"] is False
        st, s = await anon.get("/api/users", use_cookie=False, api_key=key)
        assert st == 403                                           # ロール制限はそのまま効く
        st, d = await admin.post(f"/api/users/{me['id']}/apikey")   # 管理者キーなら管理者機能も
        assert st == 200
        st, _ = await anon.get("/api/users", use_cookie=False, api_key=d["api_key"])
        assert st == 200
        st, d2 = await admin.post(f"/api/users/{u['id']}/apikey")   # 再生成で旧キーは無効
        assert st == 200 and d2["api_key"] != key
        st, _ = await anon.get("/api/state", use_cookie=False, api_key=key)
        assert st == 401
        st, _ = await anon.get("/api/state", use_cookie=False, api_key=d2["api_key"])
        assert st == 200
        st, _ = await admin.delete(f"/api/users/{u['id']}/apikey")
        assert st == 200
        st, _ = await anon.get("/api/state", use_cookie=False, api_key=d2["api_key"])
        assert st == 401
        st, s = await admin.get("/api/users")
        assert [x for x in s["users"] if x["username"] == "script"][0]["has_api_key"] is False
        st, _ = await anon.get("/api/state", use_cookie=False, api_key="llm_deadbeef")
        assert st == 401
    asyncio.run(go())


# ------------------------------------------------------------ 環境変数 / 無効化モード


def test_env_bootstrap_creates_admin(tmp_path, auth_on, fast_kdf, monkeypatch):
    monkeypatch.setenv("LLM_LAUNCHER_ADMIN_USER", "root")
    monkeypatch.setenv("LLM_LAUNCHER_ADMIN_PASSWORD", "env-pass-123")
    client = Client(make_app(tmp_path))

    async def go() -> None:
        st, s = await client.get("/api/auth/status")
        assert st == 200 and s["needs_setup"] is False and s["authenticated"] is False
        st, s = await client.post("/api/auth/login",
                                  {"username": "root", "password": "env-pass-123"})
        assert st == 200 and s["user"]["role"] == "admin"
        assert s["user"]["display_name"] == "root"   # 環境変数でも表示名は自動注入しない
    asyncio.run(go())


def test_env_bootstrap_not_reapplied(tmp_path, auth_on, fast_kdf, monkeypatch):
    """手動で管理者を作った後、環境変数だけで別管理者を復活させない。"""
    first = Client(make_app(tmp_path))
    monkeypatch.setenv("LLM_LAUNCHER_ADMIN_USER", "root")
    monkeypatch.setenv("LLM_LAUNCHER_ADMIN_PASSWORD", "env-pass-123")

    async def go() -> None:
        await setup_admin(first)
        again = Client(make_app(tmp_path))
        st, s = await again.get("/api/auth/status")
        assert st == 200 and s["needs_setup"] is False
        st, _ = await again.post("/api/auth/login",
                                 {"username": "root", "password": "env-pass-123"})
        assert st == 401
    asyncio.run(go())


def test_auth_disabled_mode(tmp_path, fast_kdf):
    """conftest が LLM_LAUNCHER_AUTH=0 なので認証なしで従来どおり動く。"""
    client = Client(make_app(tmp_path))

    async def go() -> None:
        st, s = await client.get("/api/auth/status")
        assert st == 200 and s["auth_enabled"] is False
        assert s["authenticated"] is True and s["is_admin"] is True and s["user"] is None
        st, s = await client.get("/api/state")
        assert st == 200 and "profiles" in s
        st, s = await client.get("/api/users")                     # 無効時はユーザー管理不可
        assert st == 400 and "認証" in s["detail"]
        st, s = await client.post("/api/auth/setup", {"username": "admin", "password": PW})
        assert st == 400
    asyncio.run(go())
