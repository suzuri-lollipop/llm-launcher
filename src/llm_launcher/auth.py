"""ユーザー認証とアカウント管理 (追加依存なし・標準ライブラリのみ)。

設計:
  - パスワードは PBKDF2-HMAC-SHA256 + 16 バイトソルトでハッシュ化して保存する
    (平文・可逆形式は一切残さない。比較は hmac.compare_digest)
  - セッションはサーバー側トークン (secrets.token_urlsafe) を data/sessions.json に
    永続化するため、launcher を再起動してもログイン状態は失われない。
    全セッションの一括失効 = このファイルを消すだけ。
  - ブラウザは HttpOnly Cookie、スクリプト等からはユーザー毎の API キーを
    `Authorization: Bearer <key>` で使える (既存の「API をスクリプトから叩ける」運用維持)。

保存形式:
  data/users.json    [{id, username, display_name, role, enabled, password, api_key_hash,
                       created_at, updated_at, last_login_at}]
  data/sessions.json [{token, user_id, created_at, expires_at, remember, agent}]
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from typing import Any

from .store import JsonStore, new_id, now

COOKIE_NAME = "llm_session"
TOKEN_TTL = 12 * 3600            # 「ログイン状態を保持」OFF のとき
REMEMBER_TTL = 30 * 24 * 3600    # ON のとき
PBKDF2_ITERS = 200_000
MIN_PASSWORD = 8
MAX_PASSWORD = 256
ROLES = ("admin", "user")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")

# ログイン失敗のスロットル (総当たり抑止): 同一 IP+ユーザー名で FAIL_LIMIT 回失敗 → LOCKOUT 秒凍結
FAIL_LIMIT = 5
FAIL_WINDOW = 300.0
LOCKOUT = 30.0


class AuthError(Exception):
    """API 層で HTTPException へ変換する認証エラー (status と日本語メッセージ)。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ------------------------------------------------------------ パスワードハッシュ

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERS)
    return f"pbkdf2_sha256${PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = str(stored).split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


_DUMMY: str | None = None


def _dummy_hash() -> str:
    """存在しないユーザーへの試行でも同じコストのハッシュ比較をさせるための偽ハッシュ
    (ユーザー名のみ当たり検証の時間差・列挙抑止)。初回のみ生成。"""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password("dummy-" + secrets.token_hex(16))
    return _DUMMY


# ------------------------------------------------------------------ 入力検証

def validate_username(username: str) -> str:
    u = str(username or "").strip().lower()
    if not USERNAME_RE.fullmatch(u):
        raise AuthError(400, "ユーザー名は 2〜32 文字の小文字英数字と . _ - (先頭は英数字) で指定してください")
    return u


def validate_password(password: str) -> str:
    p = str(password or "")
    if len(p) < MIN_PASSWORD:
        raise AuthError(400, f"パスワードは {MIN_PASSWORD} 文字以上にしてください")
    if len(p) > MAX_PASSWORD:
        raise AuthError(400, f"パスワードは {MAX_PASSWORD} 文字以下にしてください")
    return p


def validate_display_name(display_name: str) -> str:
    return str(display_name or "").strip()[:60]


def validate_role(role: str) -> str:
    r = str(role or "user").strip().lower()
    if r not in ROLES:
        raise AuthError(400, "ロールは admin / user のいずれかを指定してください")
    return r


# ------------------------------------------------------------------ AuthManager

