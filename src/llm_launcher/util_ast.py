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
    """`NAME = [ ... ]` / `NAME = ( ... )` を {name: [items]} で返す。"""
    consts: dict[str, list] = {}
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name) and isinstance(stmt.value, (ast.List, ast.Tuple)):
                    vals = []
                    for e in stmt.value.elts:
                        if isinstance(e, ast.Constant) and isinstance(e.value, str):
                            vals.append(e.value)
                    if vals:
                        consts[t.id] = vals
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
