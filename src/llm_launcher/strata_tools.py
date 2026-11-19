"""Strata の「Python パッケージ導入」部分だけを純正 setup.py 経由で走らせる薄い driver。

launcher のセットアップ (installer.INSTALL_SPECS["strata"]) の tools step から、
`cwd = module/strata` (0.2.16+ は terminal/) で `.venv/bin/python <launcher>/strata_tools.py`
として実行される。Maya の maya_tools.py と同じ手法:

  * ① が引き受けるのは家系に依存しない機械部分だけ。純正 setup.sh / setup.py は
    「PC 確認 → 対話質問 → .venv → pip → エンジン (既製受信 / ビルド) → モデル DL →
    strata-<model>.json 生成 → 起動」を一体でやる対話インストーラなので、そのうちの
    **pip_install(runtime_packages())** (pinned requirements.txt + textual) だけを呼ぶ。
    stamp (.venv/.strata-pip.json) 管理は純正実装そのまま (同じリストなら入れ直さない)。
  * **エンジン (既製受信 / ローカルビルド / CUDA ホイール) とモデルダウンロードは ② の仕事**:
    純正 setup.py --yes --no-start が engine_step で純正どおり行う (Maya の家系別
    エンジンビルドが ② の中なのと同じ立場)。① はエンジンに触らない。

0.2.16 は Terminal 部分 (setup.py / strata.py / serve / requirements.txt / .venv) を
リポジトリ直下から terminal/ へ移した。driver はそれを自前で解決する (純正 setup.sh の
移行と同じ見立て)。requirements.txt のパスは純正が知るので launcher 側は持たない。

純正実装の関数名 (pip_install / runtime_packages / requirement_lines / PY_PACKAGES) への
結合なので、upstream の bump で driver が落ちたらジョブログに正直に出る — そのときは
`cd module/strata && ./setup.sh` 1 回が正 (.venv もパッケージも在ればそこは素通りする)。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def fail(msg: str, hint: str = "") -> int:
    print(f"strata_tools: {msg}")
    if hint:
        print(f"strata_tools: → {hint}")
    return 1


def main() -> int:
    root = Path.cwd()
    # 0.2.16 レイアウト: terminal/ に setup.py が在ればそこが Terminal の本体。
    # installer は setup_dir を cwd にしてくるが、driver 単体でも解決できるようにする。
    d = root / "terminal" if (root / "terminal" / "setup.py").is_file() else root
    if not ((d / "setup.py").is_file() and (d / "serve" / "server.py").is_file()):
        return fail("Strata リポジトリ (module/strata または terminal/) を cwd にして実行してください",
                    "launcher のセットアップジョブから自動で呼ばれます")
    os.chdir(d)   # 純正 setup.sh と同じく setup.py の在るディレクトリで実行する
    sys.path.insert(0, str(d))
    try:
        import setup as S
    except Exception as e:  # noqa: BLE001 — 純正 setup.py の import 失敗は正直に
        return fail(f"純正 setup.py を import できませんでした: {e}",
                    "module/strata を更新するか (git submodule update --init) ./setup.sh を 1 回")
    pip_install = getattr(S, "pip_install", None)
    if not callable(pip_install):
        return fail("この setup.py に pip_install がありません (純正と異なる版です)",
                    "module/strata を更新するか ./setup.sh を 1 回実行してください")
    # パッケージ一覧は純正の選び方そのまま: runtime_packages (0.2.16+) →
    # pinned requirements.txt → PY_PACKAGES (古い版の fallback)
    if callable(getattr(S, "runtime_packages", None)):
        pkgs = S.runtime_packages()
    elif callable(getattr(S, "requirement_lines", None)) \
            and getattr(S, "REQUIREMENTS", None) is not None and S.REQUIREMENTS.exists():
        pkgs = S.requirement_lines()
    else:
        pkgs = list(getattr(S, "PY_PACKAGES", []))
    if not pkgs:
        return fail("パッケージ一覧を読めませんでした (runtime_packages / requirements.txt / PY_PACKAGES)",
                    "./setup.sh を 1 回実行してください")
    print("strata_tools: 純正 pip step を実行します — Python パッケージ (.venv) + stamp .venv/.strata-pip.json")
    print("strata_tools: (エンジンの受信 / ビルドとモデルダウンロードは ② のモデル構成で行われます)")
    pip_install(pkgs, "numpy, jinja2, regex, pyyaml, tqdm, requests, pillow, psutil, textual (the screens)")
    print("strata_tools: 完了")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("strata_tools: 中断されました")
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 — ジョブログに traceback の末尾でなく原因を残す
        print(f"strata_tools: 予期しないエラー: {type(e).__name__}: {e}")
        sys.exit(1)
