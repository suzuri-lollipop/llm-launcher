"""Strata の「モデル構成」(セットアップ画面 ②) のための補助。

背景: Strata の純正 setup.sh / setup.py は対話インストーラだが、質問はすべて
`--family / --model / --context / --vision` の明示フラグ + `--yes` でデフォルト確定に
できる (ask() は yes なら無入力 = 既定を返し、GPU role も推奨構成がそのまま通る)。
つまり **upstream のインストーラを非対話でそのまま走らせられる** — モデル選定の表や
ダウンロード再開・SHA-256・MTP ドラフト層の準備まで再実装せず、launcher は「表の読み出し」
と「ホワイトリスト検証 + argv 組み立て」だけを持つ。

  * load_options(): 純正 setup.py を submodule cwd で import する短い subprocess から
    FAMILIES / MODELS / MULTI_FILE_FAMILIES / CONTEXTS と PC の RAM・データディスクの
    空きを JSON で受け取る (表の単一の源は常に submodule 側。upstream の bump で
    選択肢も増えていく)。60 秒キャッシュ。
  * build_argv(): 検証済みフラグのみ argv にする。family / model は読み出した表の
    キーそのもの、context は表の CONTEXTS の値、vision は列挙値だけを通す
    (shell は挟まないが、argv 化に値の検証は必須)。
  * 設定 JSON の名前は純正の作法どおり `strata-<fam.tag + model>.json` (module/strata 直下)。
    ジョブ完了後の result.config はこの名前ピンポイント + mtime フォールバックで解決する。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# 表の読み出しだけ行う subprocess スニペット (純正 setup.py の import は stdlib 依存のみ)。
# 出力は最後の 1 行の JSON だけ — 何か print が増えても最後の {...} を取るので壊れない。
_DUMP_SNIPPET = r"""
import json, shutil, sys
sys.path.insert(0, ".")
import setup as S
def m_entry(d):
    return {"about": str(d.get("about", "")), "label": str(d.get("label", "")),
            "download_gb": d.get("download_gb"), "ram_gb": d.get("ram_gb"),
            "vision": bool(d.get("vision"))}
fams = {}
for fid, f in getattr(S, "FAMILIES", {}).items():
    fams[fid] = {"title": str(f.get("title", fid)), "about": str(f.get("about", "")),
                 "tag": str(f.get("tag", "")),
                 "models": {m: m_entry(d) for m, d in S.MODELS.items()
                            if fid in d.get("families", ("qwen", "swift"))}}
for src in ("PREVIEW_FAMILIES", "MULTI_FILE_FAMILIES"):
    for fid, f in getattr(S, src, {}).items():
        ms = {m: m_entry(d) for m, d in (f.get("models") or {}).items()}
        if ms and fid not in fams:
            fams[fid] = {"title": str(f.get("title", fid)), "about": str(f.get("about", "")),
                         "tag": str(f.get("tag", "")), "models": ms}
pc = {}
try:
    pc["ram_gb"] = round(float(S.ram_gb()), 1)
except Exception:
    pc["ram_gb"] = None
try:
    data = S.home().parent / "Strata-data"
    probe = data if data.exists() else data.parent
    free = shutil.disk_usage(str(probe)).free / 2**30
    pc["data_dir"] = str(data)
    pc["disk_free_gb"] = round(free, 1)
except Exception:
    pass
