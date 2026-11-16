"""全バックエンド横断のモデル管理 (棚卸し / 削除)。

対応バックエンド (vLLM / SGLang / llama.cpp / FreeToken 系 / Strata 系 / Maya / Basalt)
のモデル置き場はそれぞれ違う:

  * HF Hub キャッシュ (models--org--name)     … vLLM / SGLang / llama.cpp / FreeToken / Basalt
  * <data>/models/<org>--<name> (model_data)  … FreeToken (fork) の Freetoken-data
  * Strata-data/models・packs                 … Strata / Strata (fork) (純正 setup.py の置き場)
  * Maya-data/models                          … Project Maya

このモジュールはそれらを 1 つの棚卸し (inventory) に束ね、各エントリに
「どのバックエンドが読める形か」「どのプロファイルが参照しているか」「稼働中か」
「削除して安全か」を付ける。削除はエントリ ID からのみ解決し (任意パスは受けない)、
稼働中サーバー / 参照プロファイル / ダウンロード中ジョブのガードを通す。

起動 config JSON (strata-<model>.json / maya-<model>.json) は棚卸しの対象外 —
あれはプロファイル側の設定で、管理はプロファイル管理の責務。ここでは重みの
削除ガード (「この重みを参照している config / プロファイル」) の解決のために
読むだけ (エントリとしては出さない = モデル管理でプロファイルを管理しない)。

スキャナ (scan_hf_cache / scan_data_models / hf_hub_cache_dir) は元 app.py にあったが、
モデル管理の単一の源としてここへ移した。app.py は再エクスポートするので
`from llm_launcher.app import scan_hf_cache` の既存互換は変わらない。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .backends import BACKENDS, backend_for, fork_model_root, supported_models
from . import strata_setup

# 重みファイルと見なす拡張子 (scan_hf_cache / scan_data_models 共通の基準)。
# .basalt は Basalt 専用 pack なので「汎用重み」には数えない (他バックエンドは読めない)。
_WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".gguf")
_WEIGHT_MIN = 1 << 20
# サイズ計算の_walk_上限 (巨大なデータフォルダで API が固まらないための保険)
_SIZE_MAX_FILES = 20000


class ModelError(Exception):
    """モデル管理 API のエラー (status は HTTP ステータスにそのまま使う)。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# スキャナ (app.py から移設。既存のキー形は保ち、size_bytes / mtime を足した)
# ---------------------------------------------------------------------------


