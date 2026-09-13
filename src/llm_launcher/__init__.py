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
    args = parser.parse_args(argv)

    import uvicorn

    from .app import create_app, lan_ip

    root = find_root()
    app = create_app(root)
    print(f"llm-launcher v{__version__}")
    print(f"  project root : {root}")
    print(f"  local        : http://127.0.0.1:{args.port}")
    print(f"  network      : http://{lan_ip()}:{args.port}  <- スマホからはこのURL")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
