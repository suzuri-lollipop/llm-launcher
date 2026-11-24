"""Project Maya の「モデル構成」(セットアップ画面 ②) のための補助。

背景: Maya の純正 maya.sh / maya.py は対話インストーラだが、質問はすべて
`--model / --ds4-quant / --mm3-quant / --hy4-quant / --qwen-quant / --context /
--gpu / --gpus / --parallel` の明示フラグ + `--yes` で既定確定にできる (ask() は
yes なら無入力 = 既定を返し、モデルダウンロードも `--download-model` で確認なし)。
つまり **upstream のインストーラを非対話でそのまま走らせられる** — モデル選定の表
(MODELS + G53_MODELS / DS4_QUANTS / MM3_QUANTS / HY4_QUANTS / QWEN_QUANTS) や
ダウンロード再開・SHA-256 検証・エンジンビルド・pack 作成まで再実装せず、
launcher は「表の読み出し」と「ホワイトリスト検証 + argv 組み立て」だけを持つ
(strata_setup と同じ立場)。

  * load_options(): 純正 maya.py を submodule cwd で import する短い subprocess から
    家系 (glm / deepseek / minimax / qwen / hy4 / ik2) 別のモデル表・CONTEXTS・PC の RAM・
    VRAM・GPU 一覧・データディスクの空き・エンジン実在 (build/maya 等)・家系別の推奨
    コンテキスト (純正の recommended_context / ds4_recommended_context /
    mm3_recommended_context / qwen_recommended_context そのもの。Hy4-Flash は
    hy4_fit / hy4_q4_contexts の「この PC に収まるコンテキスト」そのもの) と
    重み実在 (have) を JSON で受け取る。表の単一の源は常に submodule 側
    (upstream の bump で選択肢も増えていく)。60 秒キャッシュ。
    v1.1.68+ の Hy4-Flash (maya-hy4 エンジン) はモデルごとに contexts / rec_context /
    fits を持つ (VRAM プラン hy4_fit がサイズ別に計算する。GLM-5.3 (744B, G53_MODELS)
    も g53_contexts のモデル別 contexts)。v1.1.66+ の DeepSeek は量子化ごとに
    config が分かれる (ds4_config_path: maya-ds4.json / maya-deepseek-maya-s.json)。
    v1.1.72+ の Inkling-2-Mini (ik2, maya-ik2 エンジン・実験的プレビュー) は
    1 ファイルセット (unsloth UD-Q4_K_XL) で contexts / rec_context は ik2_fit
    (このカードの VRAM に全エキスパートが収まる最大。収まらないカードは offload
    パス = VRAM tier + pinned RAM、長いコンテキストは KV streaming でキャッシュの
    行を RAM に置く)。v1.1.74 で trained 1M まで・Windows + NVIDIA 対応・
    NVIDIA 2 枚まで (Linux のみ。max_gpus=2)。拒否は純正 IK2F.unsupported の
    理由そのまま (fits=False)。--parallel は CLI 非対応のまま (同時回答数は
    ダッシュボード Models ページが config に書く)。
  * build_argv(): 検証済みフラグのみ argv にする。family / model は読み出した表の
    キーそのもの、context はモデル別 contexts (Hy4-Flash / GLM-5.3) か家系別
    CONTEXTS の値、vision は on/off、gpus は検出した usable GPU の index 集
    (単一カード = --gpu N、複数 = --gpus "0,1" / all = 全 usable。家系の上限
    (max_gpus: ik2 = 2) 超えは拒否 — 純正 check_pc / unsupported が拒否する)。
    どれか一つでも外れたら InstallError 相当の ValueError を投げる。
    常に --yes --no-start --plain --download-model を付ける (完全非対話・起動しない・
    TUI でなくプレーンテキスト・モデルダウンロード確認なし)。
  * weights_status() / installed_configs(): maya-<model>.json が参照する重み
    (--glm-pack / --pack の index とその実ファイル、--gguf / --native のシャード、
    vision / tokenizer) の実在とサイズを検証する — 「一覧に出る = 起動できる」の根拠。
    UI に JSON 名は出さない (モデル名で出す)。
  * 設定 JSON の名前は純正の作法どおり (GLM: maya-<quant>.json、DeepSeek:
    maya-ds4.json / 2 つ目以降の量子化は maya-deepseek-maya-*.json (v1.1.66+ の
    ds4_config_path)、MiniMax: maya-mm3.json / maya-minimax-maya-*.json、
    Qwen: maya-cyber-frost.json / maya-qwen-ud-iq2_xs.json、Hy4-Flash:
    maya-hy4-flash-maya-*.json / maya-hy4-flash.json、Inkling-2-Mini:
    maya-inkling-2-mini.json — maya_quants.config_stem)。
    ジョブ完了後の result.config はこの名前ピンポイント + mtime フォールバックで
    解決し、result.parallel は config の args の --parallel (③ の並列数既定)。

① のインストール (installer.INSTALL_SPECS["maya"]) は機械部分だけ: .venv (標準
venv+pip — maya.sh / 純正 pip_install が -m pip を前提) → 薄い driver maya_tools.py が
純正 tools_step() で PY_PACKAGES + llama.cpp ソースを入れる。エンジンビルドは家系ごとに
異なる (build/maya / maya-ds4 / maya-mm3 / maya-hy4 / maya-ik2) ため ② の純正セットアップ内で
行われる (在ればスキップされる)。
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

# 表の読み出しだけ行う subprocess スニペット (純正 maya.py の import は stdlib 依存のみ)。
# 出力は最後の 1 行の JSON だけ — 何か print が増えても最後の {...} を取るので壊れない。
# 純正の fail() は sys.exit(1) なので、表の 1 エントリずつ (Exception, SystemExit) で
# 受ける — 1 つの manifest 欠損で表全体を落とさない (バッジが出ないだけ)。
_DUMP_SNIPPET = r"""
import argparse, json, shutil, sys
from pathlib import Path
sys.path.insert(0, ".")
sys.path.insert(0, "tools")
import maya as M
import setup as S
import maya_quants as Q
NS = argparse.Namespace(model_dir=None, models_dir=None)
def guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except (Exception, SystemExit):
        return None
