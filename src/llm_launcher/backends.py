"""バックエンド(vllm / sglang / llama.cpp / FreeToken)の定義と起動コマンド組み立て。

各バックエンドは `module/` 配下の git submodule として登録されている。
ここでは「起動コマンドテンプレート」「UI の引数フォーム用のフィールド定義」
「導入検出方法」を宣言的に持つ。
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


def f_str(key: str, flag: str, label: str, *, placeholder: str = "", default: str = "",
          required: bool = False, help: str = "") -> dict[str, Any]:
    return {"key": key, "flag": flag, "type": "str", "label": label,
            "placeholder": placeholder, "default": default, "required": required, "help": help}


def f_num(key: str, flag: str, label: str, *, numtype: str = "int", default: str = "",
          placeholder: str = "", help: str = "") -> dict[str, Any]:
    return {"key": key, "flag": flag, "type": numtype, "label": label,
            "default": default, "placeholder": placeholder, "help": help}


def f_bool(key: str, flag: str, label: str, *, help: str = "") -> dict[str, Any]:
    return {"key": key, "flag": flag, "type": "bool", "label": label, "help": help}


def f_select(key: str, flag: str, label: str, options: list[str], *, default: str = "",
             help: str = "") -> dict[str, Any]:
    return {"key": key, "flag": flag, "type": "select", "label": label,
            "options": options, "default": default, "help": help}


# ---------------------------------------------------------------------------
# バックエンド定義
# ---------------------------------------------------------------------------

BACKENDS: dict[str, dict[str, Any]] = {
    "vllm": {
        "id": "vllm",
        "name": "vLLM",
        "repo": "https://github.com/vllm-project/vllm",
        "module_path": "module/vllm",
        "kind": "python",
        "desc": "高スループットな OpenAI 互換 API サーバー。pip/uv で vllm をインストールして使用。",
        "default_command": "{python} -m vllm.entrypoints.openai.api_server",
        "check_module": "vllm",
        "health_path": "/health",
        "ready_markers": ["Application startup complete", "Uvicorn running"],
        "default_port": "8000",
        "fields": [
            f_str("model", "--model", "モデル", placeholder="Qwen/Qwen2.5-7B-Instruct またはローカルパス", required=True),
            f_str("served_model_name", "--served-model-name", "提供モデル名 (任意)", placeholder="省略時は --model が使われる"),
            f_str("host", "--host", "ホスト", default="0.0.0.0"),
            f_num("port", "--port", "ポート", default="8000"),
            f_num("tensor_parallel_size", "--tensor-parallel-size", "Tensor並列数 (TP)", default="1"),
            f_num("pipeline_parallel_size", "--pipeline-parallel-size", "Pipeline並列数 (PP)", placeholder="1"),
            f_num("gpu_memory_utilization", "--gpu-memory-utilization", "GPUメモリ使用率", numtype="float", default="0.9"),
            f_num("max_model_len", "--max-model-len", "最大コンテキスト長", placeholder="8192"),
            f_select("dtype", "--dtype", "dtype", ["", "auto", "bfloat16", "float16", "half", "float32"], default="auto"),
            f_select("quantization", "--quantization", "量子化", ["", "awq", "gptq", "fp8", "bitsandbytes", "gguf"], help="量子化チェックポイントを指定する場合のみ設定"),
            f_bool("enforce_eager", "--enforce-eager", "eager モード (CUDA graph 無効)"),
            f_bool("trust_remote_code", "--trust-remote-code", "trust-remote-code"),
            f_bool("enable_prefix_caching", "--enable-prefix-caching", "prefix caching"),
            f_bool("enable_chunked_prefill", "--enable-chunked-prefill", "chunked prefill"),
            f_str("api_key", "--api-key", "APIキー (任意)", placeholder="設定すると Bearer 認証が有効になる"),
        ],
    },
    "sglang": {
        "id": "sglang",
        "name": "SGLang",
        "repo": "https://github.com/sgl-project/sglang",
        "module_path": "module/sglang",
        "kind": "python",
        "desc": "RadixAttention による高速な OpenAI 互換サーバー。pip/uv で sglang をインストールして使用。",
        "default_command": "{python} -m sglang.launch_server",
        "check_module": "sglang",
        "health_path": "/health",
        "ready_markers": ["The server is fired up and ready to roll", "Uvicorn running"],
        "default_port": "30000",
        "fields": [
            f_str("model", "--model-path", "モデル", placeholder="Qwen/Qwen2.5-7B-Instruct またはローカルパス", required=True),
            f_str("served_model_name", "--served-model-name", "提供モデル名 (任意)"),
            f_str("host", "--host", "ホスト", default="0.0.0.0"),
            f_num("port", "--port", "ポート", default="30000"),
            f_num("tp", "--tp", "Tensor並列数 (TP)", default="1"),
            f_num("dp", "--dp", "Data並列数 (DP)", placeholder="1"),
            f_num("mem_fraction_static", "--mem-fraction-static", "静的メモリ比率", numtype="float", default="0.85"),
            f_num("context_length", "--context-length", "最大コンテキスト長", placeholder="8192"),
            f_select("dtype", "--dtype", "dtype", ["", "auto", "bfloat16", "float16", "float32"], default="auto"),
            f_select("quantization", "--quantization", "量子化", ["", "awq", "gptq", "fp8", "w8a8_int8"]),
            f_bool("trust_remote_code", "--trust-remote-code", "trust-remote-code"),
            f_bool("enable_torch_compile", "--enable-torch-compile", "torch.compile"),
            f_bool("disable_cuda_graph", "--disable-cuda-graph", "CUDA graph 無効"),
            f_str("api_key", "--api-key", "APIキー (任意)"),
        ],
    },
    "llamacpp": {
        "id": "llamacpp",
        "name": "llama.cpp",
        "repo": "https://github.com/ggml-org/llama.cpp",
        "module_path": "module/llama.cpp",
        "kind": "binary",
        "desc": "GGUF モデル用の軽量サーバー (llama-server)。module/llama.cpp を cmake ビルドすると自動検出。",
        "default_command": "{llama_server}",
        "binaries": ["llama-server"],
        "build_candidates": ["module/llama.cpp/build/bin/llama-server"],
        "health_path": "/health",
        "ready_markers": ["startup complete", "listening"],
        "default_port": "8080",
        "fields": [
            f_str("model", "--model", "GGUF モデル", placeholder="~/models/xxx-Q4_K_M.gguf または HF Repo:FILE", required=True),
            f_str("alias", "--alias", "提供モデル名 (任意)"),
            f_str("hf_repo", "--hf-repo", "HuggingFaceリポジトリ (任意)", placeholder="モデルを自動ダウンロードする場合のみ"),
            f_str("host", "--host", "ホスト", default="0.0.0.0"),
            f_num("port", "--port", "ポート", default="8080"),
            f_num("ctx_size", "--ctx-size", "コンテキスト長", default="4096"),
            f_num("n_gpu_layers", "--n-gpu-layers", "GPUに載せる層数 (ngl)", placeholder="0 でCPU実行, -1 で全て"),
            f_num("threads", "--threads", "スレッド数", placeholder="自動"),
            f_num("parallel", "--parallel", "並列リクエスト数", default="1"),
            f_num("batch_size", "--batch-size", "バッチサイズ", default="2048"),
            f_select("flash_attn", "--flash-attn", "Flash Attention", ["", "auto", "on", "off"]),
            f_str("api_key", "--api-key", "APIキー (任意)"),
            f_bool("jinja", "--jinja", "Jinjaチャットテンプレート"),
            f_bool("no_mmap", "--no-mmap", "mmap 不使用"),
            f_bool("cont_batching", "--cont-batching", "継続バッチング"),
        ],
    },
    "freetoken": {
        "id": "freetoken",
        "name": "FreeToken",
        "repo": "https://github.com/FlashML-org/FreeToken",
        "module_path": "module/freetoken",
        "kind": "python",
        "desc": "MoEエキスパートオフロードによる大型モデルのローカル推論。pip/uv で freetoken をインストールして使用。",
        "default_command": "{python} -m freetoken",
        "check_module": "freetoken",
        "health_path": "/health",
        "ready_markers": ["Uvicorn running", "Application startup complete"],
        "default_port": "8020",
        "fields": [
            f_str("model", "--model-path", "モデル", placeholder="deepseek-ai/DeepSeek-V4 等", required=True),
            f_str("served_model_name", "--served-model-name", "提供モデル名 (任意)"),
            f_str("host", "--host", "ホスト", default="0.0.0.0"),
            f_num("port", "--port", "ポート", default="8020"),
            f_num("tp", "--tp-size", "Tensor並列数 (TP)", default="1"),
            f_str("gpu", "--gpu", "GPUデバイス", placeholder="0 または 0,1"),
            f_num("memory_ratio", "--memory-ratio", "メモリ比率", numtype="float", default="0.9"),
            f_num("max_seq_len", "--max-seq-len-override", "最大コンテキスト長", placeholder=""),
            f_select("dtype", "--dtype", "dtype", ["", "auto", "bfloat16", "float16"], default="auto"),
            f_select("moe_strategy", "--moe-strategy", "MoE戦略", ["", "hybrid", "offload", "gpu"]),
            f_select("quant_backend", "--quant-backend", "量子化バックエンド", ["", "auto", "marlin", "triton"]),
            f_select("attention_backend", "--attention-backend", "Attentionバックエンド", ["", "auto", "flashinfer", "triton"]),
            f_bool("dummy_weight", "--dummy-weight", "ダミーウェイト (動作確認用)"),
            f_bool("enable_cache_report", "--enable-cache-report", "キャッシュレポート"),
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
    if not resolved.strip():
        raise ValueError("起動コマンドが空です")
    argv = shlex.split(resolved)
    if backend["kind"] == "binary" and "{llama_server}" in command and not binary:
        raise ValueError("llama-server が見つかりません。バックエンド設定でパスを指定するか、"
                         "module/llama.cpp をビルドしてください")
    for field in ([] if profile.get("custom_only") else backend.get("fields", [])):
        key = field["key"]
        val = values.get(key)
        if val is None:
            continue
        sval = str(val).strip()
        if field["type"] == "bool":
            # enforce_eager / vllm の CUDA graph セレクトのような特別扱いには使わない(boolのみ)
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


def backend_field_names(backend: dict[str, Any]) -> set[str]:
    return {f["flag"] for f in backend.get("fields", [])}
