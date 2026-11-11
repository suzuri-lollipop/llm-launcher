"""Strata の「既製エンジンを受信する」部分だけを純正 setup.py 経由で走らせる薄い driver。

launcher のセットアップ (installer.INSTALL_SPECS["strata"]) の engine step から、
`cwd = module/strata` で `.venv/bin/python <launcher>/strata_engine.py` として実行される。

背景: Strata の純正 setup.sh / setup.py は「PC 確認 → モデル/サイズ/コンテキストの
対話質問 → .venv → pip → エンジン (既製受信 or コンパイル) → モデル DL (~70-190 GB) →
strata-<model>.json 生成 → 起動」を一体でやる対話インストーラで、GUI からそのまま
走らせると質問で止まるかモデルを一気に引きに来る。ここで引き受けるのは機械部分の
エンジン受信だけ:

  * engine/strata は公式リリースの zip (strata-linux-x64.zip / strata-windows-x64.zip) を
    純正 get_prebuilt() の経路で受信する — SHA-256 検証と署名フィードのフォールバックは
    upstream 実装そのもの (コピーも再発明もしない)
  * エンジンが読む NVIDIA CUDA ランタイムホイール (cuBLAS ほか) も純正 pip_cuda_libs()。
    これが `.venv/bin/python -m pip` を前提にするので、launcher 側は .venv を標準 venv+pip
    で作っている (spec["venv_pip"])。純正 main と同じく **release エンジンのときだけ**呼ぶ
    (ローカルビルドのエンジンは BUILD.json の cuda_dirs = システム CUDA を読む)
  * 在れば再取得しない。source=local (この PC で ./setup.sh がビルドしたエンジン) は
    純正設計上 get_prebuilt が黙って None を返す (既製で置き換えず build_engine 側が
    source とカードを見て決める) ので、**driver が先に engine/BUILD.json を読んで**
    「ローカルビルドで archs が合っている = 満足」と正しく判定する。arch が足りないとき
    は純正なら再ビルドするところ — ここはコンパイルしないので理由を出して setup.sh へ
    導線する (受信失敗と誤読しないこと。2026-11-09 の clean 再インストールで実証)

純正実装の関数名 (gpus / gpu_problem / get_prebuilt / pip_cuda_libs) への結合なので、
upstream の bump で driver が落ちたらジョブログに正直に出る — そのときは
`cd module/strata && ./setup.sh` 1 回が正 (エンジンも venv も在ればそこは素通りする)。
古い GPU (sm_60/70) や AMD・ARM のエンジンビルドは純正 setup.sh の対話領域なので
ここでは作らず、理由を出して setup.sh へ導線する。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def fail(msg: str, hint: str = "") -> int:
    print(f"strata_engine: {msg}")
    if hint:
        print(f"strata_engine: → {hint}")
    return 1


def main() -> int:
    root = Path.cwd()
    if not ((root / "setup.py").is_file() and (root / "serve" / "server.py").is_file()):
        return fail("Strata リポジトリ直下 (module/strata) を cwd にして実行してください",
                    "launcher のセットアップジョブから自動で呼ばれます")
    if sys.platform == "darwin":
        return fail("macOS のエンジンビルド (Metal) は純正 setup.sh の領域です",
                    "cd module/strata && ./setup.sh を 1 回実行してください")
    sys.path.insert(0, str(root))
    try:
        import setup as S
    except Exception as e:  # noqa: BLE001 — 純正 setup.py の import 失敗は正直に
        return fail(f"純正 setup.py を import できませんでした: {e}",
                    "module/strata を更新するか (git submodule update --init) ./setup.sh を 1 回")
    try:
        cards = S.gpus()
        problems = {g["index"]: S.gpu_problem(g) for g in cards}
    except Exception as e:  # noqa: BLE001 — 検出も判定も正直に
        return fail(f"NVIDIA GPU の検出 / 判定に失敗しました: {e}",
                    "nvidia-smi が動くこと、CUDA ドライバが入っていることを確認してください")
    usable = [g for g in cards if problems[g["index"]] is None]
    for g in cards:
        mark = "使用可" if problems[g["index"]] is None else f"使用不可 ({problems[g['index']]})"
        print(f"strata_engine: GPU {g['index']}: {g['name']} (sm_{g['arch']}) — {mark}")
    if not usable:
        return fail("Strata が使える NVIDIA GPU が見つかりません"
                    + (f" (検出: {', '.join(g['name'] for g in cards)})" if cards else ""),
                    "AMD カード / 古い GPU のエンジンビルドは ./setup.sh 1 回が正です "
                    "(docs/OLDER_GPUS.md)")
    archs = sorted({int(g["arch"]) for g in usable})
    min13 = int(getattr(S, "CUDA13_MIN_ARCH", 75))
    if archs[0] < min13:
        return fail(f"sm_{archs[0]} は CUDA 13 エンジン (sm_{min13}+) の既製ビルドの対象外です",
                    "実験的 CUDA 12 エンジン / ソースビルドは ./setup.sh 1 回が正です "
                    "(docs/OLDER_GPUS.md)")
    gpu = {"arch": archs[-1], "archs": archs}
    # 純正 main と同じ優先: engine/ に動くエンジンが在れば受信もビルドも不要。
    # source=local は get_prebuilt が無出力で None を返す純正設計 (既製で上書きせず
    # build_engine 側が決める) なので、ここで読んで判定する — 誤って「受信失敗」にしない
    eng_dir = S.engine_dir(13)
    info = eng_dir / "BUILD.json"
    if info.is_file() and (eng_dir / getattr(S, "EXE", "strata")).is_file():
        try:
            meta = json.loads(info.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        if meta.get("source") == "local" and meta.get("backend") != "hip":
            have = S.arch_numbers(meta.get("archs"))
            miss = [a for a in archs if have and a not in have
                    and not (meta.get("ptx") and a > max(have))]
            if miss:
                return fail(f"ローカルビルドのエンジン (v{meta.get('version')}, archs {have or '?'}) に "
                            f"sm_{max(miss)} のコードがありません",
                            "./setup.sh を 1 回実行するとこの PC で作り直します (ウィザードはコンパイルしません)")
            print(f"strata_engine: エンジンは在ります (ローカルビルド v{meta.get('version')}, "
                  f"archs {have or '?'}, vision {meta.get('vision', '?')}) — 受信もビルドも不要です")
            return 0
    url = (os.environ.get("STRATA_PREBUILT_URL") or "").strip() or S.PREBUILT_URL
    print(f"strata_engine: 既製エンジンを受信します ({url}) archs={archs} vision=gpu")
    eng = S.get_prebuilt(url, gpu, "gpu")
    if eng is None:
        return fail("既製エンジンを受信できませんでした "
                    "(このリリースにこの PC の既製ビルドが無い / ネットワーク不通)",
                    "./setup.sh を 1 回実行するとエンジンがコンパイルされる場合があります")
    try:
        meta = json.loads((eng / "BUILD.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    print(f"strata_engine: エンジン: {eng / getattr(S, 'EXE', 'strata')} "
          f"(v{meta.get('version', '?')}, archs {meta.get('archs', '?')})")
    S.pip_cuda_libs(13)   # エンジンが読む cuBLAS / CUDA runtime (純正の pin と stamp 管理)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("strata_engine: 中断されました")
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 — ジョブログに traceback の末尾でなく原因を残す
        print(f"strata_engine: 予期しないエラー: {type(e).__name__}: {e}")
        sys.exit(1)