pc = {}
try:
    pc["ram_gb"] = round(float(M.mem_gb()[0]), 1)
except Exception:
    pc["ram_gb"] = None
try:
    cards = S.gpus()
    pc["gpus"] = [{"index": int(g["index"]), "name": str(g["name"]),
                   "vram_gb": round(float(g.get("vram_gb") or 0.0), 1),
                   "usable": int(g.get("arch") or 0) >= M.MIN_CC} for g in cards]
except Exception:
    pc["gpus"] = []
usable = [g for g in pc["gpus"] if g["usable"]]
try:
    pc["vram_gb"] = round(max([g["vram_gb"] for g in usable], default=0.0), 1)
except Exception:
    pc["vram_gb"] = None
# --yes の GPU 既定は「usable 全部」(choose_gpus の推奨)。推奨コンテキストの計算も同じ立场
vram_total = sum(g["vram_gb"] for g in usable)
ram = pc.get("ram_gb") or 0.0
# モデル置き場は launcher の data/Maya-data (cwd = module/maya → root は 2 つ上)。
# 純正既定 (clone 隣接 Maya-data) ではなく launcher が --models-dir で渡す先を報告する。
data = Path.cwd().parent.parent / "data" / "Maya-data"
models = data / "models"
try:
    pc["data_dir"] = str(data)
    probe = data if data.exists() else data.parent
    pc["disk_free_gb"] = round(shutil.disk_usage(str(probe)).free / 2**30, 1)
except Exception:
    pass
# 純正 main() の backend 選択と同じ見立て (config が在ればその backend、無ければ
# first_run_backend)。DeepSeek の config 名 (ds4_config_path) が -hip  suffix /
# 量子化別名 (maya-deepseek-maya-s.json) を分けるのに使う
backend = "cuda"
try:
    cfgs = M.configs()
    if cfgs:
        b0 = M.read_json(cfgs[0]).get("backend")
        backend = b0 if b0 in ("hip", "metal") else "cuda"
    else:
        backend = M.first_run_backend()
except Exception:
    pass
families = {}
# ---- GLM-5.3-Flash (MODELS の GGUF / EXL3 ダウンロード) + GLM-5.3 (744B, G53_MODELS)
# 純正のモデルメニューは 2 行 (Flash / 744B) だが家系はどちらも glm (config の
# installer は quant のみ・exe は build/maya)。744B はモデル別に contexts を持つ
# (g53_contexts: KV streaming 無しで VRAM に収まる分だけ。純正 setup と同じ式)
glm = {}
for q, m in M.MODELS.items():
    if q in M.MODELS_BY_HAND:      # 手選びのダウンロード: 一覧には出さない (純正と同じ)
        continue
    e = {"about": str(m.get("about", "")), "label": Q.shown("glm", q),
         "download_gb": m.get("download_gb"), "vision": bool(m.get("vision")),
         "config": "maya-" + q.lower() + ".json"}
    d = guard(M.download_dir, models, q)
    shards = guard(M.local_shards, m, d) if d else None
    if shards:
        found = [s for s in shards if guard(M.file_incomplete, m, s) is None]
        e["have"] = {"files": len(shards), "found": len(found),
                     "gb": round(sum(s.stat().st_size for s in found) / 2**30, 1)}
    rc = guard(M.recommended_context, ("download", q), {"gpus": usable, "backend": "cuda"})
    if rc:
        e["rec_context"] = int(rc)
    glm[q] = e
for q, m in getattr(M, "G53_MODELS", {}).items():
    fig = guard(M.g53_figures, q) or (None, None)
    e = {"about": str(m.get("about", "")),
         "label": M.G53.LABEL + " · " + str(Q.glm53_quant(q)),
         "download_gb": m.get("download_gb"), "memory_gb": fig[0],
         "vision": False, "g53": True,
         "config": "maya-" + q.lower() + ".json"}
    d = guard(M.download_dir, models, q)
    shards = guard(M.local_shards, m, d) if d else None
    if shards:
        found = [s for s in shards if guard(M.file_incomplete, m, s) is None]
        e["have"] = {"files": len(shards), "found": len(found),
                     "gb": round(sum(s.stat().st_size for s in found) / 2**30, 1)}
    ctxs = guard(M.g53_contexts, {"gpus": cards, "backend": backend}, 1, None, None, fig[1])
    ctxs = [int(c) for c in (ctxs or M.CONTEXTS)]
    e["contexts"] = ctxs
    # 純正 choose_context の既定と同じ: DEFAULT_CONTEXT が一覧に在ればそれ、無ければ最大
    rc = M.DEFAULT_CONTEXT if M.DEFAULT_CONTEXT in ctxs else (ctxs[-1] if ctxs else None)
    if rc:
        e["rec_context"] = int(rc)
    glm[q] = e
# 純正 choose_model の推奨: 24GB 以下のカードだけなら Maya-S24、でなければ先頭
glm_rec = next(iter(glm), "")
if usable and all(g["vram_gb"] <= 24.5 for g in usable) and "Maya-S24" in glm:
    glm_rec = "Maya-S24"
