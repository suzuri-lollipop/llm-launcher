"""ロールと機能権限 (capability) の定義 — 認可と UI 表示が同じキーを見るための単一の源。

ロール 3 段階:
  - admin       管理者   : 全機能 + メンテナーへの開示 ON/OFF + ユーザー管理
  - maintainer メンテナー : 起動・停止などの基本操作は常に可、それ以外は管理者が
                          settings.json で開示を切り替える (既定は閲覧系のみ ON)
  - user       ユーザー: 簡易画面 (/simple + /api/simple*) のみ。設定値やログは読まない

保存先: data/settings.json
  {"roles": {"maintainer": {"dashboard": true, ...}},
   "simple": {"contact": "機械に詳しい人 内線 1234"}}
  既定値は CAPS から復元するため、キーを追加してもマイグレーションは不要
  (未知のキーは無視、欠損キーは既定値で埋める)。
"""

from __future__ import annotations

from typing import Any

ROLE_ADMIN = "admin"
ROLE_MAINTAINER = "maintainer"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_MAINTAINER, ROLE_USER)
ROLE_LABELS = {ROLE_ADMIN: "管理者", ROLE_MAINTAINER: "メンテナー", ROLE_USER: "ユーザー"}
ROLE_RANK = {ROLE_ADMIN: 0, ROLE_MAINTAINER: 1, ROLE_USER: 2}

# 簡易画面に出すプロファイルのフラグ名 (ProfileBody / profiles.json 共通)
SIMPLE_FIELD = "simple_visible"

# required_cap の特殊戻り値
ADMIN_ONLY = "*"      # 管理者のみ (ユーザー管理・ロール設定)
CORE = ""             # メンテナー常に許可 (起動 / 停止 / 再起動 / 一覧取得)
ALL = "~"             # 全ロール許可 (簡易画面と自分のパスワード)
# None = 未知の /api/* → admin のみ (deny by default)

# 機能キー: maintainer への開示を管理者が ON/OFF できる。default = メンテナーの既定値
CAPS: tuple[dict[str, Any], ...] = (
    {"key": "dashboard", "label": "ダッシュボード", "default": True,
     "desc": "ホスト / IP / GPU / リソース使用率 / OOM 保護状態の閲覧"},
    {"key": "logs", "label": "起動ログ", "default": True,
     "desc": "セッションのライブログ閲覧"},
    {"key": "history", "label": "セッション履歴", "default": True,
     "desc": "終了した起動の履歴閲覧と記録削除"},
    {"key": "playground", "label": "プレイグラウンド", "default": True,
     "desc": "起動中サーバーとのチャット (OpenAI 互換 API の透過)"},
    {"key": "config.read", "label": "設定値の閲覧", "default": True,
     "desc": "プロファイルの値・起動コマンド・環境変数。OFF にすると API 側でマスクする"},
    {"key": "bench", "label": "ベンチマーク", "default": False,
     "desc": "組み込みベンチマークの実行と履歴"},
    {"key": "calibrate", "label": "キャリブレーション", "default": False,
     "desc": "Maya / Strata のワンクリック・キャリブレーション (エンジン起動とconfig書き換えを伴う)"},
    {"key": "profiles.edit", "label": "プロファイル編集", "default": False,
     "desc": "新規・編集・削除・インポート・コマンドプレビュー"},
    {"key": "backends.edit", "label": "バックエンド設定", "default": False,
     "desc": "python パスの指定・再検出・HF キャッシュ参照"},
    {"key": "oom.edit", "label": "OOM 対策の設定", "default": False,
     "desc": "保護設定の変更と起動中セッションへの再適用"},
)
CAP_KEYS = tuple(c["key"] for c in CAPS)
CAP_BY_KEY = {c["key"]: c for c in CAPS}

SIMPLE_CONTACT_MAX = 120


# ------------------------------------------------------------------ 権限セット

def default_perms() -> dict[str, bool]:
    """メンテナー用機能権限の既定値。"""
    return {c["key"]: bool(c["default"]) for c in CAPS}


def merged_perms(raw: Any) -> dict[str, bool]:
    """保存済み dict を既定値とマージ (未知キーは無視 / 欠損は既定値)。"""
    out = default_perms()
    if isinstance(raw, dict):
        for k in CAP_KEYS:
            if k in raw:
                out[k] = bool(raw[k])
    return out


def maintainer_perms(settings: Any) -> dict[str, bool]:
    """settings.json → メンテナーの実効権限。"""
    roles = (settings or {}).get("roles") if isinstance(settings, dict) else None
    return merged_perms((roles or {}).get("maintainer") if isinstance(roles, dict) else None)


