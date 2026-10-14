"""llm-launcher: vLLM / SGLang / llama.cpp / FreeToken をWeb UIで管理するローンチャー。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

__version__ = "0.1.0"


def find_root() -> Path:
    """プロジェクトルート (module/ を含むディレクトリ) を探す。"""
    env = os.environ.get("LLM_LAUNCHER_ROOT")
    if env:
        return Path(env).resolve()
    cur = Path.cwd()
    for cand in [cur, *cur.parents]:
        if (cand / "module").is_dir() and (cand / "pyproject.toml").exists():
            return cand
    return cur


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="llm-launcher",
        description="LLM バックエンド (vLLM / SGLang / llama.cpp / FreeToken) 管理サーバー",
    )
    parser.add_argument("--host", default=os.environ.get("LLM_LAUNCHER_HOST", "0.0.0.0"),
                        help="待ち受けホスト (既定: 0.0.0.0)")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("LLM_LAUNCHER_PORT", "8036")),
                        help="待ち受けポート (既定: 8036)")
    parser.add_argument("--no-auth", action="store_true",
                        help="認証を無効化して起動 (開発・単機検証用 / LLM_LAUNCHER_AUTH=0 と同等)")
    args = parser.parse_args(argv)
    if args.no_auth:
        os.environ["LLM_LAUNCHER_AUTH"] = "0"

    import uvicorn

    from .app import create_app, lan_ip

    root = find_root()
    app = create_app(root)
    print(f"llm-launcher v{__version__}")
    print(f"  project root : {root}")
    print(f"  local        : http://127.0.0.1:{args.port}")
    print(f"  network      : http://{lan_ip()}:{args.port}  <- スマホからはこのURL")
    auth = getattr(app.state, "auth", None)
    if auth is not None:
        if not getattr(app.state, "auth_enabled", True):
            print("  auth         : 無効 (--no-auth / LLM_LAUNCHER_AUTH=0)")
        elif not auth.has_users():
            print("  auth         : 初回セットアップ待ち — ブラウザで上記 URL を開いて管理者を作成してください")
        else:
            users = auth.list_users()
            names = ", ".join(u["username"] for u in users[:5])
            print(f"  auth         : 有効 (ユーザー {len(users)} 名: {names})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