# GLM-5.3-Flash の trained context は 1M (GGUF ヘッダの context_length = 1048576)。エンジンは
# KV streaming で latent cache を RAM に持つため 1M が動き、ダッシュボードも 1M まで出す。
# 純正メニューの CONTEXTS は 128K で止まるが、owner 方針 (2026-11-17: 対応する全ての
# バックエンド / モデルで 1M まで) で選択肢をここで拡張する。推奨は純正の式
# (recommended_context) のまま — より大きい選択は UI が注記して従う (純正の対話と同じ立場)。
glm_contexts = sorted(set(M.CONTEXTS) | {262144, 524288, 1048576})
families["glm"] = {"title": "GLM-5.3-Flash / GLM-5.3 (744B)", "models": glm,
                   "contexts": glm_contexts,
                   "max_context": max(glm_contexts), "multi_gpu": True, "rec": glm_rec,
                   "engine_ready": M.EXE.exists()}
# ---- DeepSeek-V4.1-Flash-1017 (DS4_QUANTS: Maya quant / unsloth GGUF / EXL3 / abliterated)
ds4 = {}
for k in M.DS4_QUANTS:
    m = guard(M.ds4_gguf_model, k) if k not in M.DS4_EXL3 else guard(M.ds4_model, k)
    m = m or {}
    gb = m.get("download_gb") or {"gguf-q8": 508, "gguf-q4": 501, "exl3": 414}.get(k)
    mem = guard(M.ds4_memory_gb, k)
    # v1.1.66+: 量子化ごとに config が分かれる (ds4_config_path — 既存の maya-ds4.json
    # が別の量子化のものなら maya-deepseek-maya-s.json 等の own 名)。純正と同じ解決
    cfg_name = guard(lambda: M.ds4_config_path({"backend": backend}, k).name)
    e = {"about": str(m.get("about", "")), "label": Q.shown("deepseek", k, gb),
         "download_gb": gb, "memory_gb": mem, "vision": True,
         "gated": k in M.DS4_ABL, "published": k not in M.DS4_MAYA or bool(guard(M.ds4_maya_published, k)),
         "config": cfg_name or "maya-ds4.json"}
    d = guard(M.ds4_dir, NS, models, {}, k)
    files = m.get("files") or {}
    if d and files:
        found = [n for n, sz in files.items() if (d / n).is_file() and (d / n).stat().st_size == sz]
        e["have"] = {"files": len(files), "found": len(found),
                     "gb": round(sum(files[n] for n in found) / 2**30, 1)}
    rc = guard(M.ds4_recommended_context, vram_total, ram, mem or M.DS4_MEMORY_GB, max(1, len(usable)), 1)
    if rc:
        e["rec_context"] = int(rc)
    ds4[k] = e
maya_pub = [k for k in M.DS4_MAYA if ds4.get(k, {}).get("published")]
families["deepseek"] = {"title": M.DS4, "models": ds4, "contexts": list(M.DS4_CONTEXTS),
                        "max_context": max(M.DS4_CONTEXTS), "multi_gpu": True,
                        "rec": maya_pub[0] if maya_pub else M.DS4_DEFAULT_QUANT,
                        "model_arg": M.DS4, "quant_flag": "--ds4-quant",
                        "engine_ready": M.DS4_EXE.exists()}
# ---- MiniMax-M3.1-Flash (MM3_QUANTS: EXL3 / Maya の GGUF)
mm3 = {}
mm3_exl3 = guard(lambda: M.mm3_models()[0])   # (model entry, dspark entry) の model 側
for k in M.MM3_QUANTS:
    gguf = k != "exl3"
    m = guard(M.mm3_gguf_entry, k) if gguf else mm3_exl3
    m = m or {}
    mem = guard(M.mm3_memory_gb, k)
    e = {"about": str(m.get("about", "")), "label": Q.shown("minimax", k, m.get("download_gb")),
         "download_gb": m.get("download_gb"), "memory_gb": mem, "vision": True,
         "published": not gguf or bool(guard(M.mm3_gguf_published, k)),
         "config": (Q.config_stem("minimax", k) or "maya-mm3") + ".json"}
    d = guard(M.mm3_dir, NS, models, {}, k)
    files = m.get("files") or {}
    if d and files:
        found = [n for n, sz in files.items() if (d / n).is_file() and (d / n).stat().st_size == sz]
        e["have"] = {"files": len(files), "found": len(found),
                     "gb": round(sum(files[n] for n in found) / 2**30, 1)}
    rc = guard(M.mm3_recommended_context, vram_total, ram, "int8", mem or M.MM3_MEMORY_GB, 1, None)
    if rc:
        e["rec_context"] = int(rc)
    mm3[k] = e
# 純正 choose_mm3_quant の推奨: この PC の VRAM+RAM に収まるもの (EXL3 優先、無ければ最小)
mm3_keys = ["exl3"] + [k for k in ("gguf-xs", "gguf-s") if mm3.get(k, {}).get("published")]
fit = [k for k in mm3_keys if vram_total + ram >= (mm3[k].get("memory_gb") or M.MM3_MEMORY_GB)]
mm3_rec = "exl3" if "exl3" in fit else (max(fit, key=lambda k: mm3[k].get("memory_gb") or 0) if fit
                                         else min(mm3_keys, key=lambda k: mm3[k].get("memory_gb") or 0))
families["minimax"] = {"title": M.MM3, "models": mm3, "contexts": list(M.MM3_CONTEXTS),
                       "max_context": max(M.MM3_CONTEXTS), "multi_gpu": True, "rec": mm3_rec,
                       "model_arg": M.MM3, "quant_flag": "--mm3-quant",
                       "engine_ready": M.MM3_EXE.exists()}
