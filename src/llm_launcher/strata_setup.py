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
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# 表の読み出しだけ行う subprocess スニペット (純正 setup.py の import は stdlib 依存のみ)。
# 出力は最後の 1 行の JSON だけ — 何か print が増えても最後の {...} を取るので壊れない。
_DUMP_SNIPPET = r"""
import json, shutil, sys
from pathlib import Path
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
    usable = [g for g in S.gpus() if S.gpu_problem(g) is None]
    pc["vram_gb"] = round(max([float(g.get("vram_gb") or 0.0) for g in usable], default=0.0), 1)
except Exception:
    pc["vram_gb"] = None
try:
    st = S.load_settings()
    data = Path(st["data_dir"]) if st.get("data_dir") else S.home().parent / "Strata-data"
except Exception:
    data = S.home().parent / "Strata-data"
try:
    pc["data_dir"] = str(data)
    probe = data if data.exists() else data.parent
    pc["disk_free_gb"] = round(shutil.disk_usage(str(probe)).free / 2**30, 1)
except Exception:
    pass
# 重み実在の判定は「ファイル名インデックス × 純正のファイル名解決 (model_file /
# shards リスト)」— パス組み立てを再発明せず、名前で当てる。取れても取れなくても
# 選定は壊れない (バッジが出ないだけ)。ダウンロードの有無は setup.py 自身が毎回
# 実ファイルで判定するので、ここは UI の案内専用。
idx = {}
for root in (data / "models", data / "packs"):
    try:
        if root.is_dir():
            for f in root.rglob("*"):
                if f.is_file():
                    idx[f.name] = idx.get(f.name, 0) + f.stat().st_size
    except Exception:
        pass
multi = getattr(S, "MULTI_FILE_FAMILIES", {})
own = getattr(S, "PREVIEW_FAMILIES", {})
def shards_of(fid, raw, model):
    try:
        if fid in multi:
            return [Path(s).name for s in (raw.get("models", {}).get(model, {}).get("shards") or [])]
        m = S.MODELS.get(model, {})
        n = int(m.get("shards") or raw.get("shards") or 2)
        return [S.model_file(raw, model, i) for i in range(1, n + 1)]
    except Exception:
        return []
# 推奨コンテキストは純正の式そのもの (own-path / multi-file 系は own_path_context、
# flash-next 系は min(128K, ram_ctx))。UI は既定を推奨にして、より大きい選択に注記を出す
# — 純正の対話質問の「(recommended for your GPU)」と同じ立場 (大きくても注記して従う)。
ram = pc.get("ram_gb") or 0.0
vram = pc.get("vram_gb") or 0.0
for fid, f in fams.items():
    raw = getattr(S, "FAMILIES", {}).get(fid) or own.get(fid) or multi.get(fid) or {}
    f["max_context"] = 262144 if fid in multi else (max(S.CONTEXTS) if getattr(S, "CONTEXTS", []) else 524288)
    for m, d in f["models"].items():
        try:
            if fid in own or fid in multi:
                d["rec_context"] = int(S.own_path_context(raw, vram))
            else:
                d["rec_context"] = int(min(131072, S.ram_ctx(m, ram, False)))
        except Exception:
            pass
        names = shards_of(fid, raw, m)
        if names:
            found = [n for n in names if n in idx]
            d["have"] = {"files": len(names), "found": len(found),
                         "gb": round(sum(idx[n] for n in found) / 2**30, 1)}
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
            _merge_config_ready(module_dir, data)
            out = data
    except Exception:  # noqa: BLE001 — 表が読めないだけでセットアップ全体を止めない
        pass
    _CACHE[key] = (time.monotonic(), out)
    return out


_SHARD_RE = re.compile(r"^(.*-)(\d{5})-of-(\d{5})(\.\w+)$")
_PACK_SUFFIXES = (".gguf", ".bin", ".safetensors")


def _merge_config_ready(module_dir: Path, data: dict[str, Any]) -> None:
    """既存 config (strata-<tag+model>.json) の重み検証が ok なら、その実在をモデル表へ
    引き写して「取得済み」バッジにする — ユーザーに見えるのは常にモデルだけ (JSON は
    内部の命名作法のまま)。ファイル名解決が通らない特殊レイアウト (nvfp4 の dense+pack)
    も、config が実パスを持っているのでこれでカバーされる。"""
    ready: dict[str, Any] = {}
    for c in installed_configs(module_dir):
        w = c.get("weights") or {}
        if w.get("ok"):
            ready[c["name"][len("strata-"):-len(".json")].lower()] = w
    for f in (data.get("families") or {}).values():
        tag = str(f.get("tag") or "")
        for m, d in (f.get("models") or {}).items():
            w = ready.get((tag + m).lower())
            if w and not (d.get("have") or {}).get("found"):
                d["have"] = {"files": 1, "found": 1, "gb": w.get("size_gb")}


def weights_status(data: dict) -> dict:
    """config JSON が参照する重みの実在チェック — 「一覧に JSON が並んでいる」だけでは
    起動能否は分からない (削除/移動済み・シャード欠損がある)。参照パターンは純正 config の
    3 種: --native/--model/--gguf/-m のファイル (マルチシャードは -0000X-of-0000N の
    兄弟も)、--pack のディレクトリ、vision.model/mmproj。tokenizer ディレクトリも見る。
    ok=None は「参照が見付からない config」= 判断しない (UI は重み表示なしで通す)。"""
    args = data.get("args") or []
    refs: list[Path] = []
    for i, a in enumerate(args[:-1]):
        if str(a) in ("--native", "--model", "--gguf", "-m", "--pack"):
            refs.append(Path(str(args[i + 1])))
    v = data.get("vision") or {}
    for k in ("model", "mmproj"):
        p = Path(str(v.get(k) or ""))
        if v.get(k) and p not in refs:
            refs.append(p)
    missing: list[str] = []
    present = 0
    total = 0.0
    tok = str(data.get("tokenizer") or "")
    if tok and not Path(tok).is_dir():
        missing.append("tokenizer")        # 早期 return しない — 重み側の欠損も並べて出す
    if not refs:
        return {"ok": None if not missing else False, "size_gb": None, "missing": missing}
    for p in refs:
        if p.is_dir():
            files = [f for f in p.rglob("*") if f.is_file() and f.suffix in _PACK_SUFFIXES]
            if files:
                present += 1
                total += sum(f.stat().st_size for f in files) / 2**30
            else:
                missing.append(p.name)
        elif p.is_file():
            present += 1
            total += p.stat().st_size / 2**30
            m = _SHARD_RE.match(p.name)
            if m:
                for n in range(1, int(m.group(3)) + 1):
                    sib = p.with_name(f"{m.group(1)}{n:05d}-of-{m.group(3)}{m.group(4)}")
                    if sib == p:
                        continue
                    if sib.is_file():
                        total += sib.stat().st_size / 2**30
                    else:
                        missing.append(sib.name)
        else:
            missing.append(p.name)
    return {"ok": present > 0 and not missing, "size_gb": round(total, 1) if total else None,
            "missing": missing[:8]}


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
        par = data.get("parallel")
        out.append({"file": str(p), "name": p.name,
                    "model_name": str(data.get("model_name") or p.stem[len("strata-"):] or ""),
                    "parallel": int(par) if str(par).isdigit() else None,
                    "weights": weights_status(data),
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
