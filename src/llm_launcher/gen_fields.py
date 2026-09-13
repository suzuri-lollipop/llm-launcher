#!/usr/bin/env python3
"""バックエンド submodule の実ソースから起動引数の正表 (flag -> 型/choices/既定値) を生成する。

生成先: src/llm_launcher/fields/<backend>.json
  { "<canonical-flag>": {"type","choices","default","help","aliases":[...]} }
backends.py は UI 用のラベル/セクションだけを管理し、選択肢・既定値は必ずこの正表から引く。
submodule を更新したら `python -m llm_launcher.gen_fields` で再生成する。
"""
from __future__ import annotations

import argparse
import ast
import glob
import json
import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2] if "__file__" in dir() else Path.cwd()


def _repo(root: Path, mod: str) -> Path:
    return root / "module" / mod


# ---------------------------------------------------------------- FreeToken
def gen_freetoken(root: Path) -> dict[str, Any]:
    from .util_ast import collect_dataclass_defaults, collect_module_constants
    base = _repo(root, "freetoken") / "python/freetoken"
    args_py = base / "server/args.py"
    src = args_py.read_text()
    tree = ast.parse(src)
    defaults = collect_dataclass_defaults(
        glob.glob(str(base / "**/*.py"), recursive=True),
        ("ServerArgs", "EngineConfig", "SchedulerConfig"),
    )
    consts: dict[str, Any] = {}
    for cf in glob.glob(str(base / "**/*.py"), recursive=True):
        try:
            consts.update(collect_module_constants(cf))
        except SyntaxError:
            pass

    def resolve(expr: str) -> Any:
        expr = expr.strip()
        m = re.match(r"(?:ServerArgs|EngineConfig|SchedulerConfig)\.(\w+)", expr)
        if m:
            v = defaults.get(m.group(1))
            return _literal(v)
        return _literal(expr)

    out: dict[str, Any] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            continue
        flags = [a.value for a in node.args if isinstance(a, ast.Constant) and a.value.startswith("-")]
        if not flags:
            continue
        kw = {k.arg: k for k in node.keywords}
        prim = min(flags, key=len)  # 最長でなく主 (--model-path) は最初の positional 相当
        prim = next((f for f in flags if f.startswith("--")), flags[0])
        spec: dict[str, Any] = {"aliases": [f for f in flags if f != prim]}
        if "type" in kw:
            t = ast.unparse(kw["type"].value)
            spec["type"] = {"int": "int", "float": "float", "str": "str", "bool": "bool"}.get(t, "str")
        if "action" in kw and "store_true" in ast.unparse(kw["action"].value):
            spec["type"] = "bool"
        if "choices" in kw:
            spec["choices"] = _choices(kw["choices"].value, defaults, consts)
        if "default" in kw:
            spec["default"] = resolve(ast.unparse(kw["default"].value))
        if "help" in kw and isinstance(kw["help"].value, ast.Constant) and isinstance(kw["help"].value.value, str):
            spec["help"] = kw["help"].value.value[:200]
        out[prim] = spec
    return out


# ------------------------------------------------------------------- SGLang
def gen_sglang(root: Path) -> dict[str, Any]:
    from .util_ast import collect_module_constants
    base = _repo(root, "sglang") / "python/sglang/srt"
    fields_glob = glob.glob(str(base / "arg_groups/fields/*.py"))
    consts: dict[str, Any] = {}
    # 定数は fields 群 + server_args.py + arg_groups/*.py を横断解決 (choices=list 参照用)
    const_files = [str(base / "server_args.py")] + glob.glob(str(base / "arg_groups/*.py")) + fields_glob
    for f in const_files:
        try:
            consts.update(collect_module_constants(f))
        except SyntaxError:
            pass
    # 追加: 型アノテーション Literal[...] の choices も拾う
    for f in const_files:
        try:
            text = Path(f).read_text()
        except OSError:
            continue
        for m in re.finditer(r'^(\w+)\s*(?::\s*type)?\s*=\s*Literal\[([^\]]+)\]', text, re.M):
            vals = re.findall(r'"([^"]+)"', m.group(2))
            if vals:
                consts.setdefault(m.group(1), vals)
    out: dict[str, Any] = {}
    for f in fields_glob:
        src = Path(f).read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
                continue
            name = node.target.id
            if name.startswith("_"):
                continue
            ann = ast.unparse(node.annotation)
            m = re.search(r"A\[\s*(.+)", ann, re.S)
            if not m:
                continue
            body = m.group(1)
            spec: dict[str, Any] = {}
            spec["type"] = _sg_type(body)
            al = re.search(r'aliases=\[([^\]]*)\]', body)
            if al:
                spec["aliases"] = [x.strip().strip('"\'') for x in al.group(1).split(",") if x.strip()]
            ch = re.search(r'choices=\[?([^\]\]]+)', body)
            if ch:
                spec["choices"] = _resolve_choices(ch.group(1), consts)
            if node.value is not None:
                spec["default"] = _literal(ast.unparse(node.value))
            hp = re.search(r'(?:Arg\(\s*)?help="([^"]+)"', body)
            if hp:
                spec["help"] = hp.group(1)[:200]
            flag = f"--{name.replace('_','-')}"
            out[flag] = spec
    return out