def hf_hub_cache_dir() -> Path:
    """huggingface_hub の既定キャッシュパス解決 (HF_HUB_CACHE > HF_HOME > ~/.cache)。"""
    env = os.environ.get("HF_HUB_CACHE")
    if env:
        return Path(env).expanduser()
    home = os.environ.get("HF_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".cache" / "huggingface"
    return base / "hub"


def dir_size(path: Path, max_files: int = _SIZE_MAX_FILES, *, local_only: bool = False) -> int:
    """ディレクトリ以下のファイルサイズ合計 (モデルの論理サイズ)。

    symlink は実体を辿るが、解決先が同じファイルは 1 回だけ数える — HF キャッシュは
    snapshots/ が blobs/ へのリンクなので、素朴に足すと二重計上になる。Xet 形式の
    共有 blob (repo 外へのリンク) も論理サイズには含まれる (ユーザーの「モデルの大きさ」)。
    local_only=True はリンクを数えない = そのディレクトリを削除したときに実際に
    解放されるバイト数 (共有 blob は HF キャッシュ側が管理するので含めない)。
    数え切れなければそこまでの合計 (概算で十分 — 削除前の確認表示用)。"""
    total = 0
    n = 0
    seen: set[Path] = set()
    try:
        for f in path.rglob("*"):
            if n >= max_files:
                break
            try:
                if not f.is_file():
                    continue
                if f.is_symlink():
                    if local_only:
                        continue
                    rp = f.resolve()
                    if rp in seen:
                        continue
                    seen.add(rp)
                    total += rp.stat().st_size
                else:
                    rp = f.resolve()
                    if rp in seen:
                        continue
                    seen.add(rp)
                    total += f.stat().st_size
                n += 1
            except OSError:
                continue
    except OSError:
        pass
    return total


def scan_data_models(models_root: Path, max_files: int = 400) -> list[dict[str, Any]]:
    """setup.sh / UI が <data>/models/<org>--<name> へ置いたモデルを一覧化する。
    scan_hf_cache と同じ形のレコード (snapshot はフォルダそのもの)。"""
    out: list[dict[str, Any]] = []
    if not models_root.is_dir():
        return out
    for d in models_root.iterdir():
        if not d.is_dir():
            continue
        gguf: list[dict[str, Any]] = []
        basalt: list[dict[str, Any]] = []
        has_weights = False
        try:
            for f in sorted(d.iterdir()):
                if not f.is_file():
                    continue
                size = f.stat().st_size
                suf = f.suffix.lower()
                if suf in _WEIGHT_SUFFIXES and size > _WEIGHT_MIN:
                    has_weights = True
                if suf == ".gguf":
                    gguf.append({"name": f.name, "size": size})
                if suf == ".basalt":
                    basalt.append({"name": f.name, "size": size})
                if len(gguf) + len(basalt) >= max_files:
                    break
            mtime = d.stat().st_mtime
        except OSError:
            continue
        out.append({"repo_id": d.name.replace("--", "/", 1), "snapshot": str(d),
                    "revision": "", "has_weights": has_weights, "gguf": gguf,
                    "basalt": basalt, "source": "data", "mtime": mtime,
                    "size_bytes": dir_size(d)})
    out.sort(key=lambda m: m["mtime"], reverse=True)
    return out


def scan_hf_cache(base: Path | None = None, max_files: int = 400) -> dict[str, Any]:
    """HF Hub キャッシュの models--org--name からモデル一覧を構築する。"""
    base = base or hf_hub_cache_dir()
    result: dict[str, Any] = {"cache_dir": str(base), "exists": base.is_dir(), "models": []}
    if not base.is_dir():
        return result
    entries: list[tuple[float, dict[str, Any]]] = []
    for d in base.iterdir():
        if not d.is_dir() or not d.name.startswith("models--"):
            continue
        repo_id = d.name[len("models--"):].replace("--", "/", 1)
        snaps_dir = d / "snapshots"
        if not snaps_dir.is_dir():
            continue
        refs: dict[str, str] = {}
        refs_dir = d / "refs"
        if refs_dir.is_dir():
            for rf in refs_dir.rglob("*"):        # refs/pr/1 のような入れ子ブランチにも対応
                try:
                    if rf.is_file():
                        refs[str(rf.relative_to(refs_dir))] = rf.read_text().strip()
                except OSError:
                    continue
        snaps = [s for s in snaps_dir.iterdir() if s.is_dir()]
        if not snaps:
            continue
        def snap_mtime(s: Path) -> float:
            try:
                return s.stat().st_mtime
            except OSError:
                return 0
        snaps.sort(key=snap_mtime, reverse=True)
        wanted = refs.get("main") or refs.get("master")
        chosen = next((s for s in snaps if wanted and s.name.startswith(wanted)), snaps[0])
        revision = next((k for k, v in refs.items() if chosen.name.startswith(v)), None) or (wanted or "")[:10] or "main"
        gguf: list[dict[str, Any]] = []
        basalt: list[dict[str, Any]] = []
        has_weights = False
        try:
            for f in sorted(chosen.iterdir()):
                if not f.is_file():
                    continue
                size = f.stat().st_size
                suf = f.suffix.lower()
                # has_weights は「汎用重み」の有無 (scan_data_models と同じ基準)。
                # .basalt は Basalt 専用 pack で他バックエンドは読めないため含めない
                # (含めると FreeToken 等の「ダウンロード済みモデル」に basalt pack が漏れる)
                if suf in _WEIGHT_SUFFIXES and size > _WEIGHT_MIN:
                    has_weights = True
                if suf == ".gguf":
                    gguf.append({"name": f.name, "size": size})
                if suf == ".basalt":        # Basalt の pack (単一ファイル = モデル正体)
                    basalt.append({"name": f.name, "size": size})
                if len(gguf) + len(basalt) >= max_files:
                    break
        except OSError:
            pass
        entries.append((snap_mtime(chosen), {
            "repo_id": repo_id,
            "snapshot": str(chosen),
            "revision": revision,
            "has_weights": has_weights,
            "gguf": gguf,
            "basalt": basalt,
            "size_bytes": dir_size(d),
            "mtime": snap_mtime(chosen),
        }))
    entries.sort(key=lambda t: t[0], reverse=True)
    result["models"] = [m for _, m in entries]
    return result


# ---------------------------------------------------------------------------
# モデル置き場の解決
# ---------------------------------------------------------------------------


def strata_data_dir(root: Path) -> Path | None:
    """Strata のデータフォルダ (純正 setup.py の load_settings と同じ解決)。
    表の読み出し (strata_setup.load_options) は 60s キャッシュ済み subprocess なので
    棚卸しのたびに叩いても安い。未 clone / 失敗時は module 隣接の Strata-data を見る。"""
    mod = root / "module/strata"
    try:
        opts = strata_setup.load_options(mod)
        dd = str((opts.get("pc") or {}).get("data_dir") or "").strip()
        if dd:
            return Path(dd)
    except Exception:  # noqa: BLE001 — 表が読めなくても棚卸し全体は止めない
        pass
    for cand in (mod.parent / "Strata-data", root / "module/Strata-data"):
        if cand.is_dir():
            return cand
    return None


def maya_data_dir(root: Path) -> Path:
    """Maya のデータフォルダ (maya.sh の既定 = clone 隣接の Maya-data)。"""
    return (root / "module/maya").parent / "Maya-data"


def model_data_roots(root: Path) -> list[tuple[str, Path]]:
    """model_data を持つバックエンドの (backend_id, models ルート) 一覧。"""
    out: list[tuple[str, Path]] = []
    for bid, b in BACKENDS.items():
        if not b.get("model_data"):
            continue
        mr = fork_model_root(root, backend_for(root, bid))
        if mr is not None:
            out.append((bid, mr))
    return out


def _config_refs(module_dir: Path, prefix: str) -> list[dict[str, Any]]:
    """strata-<model>.json / maya-<model>.json の重み参照だけを読む。
    config JSON はプロファイル側の設定なので棚卸しエントリにはしない —
    重み削除ガードの「この重みを参照している config」突合 (affected_configs) 用。"""
    out: list[dict[str, Any]] = []
    if not module_dir.is_dir():
        return out
    if prefix == "strata":
        files = [Path(c["file"]) for c in strata_setup.installed_configs(module_dir)]
    else:
        files = []
        for p in sorted(module_dir.glob(f"{prefix}-*.json")):
            try:
                data = json.loads(p.read_text("utf-8", errors="replace"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or ("exe" not in data and "args" not in data):
                continue
            files.append(p)
    for p in files:
        out.append({"path": str(p),
                    "weight_paths": [str(w) for w in config_weight_paths(p)]})
    return out


def config_weight_paths(cfg_file: Path) -> list[Path]:
    """config JSON が参照する重みパス (weights_status と同じ参照パターン)。
    削除ガードの「この重みを参照している config / プロファイル」突合に使う。"""
    try:
        data = json.loads(cfg_file.read_text("utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    args = data.get("args") or []
    refs: list[Path] = []

    def add(p: Path) -> None:
        if p not in refs:       # vision.model が --native と同じファイルを指すことがある
            refs.append(p)

    for i, a in enumerate(args[:-1]):
        if str(a) in ("--native", "--model", "--gguf", "-m", "--pack"):
            add(Path(str(args[i + 1])))
    v = data.get("vision") or {}
    if isinstance(v, dict):
        for k in ("model", "mmproj"):
            if v.get(k):
                add(Path(str(v[k])))
    if data.get("tokenizer"):
        add(Path(str(data["tokenizer"])))
    return refs


# ---------------------------------------------------------------------------
# プロファイルのモデル参照
# ---------------------------------------------------------------------------


def _launch_json_refs(path: Path) -> list[str]:
    """起動 JSON (freetoken 系: {python, argv, ...}) のモデル参照。"""
    try:
        doc = json.loads(path.read_text("utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    if not isinstance(doc, dict):
        return []
    argv = [str(a) for a in (doc.get("argv") or [])]
    out: list[str] = []
    for i, a in enumerate(argv[:-1]):
        if a in ("--model-path", "--model", "--gguf", "-m"):
            out.append(argv[i + 1])
    return out


def _config_json_refs(path: Path) -> list[str]:
    """エンジン config JSON のモデル参照。strata / maya 形 (args) と
    basalt 形 (models[].model) の両方を読む。"""
    try:
        doc = json.loads(path.read_text("utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    if not isinstance(doc, dict):
        return []
    out: list[str] = []
    args = doc.get("args")
    if isinstance(args, list):
        for i, a in enumerate(args[:-1]):
            if str(a) in ("--native", "--model", "--gguf", "-m", "--pack"):
                out.append(str(args[i + 1]))
        v = doc.get("vision") or {}
        if isinstance(v, dict):
            out += [str(v[k]) for k in ("model", "mmproj") if v.get(k)]
        if doc.get("tokenizer"):
            out.append(str(doc["tokenizer"]))
    models = doc.get("models")
    if isinstance(models, list):
        for m in models:
            if isinstance(m, dict):
                out += [str(m[k]) for k in ("model", "tokenizer") if m.get(k)]
    return out


def profile_model_refs(root: Path, profile: dict[str, Any]) -> set[str]:
    """プロファイルが参照するモデルパスの集合 (絶対パス文字列)。
    values.model / model_path 直書き + 起動 JSON / config JSON 経由の参照。"""
    values = profile.get("values") or {}
    out: set[str] = set()

    def add(v: Any) -> None:
        s = str(v or "").strip()
        if not s:
            return
        p = Path(s).expanduser()
        out.add(str(p if p.is_absolute() else (root / p)))

    for k in ("model", "model_path"):
        add(values.get(k))
    cfg = str(values.get("config") or "").strip()
    if cfg:
        p = Path(cfg).expanduser()
        cp = p if p.is_absolute() else root / p
        out.add(str(cp))
        bid = str(profile.get("backend") or "")
        if bid in ("freetoken", "freetoken-fork"):
            for r in _launch_json_refs(cp):
                add(r)
        else:
            for r in _config_json_refs(cp):
                add(r)
    return out


def _path_matches(ref: str, entry_path: str) -> bool:
    """参照 ref がエントリ (ファイル/ディレクトリ) を指すか。
    完全一致 / ref がエントリ配下 / エントリが ref 配下 (ディレクトリ指定) のいずれか。"""
    if ref == entry_path:
        return True
    return ref.startswith(entry_path.rstrip("/") + "/") or entry_path.startswith(ref.rstrip("/") + "/")


# ---------------------------------------------------------------------------
# 棚卸し
# ---------------------------------------------------------------------------


def _repo_backends(m: dict[str, Any], catalogs: dict[str, list[dict[str, Any]]]) -> list[str]:
    """repo エントリを読めるバックエンド (形の互換 + 対応モデル表への掲載)。"""
    bids: list[str] = []
    if m.get("basalt"):
        bids.append("basalt")
    if m.get("gguf"):
        bids += ["llamacpp", "strata", "strata-fork"]
    if m.get("has_weights"):
        bids += ["vllm", "sglang", "freetoken", "freetoken-fork"]
    repo = str(m.get("repo_id") or "").lower()
    for bid, entries in catalogs.items():
        if any(str(e.get("repo_id") or "").lower() == repo for e in entries):
            if bid not in bids:
                bids.append(bid)
    return [b for b in BACKENDS if b in bids]   # BACKENDS の順で安定に


def _fmt_list(m: dict[str, Any]) -> list[str]:
    fmt: list[str] = []
    if m.get("gguf"):
        fmt.append("GGUF")
    if m.get("basalt"):
        fmt.append("Basalt pack")
    if m.get("has_weights"):
        fmt.append("safetensors 等")
    return fmt


def build_inventory(root: Path, profiles: list[dict[str, Any]],
                    sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """全バックエンドのモデル棚卸し。models (エントリ) / backends (対応モデル表) /
    dirs (置き場) / disks (空き容量) を返す。"""
    root = root.resolve()
    catalogs: dict[str, list[dict[str, Any]]] = {
        bid: supported_models(root, backend_for(root, bid)) for bid in BACKENDS
    }
    # プロファイル参照と稼働中セッション (削除ガードと used_by の材料)
    prefs = [(p, profile_model_refs(root, p)) for p in profiles]
    live = [s for s in sessions if s.get("status") in ("running", "starting", "stopping")]

    def used_by(paths: list[str]) -> list[dict[str, Any]]:
        out = []
        for p, refs in prefs:
            if any(_path_matches(r, ep) for r in refs for ep in paths):
                out.append({"id": p.get("id"), "name": p.get("name") or "",
                            "backend": p.get("backend") or ""})
        return out

    def running(ub: list[dict[str, Any]], paths: list[str]) -> bool:
        ids = {u["id"] for u in ub}
        for s in live:
            if s.get("profile_id") in ids:
                return True
            for tok in (s.get("argv") or []):
                t = str(tok)
                if any(_path_matches(t, ep) for ep in paths):
                    return True
        return False

    models: list[dict[str, Any]] = []

    def add_repo(m: dict[str, Any], source: str, owner: str = "") -> None:
        snap = str(m.get("snapshot") or "")
        ub = used_by([snap])
        # HF キャッシュは repo ディレクトリ全体 (models--org--name) を削除単位にする。
        # snapshot だけ消すと実体の blobs が残って容量が解放されないため。
        del_paths = ([str(Path(snap).parent.parent)] if source == "cache" else [snap])
        models.append({
            "id": f"{source}:{m['repo_id']}" if source == "cache" else f"data:{snap}",
            "kind": "repo", "source": source, "owner": owner,
            "name": str(m.get("repo_id") or ""), "repo_id": str(m.get("repo_id") or ""),
            "path": snap, "paths": del_paths,
            "revision": str(m.get("revision") or ""),
            "size_bytes": int(m.get("size_bytes") or 0), "mtime": m.get("mtime") or 0,
            "has_weights": bool(m.get("has_weights")),
            "gguf": m.get("gguf") or [], "basalt": m.get("basalt") or [],
            "formats": _fmt_list(m),
            "backends": _repo_backends(m, catalogs),
            "used_by": ub, "running": running(ub, [snap]),
            "note": "",
        })

    # 1) HF キャッシュ
    cache = scan_hf_cache()
    for m in cache["models"]:
        add_repo(m, "cache")
    # 2) model_data (FreeToken fork の Freetoken-data 等)
    data_roots = model_data_roots(root)
    for bid, mr in data_roots:
        for m in scan_data_models(mr):
            add_repo(m, "data", owner=bid)

    # 3) Strata / Maya の起動 config JSON はエントリにしない (プロファイル側の設定 =
    #    プロファイル管理の責務)。重みの削除ガード (affected_configs) のためだけ読む。
    cfg_groups: list[tuple[str, Path]] = []
    st_mods = [root / "module/strata"]
    for cand in (root / "module/fork/strata", root / "module/fork/Strata"):
        if cand.is_dir():
            st_mods.append(cand)
            break
    for mod in st_mods:
        cfg_groups.append(("strata", mod))
    cfg_groups.append(("maya", root / "module/maya"))

    cfg_refs: list[dict[str, Any]] = []
    for prefix, mod in cfg_groups:
        cfg_refs += _config_refs(mod, prefix)

    # 4) データフォルダ直下の重み (config が無いものも見えるように)
    dir_groups: list[tuple[str, Path, list[str], str]] = []
    sdd = strata_data_dir(root)
    if sdd is not None:
        for sub in ("models", "packs"):
            dir_groups.append(("strata-data", sdd / sub, ["strata", "strata-fork"], sub))
    mdd = maya_data_dir(root)
    dir_groups.append(("maya-data", mdd / "models", ["maya"], "models"))
    for source, droot, bids, _sub in dir_groups:
        if not droot.is_dir():
            continue
        for d in sorted(droot.iterdir()):
            if not d.is_dir():
                continue
            p = str(d)
            ub = used_by([p])
            affected = [Path(c["path"]).name for c in cfg_refs
                        if any(_path_matches(wp, p) for wp in c["weight_paths"])]
            try:
                mtime = d.stat().st_mtime
            except OSError:
                mtime = 0
            models.append({
                "id": f"{source}:{p}", "kind": "dir", "source": source, "owner": "",
                "name": d.name, "repo_id": "", "path": p, "paths": [p],
                "revision": "", "size_bytes": dir_size(d), "mtime": mtime,
                "has_weights": True, "gguf": [], "basalt": [],
                "formats": ["Strata pack" if source == "strata-data" else "Maya pack"],
                "backends": list(bids),
                "used_by": ub, "running": running(ub, [p]),
                "affected_configs": affected,
                "note": ("参照している設定 JSON: " + ", ".join(affected)) if affected else "",
            })

    models.sort(key=lambda m: (-(m.get("mtime") or 0), m["name"]))

    # バックエンド別の対応モデル表 (ダウンロード済み判定付き)
    by_repo: dict[str, dict[str, Any]] = {}
    for m in models:
        if m["kind"] == "repo":
            by_repo.setdefault(m["repo_id"].lower(), m)
    backends: list[dict[str, Any]] = []
    for bid, b in BACKENDS.items():
        bb = backend_for(root, bid)
        entries = []
        for sm in catalogs[bid]:
            repo = str(sm.get("repo_id") or "")
            local = by_repo.get(repo.lower())
            ok = False
            if local is not None:
                if bid == "basalt":
                    ok = bool(local.get("basalt"))
                elif bid == "llamacpp":
                    ok = bool(local.get("gguf")) or bool(local.get("has_weights"))
                else:
                    ok = bool(local.get("has_weights")) or bool(local.get("gguf"))
            entries.append({**sm, "downloaded": ok,
                            "entry_id": local["id"] if local is not None and ok else ""})
        mod = root / str(bb.get("module_path") or "")
        note = ""
        if bid in ("strata", "strata-fork"):
            note = "モデルの選択・ダウンロードはセットアップウィザード (②) が担います"
        elif bid == "maya":
            note = "モデルの選択・ダウンロードはセットアップウィザード (②) が担います (純正 maya.py を非対話実行)"
        elif not entries:
            note = "対応モデル一覧はありません — repo id を直接指定してダウンロードできます"
        backends.append({
            "id": bid, "name": b["name"], "module_path": bb.get("module_path") or "",
            "cloned": mod.is_dir(),
            "wizard": bid in ("strata", "strata-fork", "maya"),
            "supported": entries, "note": note,
        })

    # 置き場とディスク空き
    dirs: list[dict[str, Any]] = [{"path": cache["cache_dir"], "label": "HF キャッシュ",
                                   "exists": bool(cache["exists"])}]
    for bid, mr in data_roots:
        dirs.append({"path": str(mr), "label": f"{BACKENDS[bid]['name']} データフォルダ",
                     "exists": mr.is_dir()})
    if sdd is not None:
        dirs.append({"path": str(sdd), "label": "Strata データフォルダ", "exists": sdd.is_dir()})
    dirs.append({"path": str(mdd), "label": "Maya データフォルダ", "exists": mdd.is_dir()})
    disks: list[dict[str, Any]] = []
    seen_dev: set[int] = set()
    for d in dirs:
        p = Path(d["path"])
        probe = p if p.exists() else p.parent
        try:
            st = probe.stat()
            if st.st_dev in seen_dev:
                continue
            seen_dev.add(st.st_dev)
            du = shutil.disk_usage(str(probe))
            disks.append({"path": str(probe), "free": du.free, "total": du.total})
        except OSError:
            continue

    return {"models": models, "backends": backends, "dirs": dirs, "disks": disks,
            "cache_dir": cache["cache_dir"]}


# ---------------------------------------------------------------------------
# 削除
# ---------------------------------------------------------------------------


def allowed_delete_roots(root: Path) -> list[Path]:
    """削除を許す置き場のルート (これより上は絶対に消さない)。
    重みの置き場だけ — config JSON (module/strata・module/maya 直下) は
    プロファイル側なのでモデル管理からの削除対象にしない。"""
    out = [hf_hub_cache_dir().resolve()]
    for _bid, mr in model_data_roots(root):
        out.append(mr.resolve())
    sdd = strata_data_dir(root)
    if sdd is not None:
        out.append(sdd.resolve())
    out.append(maya_data_dir(root).resolve())
    return out


def delete_entry(root: Path, entry_id: str, *, force: bool,
                 profiles: list[dict[str, Any]], sessions: list[dict[str, Any]],
                 busy_dirs: list[str] | None = None) -> dict[str, Any]:
    """棚卸しエントリを ID で解決して削除する。

    ガード: 稼働中サーバーが参照 → 409 / プロファイルが参照 (force 無し) → 409 /
    ダウンロード中の保存先 → 409 / 置き場ルートより上 or 未知 ID → 拒否。
    """
    inv = build_inventory(root, profiles, sessions)
    ent = next((m for m in inv["models"] if m["id"] == entry_id), None)
    if ent is None:
        raise ModelError(404, "モデルエントリが見つかりません (再スキャンしてください)")
    paths = [str(p) for p in ent.get("paths") or []]
    if ent.get("running"):
        raise ModelError(409, "稼働中のサーバーが参照しているため削除できません。"
                              "サーバーを停止してから削除してください")
    if ent.get("used_by") and not force:
        names = ", ".join(u["name"] or u["id"] for u in ent["used_by"])
        raise ModelError(409, f"プロファイルが参照しています: {names}。"
                              "削除すると起動できなくなります — 続行するには force が必要です")
    for bd in busy_dirs or []:
        if bd and any(_path_matches(str(bd), p) or _path_matches(p, str(bd)) for p in paths):
            raise ModelError(409, "ダウンロードが進行中のフォルダは削除できません")
    allowed = allowed_delete_roots(root)
    resolved: list[Path] = []
    for p in paths:
        rp = Path(p).resolve()
        if rp in allowed or not any(a in rp.parents for a in allowed):
            raise ModelError(400, f"削除できないパスです: {p}")
        resolved.append(rp)
    freed = 0
    deleted: list[str] = []
    for rp in resolved:
        try:
            if rp.is_dir():
                # 実際に解放されるのはローカル実体だけ (Xet 共有 blob は repo 外)
                freed += dir_size(rp, local_only=True)
                shutil.rmtree(rp)
            elif rp.is_file():
                freed += rp.stat().st_size
                rp.unlink()
            else:
                continue    # 既に無い (再スキャンまでの間に消えた) はスキップ
            deleted.append(str(rp))
        except OSError as e:
            raise ModelError(500, f"削除に失敗しました: {rp} ({e})")
    return {"ok": True, "freed_bytes": freed, "deleted": deleted,
            "note": ent.get("note") or ""}
