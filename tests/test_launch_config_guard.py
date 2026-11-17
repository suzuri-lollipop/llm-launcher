"""config 主体バックエンド (Strata / Maya) の起動ガードの回帰テスト。

2026-11-16 の実害: config 未紐付けのプロファイル (ウィザードのモデルジョブが
完了しなかったもの) を起動したら、純正 maya.sh が --config 無しで「直近に使った
config」(maya-ds4.json / gpu 2 枚) を黙って拾い、別モデル / 別 GPU 構成で起動した。
ユーザーは「GPU 分割数 1」を指定していたのに 2 枚で計測 / 起動されていた。

見張る対象:
  1. POST /api/sessions は config 無しの maya / strata / strata-fork プロファイルを
     400 で拒否する (セッションを作らない / 起動しない)
  2. restart も停止する前に同じガードで拒否する (止めだけしない)
  3. custom_only (独自コマンド) はガードの対象外
"""
from __future__ import annotations

import asyncio

from test_auth_users import Client, make_app

GUARD_MSG = "紐付いていません"


async def _profile(c: Client, backend: str, values: dict | None = None,
                   custom_only: bool = False, command: str = "") -> dict:
    st, p = await c.post("/api/profiles", {"name": backend, "backend": backend,
                                           "values": values or {},
                                           "custom_only": custom_only, "command": command})
    assert st == 201, p
    return p


def test_maya_without_config_refused(tmp_path):
    app = make_app(tmp_path)
    c = Client(app)

    async def go() -> None:
        p = await _profile(c, "maya", {"tp": "1", "parallel": "1", "port": "1919"})
        st, r = await c.post("/api/sessions", {"profile_id": p["id"]})
        assert st == 400 and GUARD_MSG in r["detail"], r
        st, s = await c.get("/api/state")
        assert st == 200 and not s["sessions"]   # 起動していない

    asyncio.run(go())


def test_strata_without_config_refused(tmp_path):
    app = make_app(tmp_path)
    c = Client(app)

    async def go() -> None:
        for bid in ("strata", "strata-fork"):
            p = await _profile(c, bid, {"engine": "strata"})
            st, r = await c.post("/api/sessions", {"profile_id": p["id"]})
            assert st == 400 and GUARD_MSG in r["detail"], (bid, r)

    asyncio.run(go())


def test_restart_refused_before_stopping(tmp_path):
    app = make_app(tmp_path)
    c = Client(app)

    async def go() -> None:
        p = await _profile(c, "maya", {})
        app.state.manager.sessions["s_test"] = {
            "id": "s_test", "profile_id": p["id"], "profile_name": p["name"],
            "backend": "maya", "status": "running", "ready": True,
            "pid": 2_000_000_000, "port": 1919, "started_at": 1.0,
        }
        st, r = await c.post("/api/sessions/s_test/restart", {})
        assert st == 400 and GUARD_MSG in r["detail"], r
        # ガードは停止の前に働く — 稼働中セッションを止めだけにしない
        assert app.state.manager.sessions["s_test"]["status"] == "running"

    asyncio.run(go())


def test_custom_only_is_exempt(tmp_path):
    """独自コマンドのプロファイルは config を要求しない (ガードは素通りする)。"""
    app = make_app(tmp_path)
    c = Client(app)

    async def go() -> None:
        p = await _profile(c, "maya", {}, custom_only=True, command="true")
        st, r = await c.post("/api/sessions", {"profile_id": p["id"]})
        assert not (st == 400 and GUARD_MSG in str(r.get("detail", ""))), r

    asyncio.run(go())