# ---- Qwen3.8-Flash-Next (CYBER-FROST / unsloth UD-IQ2_XS)
qwen = {}
for k in M.QWEN_QUANTS:
    m = guard(M.qwen_entry, k) or {}
    e = {"about": str(m.get("about", "")), "label": Q.shown("qwen", k, m.get("download_gb")),
         "download_gb": m.get("download_gb"), "memory_gb": m.get("memory_gb"),
         "vision": bool(guard(M.qwen_vision_model)), "config": Q.config_stem("qwen", k) + ".json"}
    d = guard(M.qwen_dir, NS, models, {}, k)
    files = m.get("files") or {}
    if d and files:
        found = [n for n, sz in files.items() if (d / n).is_file() and (d / n).stat().st_size == sz]
        e["have"] = {"files": len(files), "found": len(found),
                     "gb": round(sum(files[n] for n in found) / 2**30, 1)}
    rc = guard(M.qwen_recommended_context, vram_total, vram_total + ram < (m.get("memory_gb") or 0))
    if rc:
        e["rec_context"] = int(rc)
    qwen[k] = e
families["qwen"] = {"title": M.QWEN_SHOWN, "models": qwen, "contexts": list(M.QWEN_CONTEXTS),
                    "max_context": max(M.QWEN_CONTEXTS), "multi_gpu": True, "rec": "q5_k_m",
                    "model_arg": "cyber-frost", "quant_flag": "--qwen-quant",
                    "engine_ready": M.EXE.exists()}
# ---- Hy4-Flash (v1.1.68+: maya-hy4 エンジン。Maya の 4 サイズ + Tencent Q4_K_M)
# コンテキストはモデル別: Maya サイズは hy4_fit (この PC の VRAM/RAM に収まる分だけ。
# KV streaming で RAM に載る分も含む)、Q4_K_M は hy4_q4_contexts。純正のモデルメニュー
# (hy4_fit_step / set_up_hy4) と同じ計算。収まらないサイズは fits=False で選択不可
if hasattr(M, "HY4_QUANTS"):
    hy4 = {}
    hy4_usable = [g for g in cards if int(g.get("arch") or 0) >= M.HY4_MIN_CC]
    for k in list(M.HY4_QUANTS) + [M.HY4_Q4]:
        m = guard(M.hy4_entry, k) or {}
        gb = m.get("download_gb")
        e = {"about": str(m.get("about", "")), "label": Q.shown("hy4", k, gb),
             "download_gb": gb, "memory_gb": m.get("memory_gb"), "vision": True,
             "published": bool(guard(M.hy4_published, k)),
             "config": (Q.config_stem("hy4", k) or "maya-hy4-flash") + ".json"}
        d = guard(M.hy4_dir, NS, models, {}, k)
        files = m.get("files") or {}
        if d and files:
            found = []
            for n, sz in files.items():
                p = d / n
                if p.is_file() and p.stat().st_size == sz:
                    found.append(n)
                elif n == m.get("ngram"):
                    # n-gram テーブル (63.4 GB) は全サイズ共有: 別の Hy4 フォルダに
                    # 在れば純正 (hy4_places) と同じく再利用 = 取得済み扱い。
                    # hy4_places は <dir>/<name> のフルパスを返す
                    for alt in guard(M.hy4_places, models, n) or []:
                        if alt.is_file() and alt.stat().st_size == sz:
                            found.append(n)
                            break
            e["have"] = {"files": len(files), "found": len(found),
                         "gb": round(sum(files[n] for n in found) / 2**30, 1)}
        if k == M.HY4_Q4:
            ctxs = guard(M.hy4_q4_contexts, hy4_usable) or list(M.HY4_Q4_CONTEXTS)
            rc = guard(M.hy4_recommended_context, {"gpus": hy4_usable})
            e["contexts"] = [int(c) for c in ctxs]
        else:
            fit = guard(lambda: M.hy4_fit(M.hy4_quant_table(k), hy4_usable,
                                          pictures=True, ram_gb=ram))
            if fit and "error" not in fit:
                e["contexts"] = [int(c) for c in fit.get("contexts") or []]
                rc = fit.get("recommended")
                if fit.get("layout") and fit["layout"] != M.HY4_STANDARD:
                    e["layout"] = str(fit["layout"])
                if fit.get("streamed"):
                    e["streamed_contexts"] = [int(c) for c in fit["streamed"]]
            else:
                e["contexts"] = []
                e["fits"] = False
                e["fit_error"] = str((fit or {}).get("error")
                                     or "この PC の VRAM では起動できません")
                rc = None
        if rc:
            e["rec_context"] = int(rc)
        hy4[k] = e
    # 純正 choose_hy4_quant の推奨: この PC の VRAM+RAM に収まる Maya サイズ
    # (fit_recommendation)。公開済みが無ければ先頭
    hy4_sizes = {k: hy4[k].get("memory_gb") or 0 for k in M.HY4_QUANTS
                 if hy4.get(k, {}).get("published")}
    hy4_rec = guard(M.fit_recommendation, hy4_sizes, hy4_usable, ram) if hy4_sizes else None
    if not hy4_rec or hy4_rec not in hy4:
        hy4_rec = next((k for k in hy4 if hy4[k].get("published")), next(iter(hy4), ""))
    families["hy4"] = {"title": M.HY4_SHOWN, "models": hy4, "contexts": list(M.HY4_CONTEXTS),
                       "max_context": max(M.HY4_CONTEXTS), "multi_gpu": True, "rec": hy4_rec,
                       "model_arg": M.HY4, "quant_flag": "--hy4-quant",
                       "engine_ready": M.HY4_EXE.exists()}