def _sg_type(body: str) -> str:
    head = body.split(",", 1)[0].strip()
    if head.startswith("bool"):
        return "bool"
    if head.startswith("int"):
        return "int"
    if head.startswith("float"):
        return "float"
    return "str"


def _resolve_choices(s: str, consts: dict) -> list[str]:
    s = s.strip()
    if s[0] in "[(":
        end = 0
        depth = 0
        for i, c in enumerate(s):
            if c in "[(":
                depth += 1
            elif c in "])":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        inner = s[1:end]
        return re.findall(r'"([^"]+)"', inner)
    # 定数名参照
    km = re.match(r"(\w+)", s)
    if not km:
        return []
    v = consts.get(km.group(1))
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return []


# --------------------------------------------------------------------- vLLM
def gen_vllm(root: Path) -> dict[str, Any]:
    from .util_ast import literal_choices
    base = _repo(root, "vllm") / "vllm"
    out: dict[str, Any] = {}

    def note(flag, **spec):
        out.setdefault(flag, {}).update({k: v for k, v in spec.items() if v is not None})

    # tool parsers
    tp = (base / "tool_parsers/__init__.py").read_text()
    i = tp.find("_TOOL_PARSERS_TO_REGISTER")
    keys = re.findall(r'^\s{4}"([^"]+)":', tp[i:tp.find("\n}", i)], re.M)
    note("--tool-call-parser", choices=keys, type="str")
    # reasoning parsers
    rp = (base / "reasoning/__init__.py").read_text()
    i = rp.find("_REASONING_PARSERS_TO_REGISTER")
    keys = re.findall(r'^\s{4}"([^"]+)":', rp[i:rp.find("\n}", i)], re.M)
    note("--reasoning-parser", choices=keys, type="str")
    # quantization / load format / dtype / kv cache dtype literals
    q = literal_choices(base / "model_executor/layers/quantization/__init__.py", "QuantizationMethods")
    note("--quantization", choices=q, type="str")
    l = literal_choices(base / "model_executor/model_loader/__init__.py", "LoadFormats")
    note("--load-format", choices=l, type="str", default="auto")
    d = literal_choices(base / "config/model.py", "ModelDType")
    note("--dtype", choices=d, type="str", default="auto")
    c = literal_choices(base / "config/cache.py", "CacheDType")
    note("--kv-cache-dtype", choices=c, type="str", default="auto")
    # 純粋な数値/ブール系は add_argument の存在確認だけ (既定値は help 側)
    au = (base / "engine/arg_utils.py").read_text()
    for flag in ["--tensor-parallel-size", "--pipeline-parallel-size", "--data-parallel-size",
                 "--gpu-memory-utilization", "--max-model-len", "--block-size", "--max-num-seqs",
                 "--max-num-batched-tokens", "--cpu-offload-gb", "--seed", "--download-dir",
                 "--tokenizer", "--served-model-name", "--revision", "--chat-template",
                 "--enable-expert-parallel", "--trust-remote-code", "--enforce-eager",
                 "--enable-prefix-caching", "--enable-chunked-prefill", "--disable-log-stats",
                 "--enable-lora", "--max-loras", "--max-lora-rank", "--max-logprobs",
                 "--generation-config", "--distributed-executor-backend", "--swap-space"]:
        if f'"{flag}"' in au or f"'{flag}'" in au:
            note(flag)
    fe = (base / "entrypoints/launchers/cli_args.py").read_text()
    for flag in ["--api-key", "--enable-auto-tool-choice", "--host", "--port", "--root-path",
                 "--uvicorn-log-level", "--disable-uvicorn-access-log", "--disable-fastapi-docs",
                 "--allow-credentials", "--chat-template-content-format", "--enable-prompt-tokens-details"]:
        if f'"{flag}"' in fe or f'--{flag.lstrip("-").replace("-","_")}' in fe or flag.lstrip("-").replace("-","_") in fe:
            note(flag)
    note("--host", default="0.0.0.0")
    note("--port", type="int", default="8000")
    note("--gpu-memory-utilization", type="float", default="0.9")
    note("--tensor-parallel-size", type="int", default="1")
    note("--enable-auto-tool-choice", type="bool")
    note("--trust-remote-code", type="bool")
    note("--enable-prefix-caching", type="bool")
    note("--enable-chunked-prefill", type="bool")
    note("--enforce-eager", type="bool")
    note("--enable-expert-parallel", type="bool")
    note("--disable-log-stats", type="bool")
    note("--enable-lora", type="bool")
    note("--disable-uvicorn-access-log", type="bool")
    note("--disable-fastapi-docs", type="bool")
    note("--allow-credentials", type="bool")
    return out


