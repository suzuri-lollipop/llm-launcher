"""バックエンド(vllm / sglang / llama.cpp / FreeToken)の定義と起動コマンド組み立て。

各バックエンドは `module/` 配下の git submodule として登録されている。
ここでは「起動コマンドテンプレート」と「UI 用の完全な起動パラメータ定義」を持つ。

フィールドはセクション (モデル設定 / 並列・分散 / スケジューリング / サーバー /
サンプリング / ログ ...) に分類され、UI はセクションごとにグループ表示する。
追加引数を素の CLI で書かなくても、主要パラメータはすべてリストから設定できる。
"""

from __future__ import annotations

import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# フィールド定義ヘルパ
# ---------------------------------------------------------------------------


def f_str(key: str, flag: str, label: str, *, section: str = "model", placeholder: str = "",
          default: str = "", help: str = "") -> dict[str, Any]:
    return {"key": key, "flag": flag, "type": "str", "label": label, "section": section,
            "placeholder": placeholder, "default": default, "help": help}


def f_num(key: str, flag: str, label: str, *, section: str = "model", numtype: str = "int",
          default: str = "", placeholder: str = "", help: str = "") -> dict[str, Any]:
    return {"key": key, "flag": flag, "type": numtype, "label": label, "section": section,
            "default": default, "placeholder": placeholder, "help": help}


def f_bool(key: str, flag: str, label: str, *, section: str = "model", help: str = "") -> dict[str, Any]:
    return {"key": key, "flag": flag, "type": "bool", "label": label, "section": section, "help": help}


def f_select(key: str, flag: str, label: str, options: list[str], *, section: str = "model",
             default: str = "", help: str = "") -> dict[str, Any]:
    return {"key": key, "flag": flag, "type": "select", "label": label, "section": section,
            "options": options, "default": default, "help": help}