# ---- Inkling-2-Mini (v1.1.72+: maya-ik2 エンジン・実験的プレビュー。1 ファイルセット =
# unsloth UD-Q4_K_XL)。コンテキストは ik2_fit.CONTEXTS (v1.1.74 で trained 1M まで。
# VRAM に収まらない分は offload パス / KV streaming = キャッシュの行を RAM に置く)。
# 推奨は recommended_context (全エキスパートが VRAM に収まる最大。RECOMMEND_MAX=262144
# 止まり)。v1.1.74: Windows + NVIDIA 対応・NVIDIA 2 枚まで (Linux のみ。大きい方が先)。
# 拒否は純正 IK2F.unsupported の理由そのまま (fits=False)。
# drafter は --drafting 既定 (auto): セットアップで取得し、エンジンが初回起動時に
# 1 回だけ計測する (⑤ の --speed-calibration は記録を消して再計測を促す)。
# --parallel はセットアップ / 起動 CLI では非対応のまま (純正 check_parallel。
# 同時回答数はダッシュボード Models ページの「Answers at once」が config に書く)
if hasattr(M, "IK2"):
    import ik2_fit as IK2F
    ik2 = {}
    m = guard(M.ik2_entry) or {}
    gb = m.get("download_gb")
    ik2_usable = [g for g in cards if int(g.get("arch") or 0) >= M.IK2_MIN_CC]
    vram_ik2 = max([g["vram_gb"] for g in ik2_usable], default=0.0)
    e = {"about": str(m.get("about", "")), "label": Q.shown("ik2", Q.IK2_QUANT, gb),
         "download_gb": gb, "vision": bool(guard(M.ik2_vision_model)),
         "experimental": True,
         "config": (Q.config_stem("ik2", Q.IK2_QUANT) or "maya-inkling-2-mini") + ".json"}
    d = guard(M.ik2_dir, NS, models, {})
    files = m.get("files") or {}
    if d and files:
        found = [n for n, sz in files.items() if (d / n).is_file() and (d / n).stat().st_size == sz]
        e["have"] = {"files": len(files), "found": len(found),
                     "gb": round(sum(files[n] for n in found) / 2**30, 1)}
    # 純正 check_pc の拒否と同じ見立て: カードが読めていない / 古すぎる / VRAM 不足 /
    # RAM+VRAM がモデルに届かない / Mac。理由が在れば fits=False で選択不可
    why = None
    if not ik2_usable:
        why = ("Inkling 2 Mini needs an NVIDIA V100 / RTX 20 or newer GPU (compute capability 7.0 or newer, "
               "8 GB of VRAM or more)")
    else:
        why = guard(IK2F.unsupported, "cuda", sys.platform.startswith("win"), 0, ik2_usable, ram)
    if why:
        e["contexts"] = []
        e["fits"] = False
        e["fit_error"] = str(why)
    else:
        e["contexts"] = [int(c) for c in IK2F.CONTEXTS]
        rc = guard(IK2F.recommended_context, vram_ik2, IK2F.DEFAULT_KV)
        if rc:
            e["rec_context"] = int(rc)
        plan = guard(IK2F.plan, vram_ik2, e.get("rec_context") or IK2F.CONTEXTS[0], IK2F.DEFAULT_KV) or {}
        if plan.get("layout"):
            e["layout"] = str(plan["layout"])   # resident / offload (UI の注記用)
        if plan.get("kv_streamed"):
            e["kv_streamed"] = True             # キャッシュの行を RAM に置く (UI の注記用)
    ik2[Q.IK2_QUANT] = e
    families["ik2"] = {"title": M.IK2_SHOWN + " (experimental)", "models": ik2,
                       "contexts": [int(c) for c in IK2F.CONTEXTS],
                       "max_context": max(IK2F.CONTEXTS), "multi_gpu": True, "max_gpus": 2,
                       "rec": Q.IK2_QUANT, "model_arg": M.IK2,
                       "engine_ready": M.IK2_EXE.exists()}
