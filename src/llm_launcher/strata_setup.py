"""Strata の「モデル構成」(セットアップ画面 ②) のための補助。

背景: Strata の純正 setup.sh / setup.py は対話インストーラだが、質問はすべて
`--family / --model / --context / --vision` の明示フラグ + `--yes` でデフォルト確定に
できる (ask() は yes なら無入力 = 既定を返し、GPU role も推奨構成がそのまま通る)。
つまり **upstream のインストーラを非対話でそのまま走らせられる** — モデル選定の表や
ダウンロード再開・SHA-256・MTP ドラフト層の準備まで再実装せず、launcher は「表の読み出し」
と「ホワイトリスト検証 + argv 組み立て」だけを持つ。

  * load_options(): 純正 setup.py を submodule cwd で import する短い subprocess から
    FAMILIES / MODELS / MULTI_FILE_FAMILIES / HY4_FAMILIES / INKLING_FAMILIES と
    PC の RAM・VRAM・GPU 一覧・データディスクの空き、家系別の max_context /
    multi_gpu、モデル別の rec_context と重み実在 (have) を JSON で受け取る
    (表の単一の源は常に submodule 側。upstream の bump で選択肢も増えていく)。
    60 秒キャッシュ。
    0.2.19+ の Hy4-Flash (strata-hy4) / Inkling-2-Mini (strata-inkling) は
    HY4_FAMILIES / INKLING_FAMILIES の独自エンジン家系: コンテキスト / 推奨は
    hy4_family / inkling_family の CONTEXTS / DEFAULT_CONTEXT そのもの
    (Inkling は「up to 1M supported」= 明示 --context が素通りするので owner 方針で
    1M まで拡張。ただし Inkling のモデル別 contexts はこの PC の VRAM で安定起動
    できる分だけに制限する — 単一 GPU CUDA エンジンは全エキスパートを VRAM に
    常駐させ KV を貪欲に自動割り当てするため、大きい ctx はワークスペース確保が
    OOM する。2026-11-23 実測。inkling_ctxs のコメント参照)。
    重みは .strata ネイティブファイル (build + 共有 Engram / Vision)。
    純正メニューが出さない (ダウンロードソース未設定・ローカルファイル無し) 家系は
    表にも出さない (hy4_offered / inkling_offered と同じ見立て)。
  * build_argv(): 検証済みフラグのみ argv にする。family / model は読み出した表の
    キーそのもの、context は表の CONTEXTS の値、vision は列挙値、gpus は検出した
    usable GPU の index 集 (単一カード = --gpu N、複数 = --gpus "0,1"/all、
    単一 GPU 宣言の家系 = own-path / Hy4-Flash の複数指定は拒否)。
    Inkling-2-Mini は 0.2.19 の複数 GPU セットアップが upstream バグ
    (install_inkling2 の AttributeError) のため force_single_gpu: 複数指定は
    理由付きで拒否し、未指定でも --gpu を明示して単一 GPU でセットアップする
    (upstream が直したら表の multi_gpu を戻す)。どれか一つでも外れたら
    InstallError 相当の ValueError を投げる (argv 化に値の検証は必須)。
  * weights_status() / installed_configs(): config が参照する重み (--native / --pack /
    vision / tokenizer、マルチシャードは兄弟も) の実在とサイズを検証する —
    「一覧に出る = 起動できる」の根拠。UI に JSON 名は出さない (モデル名で出す)。
  * 設定 JSON の名前は純正の作法どおり `strata-<fam.tag + model>.json` (module/strata 直下)。
    ジョブ完了後の result.config はこの名前ピンポイント + mtime フォールバックで解決し、
    result.parallel は config の parallel (③ の並列数既定 — 全局推奨を焼いて
    エンジンに VRAM 不足で弾かれるのを防ぐ)。
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
                 "tag": str(f.get("tag", "")), "arch": str(f.get("arch", "")),
                 "models": {m: m_entry(d) for m, d in S.MODELS.items()
                            if fid in d.get("families", ("qwen", "swift"))}}
for src in ("PREVIEW_FAMILIES", "MULTI_FILE_FAMILIES", "HY4_FAMILIES", "INKLING_FAMILIES"):
    for fid, f in getattr(S, src, {}).items():
        ms = {m: m_entry(d) for m, d in (f.get("models") or {}).items()}
        if ms and fid not in fams:
            fams[fid] = {"title": str(f.get("title", fid)), "about": str(f.get("about", "")),
                         "tag": str(f.get("tag", "")), "arch": str(f.get("arch", "")),
                         "models": ms}
# 0.2.19+ の独自エンジン家系 (strata-hy4 / strata-inkling): 純正メニューと同じく
# ダウンロードソースが設定済みかローカルファイルが在るときだけ出す (offered ゲート)
hy4 = getattr(S, "HY4_FAMILIES", {})
ink = getattr(S, "INKLING_FAMILIES", {})
for tbl, gate in ((hy4, "hy4_offered"), (ink, "inkling_offered")):
    g = getattr(S, gate, None)
    for fid in tbl:
        try:
            offered = bool(g()) if callable(g) else True
        except Exception:
            offered = True
        if not offered:
            fams.pop(fid, None)
pc = {}
cards = []
try:
    pc["ram_gb"] = round(float(S.ram_gb()), 1)
except Exception:
    pc["ram_gb"] = None
try:
    cards = S.gpus()
    pc["gpus"] = [{"index": int(g["index"]), "name": str(g["name"]),
                   "vram_gb": round(float(g.get("vram_gb") or 0.0), 1),
                   "usable": S.gpu_problem(g) is None} for g in cards]
except Exception:
    pc["gpus"] = []
try:
    pc["vram_gb"] = round(max([g["vram_gb"] for g in pc["gpus"] if g["usable"]], default=0.0), 1)
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
# 家系別のコンテキスト選択肢 (owner 方針 2026-11-17: 対応する全てのバックエンド /
# モデルで 1M まで。推奨は純正の式のまま、大きい選択は UI が注記して従う):
#  - Flash-Next 系: trained 262144、超過は yarn rope scaling (純正 resolve_rope が
#    factor = 最終 / 262144 を導出)。純正メニューは 512K 止まりだが 1M (factor 4) まで出す。
#  - DeepSeek-V4.1-Flash: native に 1M 読む (strata_control/dsv41_context.CHOICES、
#    純正メニューも 8K–1M)。旧い 262144 上限は trained の誤読だった。
#  - own-path (Qwen3.6): このエンジンパスに rope scaling が無く trained 262144 が上限。
#  - Hy4-Flash (0.2.19+): 純正メニューの 8K–128K がそのまま (hy4_family.CONTEXTS。
#    既定 128K は owner 決定 2026-11-19)。
#  - Inkling-2-Mini (0.2.19+): メニューは 256K 止まりだが "up to 1M supported"
#    (明示 --context は検証されず素通り) なので 1M まで出す。既定 32K。
D = getattr(S, "dsv41_context", None)
ds41_choices = list(getattr(D, "CHOICES", (8192, 32768, 65536, 131072, 262144, 524288, 1048576)))
ctx_all = sorted(set(getattr(S, "CONTEXTS", []) or
                     [8192, 32768, 65536, 131072, 204800, 262144, 393216, 524288]) | {1048576})
# Inkling-2-Mini のコンテキスト上限はこの PC の VRAM から計算する (モデル別 contexts)。
# 単一 GPU CUDA エンジン (0.1.65) は --vram-experts-gb / --ram-experts-gb を無視
# (HIP / CPU / 2 枚 EP 専用のフラグ) し、自分の規則で全エキスパートを VRAM に常駐させる
# (ink_runner.cuh: vfree > sec4 + reserve なら 100% VRAM)。さらに KV の自動サイジングが
# 空き VRAM を 480MiB の予約だけ残して貪欲に埋めるため、その後のワークスペース (~40 バッファ)
# + DSpark + cuBLAS + CUDA グラフの確保が OOM する (2026-11-23 実測: RTX PRO 4000
# Blackwell 24GB で SQ-20 @ 262144 → auto 131520 → exit 3 / ink_cuda.cuh:54 cudaMalloc)。
# そこで「KV + 固定 + 余裕 <= エキスパートロード後の空き」になる ctx だけを出す。
# 定数は inkling_fit (submodule) とエンジンソース (ink_cuda.cuh / ink_runner.cuh)。
# 余裕 0.8 GiB = ワークスペース ~0.5 (MB=512 × hidden=2304 の ~40 バッファ + DSpark
# scratch 9216×2304 bf16 + stage 48×32×6656 等を実計算) + cuBLAS / CUDA グラフ +
# アロケータ断片化。Vision は VRAM が足りなければ CPU ViT にフォールバックする
# (ink_runner.cuh) ので VRAM 余裕には含めない。parallel=1 / FP16 KV 基準
# (KV は並列数に比例 — ③ の並列数を上げるときは ctx を下げることが要る。UI が注記)
def inkling_ctxs(build, vram_gib, uma):
    IF = getattr(S, "inkling_fit", None)
    full = sorted(set(int(c) for c in (getattr(IF, "CONTEXTS", []) or [])) | {524288, 1048576})
    if IF is None or not vram_gib or uma:
        return full, None      # GPU 未検出 / UMA: エンジンの UMA 経路は別 (上限なし)
    try:
        b = IF.BUILDS[build]
        G = float(IF.GIB_TO_GB)
        dense = float(b["dense_gb"]) / G
        bodies = float(b["bodies_gb"]) / G
        # エンジンの常駐判定 reserve (ink_runner.cuh: >=20GiB 1600MiB / <=13GiB 780MiB / 他 1280MiB)
        reserve = 1.6 if vram_gib >= 20 else (0.78 if vram_gib <= 13 else 1.28)
        if vram_gib - dense - 0.8 > bodies + reserve:
            free_after = vram_gib - dense - bodies - 1.2   # driver+context (実測 4.72GiB と一致)
        else:
            free_after = 1.91                              # hybrid: hyb_reserve (1664+256 MiB)
        budget = free_after - 0.8 - float(IF.SWA_AND_SCONV_GB_FP16) / G
        max_ctx = int(budget * G * 1e9 / float(IF.GLOBAL_KV_BYTES_PER_TOK_FP16))
        ctxs = [c for c in full if c <= max_ctx]
        if not ctxs:
            return [], ("全エキスパートを VRAM に載えた後の KV キャッシュ + ワークスペースの"
                        "余裕が足りません (エンジンの自動割り当てが OOM します)。"
                        "より小さいビルドがあるならそれを選ぶか、VRAM の大きいカードで実行してください")
        return ctxs, None
    except Exception:
        return full, None
ink_cards = [g for g in cards if S.gpu_problem(g) is None]
ink_best = max(ink_cards, key=lambda g: float(g.get("vram_gb") or 0)) if ink_cards else {}
for fid, f in fams.items():
    raw = getattr(S, "FAMILIES", {}).get(fid) or own.get(fid) or multi.get(fid) or hy4.get(fid) or ink.get(fid) or {}
    if fid in hy4 or fid in ink:
        # 独自エンジン家系: ファイル / 既定は strata_control の hy4_family / inkling_family
        # (setup.py が import したものがそのまま乗っている)。重みは .strata ネイティブ
        # (build + 共有 Engram / Vision shard)。vision は家系既定 (両家系とも ON)
        HF = getattr(S, "hy4_family", None) if fid in hy4 else getattr(S, "inkling_family", None)
        ctxs = [int(c) for c in (getattr(HF, "CONTEXTS", []) or [])]
        if fid in ink:
            ctxs = sorted(set(ctxs) | {524288, 1048576})
        f["contexts"] = ctxs
        f["max_context"] = max(ctxs) if ctxs else 262144
        # hy4: 純正が単一 GPU 宣言 ("runs on one GPU so far")。inkling: NVIDIA 2 枚の
        # expert parallel (--gpus 2 EP) が純正の仕様だが、0.2.19 の install_inkling2 は
        # multi (choose_gpus が返すカード番号のリスト) をカード辞書のリストとして
        # g.get("vendor") して AttributeError で落ちる upstream バグ (setup.py:10197)。
        # --yes の既定も 2 枚を選ぶため GPU 指定なしでも同じく落ちる。upstream が直すまで
        # launcher は単一 GPU でセットアップする (force_single_gpu: build_argv が --gpu を
        # 明示して純正の複数枚既定も回避。単一カードなら multi=[] でバグを踏まない)
        f["multi_gpu"] = False
        if fid in ink:
            f["force_single_gpu"] = True
            f["single_gpu_reason"] = ("Strata 0.2.19 の Inkling-2-Mini 複数 GPU セットアップは upstream バグ "
                                      "(install_inkling2 がカード番号のリストをカード辞書として扱い "
                                      "AttributeError で落ちる) のため、修正まで launcher は単一 GPU でセットアップします")
        for m, d in f["models"].items():
            d["vision"] = bool(raw.get("vision"))
            if fid in ink:
                # モデル別 contexts: この PC の VRAM で安定起動できる分だけ (上記 inkling_ctxs)
                ctxs, ferr = inkling_ctxs(m, float(ink_best.get("vram_gb") or 0),
                                          bool(ink_best.get("uma")))
                d["contexts"] = ctxs
                if ferr:
                    d["fits"] = False
                    d["fit_error"] = ferr
                d["rec_context"] = 32768 if 32768 in ctxs else (max(ctxs) if ctxs else None)
            try:
                if fid not in ink:
                    d["rec_context"] = int(HF.DEFAULT_CONTEXT)
                files = HF.files_of(m, True)      # vision ON 既定のダウンロードセット
                d["download_gb"] = round(sum(HF.size_of(n) or 0 for n in files) / 1e9, 1)
                found = [n for n in files if n in idx]
                d["have"] = {"files": len(files), "found": len(found),
                             "gb": round(sum(idx[n] for n in found) / 2**30, 1)}
            except Exception:
                pass
        continue
    if fid in own:
        f["contexts"] = [c for c in ctx_all if c <= 262144]
        f["max_context"] = 262144
    elif fid in multi:
        f["contexts"] = ds41_choices
        f["max_context"] = max(ds41_choices)
    else:
        f["contexts"] = ctx_all
        f["max_context"] = max(ctx_all)
    # own-path は純正が単一 GPU 宣言 ("runs on one GPU so far")。multi-file は tp 可
    f["multi_gpu"] = fid not in own
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
print(json.dumps({"families": fams, "contexts": ctx_all, "pc": pc}))
"""