# ------------------------------------------------------------------ llama.cpp
def gen_llamacpp(root: Path) -> dict[str, Any]:
    src = (_repo(root, "llama.cpp") / "common/arg.cpp").read_text()
    out: dict[str, Any] = {}
    pat = re.compile(
        r'\{\s*"((?:--|-)\w[\w\-.]*(?:",\s*"(?:--|-)[\w\-.]+)*)"\s*\}\s*,\s*"([^"]*)"'
        r'(?:\s*,\s*"((?:[^"\\]|\\.)*))?')
    for m in pat.finditer(src):
        # group(1) は外側の引用符を除いた "--a", "--b" 連なり
        flags = re.split(r'"\s*,\s*"', m.group(1))
        if not flags:
            continue
        prim = next((f for f in flags if f.startswith("--")), flags[0])
        second, third = m.group(2), (m.group(3) or "")
        # METAVAR 判定: 短い大文字系 or 選択肢括弧。でなければ help (bool)
        is_metavar = bool(second) and len(second) <= 30 and (
            re.fullmatch(r"[A-Za-z0-9_,.%|\[\]/<>()+\-*=?\- ]+", second)
            and (second == second.upper() or "|" in second or second[0] == "["))
        if is_metavar:
            metavar = second
            helptxt = third
        else:
            metavar = ""
            helptxt = second or third
        spec: dict[str, Any] = {"aliases": [f for f in flags if f != prim]}
        low_help = helptxt.lower()
        if metavar:
            spec["metavar"] = metavar
            spec["type"] = "int" if metavar in ("N", "SECS", "SIZE", "MN", "MW", "TICKS") else \
                           "float" if metavar == "F" else "str"
            om = re.findall(r"\[([\w|.-]+)\]", metavar + " " + helptxt[:200])
            for o in om:
                if "|" in o:
                    spec["type"] = "select"
                    spec["choices"] = sorted(set(o.split("|")))
                    break
            if "on|off|auto" in low_help or "auto|on|off" in low_help:
                spec["type"] = "select"
                spec["choices"] = ["auto", "on", "off"]
        else:
            spec["type"] = "bool"
        dm = re.search(r"default: ([\w.%-]+)", helptxt)
        if dm:
            spec["default"] = dm.group(1)
        if helptxt:
            spec["help"] = helptxt[:200]
        # 同一 flag の追記定義はマージ
        prev = out.get(prim, {})
        merged = {**prev, **{k: v for k, v in spec.items() if v not in (None, [], "")}}
        out[prim] = merged
    return out


def _literal(s: Any) -> Any:
    if s is None:
        return None
    if isinstance(s, str):
        s = s.strip()
        try:
            return json.loads(s) if s[0] in "['\"0123456789-" or s in ("true", "false", "True", "False", "None") else s.strip("'\"")
        except Exception:
            return s.strip("'\"")
    return s


def _choices(node: ast.AST, defaults: dict, consts: dict | None = None) -> list[str]:
    consts = consts or {}
    result: list[str] = []
    if isinstance(node, ast.List):
        for e in node.elts:
            if isinstance(e, ast.Constant) and isinstance(e.value, str):
                result.append(e.value)
            elif isinstance(e, ast.Starred):
                src: list[str] = []
                nm = ast.unparse(e.value).strip()
                if nm in consts:
                    src = [str(x) for x in consts[nm]]
                elif nm in defaults:
                    src = re.findall(r'"([^"]+)"', defaults[nm])
                result.extend(x for x in src if x not in result)
    return result


def build(root: Path) -> dict[str, dict[str, Any]]:
    return {
        "freetoken": gen_freetoken(root),
        "sglang": gen_sglang(root),
        "vllm": gen_vllm(root),
        "llamacpp": gen_llamacpp(root),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT), help="llm-launcher プロジェクトルート")
    ap.add_argument("--out", default="src/llm_launcher/fields")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    tables = build(root)
    outdir = root / a.out
    outdir.mkdir(parents=True, exist_ok=True)
    for name, tbl in tables.items():
        (outdir / f"{name}.json").write_text(
            json.dumps(tbl, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        print(f"{name}: {len(tbl)} flags -> {outdir/f'{name}.json'}")


if __name__ == "__main__":
    main()