print(json.dumps({"families": families, "pc": pc}))
"""

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL = 60.0
# 読み出し失敗は長くキャッシュしない: 短いバックオフで再試行し、その間は直近の
# 成功表を返す。キャリブレーション等で CPU が占有されると dump が timeout するが、
# 失敗を 60s キャッシュするとウィザード② / ジョブ検証がその間死ぬ (2026-11-16 実害:
# 256G ダウンロードが開始できないまま「一覧が読めません」になり続けた)。
_FAIL: dict[str, float] = {}
_FAIL_RETRY = 10.0
_GOOD_MAX_AGE = 600.0
_READ_TIMEOUT = 180.0

VISIONS = ["on", "off"]


def load_options(module_dir: Path) -> dict[str, Any]:
    """submodule 純正の表を JSON で読む。未 clone / 読み出し失敗なら {"available": false}。
    失敗時は直近の成功表 (10 分以内) を返し、短いバックオフで再試行する。"""
    if not (module_dir / "maya.py").is_file():
        return {"available": False, "families": {}, "pc": {}}
    key = str(module_dir)
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    stale = hit[1] if hit and now - hit[0] < _GOOD_MAX_AGE else None
    if key in _FAIL and now - _FAIL[key] < _FAIL_RETRY:
        return stale if stale is not None else {"available": False, "families": {}, "pc": {}}
    py = module_dir / ".venv" / "bin" / "python"
    if not py.exists():
        py = Path(sys.executable)
    out: dict[str, Any] = {"available": False, "families": {}, "pc": {}}
    try:
        proc = subprocess.run([str(py), "-c", _DUMP_SNIPPET], cwd=str(module_dir),
                              capture_output=True, text=True, timeout=_READ_TIMEOUT,
                              env=dict(os.environ, PYTHONNOUSERSITE="1"))
        text = proc.stdout.strip()
        data = None
        if proc.returncode == 0:
            # 出力は最後の 1 行の JSON だけ。前置きの print が混ざっても壊れないよう
            # 後ろから行単位で parse する
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
# pack の index ファイル (家系別: GLM = index.txt / native_experts.txt、DS4 = tensors.txt、
# MM3 = mm3.txt、Hy4 = hy4.txt + tensors.txt)。pack が在る = そのモデルの index が作られている
_PACK_INDEX = ("index.txt", "native_experts.txt", "tensors.txt", "mm3.txt", "ds4.txt", "hy4.txt")
_WEIGHT_SUFFIXES = (".gguf", ".safetensors", ".bin")


def _pack_model_files(pack: Path) -> list[Path]:
    """pack が index する実モデルファイル。DS4 の tensors.txt は 'file N <path>' 行、
    GLM の native_experts.txt は先頭行に 'absolute offsets in <file>' を持つ。
    読めなければ空 (pack 自体の実在だけで ok 判定 — サイズは出さない)。"""
    out: list[Path] = []
    try:
        tensors = pack / "tensors.txt"
        if tensors.is_file():
            for ln in tensors.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"file \d+ (.+)", ln)
                if m:
                    p = Path(m.group(1).strip())
                    out.append(p if p.is_absolute() else pack.parent / p)
            if out:
                return out
        native = pack / "native_experts.txt"
        if native.is_file():
            with open(native, encoding="utf-8", errors="replace") as f:
                m = re.search(r"absolute offsets in ([^,)]+)", f.readline())
            if m:
                p = Path(m.group(1).strip())
                if not p.is_absolute():
                    p = pack.parent / p
                out.append(p)
                sm = _SHARD_RE.match(p.name)
                if sm:
                    for n in range(1, int(sm.group(3)) + 1):
                        sib = p.with_name(f"{sm.group(1)}{n:05d}-of-{sm.group(3)}{sm.group(4)}")
                        if sib != p:
                            out.append(sib)
    except OSError:
        pass
    return out


def weights_status(data: dict) -> dict:
    """maya-<model>.json が参照する重みの実在チェック。参照パターンは純正 config の形:
    --glm-pack / --pack (pack ディレクトリ + その index が指す実ファイル)、
    --gguf / --native / --model / --ple-gguf (GGUF。マルチシャードは兄弟も。
    --model は Inkling-2-Mini の GGUF 直読み)、vision.model/mmproj、
    tokenizer ディレクトリ。ok=None は「参照が見付からない config」= 判断しない。"""
    args = data.get("args") or []
    refs: list[Path] = []
    for i, a in enumerate(args[:-1]):
        if str(a) in ("--glm-pack", "--pack", "--gguf", "--native", "--model", "-m", "--ple-gguf"):
            p = Path(str(args[i + 1]))
            if p not in refs:
                refs.append(p)
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
        missing.append("tokenizer")
    if not refs:
        return {"ok": None if not missing else False, "size_gb": None, "missing": missing}
    for p in refs:
        if p.is_dir():
            # pack: index が在り、その指す実ファイルが揃っていること
            if not any((p / f).is_file() for f in _PACK_INDEX):
                missing.append(p.name)
                continue
            present += 1
            files = _pack_model_files(p)
            if files:
                for f in files:
                    if f.is_file():
                        total += f.stat().st_size / 2**30
                    else:
                        missing.append(f.name)
            else:
                # index のない旧 pack: 兄弟の重み (GGUF / safetensors) を数える
                for f in sorted(p.parent.iterdir()):
                    if f.is_file() and f.suffix in _WEIGHT_SUFFIXES:
                        total += f.stat().st_size / 2**30
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


def config_parallel(data: dict) -> int | None:
    """config の並列数。Maya は args の --parallel に入る (Strata のような top-level
    parallel は無い)。無い = 純正既定の 1 (③ の既定は None → UI が 1 に倒す)。"""
    args = data.get("args") or []
    for i, a in enumerate(args[:-1]):
        if str(a) == "--parallel":
            v = str(args[i + 1])
            return int(v) if v.isdigit() else None
    return None


def config_family(data: dict) -> str:
    """config の家系 (maya_quants.family_of と同じ見立て): installer.family → exe 名 → args。
    Hy4 (maya-hy4) も --pack を持つので DeepSeek より先に判定する。ik2 (maya-ik2) は
    --model (GGUF 直読み) で pack を持たない — exe / installer で判定する。"""
    inst = data.get("installer") or {}
    if inst.get("family") in ("glm", "deepseek", "minimax", "qwen", "hy4", "ik2"):
        return str(inst["family"])
    exe = Path(str(data.get("exe", ""))).name
    args = data.get("args") or []
    if exe.startswith("maya-hy4"):
        return "hy4"
    if exe.startswith("maya-ik2"):
        return "ik2"
    if exe.startswith("maya-mm3"):
        return "minimax"
    if "--ple-gguf" in args[:-1]:
        return "qwen"
    if "--pack" in args[:-1] or exe.startswith(("maya-ds4", "strata-ds4")):
        return "deepseek"
    return "glm"


def config_quant_key(data: dict) -> str | None:
    """config がどの量子化のものか (maya_quants.config_key と同じ)。installer 記録の
    無い古い config は None — config 名だけでしか突き合わせられない。"""
    inst = data.get("installer") or {}
    fam = config_family(data)
    if fam == "deepseek":
        return str(inst.get("ds4_quant") or "exl3") if inst else None
    if fam == "minimax":
        return str(inst.get("mm3_quant") or "exl3") if inst else None
    if fam == "hy4":
        return str(inst.get("hy4_quant") or "maya-xs") if inst else None
    if fam == "ik2":
        return str(inst.get("ik2_quant") or "ud-q4_k_xl") if inst else None
    if fam == "qwen":
        return str(inst.get("qwen_quant") or "q5_k_m").lower() if inst else None
    return str(inst["quant"]) if inst.get("quant") else None


def config_is_g53(data: dict) -> bool:
    """GLM-5.3 (744B, glm-dsa) の config か (純正 G53.config_is_glm_dsa の見立てを
    installer / model_name で代用 — pack を読まずに判定する)。家系は glm のまま:
    KV 形式がエンジン固有 (INT8 latent) で --kv 編集の対象外になる等の出し分けに使う。"""
    if str(data.get("model_name") or "") == "glm-5.3":
        return True
    q = str((data.get("installer") or {}).get("quant") or "")
    return q.upper().startswith("GLM-5.3-")


def installed_configs(module_dir: Path) -> list[dict[str, Any]]:
    """既に設定済みの maya-<model>.json (純正 setup が作った / 前回この画面で作った)。"""
    out = []
    for p in sorted(module_dir.glob("maya-*.json")):
        if p.name.endswith(".shared-settings.json"):
            continue
        try:
            data = json.loads(p.read_text("utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if "exe" not in data and "args" not in data:      # maya.py configs() と同じ見立て
            continue
        out.append({"file": str(p), "name": p.name,
                    "model_name": str(data.get("model_name") or p.stem[len("maya-"):] or ""),
                    "family": config_family(data), "quant_key": config_quant_key(data),
                    "parallel": config_parallel(data),
                    "weights": weights_status(data),
                    "mtime": p.stat().st_mtime})
    out.sort(key=lambda x: -x["mtime"])
    return out


def _merge_config_ready(module_dir: Path, data: dict[str, Any]) -> None:
    """既存 config (maya-<model>.json) の重み検証が ok なら、その実在をモデル表へ
    引き写して「取得済み」バッジにする — ユーザーに見えるのは常にモデルだけ。
    DeepSeek / MiniMax は量子化が違っても config 名を共有する (maya-ds4.json /
    maya-mm3.json) ので、installer 記録の量子化キーで突き合わせる — 名前だけだと
    「gguf-q8 が在る = 全量子化が取得済み」になって未導入の量子化まで選べなくなる。
    installer 記録の無い古い config は config 名 (純正の命名作法) で fallback。"""
    by_key: dict[tuple[str, str], Any] = {}
    by_name: dict[str, Any] = {}
    for c in installed_configs(module_dir):
        w = c.get("weights") or {}
        if not w.get("ok"):
            continue
        if c.get("quant_key"):
            by_key[(str(c.get("family") or ""), str(c["quant_key"]))] = w
        else:
            by_name[c["name"]] = w
    for fid, f in (data.get("families") or {}).items():
        for m, d in (f.get("models") or {}).items():
            w = by_key.get((fid, m)) or by_name.get(str(d.get("config") or ""))
            if w and not (d.get("have") or {}).get("found"):
                d["have"] = {"files": 1, "found": 1, "gb": w.get("size_gb")}


def build_argv(module_dir: Path, opts: dict[str, Any], *,
               root: Path | None = None) -> tuple[list[str], str]:
    """UI の選択を純正 maya.py の argv と、できるはずの設定 JSON 名に直す。

    ホワイトリスト: family / model は load_options() が submodule から読んだ表のキー、
    context は家系別 CONTEXTS の値、vision は VISIONS。どれか一つでも外れたら
    InstallError 相当の ValueError を投げる (argv 化の前に必ず here を通すこと)。
    root は launcher のプロジェクトルート (モデル置き場 data/Maya-data の解決用)。"""
    fam = str(opts.get("family") or "").strip()
    model = str(opts.get("model") or "").strip()
    vision = str(opts.get("vision") or "").strip().lower()
    try:
        context = int(opts.get("context"))
    except (TypeError, ValueError):
        raise ValueError("context が不正です")
    if vision and vision not in VISIONS:
        raise ValueError(f"vision が不正です ({'/'.join(VISIONS)})")
    if not (module_dir / "maya.py").is_file():
        raise ValueError("Maya のソースが未取得です (git submodule update --init module/maya)")
    data = load_options(module_dir)
    if not data.get("available"):
        raise ValueError("Maya セットアップの表を読み出せませんでした "
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
    # コンテキストのホワイトリスト: モデル別 contexts (Hy4-Flash = hy4_fit /
    # GLM-5.3 (744B) = g53_contexts) が在ればそれ、無ければ家系別 CONTEXTS
    src = entry.get("contexts") if "contexts" in entry else fams[fam].get("contexts")
    contexts = [int(c) for c in (src or [])]
    if "contexts" in entry and not contexts:
        raise ValueError(f"{model} で選べるコンテキストがありません (この PC の VRAM/RAM 不足)")
    if contexts and context not in contexts:
        raise ValueError(f"context が一覧にありません: {context} ({contexts})")
    if not 4096 <= context <= 4_194_304:
        raise ValueError(f"context が範囲外です: {context}")
    argv = ["maya.py"]
    model_arg = str(fams[fam].get("model_arg") or "")
    if model_arg:
        # DeepSeek / MiniMax / Qwen / Hy4-Flash: --model は家系そのもの、ファイルは
        # quant フラグで選ぶ。Inkling-2-Mini は quant フラグ無し (1 ファイルセット)
        argv += ["--model", model_arg]
        quant_flag = str(fams[fam].get("quant_flag") or "")
        if quant_flag:
            argv += [quant_flag, model]
    else:
        argv += ["--model", model]
    argv += ["--context", str(context)]
    if vision == "off" and entry.get("vision"):
        argv += ["--no-vision"]
    # 並列数 (設定先行フローの ③ の回答): セットアップ時点で config の --parallel に
    # なる。DS4 / MM3 の推奨コンテキスト計算にも使われる。Qwen / Hy4-Flash は
    # 1 リクエストずつ (純正 check_parallel が --parallel を拒否) なので渡さない
    par = opts.get("parallel")
    if par is not None and str(par).strip() != "":
        try:
            par_n = int(par)
        except (TypeError, ValueError):
            raise ValueError("parallel が不正です")
        if not 1 <= par_n <= 8:
            raise ValueError(f"parallel が範囲外です: {par_n} (1-8)")
        if fam == "qwen":
            raise ValueError("Qwen3.8-Flash-Next は --parallel 非対応です (1 リクエストずつ回答します)")
        if fam == "hy4":
            raise ValueError("Hy4-Flash は --parallel 非対応です (1 リクエストずつ回答します)")
        if fam == "ik2":
            raise ValueError("Inkling 2 Mini は --parallel 非対応です (1 リクエストずつ回答します)")
        argv += ["--parallel", str(par_n)]
    # drafter (DSpark / MTP / DFly2) の速度計測はセットアップでは行わない (--drafting off)。
    # 純正 --yes 既定は計測 (measure) だが、それは 10-30 分の GPU 独占計測で、
    # launcher では ⑤ のキャリブレーション (--calibrate + --speed-calibration) に
    # 集約する — メモリガード / OOM 保護 / GPU 独占制御が効くのはあちら。
    # off は「計測せず drafter なし」の選択を config に記録するので、起動時に
    # 勝手に計測が走ることもない (⑤ をスキップしたら drafter なしで起動)。
    # GLM は既定の MTP draft で計測自体が起きない (DFlash2 は手動 setup 専用)。
    # MiniMax の短い prefill 計測 (数分) だけは純正セットアップに含まれる (flag 無し)。
    # Hy4-Flash: MTP ブロック / DFly2 のファイル自体は (VRAM に収まるなら)  download
    # され pack に入る — ⑤ の --speed-calibration が両者の A/B を計測して速い方を
    # config に書く (純正 speed_calibration_hy4。maya_speed_wanted が対象を判定)。
    # Inkling-2-Mini (ik2) はここに含めない: 純正既定 --drafting auto はドラフターを
    # セットアップで取得し、計測はエンジンが初回起動時に 1 回だけ行う (~1 分・最初の
    # 回答の前)。セットアップ中の GPU 独占計測では無いので純正既定のまま通す。
    # ⑤ の --speed-calibration は計測記録 (data/ik2-drafting.txt) を消して次回起動時の
    # 再計測を促す (純正 ik2_draft_forget)。Volta / Turing / offload パスでは純正が
    # 自動的に drafting off にする (ik2_drafting / drafting_off_reason)
    if fam in ("deepseek", "minimax", "qwen", "hy4"):
        argv += ["--drafting", "off"]
    gpus = str(opts.get("gpus") or "").strip().lower()
    if gpus and gpus not in ("auto", ""):
        usable = sorted({int(g["index"]) for g in (data.get("pc") or {}).get("gpus") or []
                         if g.get("usable")})
        multi_ok = fams[fam].get("multi_gpu", True) is not False
        max_g = fams[fam].get("max_gpus")      # ik2: 2 枚まで (純正 unsupported が 3 枚以上を拒否)
        if gpus == "all":
            if not multi_ok:
                raise ValueError(f"{fams[fam].get('title', fam)} は単一 GPU 専用です (純正: one GPU only for now)")
            if len(usable) < 2:
                raise ValueError("使える GPU が 2 枚無い環境で all は指定できません")
            if max_g and len(usable) > max_g:
                raise ValueError(f"{fams[fam].get('title', fam)} が使えるのは最大 {max_g} 枚です (カードを選んで指定してください)")
            argv += ["--gpus", ",".join(str(i) for i in usable)]
        else:
            try:
                sel = [int(x) for x in gpus.split(",") if x.strip() != ""]
            except ValueError:
                raise ValueError(f"gpus が不正です: {gpus}")
            if not sel or len(set(sel)) != len(sel) or any(i not in usable for i in sel):
                raise ValueError(f"gpus が検出外の GPU を指しています: {gpus} (使える GPU: {usable})")
            if len(sel) > 16:
                raise ValueError("Maya が分割できるのは最大 16 枚です")
            if len(sel) > 1 and not multi_ok:
                raise ValueError(f"{fams[fam].get('title', fam)} は単一 GPU 専用です (純正: one GPU only for now)")
            if max_g and len(sel) > max_g:
                raise ValueError(f"{fams[fam].get('title', fam)} が使えるのは最大 {max_g} 枚です (純正: three or more are not in it yet)")
            if len(sel) == 1:
                argv += ["--gpu", str(sel[0])]      # 単一カード指定は純正 --gpu
            else:
                argv += ["--gpus", ",".join(str(i) for i in sel)]
    # モデル置き場は launcher の data/ (gitignore 済み) 配下へ明示指定する。
    # maya.py は config の installer.models_dir に記録するので、起動時もそこを見る。
    r = root if root is not None else module_dir.parent.parent
    argv += ["--models-dir", str(r / "data" / "Maya-data" / "models")]
    # 完全非対話: --yes (全質問が既定確定) + --no-start (起動しない) + --plain (TUI でなく
    # プレーンテキスト) + --download-model (モデルダウンロードの確認なし。再開対応は純正)
    argv += ["--yes", "--no-start", "--plain", "--download-model"]
    config_name = str(entry.get("config") or f"maya-{model.lower()}.json")
    return argv, config_name