class AuthManager:
    """ユーザー / セッション / API キーの管理。JSON ストア 2 つを権威に使う。"""

    def __init__(self, users_store: JsonStore, sessions_store: JsonStore):
        self.users = users_store
        self.sessions = sessions_store
        self._fails: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}

    # ------------------------------------------------------------- 参照

    @staticmethod
    def public(u: dict[str, Any] | None) -> dict[str, Any] | None:
        """API へ出す形 (パスワードハッシュ・API キーハッシュを落とす)。"""
        if not u:
            return None
        return {
            "id": u["id"],
            "username": u.get("username", ""),
            "display_name": u.get("display_name", ""),
            "role": u.get("role", "user"),
            "enabled": bool(u.get("enabled", True)),
            "has_api_key": bool(u.get("api_key_hash")),
            "created_at": u.get("created_at"),
            "updated_at": u.get("updated_at"),
            "last_login_at": u.get("last_login_at"),
        }

    def list_users(self) -> list[dict[str, Any]]:
        rows = [self.public(u) for u in self.users.load()]
        rows.sort(key=lambda r: (r["role"] != "admin", str(r["username"]).lower()))
        return [r for r in rows if r]

    def has_users(self) -> bool:
        return bool(self.users.load())

    def count_sessions(self, user_id: str) -> int:
        cutoff = now()
        return sum(1 for s in self.sessions.load()
                   if s.get("user_id") == user_id and s.get("expires_at", 0) > cutoff)

    @staticmethod
    def _find(data: list[dict[str, Any]], uid: str) -> dict[str, Any] | None:
        return next((u for u in data if u.get("id") == uid), None)

    @staticmethod
    def _find_name(data: list[dict[str, Any]], username: str) -> dict[str, Any] | None:
        return next((u for u in data if u.get("username") == username), None)

    @staticmethod
    def _guard_last_admin(data: list[dict[str, Any]], target: dict[str, Any], action: str) -> None:
        """最後の管理者を消すと誰も管理できなくなるため防ぐ。"""
        if target.get("role") != "admin":
            return
        others = [u for u in data if u.get("role") == "admin"
                  and u.get("enabled", True) and u.get("id") != target.get("id")]
        if not others:
            raise AuthError(409, f"最後の管理者を{action}できません。先に別の管理者を作成してください")

    # --------------------------------------------------------- ユーザー作成

    def create_user(self, username: str, password: str, display_name: str = "",
                    role: str = "user", enabled: bool = True) -> dict[str, Any]:
        name = validate_username(username)
        pwd = validate_password(password)
        rl = validate_role(role)
        ts = now()

        def _mut(data: list[dict[str, Any]]) -> dict[str, Any]:
            if self._find_name(data, name):
                raise AuthError(409, f"ユーザー名 {name} は既に使われています")
            u = {
                "id": new_id("u"),
                "username": name,
                "display_name": validate_display_name(display_name) or name,
                "role": rl,
                "enabled": bool(enabled),
                "password": hash_password(pwd),
                "api_key_hash": "",
                "created_at": ts,
                "updated_at": ts,
                "last_login_at": None,
            }
            data.append(u)
            return dict(u)

        return self.public(self.users.mutate(_mut))

    def create_first_admin(self, username: str, password: str,
                           display_name: str = "") -> dict[str, Any]:
        """初回セットアップ (ユーザー 0 人のときだけ)。

        表示名が未指定なら create_user 側でユーザー名にフォールバックする
        (「管理者」等を自動注入すると、ロール表示と並んだ UI で重複して見える)。
        """
        if self.has_users():
            raise AuthError(403, "初期セットアップは既に完了しています")
        return self.create_user(username, password, display_name, role="admin")

    def bootstrap_from_env(self) -> dict[str, Any] | None:
        """LLM_LAUNCHER_ADMIN_USER / LLM_LAUNCHER_ADMIN_PASSWORD があれば初期管理者を作る。

        既存ユーザーが 1 人でもいれば何もしない (環境変数の再適用で権限を復活させない)。
        表示名は作らない (未指定 = ユーザー名)。
        """
        if self.has_users():
            return None
        name = (os.environ.get("LLM_LAUNCHER_ADMIN_USER") or "").strip()
        pwd = os.environ.get("LLM_LAUNCHER_ADMIN_PASSWORD") or ""
        if not name or not pwd:
            return None
        return self.create_user(name, pwd, "", role="admin")


    # --------------------------------------------------------- ユーザー更新

    def update_user(self, uid: str, changes: dict[str, Any],
                    actor_id: str | None = None) -> dict[str, Any]:
        """部分更新 (username / display_name / role / enabled / password)。"""

        def _mut(data: list[dict[str, Any]]) -> dict[str, Any]:
            u = self._find(data, uid)
            if not u:
                raise AuthError(404, "ユーザーが見つかりません")
            role = validate_role(changes.get("role") or u.get("role", "user"))
            en = changes.get("enabled")
            enabled = bool(u.get("enabled", True)) if en is None else bool(en)
            if not enabled:
                self._guard_last_admin(data, u, "無効化")
            if role != "admin":
                self._guard_last_admin(data, u, "降格")
            if changes.get("username") is not None:
                name = validate_username(changes["username"])
                dup = self._find_name(data, name)
                if dup and dup.get("id") != uid:
                    raise AuthError(409, f"ユーザー名 {name} は既に使われています")
                u["username"] = name
            if changes.get("display_name") is not None:
                u["display_name"] = validate_display_name(changes["display_name"]) or u["username"]
            u["role"] = role
            u["enabled"] = bool(enabled)
            if changes.get("password"):
                u["password"] = hash_password(validate_password(changes["password"]))
            u["updated_at"] = now()
            return dict(u)

        out = self.users.mutate(_mut)
        if changes.get("password"):
            self.revoke_all(uid)          # パスワードを変えたら既存セッションは張り直させる
        return self.public(out)

    def set_password(self, uid: str, password: str) -> dict[str, Any]:
        return self.update_user(uid, {"password": validate_password(password)})

    def change_own_password(self, uid: str, current: str, new_password: str) -> None:
        u = self._find(self.users.load(), uid)
        if not u:
            raise AuthError(404, "ユーザーが見つかりません")
        if not verify_password(str(current or ""), u.get("password", "")):
            raise AuthError(401, "現在のパスワードが正しくありません")
        self.set_password(uid, new_password)

    def delete_user(self, uid: str, actor_id: str | None = None) -> None:
        if actor_id and actor_id == uid:
            raise AuthError(409, "自分自身は削除できません")

        def _mut(data: list[dict[str, Any]]) -> None:
            u = self._find(data, uid)
            if not u:
                raise AuthError(404, "ユーザーが見つかりません")
            self._guard_last_admin(data, u, "削除")
            data.remove(u)

        self.users.mutate(_mut)
        self.revoke_all(uid)

    # ------------------------------------------------------------- API キー

    def rotate_api_key(self, uid: str) -> str:
        """API キーを発行 (再生成) する。平文はこの戻り値でのみ返る — 保存は SHA-256。"""
        key = "llm_" + secrets.token_hex(20)

        def _mut(data: list[dict[str, Any]]) -> None:
            u = self._find(data, uid)
            if not u:
                raise AuthError(404, "ユーザーが見つかりません")
            u["api_key_hash"] = _hash_key(key)
            u["updated_at"] = now()

        self.users.mutate(_mut)
        return key

    def revoke_api_key(self, uid: str) -> None:
        def _mut(data: list[dict[str, Any]]) -> None:
            u = self._find(data, uid)
            if not u:
                raise AuthError(404, "ユーザーが見つかりません")
            u["api_key_hash"] = ""
            u["updated_at"] = now()

        self.users.mutate(_mut)

    # ------------------------------------------------------------- ログイン

    @staticmethod
    def _throttle_key(key: str, username: str) -> str:
        return f"{key}|{str(username or '').strip().lower()}"

    def _check_locked(self, tk: str) -> None:
        until = self._locked.get(tk, 0.0)
        left = until - time.time()
        if left > 0:
            raise AuthError(429, f"ログイン失敗が連続 {FAIL_LIMIT} 回です。"
                                 f"{int(left) + 1} 秒後に再試行してください")

    def _note_fail(self, tk: str) -> None:
        t = time.time()
        hist = [x for x in self._fails.get(tk, []) if t - x < FAIL_WINDOW]
        hist.append(t)
        if len(hist) >= FAIL_LIMIT:
            self._locked[tk] = t + LOCKOUT
            hist = []
        self._fails[tk] = hist

    def _clear_fails(self, tk: str) -> None:
        self._fails.pop(tk, None)
        self._locked.pop(tk, None)

    def authenticate(self, username: str, password: str, key: str = "") -> dict[str, Any]:
        """ユーザー名+パスワード認証。成否に関わらず PBKDF2 を 1 回実行し、
        ユーザー名の存在を応答時間・メッセージから推測させない。"""
        tk = self._throttle_key(key, username)
        self._check_locked(tk)
        name = str(username or "").strip().lower()
        u = self._find_name(self.users.load(), name)
        ok = verify_password(str(password or ""),
                             (u or {}).get("password", "") or _dummy_hash())
        if not u or not ok:
            self._note_fail(tk)
            raise AuthError(401, "ユーザー名またはパスワードが正しくありません")
        if not u.get("enabled", True):
            self._note_fail(tk)
            raise AuthError(403, "このアカウントは無効化されています。管理者に問い合わせてください")
        self._clear_fails(tk)

        def _mut(data: list[dict[str, Any]]) -> None:
            row = self._find(data, u["id"])
            if row:
                row["last_login_at"] = now()

        self.users.mutate(_mut)
        return dict(u)

    # ---------------------------------------------------------- セッション

    def issue(self, user_id: str, remember: bool = False, agent: str = "") -> str:
        ttl = REMEMBER_TTL if remember else TOKEN_TTL
        token = secrets.token_urlsafe(32)

        def _mut(data: list[dict[str, Any]]) -> None:
            cutoff = now()
            data[:] = [s for s in data if s.get("expires_at", 0) > cutoff]   # 期限切れ掃除
            data.append({"token": token, "user_id": user_id, "created_at": cutoff,
                         "expires_at": cutoff + ttl, "remember": bool(remember),
                         "agent": str(agent or "")[:120]})

        self.sessions.mutate(_mut)
        return token

    def resolve(self, token: str) -> dict[str, Any] | None:
        """セッショントークン → ユーザー。期限切れ / 無効化 / 削除済みなら None。"""
        if not token:
            return None
        s = next((x for x in self.sessions.load() if x.get("token") == token), None)
        if not s:
            return None
        if s.get("expires_at", 0) <= now():
            self.revoke(token)
            return None
        u = self._find(self.users.load(), s.get("user_id"))
        if not u or not u.get("enabled", True):
            self.revoke(token)
            return None
        return dict(u)

    def revoke(self, token: str) -> None:
        def _mut(data: list[dict[str, Any]]) -> None:
            data[:] = [s for s in data if s.get("token") != token]

        self.sessions.mutate(_mut)

    def revoke_all(self, user_id: str) -> None:
        def _mut(data: list[dict[str, Any]]) -> None:
            data[:] = [s for s in data if s.get("user_id") != user_id]

        self.sessions.mutate(_mut)

    def authenticate_token(self, raw: str) -> dict[str, Any] | None:
        """Cookie のセッショントークンと API キー (Bearer) のどちらでも解決する。"""
        if not raw:
            return None
        u = self.resolve(raw)
        if u:
            return u
        h = _hash_key(raw)
        u = next((x for x in self.users.load() if x.get("api_key_hash") == h), None)
        if u and u.get("enabled", True):
            return dict(u)
        return None