SECTIONS_JA = {
    "model": "モデル・提供設定",
    "parallel": "並列・分散",
    "sched": "スケジューリング・メモリ",
    "exec": "実行エンジン・最適化",
    "server": "サーバー・API",
    "tools": "ツール呼び出し・推論・LoRA",
    "sampling": "デフォルトサンプリング",
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
# バックエンド定義 (flags/既定値は submodule ソース準拠)
# ---------------------------------------------------------------------------

BACKENDS: dict[str, dict[str, Any]] = {
    # =====================================================================
    "vllm": {
        "id": "vllm",
        "name": "vLLM",
        "repo": "https://github.com/vllm-project/vllm",
        "module_path": "module/vllm",
        "kind": "python",
        "desc": "高スループットな OpenAI 互換 API サーバー。",
        "default_command": "{python} -m vllm.entrypoints.cli.main serve",
        "check_module": "vllm",
        "health_path": "/health",
        "ready_markers": ["Application startup complete", "Uvicorn running"],
        "default_port": "8000",
        "fields": [
            # --- モデル -----------------------------------------------------
            f_str("model", "--model", "モデル", placeholder="Qwen/Qwen2.5-7B-Instruct またはローカルパス"),
            f_str("served_model_name", "--served-model-name", "提供モデル名", placeholder="省略時は --model"),
            f_str("tokenizer", "--tokenizer", "トークナイザパス (任意)", placeholder="モデルと異なる場合のみ"),
            f_str("revision", "--revision", "リビジョン/ブランチ (任意)"),
            f_str("chat_template", "--chat-template", "チャットテンプレートファイル (任意)"),
            f_num("max_model_len", "--max-model-len", "最大コンテキスト長", placeholder="空=自動"),
            f_select("dtype", "--dtype", "dtype", ["", "auto", "half", "float16", "bfloat16", "float", "float32"], default="auto"),
            f_select("quantization", "--quantization", "量子化",
                     ["", "awq", "fp8", "gptq", "gptq_marlin", "bitsandbytes", "gguf", "modelopt", "experts_int8"]),
            f_select("load_format", "--load-format", "ウェイト読み込み形式",
                     ["", "auto", "pt", "safetensors", "dummy", "sharded_state", "gguf", "bitsandbytes", "fastsafetensors", "mistral"], default="auto"),
            f_str("download_dir", "--download-dir", "DLキャッシュDIR (任意)"),
            f_bool("trust_remote_code", "--trust-remote-code", "trust-remote-code"),
            f_num("seed", "--seed", "乱数seed", placeholder="空=ランダム"),
            f_select("generation_config", "--generation-config", "生成設定", ["", "auto", "vllm"], default="auto",
                     help="モデル同梱 generation_config.json を尊重するか"),
            # --- 並列・分散 -------------------------------------------------
            f_num("tensor_parallel_size", "--tensor-parallel-size", "Tensor並列数 (TP)", section="parallel", default="1"),
            f_num("pipeline_parallel_size", "--pipeline-parallel-size", "Pipeline並列数 (PP)", section="parallel", placeholder="1"),
            f_num("data_parallel_size", "--data-parallel-size", "Data並列数 (DP)", section="parallel", placeholder="1"),
            f_bool("enable_expert_parallel", "--enable-expert-parallel", "Expert並列 (MoE)", section="parallel"),
            f_select("distributed_executor_backend", "--distributed-executor-backend", "実行バックエンド",
                     ["", "mp", "ray", "uni", "external_launcher"], section="parallel", help="空=自動 (複数node時はray)"),
            # --- スケジューリング・メモリ ------------------------------------
            f_num("gpu_memory_utilization", "--gpu-memory-utilization", "GPUメモリ使用率", section="sched", numtype="float", default="0.9"),
            f_num("cpu_offload_gb", "--cpu-offload-gb", "CPUオフロード (GB)", section="sched", numtype="float", placeholder="0"),
            f_num("max_num_batched_tokens", "--max-num-batched-tokens", "最大バッチトークン数", section="sched", placeholder="自動"),
            f_num("max_num_seqs", "--max-num-seqs", "最大同時系列数", section="sched", placeholder="自動"),
            f_num("block_size", "--block-size", "KVブロックサイズ", section="sched", placeholder="自動 (16)"),
            f_bool("enable_prefix_caching", "--enable-prefix-caching", "prefix caching", section="sched"),
            f_bool("enable_chunked_prefill", "--enable-chunked-prefill", "chunked prefill", section="sched",
                   help="v1エンジンでは既定で有効"),
            f_bool("enforce_eager", "--enforce-eager", "eager モード (CUDA graph 無効)", section="sched"),
            # --- サーバー ---------------------------------------------------
            f_str("host", "--host", "ホスト", section="server", default="0.0.0.0"),
            f_num("port", "--port", "ポート", section="server", default="8000"),
            f_str("api_key", "--api-key", "APIキー", section="server", placeholder="設定すると Bearer 認証"),
            f_select("uvicorn_log_level", "--uvicorn-log-level", "uvicornログレベル",
                     ["", "critical", "error", "warning", "info", "debug", "trace"], section="server", default="info"),
            f_bool("disable_uvicorn_access_log", "--disable-uvicorn-access-log", "accessログ無効", section="server"),
            f_bool("disable_fastapi_docs", "--disable-fastapi-docs", "APIドキュメント(/docs)無効", section="server"),
            f_bool("allow_credentials", "--allow-credentials", "CORS credentials", section="server"),
            # --- ツール・LoRA -----------------------------------------------
            f_bool("enable_auto_tool_choice", "--enable-auto-tool-choice", "auto tool choice", section="tools",
                   help="--tool-call-parser と併用必須"),
            f_select("tool_call_parser", "--tool-call-parser", "ツールコールパーサ",
                     ["", "hermes", "llama31", "llama4", "mistral", "pythonic", "qwen", "qwen25", "phi4", "deepseekv32", "minimax_m2"], section="tools"),
            f_select("reasoning_parser", "--reasoning-parser", "reasoningパーサ",
                     ["", "deepseek_r1", "qwen3", "granite", "glm4_5", "hunyuan_a13b", "kimi", "lfm2", "minimax_m2", "olmo3", "phi4", "seed_oss"], section="tools"),
            f_bool("enable_lora", "--enable-lora", "LoRA有効", section="tools"),
            f_num("max_loras", "--max-loras", "LoRA最大数", section="tools", placeholder="1"),
            f_num("max_lora_rank", "--max-lora-rank", "LoRA最大ランク", section="tools", placeholder="16"),
            f_num("max_logprobs", "--max-logprobs", "最大logprobs", section="tools", placeholder="20"),
            # --- ログ -------------------------------------------------------
            f_bool("disable_log_stats", "--disable-log-stats", "統計ログ無効", section="logs"),
        ],
    },
    # =====================================================================
    "sglang": {
        "id": "sglang",
        "name": "SGLang",
        "repo": "https://github.com/sgl-project/sglang",
        "module_path": "module/sglang",
        "kind": "python",
        "desc": "RadixAttention による高速な OpenAI 互換サーバー。",
        "default_command": "{python} -m sglang.launch_server",
        "check_module": "sglang",
        "health_path": "/health",
        "ready_markers": ["The server is fired up and ready to roll", "Uvicorn running"],
        "default_port": "30000",
        "fields": [
            # --- モデル -----------------------------------------------------
            f_str("model_path", "--model-path", "モデル", placeholder="Qwen/Qwen2.5-7B-Instruct またはローカルパス"),
            f_str("tokenizer_path", "--tokenizer-path", "トークナイザパス (任意)"),
            f_str("served_model_name", "--served-model-name", "提供モデル名 (任意)"),
            f_str("chat_template", "--chat-template", "チャットテンプレート (任意)"),
            f_num("context_length", "--context-length", "最大コンテキスト長", placeholder="空=モデル設定に従う"),
            f_select("dtype", "--dtype", "dtype", ["", "auto", "float16", "bfloat16"], section="model", default="auto"),
            f_select("kv_cache_dtype", "--kv-cache-dtype", "KVキャッシュdtype",
                     ["", "auto", "fp8_e5m2", "fp8_e4m3"], default="auto"),
            f_select("quantization", "--quantization", "量子化",
                     ["", "awq", "fp8", "gptq", "w8a8_int8", "w8a8_fp8", "modelopt", "gguf"]),
            f_select("load_format", "--load-format", "ウェイト読み込み形式",
                     ["", "auto", "pt", "safetensors", "dummy", "pt_gguf", "safetensors_gguf"], default="auto"),
            f_str("download_dir", "--download-dir", "DLキャッシュDIR (任意)"),
            f_str("revision", "--revision", "リビジョン/ブランチ (任意)"),
            f_bool("trust_remote_code", "--trust-remote-code", "trust-remote-code"),
            # --- 並列・分散 -------------------------------------------------
            f_num("tp_size", "--tp-size", "Tensor並列数 (TP)", section="parallel", default="1"),
            f_num("pp_size", "--pp-size", "Pipeline並列数 (PP)", section="parallel", placeholder="1"),
            f_num("dp_size", "--dp-size", "Data並列数 (DP)", section="parallel", placeholder="1"),
            f_num("ep_size", "--ep-size", "Expert並列数 (EP)", section="parallel", placeholder="1"),
            f_bool("enable_dp_attention", "--enable-dp-attention", "DP attention", section="parallel"),
            f_num("nnodes", "--nnodes", "ノード数", section="parallel", placeholder="1"),
            f_num("node_rank", "--node-rank", "このノードのrank", section="parallel", placeholder="0"),
            f_str("dist_init_addr", "--dist-init-addr", "分散初期化先 (host:port)", section="parallel", placeholder="マルチnode時のみ"),
            # --- スケジューリング・メモリ ------------------------------------
            f_num("mem_fraction_static", "--mem-fraction-static", "静的メモリ比率", section="sched", numtype="float", placeholder="空=自動"),
            f_num("max_running_requests", "--max-running-requests", "最大実行中リクエスト", section="sched", placeholder="自動"),
            f_num("max_total_tokens", "--max-total-tokens", "KV総トークン数", section="sched", placeholder="自動"),
            f_num("chunked_prefill_size", "--chunked-prefill-size", "chunked prefillサイズ", section="sched", placeholder="自動"),
            f_num("max_prefill_tokens", "--max-prefill-tokens", "最大prefillトークン", section="sched", default="16384"),
            f_select("schedule_policy", "--schedule-policy", "スケジューリング方針",
                     ["", "lpm", "fcfs", "random", "priority"], section="sched", default="fcfs"),
            f_num("page_size", "--page-size", "ページサイズ", section="sched", placeholder="自動"),
            f_bool("disable_overlap_schedule", "--disable-overlap-schedule", "overlapスケジューリング無効", section="sched"),
            f_bool("disable_radix_cache", "--disable-radix-cache", "Radixキャッシュ無効", section="sched"),
            f_bool("enable_hierarchical_cache", "--enable-hierarchical-cache", "階層キャッシュ (HiCache)", section="sched"),
            f_num("hicache_ratio", "--hicache-ratio", "HiCacheホスト比率", section="sched", numtype="float", placeholder="2.0"),
            # --- 実行エンジン -----------------------------------------------
            f_select("attention_backend", "--attention-backend", "Attentionバックエンド",
                     ["", "flashinfer", "triton", "torch_native", "fa3", "nsa"], section="exec", help="空=自動"),
            f_select("sampling_backend", "--sampling-backend", "Samplingバックエンド",
                     ["", "flashinfer", "pytorch"], section="exec"),
            f_select("grammar_backend", "--grammar-backend", "Grammarバックエンド",
                     ["", "xgrammar", "outlines", "llguidance", "none"], section="exec"),
            f_bool("disable_cuda_graph", "--disable-cuda-graph", "CUDA graph 無効", section="exec"),
            f_bool("enable_torch_compile", "--enable-torch-compile", "torch.compile", section="exec"),
            f_num("cuda_graph_max_bs_decode", "--cuda-graph-max-bs-decode", "CUDA graph 最大bs (decode)", section="exec", placeholder="自動"),
            f_num("cpu_offload_gb", "--cpu-offload-gb", "CPUオフロード (GB)", section="exec", placeholder="0"),
            f_num("base_gpu_id", "--base-gpu-id", "開始GPU ID", section="exec", default="0"),
            f_num("gpu_id_step", "--gpu-id-step", "GPU IDステップ", section="exec", default="1"),
            f_num("random_seed", "--random-seed", "乱数seed", section="exec", placeholder="空=ランダム"),
            # --- サーバー ---------------------------------------------------
            f_str("host", "--host", "ホスト", section="server", default="0.0.0.0"),
            f_num("port", "--port", "ポート", section="server", default="30000"),
            f_str("api_key", "--api-key", "APIキー", section="server", placeholder="設定すると Bearer 認証"),
            f_str("admin_api_key", "--admin-api-key", "管理者APIキー (任意)"),
            f_num("tokenizer_worker_num", "--tokenizer-worker-num", "トークナイザworker数", section="server", default="1"),
            f_bool("skip_server_warmup", "--skip-server-warmup", "ウォームアップスキップ", section="server"),
            f_str("fastapi_root_path", "--fastapi-root-path", "ルートパス (リバースプロキシ用)", section="server"),
            # --- ログ -------------------------------------------------------
            f_select("log_level", "--log-level", "ログレベル",
                     ["", "info", "debug", "warning", "error", "critical"], section="logs", default="info"),
            f_bool("log_requests", "--log-requests", "リクエスト内容をログ", section="logs"),
            f_num("log_requests_level", "--log-requests-level", "リクエストログ詳細度", section="logs", default="2", placeholder="0-3"),
            f_num("decode_log_interval", "--decode-log-interval", "decodeログ間隔", section="logs", default="40"),
            f_bool("enable_metrics", "--enable-metrics", "Prometheus /metrics", section="logs"),
        ],
    },
    # =====================================================================
    "llamacpp": {
        "id": "llamacpp",
        "name": "llama.cpp",
        "repo": "https://github.com/ggml-org/llama.cpp",
        "module_path": "module/llama.cpp",
        "kind": "binary",
        "desc": "GGUF モデル用軽量サーバー (llama-server)。",
        "default_command": "{llama_server}",
        "binaries": ["llama-server"],
        "build_candidates": ["module/llama.cpp/build/bin/llama-server"],
        "health_path": "/health",
        "ready_markers": ["startup complete", "listening"],
        "default_port": "8080",
        "fields": [
            # --- モデル -----------------------------------------------------
            f_str("model", "--model", "GGUFモデル", placeholder="~/models/xxx-Q4_K_M.gguf"),
            f_str("alias", "--alias", "提供モデル名 (任意)"),
            f_str("hf_repo", "--hf-repo", "HuggingFaceリポジトリ", placeholder="例: ggml-org/models", help="HFから自動ダウンロードする場合のみ"),
            f_str("hf_file", "--hf-file", "HFファイル", placeholder="例: llama-2-7b.Q4_K_M.gguf"),
            f_str("hf_token", "--hf-token", "HFトークン (Private用)", placeholder="hf_..."),
            f_str("chat_template", "--chat-template", "チャットテンプレート (任意)"),
            f_bool("jinja", "--jinja", "Jinjaテンプレートエンジン"),
            f_select("reasoning", "--reasoning", "reasoning出力", ["", "auto", "on", "off"], default="auto"),
            f_str("lora", "--lora", "LoRAアダプタ (任意)", placeholder=".gguf LoRAパス"),
            # --- 性能・メモリ ------------------------------------------------
            f_num("ctx_size", "--ctx-size", "コンテキスト長", section="sched", default="4096", placeholder="0=モデルの学習長"),
            f_num("n_gpu_layers", "--n-gpu-layers", "GPUに載せる層数 (ngl)", section="sched", placeholder="-1=全て / 0=CPU"),
            f_num("batch_size", "--batch-size", "バッチサイズ (b)", section="sched", default="2048"),
            f_num("ubatch_size", "--ubatch-size", "マイクロバッチ (ub)", section="sched", default="512"),
            f_num("parallel", "--parallel", "並列スロット数 (np)", section="sched", default="1"),
            f_num("threads", "--threads", "スレッド数 (t)", section="sched", placeholder="0=自動", help="空欄のときは自動"),
            f_select("flash_attn", "--flash-attn", "Flash Attention (fa)", ["", "auto", "on", "off"], section="sched", default="auto"),
            f_select("load_mode", "--load-mode", "モデルロード方法",
                     ["", "auto", "none", "mmap", "mmap+mlock"], section="sched", default="auto",
                     help="mmap+mlock = RAM常駐 (mlock 相当)"),
            f_num("defrag_thold", "--defrag-thold", "KV defrag閾値", section="sched", numtype="float", placeholder="有効時 0.1 程度"),
            # --- サーバー ---------------------------------------------------
            f_str("host", "--host", "ホスト", section="server", default="0.0.0.0"),
            f_num("port", "--port", "ポート", section="server", default="8080"),
            f_str("api_key", "--api-key", "APIキー", section="server", placeholder="設定すると Bearer 認証"),
            f_bool("no_webui", "--no-webui", "WebUI を無効化", section="server"),
            f_bool("metrics", "--metrics", "/metrics (Prometheus)", section="server"),
            f_bool("slots", "--slots", "/slots API (KV状態)", section="server"),
            f_bool("no_cont_batching", "--no-cont-batching", "継続バッチング無効", section="server"),
            f_bool("embedding", "--embedding", "埋め込みモード (embedding API)", section="server"),
            f_num("timeout_keep_alive", "--timeout-keep-alive", "keep-alive秒", section="server", default="5"),
            # --- デフォルトサンプリング ---------------------------------------
            f_num("temp", "--temp", "temperature", section="sampling", numtype="float", default="0.80"),
            f_num("top_k", "--top-k", "top-k", section="sampling", default="40"),
            f_num("top_p", "--top-p", "top-p", section="sampling", numtype="float", default="0.95"),
            f_num("min_p", "--min-p", "min-p", section="sampling", numtype="float", default="0.05"),
            f_num("repeat_penalty", "--repeat-penalty", "反復ペナルティ", section="sampling", numtype="float", default="1.00"),
            f_num("seed", "--seed", "乱数seed", section="sampling", placeholder="-1=ランダム"),
        ],
    },
    # =====================================================================
    "freetoken": {
        "id": "freetoken",
        "name": "FreeToken",
        "repo": "https://github.com/FlashML-org/FreeToken",
        "module_path": "module/freetoken",
        "kind": "python",
        "desc": "MoEエキスパートオフロードによる大型モデルのローカル推論 (OpenAI/Anthropic 互換)。",
        "default_command": "{python} -m freetoken",
        "check_module": "freetoken",
        "health_path": "/health",
        "ready_markers": ["Uvicorn running", "Application startup complete"],
        "default_port": "1919",
        "fields": [
            # --- モデル -----------------------------------------------------
            f_str("model_path", "--model-path", "モデル", placeholder="deepseek-ai/DeepSeek-V4 など"),
            f_str("served_model_name", "--served-model-name", "提供モデル名 (任意)"),
            f_select("model_source", "--model-source", "モデル取得元", ["", "huggingface", "modelscope"], default="huggingface"),
            f_select("dtype", "--dtype", "dtype", ["", "auto", "float16", "bfloat16", "float32"], default="auto"),
            f_num("max_seq_len_override", "--max-seq-len-override", "最大コンテキスト長上書き", placeholder="空=モデル設定"),
            f_num("max_output_tokens", "--max-output-tokens", "最大出力トークン", placeholder="空=自動"),
            f_select("sampling_defaults", "--sampling-defaults", "サンプリング既定", ["", "model", "none"], default="model"),
            # --- 並列・デバイス ----------------------------------------------
            f_num("tp_size", "--tp-size", "Tensor並列数 (TP)", section="parallel", default="1"),
            f_str("gpu", "--gpu", "GPUデバイス指定", section="parallel", placeholder="0 または 0,1 (空=自動)"),
            f_num("cuda_graph_max_bs", "--cuda-graph-max-bs", "CUDA graph 最大bs", section="parallel", placeholder="自動"),
            f_num("num_tokenizer", "--num-tokenizer", "トークナイザ数", section="parallel", placeholder="自動"),
            f_bool("disable_pynccl", "--disable-pynccl", "PyNCCL 無効", section="parallel"),
            f_bool("dummy_weight", "--dummy-weight", "ダミーウェイト (動作確認用)", section="parallel"),
            # --- スケジューリング・メモリ -------------------------------------
            f_num("max_running_requests", "--max-running-requests", "最大実行中リクエスト", section="sched", placeholder="自動"),
            f_num("memory_ratio", "--memory-ratio", "メモリ使用率", section="sched", numtype="float", default="0.9"),
            f_num("max_extend_length", "--max-extend-length", "最大extendトークン", section="sched", default="8192"),
            f_num("max_prefill_length", "--max-prefill-length", "最大prefillトークン", section="sched", placeholder="自動"),
            f_num("page_size", "--page-size", "KVページサイズ", section="sched", default="1"),
            f_num("num_pages", "--num-pages", "KVページ数", section="sched", placeholder="自動"),
            f_num("num_tokens", "--num-tokens", "KVトークン数指定", section="sched", placeholder="自動"),
            f_num("kv_reserve_tokens", "--kv-reserve-tokens", "KV予約トークン", section="sched", default="8192",
                   help="--moe-cache-auto 時のKV下限"),
            f_select("cache_type", "--cache-type", "KVキャッシュ型", ["", "radix"], default="radix"),
            # --- MoE オフロード ---------------------------------------------
            f_select("moe_strategy", "--moe-strategy", "MoE戦略", ["", "auto", "fused", "offload", "cpu", "hybrid"], default="auto"),
            f_select("expert_load", "--expert-load", "エキスパート読込", ["", "auto", "serial", "parallel"], default="auto"),
            f_select("quant_backend", "--quant-backend", "量子化バックエンド", ["", "auto", "triton", "marlin"],
                     help="moe.nvfp4=... 形式も可"),
            f_select("ple_backend", "--ple-backend", "PLEオフロード", ["", "pinned", "disk"]),
            f_select("nvfp4_backend", "--nvfp4-backend", "NVFP4バックエンド", ["", "auto", "marlin", "flashinfer", "triton"]),
            f_num("moe_cache_size", "--moe-cache-size", "MoEキャッシュサイズ", section="exec", placeholder="0=自動",
                   help="--moe-cache-rate / --moe-cache-auto と排他"),
            f_num("moe_cache_rate", "--moe-cache-rate", "MoEキャッシュ比率", section="exec", numtype="float", placeholder="例 0.5",
                   help="--moe-cache-size と排他"),
            f_bool("moe_cache_auto", "--moe-cache-auto", "MoEキャッシュ自動", section="exec",
                   help="サイズ系の他2項目と排他"),
            f_select("moe_cache_policy", "--moe-cache-policy", "MoEキャッシュ方針", ["", "lru"], section="exec"),
            f_num("moe_cpu_threads", "--moe-cpu-threads", "MoE CPUスレッド数", section="exec", placeholder="0=自動"),
            f_str("moe_cpu_layers", "--moe-cpu-layers", "CPU実行MoE層 (任意)", section="exec", placeholder="例: 0,1,2") ,
            f_num("moe_hybrid_max_fetch", "--moe-hybrid-max-fetch", "hybrid最大fetch", section="exec", placeholder="-1=無制限"),
            f_bool("disable_moe_prefill_overlap", "--disable-moe-prefill-overlap", "prefill overlap無効", section="exec"),
            f_bool("moe_prefill_hit_d2d", "--moe-prefill-hit-d2d", "prefillヒットD2D", section="exec"),
            f_bool("enable_special_token_ckpt", "--enable-special-token-ckpt", "special token checkpoint", section="exec"),
            # --- サーバー ---------------------------------------------------
            f_str("host", "--host", "ホスト", section="server", default="0.0.0.0"),
            f_num("port", "--port", "ポート", section="server", default="1919"),
            f_str("cors_origins", "--cors-origins", "CORS許可origin", section="server", placeholder="カンマ区切り"),
            f_bool("enable_cache_report", "--enable-cache-report", "キャッシュレポート", section="server"),
            f_bool("shell_mode", "--shell-mode", "シェルモード (対話)", section="server",
                   help="Webサーバーではなく対話シェルで起動する"),
            # --- ツール・推論 ------------------------------------------------
            f_select("tool_call_parser", "--tool-call-parser", "ツールコールパーサ",
                     ["", "auto", "llama3", "qwen", "qwen25", "qwen3_coder", "mistral", "deepseekv32",
                      "gemma4", "glm47", "minimax", "minimax_m3", "muse_glimmer", "gpt_oss"], default="auto"),
            f_select("reasoning_parser", "--reasoning-parser", "reasoningパーサ",
                     ["", "auto", "off", "deepseekv32", "gpt_oss", "qwen3", "glm"], default="auto"),
            # --- ログ -------------------------------------------------------
            f_num("decode_log_interval", "--decode-log-interval", "decodeログ間隔", section="logs", default="40"),
            f_select("attention_backend", "--attention-backend", "Attentionバックエンド",
                     ["", "auto", "flashinfer", "triton"], section="exec", default="auto"),
        ],
    },
}


# ---------------------------------------------------------------------------
# パス / 解決ヘルパ
# ---------------------------------------------------------------------------


def default_python(root: Path) -> str:
    """プロジェクト .venv の python。無ければ launcher 自身の python。"""
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
        val = values.get(key)
        if val is None:
            continue
        sval = str(val).strip()
        if field["type"] == "bool":
            if sval.lower() in TRUTHY:
                argv.append(field["flag"])
        elif field["type"] == "select":
            if sval:
                argv += [field["flag"], sval]
        else:
            if sval:
                argv += [field["flag"], sval]
    extra = (profile.get("extra_args") or "").strip()
    if extra:
        try:
            argv += shlex.split(extra, posix=sys.platform != "win32")
        except ValueError as e:
            raise ValueError(f"追加引数の解析に失敗しました: {e}")
    return argv


def extract_port(backend: dict[str, Any], profile: dict[str, Any]) -> int | None:
    values = profile.get("values") or {}
    raw = str(values.get("port", "")).strip() or str(backend.get("default_port", "")).strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


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