def setup_dir(module_dir: Path) -> Path:
    """setup.py / serve / .venv / engine / config JSON が在るディレクトリ。

    Strata 0.2.16 は Terminal 部分 (setup.py / strata.py / serve / requirements.txt) を
    リポジトリ直下から terminal/ へ移した (.venv も terminal/.venv。純正 setup.sh が
    旧 clone の .venv を一度だけ移す)。古い clone とフォークはリポジトリ直下のまま。
    渡されたディレクトリ自身が terminal/ ならそのまま返す (冪等)。"""
    t = module_dir / "terminal"
    return t if (t / "setup.py").is_file() else module_dir


def _remap_layout_path(s: str, module_dir: Path, work: Path, *, output: bool = False) -> str:
    """module_dir 直下の旧パスを terminal/ の新パスへ。実在するものだけ書き換える
    (output=True のログパスは出力先なので新パスが未作成でも移す)。
    既に work 配下を指すパスは触らない (冪等 — 2 回目で terminal/terminal/ にしない)。"""
    prefix = str(module_dir) + os.sep
    if not s.startswith(prefix) or s.startswith(str(work) + os.sep):
        return s
    old = Path(s)
    try:
        new = work / old.relative_to(module_dir)
    except ValueError:
        return s
    if output or (not old.exists() and new.exists()):
        return str(new)
    return s