print(json.dumps({"families": fams, "contexts": list(getattr(S, "CONTEXTS", [])), "pc": pc}))
"""

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL = 60.0

VISIONS = ["yes", "no", "gpu", "cpu"]


def load_options(module_dir: Path) -> dict[str, Any]:
    """submodule 純正の表を JSON で読む。未 clone / 読み出し失敗なら {"available": false}。"""
    if not (module_dir / "setup.py").is_file():
        return {"available": False, "families": {}, "contexts": [], "pc": {}}
    key = str(module_dir)
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL:
        return hit[1]
    py = module_dir / ".venv" / "bin" / "python"
    if not py.exists():
        py = Path(sys.executable)
    out: dict[str, Any] = {"available": False, "families": {}, "contexts": [], "pc": {}}
    try:
        proc = subprocess.run([str(py), "-c", _DUMP_SNIPPET], cwd=str(module_dir),
                              capture_output=True, text=True, timeout=30,
                              env=dict(os.environ, PYTHONNOUSERSITE="1"))
        text = proc.stdout.strip()
        data = None
        if proc.returncode == 0:
            # 出力は最後の 1 行の JSON だけ。前置きの print が混ざっても壊れないよう
            # 後ろから行単位で parse する ({ を探す方法だと本文中の { に化される)
            for line in reversed(text.splitlines()):
                try:
                    cand = json.loads(line)
                except ValueError:
                    continue
                if isinstance(cand, dict):
                    data = cand
                    break
        if data is not None:
            data["available"] = bool(data.get("families"))
            out = data
    except Exception:  # noqa: BLE001 — 表が読めないだけでセットアップ全体を止めない
        pass
    _CACHE[key] = (time.monotonic(), out)
    return out


def installed_configs(module_dir: Path) -> list[dict[str, Any]]:
    """既に設定済みの strata-<model>.json (純正 setup が作った / 前回この画面で作った)。"""
    out = []
    for p in sorted(module_dir.glob("strata-*.json")):
        if p.name.endswith(".shared-settings.json"):
            continue
        try:
            data = json.loads(p.read_text("utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if "exe" not in data and "args" not in data:      # model_config() と同じ見立て
            continue
        out.append({"file": str(p), "name": p.name,
                    "model_name": str(data.get("model_name") or p.stem[len("strata-"):] or ""),
                    "mtime": p.stat().st_mtime})
    out.sort(key=lambda x: -x["mtime"])
    return out


def build_argv(module_dir: Path, opts: dict[str, Any]) -> tuple[list[str], str]:
    """UI の選択を純正 setup.py の argv と、できるはずの設定 JSON 名に直す。

    ホワイトリスト: family / model は load_options() が submodule から読んだ表のキー、
    context は表の CONTEXTS の値、vision は VISIONS。どれか一つでも外れたら
    InstallError 相当の ValueError を投げる (argv 化の前に必ず here を通すこと)。
    """
    fam = str(opts.get("family") or "").strip()
    model = str(opts.get("model") or "").strip()
    vision = str(opts.get("vision") or "").strip().lower()
    try:
        context = int(opts.get("context"))
    except (TypeError, ValueError):
        raise ValueError("context が不正です")
    if vision and vision not in VISIONS:
        raise ValueError(f"vision が不正です ({'/'.join(VISIONS)})")
    data = load_options(module_dir)
    if not data.get("available"):
        raise ValueError("Strata セットアップの表を読めません "
                         "(module/strata を submodule update --init で更新してください)")
    fams: dict[str, Any] = data.get("families") or {}
    if fam not in fams or not fams[fam].get("models"):
        raise ValueError(f"family が一覧にありません: {fam}")
    if model not in fams[fam]["models"]:
        raise ValueError(f"model が {fam} の一覧にありません: {model}")
    contexts = [int(c) for c in (data.get("contexts") or [])]
    if contexts and context not in contexts:
        raise ValueError(f"context が一覧にありません: {context} ({contexts})")
    if not 4096 <= context <= 4_194_304:
        raise ValueError(f"context が範囲外です: {context}")
    argv = ["setup.py", "--family", fam, "--model", model, "--context", str(context)]
    if vision:   # vision 非対応パックは --vision を渡さない (upstream 側でも vision 質問自体が出ない)
        argv += ["--vision", vision]
    argv += ["--yes", "--no-start", "--no-browser"]
    tag = str(fams[fam].get("tag") or "")
    config_name = f"strata-{(tag + model).lower()}.json"
    return argv, config_name
