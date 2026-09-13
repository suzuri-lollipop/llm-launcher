"""JSON ファイルによるシンプルな永続化ストア (プロファイル / 設定 / 起動履歴)。"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def now() -> float:
    return time.time()


class JsonStore:
    """アトミック書き込みを行う 1 ファイル = 1 JSON ドキュメントのストア。"""

    def __init__(self, path: Path, default: Any):
        self.path = path
        self.default = default
        self._cache: Any = None

    def load(self) -> Any:
        if self._cache is not None:
            return self._cache
        if self.path.exists():
            try:
                self._cache = json.loads(self.path.read_text("utf-8"))
                return self._cache
            except (json.JSONDecodeError, OSError):
                # 壊れている場合はバックアップしてから初期化
                try:
                    self.path.rename(self.path.with_suffix(".corrupt"))
                except OSError:
                    pass
        self._cache = json.loads(json.dumps(self.default))
        self.save()
        return self._cache

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._cache, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def mutate(self, fn):
        data = self.load()
        result = fn(data)
        self.save()
        return result


def new_id(prefix: str = "p") -> str:
    import secrets
    return f"{prefix}_{int(now()):08x}{secrets.token_hex(2)}"