def migrate_terminal_layout(root: Path, module_dir: Path,
                            profiles: list[dict[str, Any]]) -> tuple[int, int]:
    """0.2.16 レイアウト移行: config JSON 内の旧絶対パス (exe / args / vision / log) と
    プロファイルの values.config を terminal/ へ書き換える。旧パスが消えて新パスが
    在るものだけ触る (純正 setup が既に移行済みの config は実在するので無変更)。

    対象: installer config (setup_dir 直下の strata-*.json) / プロファイル専用コピー
    (data/launch/strata-*.json) / 全プロファイルの values.config (この module 配下を
    指すもの)。profiles は in-place で書き換える (保存は呼び出し側)。
    返り値: (書き換えた config ファイル数, 書き換えたプロファイル数)。"""
    module_dir = module_dir.resolve()
    work = setup_dir(module_dir)
    if work == module_dir:
        return (0, 0)
    n_prof = 0
    for p in profiles:
        if not isinstance(p, dict):
            continue
        v = p.get("values") or {}
        raw = str(v.get("config") or "").strip()
        if not raw:
            continue
        new = _remap_layout_path(raw, module_dir, work)
        if new != raw:
            v["config"] = new
            n_prof += 1
    n_cfg = 0
    files = list(work.glob("strata-*.json"))
    files += (root / "data" / "launch").glob("strata-*.json")
    for f in files:
        if f.name.endswith(".shared-settings.json"):
            continue
        try:
            doc = json.loads(f.read_text("utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or ("exe" not in doc and "args" not in doc):
            continue
        hit = False
        for key in ("exe", "cwd", "tokenizer"):
            s = str(doc.get(key) or "")
            if s:
                r = _remap_layout_path(s, module_dir, work)
                if r != s:
                    doc[key] = r
                    hit = True
        s = str(doc.get("log") or "")
        if s:
            r = _remap_layout_path(s, module_dir, work, output=True)
            if r != s:
                doc["log"] = r
                hit = True
        for key in ("args", "args_written"):
            arr = doc.get(key)
            if isinstance(arr, list):
                for i, a in enumerate(arr):
                    if isinstance(a, str):
                        r = _remap_layout_path(a, module_dir, work)
                        if r != a:
                            arr[i] = r
                            hit = True
        vis = doc.get("vision")
        if isinstance(vis, dict):
            for k in ("exe", "model", "mmproj"):
                s = str(vis.get(k) or "")
                if s:
                    r = _remap_layout_path(s, module_dir, work)
                    if r != s:
                        vis[k] = r
                        hit = True
        if hit:
            tmp = f.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            tmp.replace(f)
            n_cfg += 1
    return (n_cfg, n_prof)


_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL = 60.0
# 読み出し失敗は長くキャッシュしない (maya_setup と同じ規律): 短いバックオフで
# 再試行し、その間は直近の成功表を返す — 高負荷時の timeout でウィザードが死ぬのを防ぐ
_FAIL: dict[str, float] = {}
_FAIL_RETRY = 10.0
_GOOD_MAX_AGE = 600.0
_READ_TIMEOUT = 180.0

VISIONS = ["yes", "no", "gpu", "cpu"]


def load_options(module_dir: Path) -> dict[str, Any]:
    """submodule 純正の表を JSON で読む。未 clone / 読み出し失敗なら {"available": false}。
    失敗時は直近の成功表 (10 分以内) を返し、短いバックオフで再試行する。"""
    d = setup_dir(module_dir)
    if not (d / "setup.py").is_file():
        return {"available": False, "families": {}, "contexts": [], "pc": {}}
    key = str(module_dir)
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    stale = hit[1] if hit and now - hit[0] < _GOOD_MAX_AGE else None
    unavailable = {"available": False, "families": {}, "contexts": [], "pc": {}}
    if key in _FAIL and now - _FAIL[key] < _FAIL_RETRY:
        return stale if stale is not None else unavailable
    py = d / ".venv" / "bin" / "python"
    if not py.exists():
        py = Path(sys.executable)
    out: dict[str, Any] = {"available": False, "families": {}, "contexts": [], "pc": {}}
    try:
        proc = subprocess.run([str(py), "-c", _DUMP_SNIPPET], cwd=str(d),
                              capture_output=True, text=True, timeout=_READ_TIMEOUT,
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
    if out.get("available"):
        _CACHE[key] = (time.monotonic(), out)
        _FAIL.pop(key, None)
        return out
    _FAIL[key] = time.monotonic()
    return stale if stale is not None else out


_SHARD_RE = re.compile(r"^(.*-)(\d{5})-of-(\d{5})(\.\w+)$")
# .strata: 0.2.19+ のネイティブ重み (Hy4-Flash / Inkling-2-Mini の build / Engram / Vision)
_PACK_SUFFIXES = (".gguf", ".bin", ".safetensors", ".strata")

# 純正 tune_commands の HY4_NO_TUNING / INKLING_NO_TUNING に対応する arch:
# この 2 家系は engine calibrate (tune) が拒否する — 速度とメモリ配置はエンジン
# (strata-hy4 / strata-inkling) が起動ごとに自動で決める。⑤ は開始前に正直に断る
NO_TUNING_ARCHS = {"hy4flash": "Tencent Hy4-Flash", "inkling2_loop": "Inkling-2-Mini"}


def config_family(data: dict[str, Any], families: dict[str, Any]) -> str | None:
    """config の家系を load_options() の表の arch から引く (Flash-Next 系は arch を
    持たないので None)。hy4flash / inkling2_loop / qwen35moe など、純正 setup.py の
    家系表が単一の源。"""
    arch = str(data.get("arch") or "")
    if not arch:
        return None
    for fid, f in (families or {}).items():
        if str(f.get("arch") or "") == arch:
            return str(fid)
    return None


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
    4 種: --native/--model/--gguf/-m のファイル (マルチシャードは -0000X-of-0000N の
    兄弟も)、--pack のディレクトリ、--engram / --vision の引数 (0.2.19+ の Hy4-Flash /
    Inkling-2-Mini: .strata ネイティブファイル)、vision.model/mmproj。tokenizer ディレクトリも見る。
    ok=None は「参照が見付からない config」= 判断しない (UI は重み表示なしで通す)。"""
    args = data.get("args") or []
    refs: list[Path] = []
    for i, a in enumerate(args[:-1]):
        if str(a) in ("--native", "--model", "--gguf", "-m", "--pack", "--engram"):
            refs.append(Path(str(args[i + 1])))
        elif str(a) == "--vision" and not str(args[i + 1]).startswith("--"):
            # Hy4-Flash / Inkling-2-Mini は --vision <file> (.strata Vision shard)。
            # Flash-Next 系の --vision は値なしのブールフラグなので参照にしない
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
    for p in sorted(setup_dir(module_dir).glob("strata-*.json")):
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
    if not (setup_dir(module_dir) / "setup.py").is_file():
        raise ValueError("Strata のソースが未取得です (git submodule update --init module/strata)")
    data = load_options(module_dir)
    if not data.get("available"):
        raise ValueError("Strata セットアップの表を読み出せませんでした "
                         "(PC が高負荷の可能性があります — 少し待ってから再試行してください)")
    fams: dict[str, Any] = data.get("families") or {}
    if fam not in fams or not fams[fam].get("models"):
        raise ValueError(f"family が一覧にありません: {fam}")
    if model not in fams[fam]["models"]:
        raise ValueError(f"model が {fam} の一覧にありません: {model}")
    entry = fams[fam]["models"][model]
    if entry.get("fits") is False:
        raise ValueError(str(entry.get("fit_error")
                             or f"{model} はこの PC の GPU では起動できません (VRAM 不足)"))
    # モデル別 contexts (Inkling-2-Mini = この VRAM で安定起動できる分) が在ればそれ、
    # 無ければ家系別の選択肢 (own-path 256K / DS4.1 の 8K–1M など。UI が出す範囲で検証)
    src = entry.get("contexts") if "contexts" in entry else fams[fam].get("contexts")
    contexts = [int(c) for c in (src or data.get("contexts") or [])]
    if "contexts" in entry and not contexts:
        raise ValueError(f"{model} で選べるコンテキストがありません (この PC の VRAM 不足)")
    if contexts and context not in contexts:
        raise ValueError(f"context が一覧にありません: {context} ({contexts})")
    if not 4096 <= context <= 4_194_304:
        raise ValueError(f"context が範囲外です: {context}")
    argv = ["setup.py", "--family", fam, "--model", model, "--context", str(context)]
    if vision:   # vision 非対応パックは --vision を渡さない (upstream 側でも vision 質問自体が出ない)
        argv += ["--vision", vision]
    gpus = str(opts.get("gpus") or "").strip().lower()
    single_reason = str(fams[fam].get("single_gpu_reason") or "")

    def _no_multi() -> ValueError:
        if single_reason:
            return ValueError(f"{fams[fam].get('title', fam)} は複数 GPU でセットアップできません: {single_reason}")
        return ValueError(f"{fams[fam].get('title', fam)} は現在単一 GPU 専用です (純正: runs on one GPU so far)")

    if gpus and gpus not in ("auto", ""):
        usable = {int(g["index"]) for g in (data.get("pc") or {}).get("gpus") or [] if g.get("usable")}
        if gpus == "all":
            if len(usable) < 2:
                raise ValueError("使える GPU が 2 枚無い環境で all は指定できません")
            if not fams[fam].get("multi_gpu", True):
                raise _no_multi()
            argv += ["--gpus", "all"]
        else:
            try:
                sel = [int(x) for x in gpus.split(",") if x.strip() != ""]
            except ValueError:
                raise ValueError(f"gpus が不正です: {gpus}")
            if not sel or len(set(sel)) != len(sel) or any(i not in usable for i in sel):
                raise ValueError(f"gpus が検出外の GPU を指しています: {gpus} (使える GPU: {sorted(usable)})")
            if len(sel) == 1:
                argv += ["--gpu", str(sel[0])]      # 単一カード指定は純正 --gpu
            else:
                if not fams[fam].get("multi_gpu", True):
                    raise _no_multi()
                argv += ["--gpus", ",".join(str(i) for i in sel)]
    # force_single_gpu (0.2.19 inkling の upstream バグ回避): 複数指定は上で拒否済み。
    # 未指定 (auto) も純正 --yes の既定が 2 枚を選んで同じバグを踏むため、明示の --gpu を
    # 渡す。カード選びは純正 fallback_card と同じ「VRAM 最大・同量は番号が小さい方」
    if fams[fam].get("force_single_gpu") and "--gpu" not in argv and "--gpus" not in argv:
        cards = [g for g in (data.get("pc") or {}).get("gpus") or [] if g.get("usable")]
        if cards:
            best = sorted(cards, key=lambda g: (-round(float(g.get("vram_gb") or 0)), int(g["index"])))[0]
            argv += ["--gpu", str(int(best["index"]))]
    argv += ["--yes", "--no-start", "--no-browser"]
    tag = str(fams[fam].get("tag") or "")
    config_name = f"strata-{(tag + model).lower()}.json"
    return argv, config_name
