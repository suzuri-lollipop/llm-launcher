"""AST ユーティリティ: submodule 内の dataclass / module 定数から情報を引き抜く。"""
from __future__ import annotations

import ast
import re
from pathlib import Path


def collect_dataclass_defaults(paths, want_classes):
    """指定 class 名の dataclass フィールド既定値を {field: unparsed-expr} で返す (先勝ち)。"""
    out: dict[str, str] = {}
    for p in paths:
        try:
            src = Path(p).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not any(c in src for c in want_classes):
            continue
        tree = ast.parse(src)
        for cls in ast.walk(tree):
            if isinstance(cls, ast.ClassDef) and cls.name in want_classes:
                for stmt in cls.body:
                    if isinstance(stmt, (ast.AnnAssign, ast.Assign)) and getattr(stmt, "value", None) is not None:
                        try:
                            nm = ast.unparse(stmt.target)
                            val = ast.unparse(stmt.value)
                        except Exception:
                            continue
                        out.setdefault(nm, val)
    return out


def collect_module_constants(path):
    """`NAME = [ ... ]` / `NAME = ( ... )` を {name: [items]} で返す。

    レジストリ dict (`KV_FORMATS = {f.name: f for f in (A, B, ...)}`) は各メンバーの
    コンストラクタ第1引数 (名前文字列) の列として解決する。choices 側の
    `*(name for name in KV_FORMATS if ...)` がこの列を引く。
    """
    consts: dict[str, list] = {}
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))

    def assigns():
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign):
                for t in stmt.targets:
                    if isinstance(t, ast.Name):
                        yield t.id, stmt.value
            elif (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                    and stmt.value is not None):
                yield stmt.target.id, stmt.value

    for name, value in assigns():
        if isinstance(value, (ast.List, ast.Tuple)):
            vals = [e.value for e in value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if vals:
                consts[name] = vals
        elif (isinstance(value, ast.Call) and value.args
                and isinstance(value.args[0], ast.Constant)
                and isinstance(value.args[0].value, str)):
            consts[name] = [value.args[0].value]
    for name, value in assigns():
        if not isinstance(value, ast.DictComp) or len(value.generators) != 1:
            continue
        gen = value.generators[0]
        key = value.key
        if not (isinstance(key, ast.Attribute) and key.attr == "name"
                and isinstance(key.value, ast.Name) and isinstance(gen.target, ast.Name)
                and key.value.id == gen.target.id and not gen.ifs
                and isinstance(gen.iter, (ast.Tuple, ast.List))):
            continue
        vals = [v for e in gen.iter.elts if isinstance(e, ast.Name)
                for v in consts.get(e.id, [])]
        if vals:
            consts[name] = vals
    return consts


def literal_choices(path, name):
    """`NAME = Literal[ "a", "b", ... ]` から文字列選択肢を抽出。"""
    try:
        s = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    i = s.find(f"{name} = Literal")
    if i < 0:
        i = s.find(f"{name}: Literal")
    if i < 0:
        return None
    j = s.find("[", i)
    depth = 0
    end = j
    for k in range(j, len(s)):
        if s[k] == "[":
            depth += 1
        elif s[k] == "]":
            depth -= 1
            if depth == 0:
                end = k
                break
    return re.findall(r'"([^"]+)"', s[j + 1:end])