def merged_simple(settings: Any) -> dict[str, Any]:
    """settings.json → 簡易画面の設定 (管理者が編集する連絡先メッセージ)。"""
    raw = (settings or {}).get("simple") if isinstance(settings, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    return {"contact": str(raw.get("contact") or "").strip()[:SIMPLE_CONTACT_MAX]}


def can(role: str, cap: str, perms: dict[str, bool] | None = None) -> bool:
    """指定ロールが機能キー cap を持つか。admin は常に True、user は常に False。"""
    if role == ROLE_ADMIN:
        return True
    if role == ROLE_MAINTAINER:
        return bool((perms or default_perms()).get(cap, False))
    return False


# ------------------------------------------------------------------ ルール表

def required_cap(method: str, path: str) -> str | None:
    """/api/* の必要権限を決める (ロール判定自体は app.py の auth_guard が行う)。

    戻り値: ADMIN_ONLY / CORE / ALL / 機能キー / None (= 未知 = 管理者のみ)
    """
    m = (method or "GET").upper()
    p = path or ""
    if p.startswith("/api/users") or p.startswith("/api/roles"):
        return ADMIN_ONLY
    if p.startswith("/api/simple") or p == "/api/auth/password":
        return ALL
    if p == "/api/state":
        return CORE
    if p.startswith("/api/sessions"):
        if p.endswith("/log"):
            return "logs"
        if m == "DELETE":
            return "history"          # 履歴レコードの削除
        return CORE                    # 起動 / 停止 / 再起動
    if p in ("/api/system", "/api/oom"):
        return "dashboard" if m == "GET" else "oom.edit"
    if p.startswith("/api/profiles"):
        return CORE if m == "GET" else "profiles.edit"
    if p == "/api/preview" or p.startswith("/api/hf-models"):
        return "profiles.edit"
    if p.startswith("/api/backends"):
        return "backends.edit"
    if p.startswith("/api/playground"):
        return "playground"
    if p.startswith("/api/benchmarks"):
        return "bench"
    if p.startswith("/api/calibrations"):
        # 実行はエンジン起動 + config 書き換え。閲覧も config パス・CLI ログを含むので同じ権限にする
        return "calibrate"
    return None


def redact_profile(p: dict[str, Any]) -> dict[str, Any]:
    """config.read OFF のメンテナーへ出すプロファイル (設定値・コマンドを落とす)。"""
    out = dict(p)
    for k in ("values", "on", "env"):
        out[k] = {}
    for k in ("extra_args", "command", "cwd"):
        out[k] = ""
    out["custom_only"] = False
    out["redacted"] = True
    return out


# ------------------------------------------------------------------ 異常アラート

# 簡易画面の警告対象にする起動経路 (process.VIA_SIMPLE / VIA_UI と同じ値)。
# ベンチマークの自動起動は計測のための一時的な起動なので警告しない。
SIMPLE_VIAS = ("simple", "ui")


def simple_alerts(sessions: list[dict[str, Any]], oom: dict[str, Any] | None = None,
                  known: set[str] | None = None,
                  owner: str | None = None) -> tuple[list[dict[str, Any]], set[str]]:
    """簡易画面に出す異常を新しい順に並べる。

    `sessions` には **プロファイル別の現在インスタンス**を渡す。履歴をそのまま
    渡すと、今は稼働中のサーバーに対して過去の異常終了を警告しにいく。

    `owner` (ログイン中のユーザー ID) を渡すと、**そのユーザーが自分で起こした
    セッション** (簡易画面の起動ボタン / 管理画面の起動・再起動) だけに絞る。
    ベンチマークの自動起動、他のユーザー (管理者含む) の起動、および起動者情報
    を知らない以前の履歴 (キー無し) は警告に出さない。ユーザー概念のない
    認証オフ運用のときだけ `owner=None` で経路判定のみ行う。

    戻り値: (アラート, 観測された全アラート ID)。known を渡すと既出 ID に
    `repeat: true` を付ける (画面側で再通知の音出し・点滅を強めるために使う)。
    """
    out: list[dict[str, Any]] = []
    for s in sessions or []:
        if s.get("status") not in ("failed", "lost"):
            continue
        if s.get("started_via") not in SIMPLE_VIAS:
            continue                          # ベンチの自動起動と旧履歴は黙る
        if owner is not None and s.get("started_by") != owner:
            continue                          # 他人 (管理者含む) の起動は本人に黙る
        out.append({
            "id": f"session:{s.get('id')}",
            "kind": s.get("status"),
            "sid": s.get("id"),
            "profile_id": s.get("profile_id"),
            "profile_name": s.get("profile_name") or "サーバー",
            "detail": str(s.get("error") or "")[:240],
            "at": s.get("finished_at") or s.get("started_at"),
        })
    o = oom or {}
    if o.get("state") == "crit":
        out.append({
            "id": "memory:crit",
            "kind": "memory",
            "sid": None,
            "profile_id": None,
            "profile_name": "この機械",
            "detail": str(o.get("note") or "")[:240],
            "at": o.get("checked_at"),
        })
    out.sort(key=lambda a: a.get("at") or 0, reverse=True)
    ids = {a["id"] for a in out}
    if known:
        for a in out:
            a["repeat"] = a["id"] in known
    return out, ids

