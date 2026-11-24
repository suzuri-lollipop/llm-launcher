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
    from .shellenv import apply_shell_env, describe_applied

    # サービスは .bashrc を読まない: プロキシ / モデル置き場 (HF_HOME /
    # HF_HUB_CACHE / FREETOKEN_DATA_DIR) が環境に無いときだけシェル設定から
    # 補完する (明示 env が優先)。create_app より前 = スキャン / 子プロセス /
    # huggingface_hub のキャッシュ解決すべてに効く。
    applied = apply_shell_env()

    root = find_root()
    # 実行データは data/ (gitignore 済み) に集約する: HF キャッシュと fork の
    # モデル置き場が明示設定 (env / シェル設定) で無ければ data/ 配下を既定にする。
    # 旧置き場からの移行は起動時に datadir.migrate_data_layout が行う。
    if not os.environ.get("HF_HOME", "").strip() and not os.environ.get("HF_HUB_CACHE", "").strip():
        os.environ["HF_HOME"] = str(root / "data" / "hf")
    if not os.environ.get("FREETOKEN_DATA_DIR", "").strip():
        os.environ["FREETOKEN_DATA_DIR"] = str(root / "data" / "Freetoken-data")
    app = create_app(root)
    print(f"llm-launcher v{__version__}")
    print(f"  project root : {root}")
    print(f"  data         : {root / 'data'} (モデル / 起動 JSON・sh / HF キャッシュ)")
    if applied:
        print(f"  shell env    : ~/.bashrc 等から補完 (サービスはシェル設定を読まないため): "
              f"{describe_applied(applied)}")
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
