"""Maya の「Python パッケージ + llama.cpp ソース」部分だけを純正 tools_step 経由で走らせる薄い driver。

launcher のセットアップ (installer.INSTALL_SPECS["maya"]) の tools step から、
`cwd = module/maya` で `.venv/bin/python <launcher>/maya_tools.py` として実行される。

背景: Maya の純正 maya.sh / maya.py は「PC 確認 → 家系/モデル/コンテキストの対話質問 →
.venv → pip (PY_PACKAGES) → llama.cpp ソース → エンジンビルド (家系別) → モデル DL →
pack → maya-<model>.json 生成 → 起動」を一体でやる対話インストーラ。GUI の ① で引き受ける
のは機械部分のうち **家系に依存しない tools_step だけ**:

  * PY_PACKAGES (numpy / jinja2 / regex / pyyaml / tqdm / requests / cmake / ninja /
    pillow / psutil / jsonschema) を .venv へ — 純正 pip_install の stamp
    (.venv/.strata-pip.json) 管理そのまま (同じリストなら入れ直さない)
  * llama.cpp のソースを pinned commit で third_party/llama.cpp へ (純正 get_llama_cpp:
    zip 受信・再開対応。エンジンビルドの ggml と pack 作成の gguf-py が読む)

エンジンビルド (build/maya / maya-ds4 / maya-mm3 / maya-hy4) は家系ごとに異なるため
① では行わず、② のモデル構成 (maya.py --yes --no-start --download-model) の中で
純正どおり走る (在ればスキップ)。モデルダウンロードも ② の仕事。

純正実装の関数名 (tools_step / PY_PACKAGES) への結合なので、upstream の bump で driver が
落ちたらジョブログに正直に出る — そのときは `cd module/maya && ./maya.sh` 1 回が正
(.venv も tools も在ればそこは素通りする)。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def fail(msg: str, hint: str = "") -> int:
    print(f"maya_tools: {msg}")
    if hint:
        print(f"maya_tools: → {hint}")
    return 1


def main() -> int:
    root = Path.cwd()
    if not ((root / "maya.py").is_file() and (root / "setup.py").is_file()
            and (root / "serve" / "server.py").is_file()):
        return fail("Maya リポジトリ直下 (module/maya) を cwd にして実行してください",
                    "launcher のセットアップジョブから自動で呼ばれます")
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "tools"))
    try:
        import maya as M
    except Exception as e:  # noqa: BLE001 — 純正 maya.py の import 失敗は正直に
        return fail(f"純正 maya.py を import できませんでした: {e}",
                    "module/maya を更新するか (git submodule update --init) ./maya.sh を 1 回")
    if not callable(getattr(M, "tools_step", None)):
        return fail("この maya.py に tools_step がありません (純正と異なる版です)",
                    "module/maya を更新するか ./maya.sh を 1 回実行してください")
    print("maya_tools: 純正 tools_step を実行します — Python パッケージ (.venv) + llama.cpp ソース")
    print("maya_tools: (エンジンビルドとモデルダウンロードは ② のモデル構成で行われます)")
    # --yes 相当: ダウンロード確認は既定 (y) で確定。llama.cpp は純正の pinned commit 先
    a = argparse.Namespace(yes=True, llama_dir=None)
    llama = M.tools_step(a)
    print(f"maya_tools: 完了 — llama.cpp: {llama}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("maya_tools: 中断されました")
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 — ジョブログに traceback の末尾でなく原因を残す
        print(f"maya_tools: 予期しないエラー: {type(e).__name__}: {e}")
        sys.exit(1)
