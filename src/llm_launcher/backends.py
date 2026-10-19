"""バックエンド(vllm / sglang / llama.cpp / FreeToken / Strata / Project Maya)の定義と起動コマンド組み立て。

各バックエンドは通常 `module/` 配下の git submodule だが、
フォーク (freetoken-fork) は `module/fork/` 配下の非追跡 clone を使う
(フォルダの .gitkeep だけを追跡。詳しくは module/fork/README.md)。

設計:
  - 起動引数の **型・choices・既定値** は `fields/<backend>.json` 正表が権威。
    正表は `python -m llm_launcher.gen_fields` で submodule ソースから生成する
    (vLLM の tool/reasoning parser 51/34種、quantization 30種、kv-cache-dtype 等は
    ソース実在値。SGLang は arg_groups/fields AST 抽出 491 flags)。
  - このファイルは UI に出すフラグの選択 + 日本語ラベル/セクション/上書き用の
    choices (動的生成で抽出不能だったもの) だけを管理する。
    正表に無いフラグを参照した場合は埋め込みフォールバックで補完する。
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

FIELDS_DIR = Path(__file__).parent / "fields"

# 派生バックエンド (fork 等) は専用の fields/<id>.json があるまで基底の正表を流用する。
# `python -m llm_launcher.gen_fields` で fork ソースから生成すればそれが優先される。
TABLE_FALLBACK: dict[str, str] = {"freetoken-fork": "freetoken", "strata-fork": "strata"}

_tables: dict[str, dict[str, Any]] = {}


def load_table(backend_id: str) -> dict[str, Any]:
    if backend_id not in _tables:
        bid = backend_id
        p = FIELDS_DIR / f"{bid}.json"
        if not p.exists() and bid in TABLE_FALLBACK:
            bid = TABLE_FALLBACK[bid]
            p = FIELDS_DIR / f"{bid}.json"
        _tables[backend_id] = json.loads(p.read_text("utf-8")) if p.exists() else {}
    return _tables[backend_id]


# llama.cpp は C++ 独自 DSL のため生成器の網羅が弱い部分があり、
# ソース照合済みの補完表を持つ (型・既定値は common/arg.cpp と common.h ベース)。
LLAMA_SUPPLEMENT: dict[str, dict[str, Any]] = {
    "--model": {"type": "str"},
    "--alias": {"type": "str"},
    "--hf-repo": {"type": "str"}, "--hf-file": {"type": "str"}, "--hf-token": {"type": "str"},
    "--chat-template": {"type": "str"},
    "--jinja": {"type": "bool"},
    "--reasoning": {"type": "select", "choices": ["auto", "on", "off"]},
    "--lora": {"type": "str"},
    "--ctx-size": {"type": "int", "default": "4096"},
    "--n-gpu-layers": {"type": "int", "default": "0"},
    "--batch-size": {"type": "int", "default": "2048"},
    "--ubatch-size": {"type": "int", "default": "512"},
    "--parallel": {"type": "int", "default": "1"},
    "--threads": {"type": "int", "default": "0"},
    "--flash-attn": {"type": "select", "choices": ["auto", "on", "off"], "default": "auto"},
    "--load-mode": {"type": "select", "choices": ["auto", "none", "mmap", "mmap+mlock"], "default": "auto"},
    "--cache-type-k": {"type": "select", "choices": ["", "f32", "f16", "bf16", "q8_0", "q4_0", "iq4_nl", "mxfp4"], "default": "f16"},
    "--cache-type-v": {"type": "select", "choices": ["", "f32", "f16", "bf16", "q8_0", "q4_0", "iq4_nl", "mxfp4"], "default": "f16"},
    "--defrag-thold": {"type": "float"},
    "--temp": {"type": "float", "default": "0.80"},
    "--top-k": {"type": "int", "default": "40"},
    "--top-p": {"type": "float", "default": "0.95"},
    "--min-p": {"type": "float", "default": "0.05"},
    "--repeat-penalty": {"type": "float", "default": "1.00"},
    "--presence-penalty": {"type": "float", "default": "0.00"},
    "--frequency-penalty": {"type": "float", "default": "0.00"},
    "--seed": {"type": "int", "default": "-1"},
    "--host": {"type": "str", "default": "127.0.0.1"},
    "--port": {"type": "int", "default": "8080"},
    "--api-key": {"type": "str"},
    "--no-webui": {"type": "bool"},
    "--webui-path": {"type": "str"},
    "--metrics": {"type": "bool"},
    "--slots": {"type": "bool"},
    "--no-cont-batching": {"type": "bool"},
    "--embedding": {"type": "bool"},
    "--timeout-keep-alive": {"type": "int", "default": "5"},
    "--warmup": {"type": "bool"},
    "--spec-type": {"type": "select", "choices": ["", "none", "draft-model", "ngram", "ngram-mod", "cheat-sheet", "dflash", "eagle3"]},
}

# 動的生成 (レジストリ・関数) で正表が空になった分の選択肢補完。
CHOICE_SUPPLEMENT: dict[str, dict[str, list[str]]] = {
    "sglang": {
        "--kv-cache-dtype": ["", "auto", "fp8_e5m2", "fp8_e4m3", "bf16", "bfloat16"],
        "--dtype": ["", "auto", "float16", "bfloat16", "float32"],
        "--schedule-policy": ["", "lpm", "fcfs", "random", "priority"],
        "--sampling-backend": ["", "flashinfer", "pytorch"],
        "--log-level": ["", "critical", "error", "warning", "info", "debug"],
        "--tool-call-parser": ["", "auto", "deepseek", "deepseekv3", "glm", "glm4", "gpt-oss", "kimi_k2",
                               "llama_3_1", "mistral", "phi4", "qwen", "qwen25", "pythonic", "step3", "step3p5"],
        "--reasoning-parser": ["", "deepseek_r1", "deepseek_v3", "glm45", "qwen3", "qwen3_thinking",
                               "qwen3_moe", "kimi_k2", "minimax_m2", "mistral", "step3", "olmo3"],
    },
    "vllm": {
        "--generation-config": ["", "auto", "vllm"],
        "--distributed-executor-backend": ["", "mp", "ray", "uni", "external_launcher"],
        "--uvicorn-log-level": ["", "critical", "error", "warning", "info", "debug", "trace"],
        "--chat-template-content-format": ["", "auto", "string", "openai"],
    },
    "freetoken": {
        "--model-source": ["", "huggingface", "modelscope"],
        "--ple-backend": ["", "pinned", "disk"],
        "--nvfp4-backend": ["", "auto", "marlin", "flashinfer", "triton"],
        "--moe-cache-policy": ["", "lru"],
        "--cache-type": ["", "radix"],
    },
    "freetoken-fork": {
        # fork は PLE テーブル read-only mmap (OS page cache がホット行を保持) を追加
        "--ple-backend": ["", "pinned", "disk", "swap"],
    },
}

# 正表自体がSparseな vllm 数値/文字列系の補完 (arg_utils.py 準拠)
VLLM_SUPPLEMENT: dict[str, dict[str, Any]] = {
    "--model": {"type": "str"}, "--served-model-name": {"type": "str"},
    "--tokenizer": {"type": "str"}, "--revision": {"type": "str"},
    "--chat-template": {"type": "str"}, "--download-dir": {"type": "str"},
    "--api-key": {"type": "str"}, "--root-path": {"type": "str"},
    "--max-model-len": {"type": "int"}, "--seed": {"type": "int"},
    "--block-size": {"type": "int"}, "--max-num-seqs": {"type": "int"},
    "--max-num-batched-tokens": {"type": "int"},
    "--cpu-offload-gb": {"type": "float", "default": "0"},
    "--pipeline-parallel-size": {"type": "int"}, "--data-parallel-size": {"type": "int"},
    "--max-logprobs": {"type": "int", "default": "20"},
    "--max-loras": {"type": "int", "default": "1"}, "--max-lora-rank": {"type": "int"},
    "--host": {"type": "str"},
}

# 抽出時に付くノイズ (help文言の断片・sentinel・空コンテナ) を弾く
_JUNK_DEFAULTS = {"unused", "%s", "n", "()", "[]", "{}", "none",
                  "argparse.suppress", "suppress", "true", "false", "none"}

_alias_index: dict[str, dict[str, str]] = {}


def _resolve_alias(backend_id: str, flag: str) -> str:
    """--tp-size のような alias でも正表の本体エントリを引けるようにする。"""
    idx = _alias_index.get(backend_id)
    if idx is None:
        idx = {}
        for prim, spec in load_table(backend_id).items():
            for a in (spec or {}).get("aliases", []):
                idx.setdefault(a, prim)
        _alias_index[backend_id] = idx
    return idx.get(flag, flag)


def _spec(backend_id: str, flag: str) -> dict[str, Any]:
    """正表→補完表の順で {type,choices,default} を解決する (alias 追従)。"""
    table = load_table(backend_id)
    spec = dict(table.get(flag) or table.get(_resolve_alias(backend_id, flag)) or {})
    if backend_id == "llamacpp":
        spec = {**spec, **{k: v for k, v in LLAMA_SUPPLEMENT.get(flag, {}).items() if v not in (None, [])}}
    sup = (VLLM_SUPPLEMENT if backend_id == "vllm" else {})
    spec = {**sup.get(flag, {}), **{k: v for k, v in spec.items() if v not in (None, [])}} if backend_id == "vllm" else spec
    # 基底バックエンドの補完を先に当て、派生 (fork) 専用の補完があればそれが上書きする
    for ch_key in (TABLE_FALLBACK.get(backend_id, backend_id), backend_id):
        if flag in CHOICE_SUPPLEMENT.get(ch_key, {}):
            spec = {**spec, "choices": CHOICE_SUPPLEMENT[ch_key][flag]}
    d = spec.get("default")
    if isinstance(d, str) and (d.strip().lower() in _JUNK_DEFAULTS or d.strip() in ("()", "[]", "{}")):
        # ただし choices に実在する値 (--speculative の "none" 等) は junk ではなく正当な既定値
        if not (isinstance(spec.get("choices"), list) and d in spec["choices"]):
            spec.pop("default")
    if isinstance(d, bool):
        # bool 既定は store_true 意味 → bool 型へ正規化し既定値は空にする
        spec.setdefault("type", "bool")
        spec["default"] = "" if not d else ""
    if spec.get("type") == "bool":
        spec.pop("default", None)
    return spec


# ---------------------------------------------------------------------------
# UI フィールド構築ヘルパ (正表参照型)
# ---------------------------------------------------------------------------


def fld(backend_id: str, flag: str, label: str, *, section: str = "model",
        placeholder: str = "", help: str = "", default: str | None = None,
        ftype: str | None = None, choices: list[str] | None = None,
        default_on: bool = True, required: bool = False) -> dict[str, Any]:
    spec = _spec(backend_id, flag)
    ch = choices if choices is not None else spec.get("choices")
    t = ftype or spec.get("type") or "str"
    if ch and ftype is None and t != "bool":
        t = "select"          # 選択肢が抽出できた項目は select 化
    key = re.sub(r"^--", "", flag).replace("-", "_")
    d = spec.get("default") if default is None else default
    if d is None:
        d = ""
    if t == "bool":
        d = ""
    if t == "select":
        ch = list(ch or [])
        if "" not in ch:
            ch.insert(0, "")
        if d and d not in ch:
            ch.insert(1, d)
    entry: dict[str, Any] = {"key": key, "flag": flag, "type": t, "label": label,
                             "section": section, "placeholder": placeholder,
                             "default": str(d) if d != "" else "", "help": help or spec.get("help", "")[:160],
                             "required": bool(required)}
    # bool 以外の送信 ON/OFF 既定 (auto 決定を上書きしうる項目は False)
    entry["default_on"] = bool(default_on) or bool(required)
    if t == "select":
        entry["options"] = ch
    return entry


SECTIONS_JA = {
    "model": "モデル・提供設定",
    "parallel": "並列・分散",
    "sched": "スケジューリング・メモリ",
    "exec": "実行エンジン・最適化",
    "server": "サーバー・API",
    "tools": "ツール呼び出し・推論・LoRA",
    "sampling": "デフォルトサンプリング",
    "cache": "KVキャッシュ・オフロード",
    "logs": "ログ・可観測性",
}


def sections_order(backend: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for f in backend.get("fields", []):
        s = f.get("section", "model")
        if s not in seen:
            seen.append(s)
    return seen


# ---------------------------------------------------------------------------
# バックエンド定義
# ---------------------------------------------------------------------------


def build_vllm() -> dict[str, Any]:
    b = "vllm"
    F = lambda flag, label, **kw: fld(b, flag, label, **kw)  # noqa: E731
    fields = [
        F("--model", "モデル", placeholder="Qwen/Qwen2.5-7B-Instruct またはローカルパス"),
        F("--served-model-name", "提供モデル名", placeholder="省略時は --model"),
        F("--tokenizer", "トークナイザ (任意)", placeholder="モデルと異なる場合のみ"),
        F("--revision", "リビジョン/ブランチ (任意)"),
        F("--chat-template", "チャットテンプレートファイル (任意)"),
        F("--chat-template-content-format", "テンプレート内容形式", choices=None),
        F("--max-model-len", "最大コンテキスト長", placeholder="空=自動 (モデルの学習長)"),
        F("--dtype", "dtype"),
        F("--quantization", "量子化"),
        F("--load-format", "ウェイト読み込み形式"),
        F("--kv-cache-dtype", "KVキャッシュ量子化", help="fp8 系で KV メモリを削減"),
        F("--download-dir", "DLキャッシュDIR (任意)"),
        F("--trust-remote-code", "trust-remote-code"),
        F("--seed", "乱数seed", placeholder="空=ランダム"),
        F("--generation-config", "生成設定"),
        F("--tensor-parallel-size", "Tensor並列数 (TP)", section="parallel"),
        F("--pipeline-parallel-size", "Pipeline並列数 (PP)", section="parallel", placeholder="1"),
        F("--data-parallel-size", "Data並列数 (DP)", section="parallel", placeholder="1"),
        F("--enable-expert-parallel", "Expert並列 (MoE)", section="parallel"),
        F("--distributed-executor-backend", "実行バックエンド", section="parallel", help="空=自動"),
        F("--gpu-memory-utilization", "GPUメモリ使用率", section="sched"),
        F("--cpu-offload-gb", "CPUオフロード (GB)", section="sched", placeholder="0"),
        F("--max-num-batched-tokens", "最大バッチトークン数", section="sched", placeholder="自動"),
        F("--max-num-seqs", "最大同時系列数", section="sched", placeholder="自動"),
        F("--block-size", "KVブロックサイズ", section="sched", placeholder="自動 (16)"),
        F("--enable-prefix-caching", "prefix caching", section="sched"),
        F("--enable-chunked-prefill", "chunked prefill", section="sched", help="v1エンジンでは既定で有効"),
        F("--enforce-eager", "eager モード (CUDA graph 無効)", section="sched"),
        F("--host", "ホスト", section="server", default="0.0.0.0"),
        F("--port", "ポート", section="server"),
        F("--api-key", "APIキー", section="server", placeholder="設定すると Bearer 認証"),
        F("--root-path", "ルートパス (任意)", section="server", help="リバースプロキシ配下運用時"),
        F("--uvicorn-log-level", "uvicornログレベル", section="server"),
        F("--disable-uvicorn-access-log", "accessログ無効", section="server"),
        F("--disable-fastapi-docs", "/docs 無効", section="server"),
        F("--allow-credentials", "CORS credentials", section="server"),
        F("--enable-auto-tool-choice", "auto tool choice", section="tools", help="--tool-call-parser と併用必須"),
        F("--tool-call-parser", "ツールコールパーサ", section="tools"),
        F("--reasoning-parser", "reasoningパーサ", section="tools"),
        F("--enable-lora", "LoRA有効", section="tools"),
        F("--max-loras", "LoRA最大数", section="tools"),
        F("--max-lora-rank", "LoRA最大ランク", section="tools", placeholder="16"),
        F("--max-logprobs", "最大logprobs", section="tools"),
        F("--disable-log-stats", "統計ログ無効", section="logs"),
    ]
    return _backend_meta(b, fields)


def build_sglang() -> dict[str, Any]:
    b = "sglang"
    F = lambda flag, label, **kw: fld(b, flag, label, **kw)  # noqa: E731
    fields = [
        F("--model-path", "モデル", placeholder="Qwen/Qwen2.5-7B-Instruct またはローカルパス"),
        F("--tokenizer-path", "トークナイザパス (任意)"),
        F("--served-model-name", "提供モデル名 (任意)"),
        F("--chat-template", "チャットテンプレート (任意)"),
        F("--context-length", "最大コンテキスト長", placeholder="空=モデル設定に従う"),
        F("--json-model-override-args", "config.json オーバーライド (JSON)", placeholder='{"max_position_embeddings": 65536}'),
        F("--dtype", "dtype"),
        F("--kv-cache-dtype", "KVキャッシュ量子化"),
        F("--quantization", "量子化"),
        F("--load-format", "ウェイト読み込み形式"),
        F("--download-dir", "DLキャッシュDIR (任意)"),
        F("--revision", "リビジョン/ブランチ (任意)"),
        F("--trust-remote-code", "trust-remote-code"),
        F("--tp-size", "Tensor並列数 (TP)", section="parallel"),
        F("--pp-size", "Pipeline並列数 (PP)", section="parallel", placeholder="1"),
        F("--dp-size", "Data並列数 (DP)", section="parallel", placeholder="1"),
        F("--ep-size", "Expert並列数 (EP)", section="parallel", placeholder="1"),
        F("--enable-dp-attention", "DP attention", section="parallel"),
        F("--nnodes", "ノード数", section="parallel", placeholder="1"),
        F("--node-rank", "このノードの rank", section="parallel", placeholder="0"),
        F("--dist-init-addr", "分散初期化先 (host:port)", section="parallel", placeholder="マルチnode時のみ"),
        F("--mem-fraction-static", "静的メモリ比率", section="sched", ftype="float", placeholder="空=自動"),
        F("--max-running-requests", "最大実行中リクエスト", section="sched", placeholder="自動"),
        F("--max-total-tokens", "KV総トークン数", section="sched", placeholder="自動"),
        F("--chunked-prefill-size", "chunked prefillサイズ", section="sched", placeholder="自動"),
        F("--max-prefill-tokens", "最大prefillトークン", section="sched"),
        F("--schedule-policy", "スケジューリング方針", section="sched"),
        F("--page-size", "ページサイズ", section="sched", placeholder="自動"),
        F("--disable-overlap-schedule", "overlapスケジューリング無効", section="sched"),
        F("--enable-mixed-chunk", "mixed chunk", section="sched"),
        F("--watchdog-timeout", "ウォッチドッグ秒", section="sched", placeholder="300"),
        F("--disable-radix-cache", "Radixキャッシュ無効", section="cache"),
        F("--radix-eviction-policy", "Radix追出方針", section="cache"),
        F("--enable-hierarchical-cache", "階層キャッシュ (HiCache)", section="cache"),
        F("--hicache-ratio", "HiCacheホスト比率", section="cache", ftype="float", placeholder="2.0"),
        F("--attention-backend", "Attentionバックエンド", section="exec"),
        F("--sampling-backend", "Samplingバックエンド", section="exec"),
        F("--grammar-backend", "Grammarバックエンド", section="exec"),
        F("--fp8-gemm-runner-backend", "FP8 GEMM バックエンド", section="exec"),
        F("--disable-cuda-graph", "CUDA graph 無効", section="exec"),
        F("--enable-torch-compile", "torch.compile", section="exec"),
        F("--cuda-graph-max-bs-decode", "CUDA graph 最大bs (decode)", section="exec", placeholder="自動"),
        F("--cpu-offload-gb", "CPUオフロード (GB)", section="exec", placeholder="0"),
        F("--base-gpu-id", "開始GPU ID", section="exec"),
        F("--gpu-id-step", "GPU IDステップ", section="exec"),
        F("--random-seed", "乱数seed", section="exec", placeholder="空=ランダム"),
        F("--host", "ホスト", section="server", default="0.0.0.0"),
        F("--port", "ポート", section="server"),
        F("--api-key", "APIキー", section="server", placeholder="設定すると Bearer 認証"),
        F("--admin-api-key", "管理者APIキー (任意)"),
        F("--tokenizer-worker-num", "トークナイザworker数", section="server"),
        F("--detokenizer-worker-num", "デトークナイザworker数", section="server", placeholder="1"),
        F("--skip-server-warmup", "ウォームアップスキップ", section="server"),
        F("--fastapi-root-path", "ルートパス (リバースプロキシ用)", section="server"),
        F("--tool-call-parser", "ツールコールパーサ", section="tools"),
        F("--reasoning-parser", "reasoningパーサ", section="tools"),
        F("--log-level", "ログレベル", section="logs"),
        F("--log-requests", "リクエスト内容をログ", section="logs"),
        F("--log-requests-level", "リクエストログ詳細度", section="logs", placeholder="0-3"),
        F("--decode-log-interval", "decodeログ間隔", section="logs"),
        F("--enable-metrics", "Prometheus /metrics", section="logs"),
        F("--enable-cache-report", "キャッシュレポート", section="logs"),
    ]
    return _backend_meta(b, fields)


def build_llamacpp() -> dict[str, Any]:
    b = "llamacpp"
    F = lambda flag, label, **kw: fld(b, flag, label, **kw)  # noqa: E731
    fields = [
        F("--model", "GGUFモデル", placeholder="~/models/xxx-Q4_K_M.gguf"),
        F("--alias", "提供モデル名 (任意)"),
        F("--hf-repo", "HuggingFaceリポジトリ", placeholder="例: ggml-org/models", help="HFから自動ダウンロードする場合のみ"),
        F("--hf-file", "HFファイル", placeholder="例: llama-2-7b.Q4_K_M.gguf"),
        F("--hf-token", "HFトークン (Private用)", placeholder="hf_..."),
        F("--chat-template", "チャットテンプレート (任意)"),
        F("--jinja", "Jinjaテンプレートエンジン"),
        F("--reasoning", "reasoning出力"),
        F("--lora", "LoRAアダプタ (任意)", placeholder=".gguf LoRAパス"),
        F("--ctx-size", "コンテキスト長", section="sched", help="0=モデルの学習長"),
        F("--n-gpu-layers", "GPUに載せる層数 (ngl)", section="sched", placeholder="-1=全て / 0=CPU"),
        F("--batch-size", "バッチサイズ (b)", section="sched"),
        F("--ubatch-size", "マイクロバッチ (ub)", section="sched"),
        F("--parallel", "並列スロット数 (np)", section="sched"),
        F("--threads", "スレッド数 (t)", section="sched", placeholder="0=自動"),
        F("--flash-attn", "Flash Attention (fa)", section="sched"),
        F("--load-mode", "モデルロード方法", section="sched", help="mmap+mlock = RAM常駐 (mlock 相当)"),
        F("--cache-type-k", "KVキャッシュ型 (K)", section="cache", help="q8_0 等でVRAM削減"),
        F("--cache-type-v", "KVキャッシュ型 (V)", section="cache"),
        F("--defrag-thold", "KV defrag閾値", section="cache", ftype="float", placeholder="有効時 0.1 程度"),
        F("--host", "ホスト", section="server", default="0.0.0.0"),
        F("--port", "ポート", section="server"),
        F("--api-key", "APIキー", section="server", placeholder="設定すると Bearer 認証"),
        F("--no-webui", "WebUI を無効化", section="server"),
        F("--webui-path", "WebUI静的ファイル先 (任意)", section="server"),
        F("--metrics", "/metrics (Prometheus)", section="server"),
        F("--slots", "/slots API (KV状態)", section="server"),
        F("--no-cont-batching", "継続バッチング無効", section="server"),
        F("--embedding", "埋め込みモード (embedding API)", section="server"),
        F("--timeout-keep-alive", "keep-alive秒", section="server"),
        F("--warmup", "起動時ウォームアップ", section="server"),
        F("--spec-type", "推測的デコード方式", section="exec"),
        F("--temp", "temperature", section="sampling"),
        F("--top-k", "top-k", section="sampling"),
        F("--top-p", "top-p", section="sampling"),
        F("--min-p", "min-p", section="sampling"),
        F("--repeat-penalty", "反復ペナルティ", section="sampling"),
        F("--presence-penalty", "presence ペナルティ", section="sampling"),
        F("--frequency-penalty", "frequency ペナルティ", section="sampling"),
        F("--seed", "乱数seed", section="sampling", placeholder="-1=ランダム"),
    ]
    return _backend_meta(b, fields)


def build_freetoken(bid: str = "freetoken") -> dict[str, Any]:
    b = bid
    F = lambda flag, label, **kw: fld(b, flag, label, **kw)  # noqa: E731
    fields = [
        F("--model-path", "モデル", placeholder="deepseek-ai/DeepSeek-V4 など"),
        F("--served-model-name", "提供モデル名 (任意)"),
        F("--model-source", "モデル取得元"),
        F("--dtype", "dtype"),
        F("--max-seq-len-override", "最大コンテキスト長上書き", placeholder="空=モデル設定"),
        F("--max-output-tokens", "最大出力トークン", placeholder="空=自動"),
        F("--sampling-defaults", "サンプリング既定"),
        F("--tp-size", "Tensor並列数 (TP)", section="parallel"),
        F("--gpu", "GPUデバイス指定", section="parallel", placeholder="0 または 0,1 (空=自動)"),
        F("--cuda-graph-max-bs", "CUDA graph 最大bs", section="parallel", placeholder="自動"),
        F("--num-tokenizer", "トークナイザ数", section="parallel", placeholder="自動"),
        F("--disable-pynccl", "PyNCCL 無効", section="parallel"),
        F("--dummy-weight", "ダミーウェイト (動作確認用)", section="parallel"),
        F("--max-running-requests", "最大実行中リクエスト", section="sched", placeholder="自動"),
        F("--memory-ratio", "メモリ使用率", section="sched", ftype="float"),
        F("--max-prefill-length", "最大prefill/extendトークン", section="sched", placeholder="自動",
          help="--max-extend-length でも指定可"),
        F("--page-size", "KVページサイズ", section="sched"),
        F("--num-pages", "KVページ数", section="sched", placeholder="自動"),
        F("--num-tokens", "KVトークン数指定", section="sched", placeholder="自動"),
        F("--kv-reserve-tokens", "KV予約トークン", section="sched", help="--moe-cache-auto 時のKV下限"),
        F("--cache-type", "KVキャッシュ型", section="sched"),
        F("--moe-strategy", "MoE戦略", section="cache"),
        F("--expert-load", "エキスパート読込", section="cache"),
        F("--quant-backend", "量子化バックエンド", section="cache", help="moe.nvfp4=... 形式も可"),
        F("--ple-backend", "PLEオフロード", section="cache",
          help="" if bid != "freetoken-fork" else
          "disk=fillの度チェックポイントから読込 / swap=チェックポイントread-only mmapでOS page cacheが"
          "ホット行を保持 (diskとpinnedの中間) / pinned=テーブル全体をピン留めホストRAMに常駐"),
        F("--nvfp4-backend", "NVFP4バックエンド", section="cache"),
        F("--moe-cache-size", "MoEキャッシュサイズ", section="cache", placeholder="0=自動", help="--moe-cache-rate / --moe-cache-auto と排他"),
        F("--moe-cache-rate", "MoEキャッシュ比率", section="cache", ftype="float", placeholder="例 0.5", help="--moe-cache-size と排他"),
        F("--moe-cache-auto", "MoEキャッシュ自動", section="cache", help="サイズ系の他2項目と排他"),
        F("--moe-cache-policy", "MoEキャッシュ方針", section="cache"),
        F("--moe-cpu-threads", "MoE CPUスレッド数", section="cache", placeholder="0=自動"),
        F("--moe-cpu-layers", "CPU実行MoE層 (任意)", section="cache", placeholder="例: 0,1,2"),
        F("--moe-hybrid-max-fetch", "hybrid最大fetch", section="cache", placeholder="-1=無制限"),
        F("--disable-moe-prefill-overlap", "prefill overlap無効", section="cache"),
        F("--moe-prefill-hit-d2d", "prefillヒットD2D", section="cache"),
        F("--enable-special-token-ckpt", "special token checkpoint", section="cache"),
        F("--attention-backend", "Attentionバックエンド", section="exec"),
        F("--host", "ホスト", section="server", default="0.0.0.0"),
        F("--port", "ポート", section="server"),
        F("--cors-origins", "CORS許可origin", section="server", placeholder="カンマ区切り", default="",
          help="既定は FreeToken クライアント用 origin。Web から叩く場合は * 等を追加"),
        F("--enable-cache-report", "キャッシュレポート", section="server"),
        F("--shell-mode", "シェルモード (対話)", section="server", help="Webサーバーではなく対話シェルで起動する"),
        F("--tool-call-parser", "ツールコールパーサ", section="tools"),
        F("--reasoning-parser", "reasoningパーサ", section="tools"),
        F("--decode-log-interval", "decodeログ間隔", section="logs"),
    ]
    if bid == "freetoken-fork":
        # fork ソース (suzuri/develop) の server/args.py に実在する fork 独自の追加項目。
        # 型・choices・既定値は fields/freetoken-fork.json 正表 (gen_fields 生成) から解決される。
        fields += [
            F("--image-max-tokens", "画像最大トークン", placeholder="自動"),
            F("--image-min-tokens", "画像最小トークン", placeholder="自動"),
            F("--kv-cache-dtype", "KVキャッシュ量子化", section="cache",
              help="fp8 系で KV メモリを削減"),
            F("--kv-cache-quant-scale", "KV量子化スケール", section="cache", ftype="float", placeholder="1.0"),
            F("--mm-embed-cache-device", "画像埋め込みキャッシュDEV", section="cache"),
            F("--mm-encoder-weights", "エンコーダ重み配置", section="cache"),
            F("--allowed-local-media-path", "ローカルメディア許可DIR", section="server",
              help="file:// 画像参照の読み込み許可ディレクトリ"),
            F("--allowed-media-domains", "メディアURL許可domain", section="server",
              placeholder="カンマ区切り", help="クライアント指定の画像URLのホスト許可リスト"),
            F("--prefill-decode-interval", "prefill/decode交互間隔", section="sched", placeholder="0=無効",
              help="連続prefill N回で1ステップ稼働中decodeに譲る。0=従来動作 (サーバー既定値は4)"),
            F("--mamba-host-cache-mb", "GDNスナップショットhost退避MiB", section="cache",
              placeholder="0=無効",
              help="退避対象のGDNスナップショットをピン留めホストRAMに待避する予算(MiB)。"
                   "マルチターン再開時にH2D復元で再prefillを回避 (実測 -85% turn2 TTFT、既定2048)"),
            F("--no-embed-fp8", "埋め込みfp8無効", section="exec", ftype="bool",
              help="入力埋め込みテーブルをbf16のまま保持 (既定はfp8-e4m3化してVRAMをMoEスロットへ回す)"),
            F("--no-decode-fetch-overlap", "デコードfetch overlap無効", section="exec", ftype="bool",
              help="MoEデコードのhit/missフェッチオーバーラップを無効化 (既定ON、無効化で直列化し低速)"),
            F("--speculative", "推測的デコード (MTP)", section="exec",
              help="mtp = チェックポイントの MTP ヘッドで1トークン先読みし単一リクエスト時のデコードを高速化"
                   " (同時リクエストが増えると自動無効化)。対応は qwen4_exp のみ、none/空=無効"),
            # カーネル最適化選択 (fork独自)
            {"key": "kernel_optimization", "flag": "", "type": "select",
             "label": "カーネル最適化", "section": "exec",
             "placeholder": "", "default": "",
             "help": "デコード時のカーネル最適化を選択。fp8_decode_linear: 大きな線形射影をfp8 GEMMで高速化 (+17% TPS)",
             "required": False, "default_on": True,
             "options": ["", "none", "fp8_decode_linear"]},
        ]
        DEFAULT_OFF_KEYS[bid] |= {"image_max_tokens", "image_min_tokens", "kv_cache_dtype",
                                  "kv_cache_quant_scale", "mm_embed_cache_device", "mm_encoder_weights",
                                  "prefill_decode_interval", "speculative"}
    return _backend_meta(b, fields)


def build_strata(bid: str = "strata") -> dict[str, Any]:
    """Strata (https://github.com/Niko1221/Strata) — MoE エキスパートを RAM/SSD に階層化して
    consumer GPU で大型モデルを動かすエンジン + OpenAI/Anthropic 互換サーバー。

    起動するのはリポジトリ直下の serve/server.py (Python) で、モデル本体の設定
    (GGUF・コンテキスト長・MTP など) は serve 層の CLI ではなく setup.py が生成する
    strata-<model>.json の "args" 配列に入る。よって GUI から出すのはサーバープロセスの
    起動フラグ (--config / --host / --port と並列チューニング系) だけ。

    bid="strata-fork": フォーク (module/fork/strata-fork worktree) 用。フィールド定義は共通で、
    型・help の正表だけ fields/strata-fork.json (フォークの server.py から生成) を参照する。
    """
    b = bid
    F = lambda flag, label, **kw: fld(b, flag, label, **kw)  # noqa: E731
    fields = [
        F("--config", "モデル設定 JSON (strata-<model>.json)",
          placeholder="module/strata/strata-qwen3.8-flash-next-q2_0.json",
          help="setup.sh / setup.py が生成する実行設定。モデル・コンテキスト長・MTP などはこの args に入る"),
        F("--engine", "エンジン", default="strata",
          help="strata = 実エンジン / mock = モデル不要の偽エンジン (API 疎通確認用)"),
        F("--tokenizer", "トークナイザ DIR",
          help="空 = config の tokenizer (setup が設定済み。通常は触らない)"),
        F("--gpu", "GPU 指定", section="parallel", placeholder="0 / 0,2 (層分割)",
          help="nvidia-smi の番号。空 = config の gpu"),
        F("--replicas", "データ並列レプリカ数", section="parallel", placeholder="空=1",
          help="gpu リストを N 群に分割し、群ごとにエンジンを起動してリクエストを分散"),
        F("--parallel", "並列バッチスロット数", section="parallel", placeholder="空=config (既定は1リクエストずつ)",
          help="エンジン内のバッチスロットで最大 N リクエストを同時処理 (超過はキュー待ち)。"
               "config の \"parallel\" と同等で、指定時はそれが優先"),
        F("--batch-groups", "バッチグループ数 (直列/パイプライン)", section="parallel",
          placeholder="空=エンジン既定 (layer split ではパイプライン)",
          help="1 = 全スロットを 1 ウィンドウで直列デコード (適応エキスパート tier と Batch MTP が有効)。"
               "auto/N = layer split のステージ毎 N 群でパイプライン (4+ 並列の総スループットは最速だが MTP/tier 無し)。"
               "config の \"batch_groups\" と同等"),
        F("--batch-mtp", "Batch MTP ドラフト", section="parallel",
          help="各バッチスロットでも MTP 提案を検証 (config の --mtp と直列ウィンドウが必要)。"
               "1-2 並列主体で有効、CPU エキスパート負荷が高い機種では不利。\"batch_mtp\": true と同等"),
        F("--adapt-async", "適応エキスパート tier の非ブロック化", section="parallel",
          help="適応 VRAM tier のラウンドでウィンドウを止めない (resident RAM / expert arena 構成向け)。"
               "実測: ソロ +5-10%、直列 4 並列 89.3→95.9 tok/s。\"adapt_async\": true と同等"),
        F("--adapt-every", "適応 tier ラウンド間隔 (ウィンドウ数)", section="parallel", placeholder="空=4"),
        F("--piped-concurrent", "プロンプト読み込み中もデコード継続", section="parallel",
          help="パイプライン群で長いプロンプトを別スレッド読み込みし、スロットはデコード継続 "
               "(実測: 読み込み中のストリーム最大停止 39.5s→1.4s、joiner の TTFT は +24% 程度の代償)。"
               "エンジン env STRATA_PIPED_CONCURRENT=1"),
        F("--yield-chunks", "長い読みの譲り合い単位 (チャンク数)", section="parallel", placeholder="空=8",
          help="デコード無しのプロンプト読み込みを N チャンク単位で区切り、短いリクエストの割り込みを許可 "
               "(1 = 毎チャンク: 実測 割り込み TTFT 30s→6.5s、長い読みは +33%)。"
               "エンジン env STRATA_BATCH_YIELD_CHUNKS"),
        F("--lazy", "遅延ロード (初回リクエストで起動)", section="sched",
          help="API は未ロードで待ち受け、最初のリクエストでエンジンとビジョンエンコーダを起動"),
        F("--idle-unload", "アイドル自動アンロード秒", section="sched", ftype="float",
          placeholder="空=常駐", help="指定秒リクエストが無いとモデルを降ろして VRAM を空ける"),
        F("--min-free-vram-mib", "ロードに必要な空き VRAM (MiB)", section="sched",
          placeholder="空=常にロード", help="未ロード時にこれ未満なら 503 を返す"),
        F("--host", "ホスト", section="server", default="0.0.0.0",
          help="0.0.0.0 で LAN 公開 (その場合は APIキーを推奨)。127.0.0.1 = この PC のみ"),
        F("--port", "ポート", section="server", default="8080"),
        F("--api-key", "APIキー", section="server", placeholder="key1,key2",
          help="/v1/* で Bearer / x-api-key を必須 (--host 0.0.0.0 と併用)。カンマ区切りで複数"),
        F("--open", "起動時にブラウザを開く", section="server"),
        F("--api-monitor", "/api-monitor 有効", section="server",
          help="直近 100 リクエストのプロンプト/回答をメモリ保持"),
        F("--fit-max-tokens", "max_tokens を残り長さに clamp", section="server",
          help="OFF はコンテキスト超過を 400 で拒否 (llama.cpp と同じ既定)"),
        F("--slot-save-path", "スロット保存 DIR", section="server",
          help="POST /slots/0?action=save|restore (llama-server 互換 API) を有効化"),
        F("--before-load", "再ロード前に実行するコマンド", section="server",
          placeholder="例: 他サーバーのモデルを降ろすスクリプト"),
        F("--mcp-config", "MCP サーバー設定 JSON", section="server",
          help='Claude Desktop 形式 {"mcpServers": {...}}。Web UI のチャットでのみ利用可'),
        F("--script", "mock エンジンの回答", section="server",
          help="--engine mock のときだけ有効。複数回指定可 (GUI は 1 つまで)"),
    ]
    return _backend_meta(b, fields)


def build_maya() -> dict[str, Any]:
    """Project Maya (https://github.com/mw00/project-maya) — GLM-5.3-Flash (321B MoE) を
    VRAM/RAM/SSD の階層キャッシュでローカル実行するエンジン + OpenAI/Anthropic 互換サーバー
    + ダッシュボード (Strata 系)。

    起動の入口はリポジトリ直下の maya.py (セットアップ+起動ラッパ)。既定コマンドは
    maya.sh 経由で、module/maya/.venv が無ければ自作し、あればそれを使って起動する。
    セットアップ済みなら対話なしで最新 (または --config で選んだ) maya-<model>.json を起動し、
    サーバー本体は serve/server.py --engine strata --config maya-<model>.json が立つ。
    モデル・コンテキスト長などの本体設定は setup が書く maya-<model>.json の args/env 側に入る
    ため、GUI からは maya.py の起動フラグ (config 選択 / GPU / サーバー) だけを出す。
    """
    b = "maya"
    F = lambda flag, label, **kw: fld(b, flag, label, **kw)  # noqa: E731
    fields = [
        F("--config", "モデル設定 JSON (maya-<model>.json)",
          placeholder="空 = 最近使った設定を自動選択",
          help="setup.sh / maya.sh が生成する実行設定。モデル・コンテキスト長などはこの args に入る"),
        F("--gpu", "GPU 指定 (単一カード)", section="parallel", placeholder="0",
          help="nvidia-smi の番号。空 = config の gpu"),
        F("--gpus", "GPU 分割 (層分散)", section="parallel", placeholder="0,1 / 0,1,2,3",
          help="この起動だけ対象のカードを変える。空 = config の gpu"),
        F("--parallel", "同時リクエスト数 (1-8)", section="parallel", placeholder="1",
          help="指定すると config に保存される。1 枚で複数リクエストをまとめてデコードし総スループット向上。"
               "1 リクエスト増えるごとに VRAM のコンテキスト分だけ expert キャッシュが減る"),
        F("--vram-reserve-mib", "GPU ごとの空き VRAM 確保 (MiB)", section="sched", placeholder="700",
          help="ドライバ・デスクトップ等向けの余白。指定すると config に保存される"),
        F("--calibrate", "起動前にこの PC 向け調整 (~10-15分)", section="sched",
          help="PCIe 共有率と CPU スレッドを実測調整してから起動する (毎回やる必要はない)"),
        F("--backend", "GPU バックエンド", section="exec", choices=["cuda", "hip"],
          help="cuda = NVIDIA / hip = AMD (実験的)。空 = config から自動判定"),
        F("--env", "エンジン設定 KEY=VALUE", section="exec",
          placeholder="STRATA_GLM_RAM_HEADROOM_GB=4",
          help="エンジンへ渡す設定を 1 つ (docs/SETTINGS.md 参照。複数ある場合は追加引数へ)"),
        F("--host", "ホスト", section="server", default="0.0.0.0",
          help="0.0.0.0 で LAN 公開 (その場合は APIキーを推奨)。127.0.0.1 = この PC のみ"),
        F("--port", "ポート", section="server", default="8080"),
        F("--api-key", "APIキー", section="server", placeholder="LAN 公開時は必須",
          help="API クライアントとダッシュボードにキーを必須化する"),
    ]
    return _backend_meta(b, fields)


# モジュール側が自動導出する項目: GUI 既定値で上書きしないよう既定 OFF (チェックで ON にすると送信)
DEFAULT_OFF_KEYS: dict[str, set[str]] = {
    "vllm": {"dtype", "load_format", "kv_cache_dtype", "generation_config",
             "distributed_executor_backend", "seed", "max_model_len", "block_size",
             "max_num_batched_tokens", "max_num_seqs", "max_logprobs", "max_loras",
             "max_lora_rank", "tool_call_parser", "reasoning_parser",
             "chat_template_content_format", "cpu_offload_gb"},
    "sglang": {"dtype", "kv_cache_dtype", "load_format", "quantization",
               "context_length", "json_model_override_args", "tp_size", "pp_size",
               "dp_size", "ep_size", "mem_fraction_static", "max_running_requests",
               "max_total_tokens", "chunked_prefill_size", "max_prefill_tokens",
               "schedule_policy", "page_size", "watchdog_timeout", "radix_eviction_policy",
               "hicache_ratio", "attention_backend", "sampling_backend", "grammar_backend",
               "fp8_gemm_runner_backend", "cuda_graph_max_bs_decode", "cpu_offload_gb",
               "base_gpu_id", "gpu_id_step", "random_seed", "tokenizer_worker_num",
               "detokenizer_worker_num", "log_requests_level", "decode_log_interval",
               "tool_call_parser", "reasoning_parser"},
    "llamacpp": {"ctx_size", "n_gpu_layers", "batch_size", "ubatch_size", "parallel",
                 "threads", "flash_attn", "load_mode", "defrag_thold", "cache_type_k",
                 "cache_type_v", "temp", "top_k", "top_p", "min_p", "repeat_penalty",
                 "presence_penalty", "frequency_penalty", "seed", "timeout_keep_alive",
                 "reasoning", "spec_type", "chat_template", "lora", "alias"},
    "freetoken": {"dtype", "model_source", "max_seq_len_override", "max_output_tokens",
                  "sampling_defaults", "tp_size", "cuda_graph_max_bs", "num_tokenizer",
                  "max_running_requests", "memory_ratio", "max_prefill_length", "page_size",
                  "num_pages", "num_tokens", "kv_reserve_tokens", "cache_type",
                  "moe_strategy", "expert_load", "quant_backend", "ple_backend",
                  "nvfp4_backend", "moe_cache_size", "moe_cache_rate", "moe_cache_policy",
                  "moe_cpu_threads", "moe_hybrid_max_fetch", "attention_backend",
                  "decode_log_interval", "tool_call_parser", "reasoning_parser"},
    # Strata: 正解を strata-<model>.json (config) が既に持っている項目は既定 OFF
    "strata": {"tokenizer", "gpu", "replicas", "idle_unload", "min_free_vram_mib",
               "slot_save_path", "before_load", "mcp_config", "script",
               # 並列チューニング系 (2026-10): config キー / env が正解を持つので既定 OFF (ON にすると CLI が優先)
               "parallel", "batch_groups", "batch_mtp", "adapt_async", "adapt_every",
               "piped_concurrent", "yield_chunks"},
    # Project Maya: 同様に maya-<model>.json (config) が正解を持つ項目 / 起動のたびに不要な項目は既定 OFF
    "maya": {"config", "gpu", "gpus", "parallel", "vram_reserve_mib", "calibrate",
             "backend", "env"},
}

# 常に送信する (無効化できない) 項目
REQUIRED_KEYS: dict[str, set[str]] = {
    "vllm": {"model", "host", "port"},
    "sglang": {"model_path", "host", "port"},
    "llamacpp": {"model", "host", "port"},
    "freetoken": {"model_path", "host", "port"},
    "strata": {"config", "engine", "host", "port"},
    "maya": {"host", "port"},
}

# fork バックエンドは基底 (freetoken) と同じ送信ポリシー (コピーして派生 = 個別追加可)
DEFAULT_OFF_KEYS["freetoken-fork"] = set(DEFAULT_OFF_KEYS["freetoken"])
REQUIRED_KEYS["freetoken-fork"] = set(REQUIRED_KEYS["freetoken"])
DEFAULT_OFF_KEYS["strata-fork"] = set(DEFAULT_OFF_KEYS["strata"])
REQUIRED_KEYS["strata-fork"] = set(REQUIRED_KEYS["strata"])


def _backend_meta(bid: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
    meta = dict(_BACKEND_META[bid])
    off = DEFAULT_OFF_KEYS.get(bid, set())
    req = REQUIRED_KEYS.get(bid, set())
    for f in fields:
        if f["key"] in req:
            f["required"] = True
            f["default_on"] = True
        elif f["type"] != "bool":
            if f["key"] in off:
                f["default_on"] = False
    meta["fields"] = fields
    return meta


_BACKEND_META: dict[str, dict[str, Any]] = {
    "vllm": {
        "id": "vllm", "name": "vLLM",
        "repo": "https://github.com/vllm-project/vllm",
        "module_path": "module/vllm", "kind": "python",
        "desc": "高スループットな OpenAI 互換 API サーバー。",
        "default_command": "{python} -m vllm.entrypoints.cli.main serve",
        "check_module": "vllm", "health_path": "/health",
        "ready_markers": ["Application startup complete", "Uvicorn running"],
        "default_port": "8000",
    },
    "sglang": {
        "id": "sglang", "name": "SGLang",
        "repo": "https://github.com/sgl-project/sglang",
        "module_path": "module/sglang", "kind": "python",
        "desc": "RadixAttention による高速な OpenAI 互換サーバー。",
        "default_command": "{python} -m sglang.launch_server",
        "check_module": "sglang", "health_path": "/health",
        "ready_markers": ["The server is fired up and ready to roll", "Uvicorn running"],
        "default_port": "30000",
    },
    "llamacpp": {
        "id": "llamacpp", "name": "llama.cpp",
        "repo": "https://github.com/ggml-org/llama.cpp",
        "module_path": "module/llama.cpp", "kind": "binary",
        "desc": "GGUF モデル用軽量サーバー (llama-server)。",
        "default_command": "{llama_server}",
        "binaries": ["llama-server"],
        "build_candidates": ["module/llama.cpp/build/bin/llama-server"],
        "health_path": "/health",
        "ready_markers": ["startup complete", "listening"],
        "default_port": "8080",
    },
    "freetoken": {
        "id": "freetoken", "name": "FreeToken",
        "repo": "https://github.com/FlashML-org/FreeToken",
        "module_path": "module/freetoken", "kind": "python",
        "desc": "MoEエキスパートオフロードによる大型モデルのローカル推論 (OpenAI/Anthropic 互換)。",
        "default_command": "{python} -m freetoken",
        "check_module": "freetoken", "health_path": "/health",
        "ready_markers": ["Uvicorn running", "Application startup complete"],
        "default_port": "1919",
    },
    "freetoken-fork": {
        "id": "freetoken-fork", "name": "FreeToken (fork)",
        "repo": "https://github.com/suzuri-lollipop/FreeToken",
        "module_path": "module/fork/freetoken", "kind": "python",
        "desc": "FreeToken フォーク。module/fork/freetoken の clone を直接使用する"
                " (llm-launcher 側ではフォルダのみ追跡、内容は追跡しない)。",
        "default_command": "{python} -m freetoken",
        "check_module": "freetoken", "health_path": "/health",
        "ready_markers": ["Uvicorn running", "Application startup complete"],
        "default_port": "1919",
    },
    "strata": {
        "id": "strata", "name": "Strata",
        "repo": "https://github.com/Niko1221/Strata",
        "module_path": "module/strata", "kind": "python",
        "desc": "MoE エキスパートを RAM/SSD に階層化して consumer GPU で大型モデルを動かすエンジン"
                " (OpenAI/Anthropic 互換)。初回は module/strata で setup.sh を実行する。",
        # serve/server.py は __file__ から自リポジトリを解決するので cwd 依存ではない
        "default_command": "{python} {root}/module/strata/serve/server.py",
        # serve はリポジトリ直下のパッケージ (未 install) → submodule を cwd に実 import して判定
        "check_module": "serve.server", "check_cwd": True, "health_path": "/health",
        "ready_markers": ["ready: http"],
        "default_port": "8080",
    },
    "strata-fork": {
        "id": "strata-fork", "name": "Strata (fork)",
        "repo": "https://github.com/suzuri-lollipop/Strata",
        "module_path": "module/fork/strata-fork", "kind": "python",
        "desc": "Strata フォーク (suzuri-lollipop/Strata) の並列チューニングブランチ "
                "(suzuri/parallel-perf: --parallel / --batch-groups / --batch-mtp / --adapt-async / "
                "--adapt-every / --piped-concurrent / --yield-chunks と layer split + arena での "
                "--adapt-async 対応エンジン)。module/fork/strata clone の git worktree として "
                "module/fork/strata-fork に展開する (deepseek ブランチと同居可)。エンジンバイナリと "
                "strata-<model>.json は module/strata のものを --config で共有できる",
        # 展開: cd module/fork/strata && git fetch <module/strata のパス> suzuri/parallel-perf:suzuri/parallel-perf
        #       && git worktree add ../strata-fork suzuri/parallel-perf
        #       && ln -s ../../strata/.venv ../strata-fork/.venv
        "default_command": "{python} {root}/module/fork/strata-fork/serve/server.py",
        "check_module": "serve.server", "check_cwd": True, "health_path": "/health",
        "ready_markers": ["ready: http"],
        "default_port": "8080",
    },
    "deepseek": {
        "id": "deepseek", "name": "DeepSeek-V4.1 3-bit (fork)",
        "repo": "https://github.com/suzuri-lollipop/Strata",
        "module_path": "module/fork/strata/deepseek", "kind": "python",
        "desc": "DeepSeek-V4.1-Flash-1017 (590B MoE) の 3bit (IQ3_S) 量子化を NVMe/RAM/VRAM "
                "階層ストリーミングで実行する OpenAI 互換サーバー (deepseek/ ツールキット、"
                "module/fork/strata)。初回は deepseek/README.md の fetch/quantize/calib を実行する",
        "default_command": "sh {root}/module/fork/strata/deepseek/serve.sh",
        "check_module": "dsv41.serve", "check_cwd": True, "health_path": "/health",
        "ready_markers": ["[serve] http"],
        "default_port": "8199",
    },
    "maya": {
        "id": "maya", "name": "Project Maya",
        "repo": "https://github.com/mw00/project-maya",
        "module_path": "module/maya", "kind": "python",
        "desc": "GLM-5.3-Flash (321B MoE) を VRAM/RAM/SSD 階層キャッシュでローカル実行するエンジン"
                " (OpenAI/Anthropic 互換 + ダッシュボード)。初回は module/maya で ./maya.sh を実行し"
                " エンジンビルドとモデルダウンロード (~100GB) を済ませる。",
        # maya.sh が module/maya/.venv を用意 (あれば再利用) して maya.py を exec。
        # セットアップ済みなら対話なしで serve/server.py --engine strata --config maya-<model>.json を起動する
        "default_command": "sh {root}/module/maya/maya.sh",
        # serve はリポジトリ直下のパッケージ (未 install) → submodule を cwd に実 import して判定。
        # 依存 (psutil/PIL/strata_tokenizer) は遅延 import なので GUI .venv でも import は通るが、
        # module/maya/.venv (setup 済み) が最優先で選ばれる
        "check_module": "serve.server", "check_cwd": True, "health_path": "/health",
        "ready_markers": ["ready: http"],
        "default_port": "8080",
    },
}

def build_deepseek() -> dict[str, Any]:
    """DeepSeek-V4.1-Flash-1017 3-bit (module/fork/strata/deepseek) — この fork の
    deepseek/ ツールキットが量子化した IQ3_S GGUF を NVMe/RAM/VRAM 階層でストリーミング実行する
    OpenAI 互換サーバー (dsv41.serve)。モデルは /media/nvme2 想定。"""
    b = "deepseek"
    F = lambda flag, label, **kw: fld(b, flag, label, **kw)  # noqa: E731
    fields = [
        F("--model", "量子化モデル目录 (manifest.json がある dir)",
          default="/media/nvme2/models/DeepSeek-V4.1-Flash-1017-IQ3",
          help="dsv41.quantize の出力先。既定は /media/nvme2/models/DeepSeek-V4.1-Flash-1017-IQ3"),
        F("--host", "ホスト", section="server", default="127.0.0.1",
          help="0.0.0.0 で LAN 公開 (非推奨: API キーなし)。127.0.0.1 = この PC のみ"),
        F("--resident-gb", "エキスパート常駐 VRAM (GB)", section="sched", default="18",
          help="expert 側 GPU に常駐させる 3bit コードの予算。0 = 無効"),
        F("--expert-cache-gb", "エキスパート RAM キャッシュ (GB)", section="sched", default="24",
          help="3bit コードの RAM LRU 予算"),
        F("--spec", "DSpark 投機デコード", section="sched",
          help="モデル同梱の MTP drafter で greedy decode を加速 (出力は greedy と同一)"),
        F("--max-seq-len", "最大コンテキスト", section="sched", default="16384",
          help="KV/圧縮KV キャッシュの上限トークン数"),
    ]
    return _backend_meta(b, fields)


BACKENDS: dict[str, dict[str, Any]] = {
    "vllm": build_vllm(),
    "sglang": build_sglang(),
    "llamacpp": build_llamacpp(),
    "freetoken": build_freetoken(),
    "freetoken-fork": build_freetoken("freetoken-fork"),
    "strata": build_strata(),
    "strata-fork": build_strata("strata-fork"),
    "maya": build_maya(),
    "deepseek": build_deepseek(),
}


# ---------------------------------------------------------------------------
# パス / 解決ヘルパ
# ---------------------------------------------------------------------------


def python_candidates(root: Path, module_path: str | None = None) -> list[tuple[str, str]]:
    """{python} トークンの解決候補 (優先順)。
    1. バックエンド submodule 自身の .venv (module/<name>/.venv — uv sync の既定)
    2. GUI(llm-launcher)の .venv
    3. launcher 自身を実行中の python
    """
    out: list[tuple[str, str]] = []
    if module_path:
        out.append((f"module venv ({module_path})", str(root / module_path / ".venv" / "bin" / "python")))
    out.append(("GUI venv", str(root / ".venv" / "bin" / "python")))
    out.append(("launcher python", sys.executable))
    return out


def default_python(root: Path) -> str:
    """後方互換: GUI .venv か self。"""
    venv = root / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def resolve_binary(root: Path, backend: dict[str, Any], override: str = "") -> str:
    """llama.cpp 等のバイナリパス解決: 明示指定 > submoduleビルド > PATH。"""
    if override:
        return override
    for cand in backend.get("build_candidates", []):
        p = root / cand
        if p.exists():
            return str(p)
    for name in backend.get("binaries", []):
        w = shutil.which(name)
        if w:
            return w
    return ""


def resolve_command_tokens(root: Path, template: str, *, python: str, binary: str,
                           values: dict[str, Any] | None = None) -> str:
    """コマンドテンプレートの {python} {llama_server} {module} {root} と
    プロファイル変数 {model} {port} 等を解決してコマンド文字列化する。"""
    mapping: dict[str, str] = {
        "python": python,
        "llama_server": binary,
        "module": str(root),
        "root": str(root),
    }
    for k, v in (values or {}).items():
        if isinstance(v, bool):
            continue
        sval = str(v).strip()
        if sval and re.fullmatch(r"\w+", str(k)):
            mapping[str(k)] = shlex.quote(sval)
    return re.sub(r"\{(\w+)\}", lambda m: mapping.get(m.group(1), m.group(0)), template)


TRUTHY = {"1", "true", "yes", "on", "はい"}


def render_argv(backend: dict[str, Any], profile: dict[str, Any], *, python: str,
                binary: str, root: Path) -> list[str]:
    """バックエンド + プロファイルから最終 argv を組み立てる。"""
    values: dict[str, Any] = profile.get("values") or {}
    on = profile.get("on")
    if not isinstance(on, dict):
        on = None
    command = (profile.get("command") or "").strip() or backend["default_command"]
    resolved = resolve_command_tokens(root, command, python=python, binary=binary,
                                      values=values)
    if backend["kind"] == "binary" and not binary:
        raise ValueError("llama-server が見つかりません。バックエンド設定でパスを指定するか、"
                         "module/llama.cpp をビルドしてください")
    if not resolved.strip():
        raise ValueError("起動コマンドが空です")
    argv = shlex.split(resolved)
    for field in ([] if profile.get("custom_only") else backend.get("fields", [])):
        key = field["key"]
        # 送信 ON/OFF: on dict が明示されたプロファイルのみ尊重 (無い=旧来動作=値があれば送信)
        if on is not None and field["type"] != "bool" and not field.get("required"):
            if not bool(on.get(key, field.get("default_on", True))):
                continue
        val = values.get(key)
        if val is None:
            continue
        sval = str(val).strip()
        if field["type"] == "bool":
            if sval.lower() in TRUTHY:
                argv.append(field["flag"])
        else:
            if sval and field["flag"]:
                argv += [field["flag"], sval]
    extra = (profile.get("extra_args") or "").strip()
    if extra:
        try:
            argv += shlex.split(extra, posix=sys.platform != "win32")
        except ValueError as e:
            raise ValueError(f"追加引数の解析に失敗しました: {e}")

    # カーネル最適化の環境変数を設定
    kernel_opt = str(values.get("kernel_optimization", "")).strip()
    env = profile.get("env") or {}
    if kernel_opt == "fp8_decode_linear":
        env = {**env, "FREETOKEN_FP8_DECODE_LINEAR": "1"}
    elif kernel_opt == "none":
        env = {k: v for k, v in env.items() if k != "FREETOKEN_FP8_DECODE_LINEAR"}
    # 更新されたenvをprofileに反映 (呼び出し側で使う)
    profile["_resolved_env"] = env

    return argv


def port_from_argv(argv: list[str]) -> int | None:
    """レンダリング済み argv から --port / -p の値を取り出す。"""
    for i, tok in enumerate(argv):
        if tok in ("--port", "-p", "--api-port") and i + 1 < len(argv):
            try:
                return int(argv[i + 1])
            except ValueError:
                return None
        if tok.startswith("--port="):
            try:
                return int(tok.split("=", 1)[1])
            except ValueError:
                return None
    return None


def resolve_port(backend: dict[str, Any], profile: dict[str, Any], argv: list[str]) -> int | None:
    """ポート確定の優先順位: プロファイル values.port > 組み立て済み argv > バックエンド既定。"""
    v = str((profile.get("values") or {}).get("port", "")).strip()
    if v.isdigit():
        return int(v)
    p = port_from_argv(argv)
    if p:
        return p
    d = str(backend.get("default_port", "")).strip()
    return int(d) if d.isdigit() else None
