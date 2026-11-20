"""ダッシュボードの平均アクティブ数 (1 日窓): プロファイル毎の 1 分バケット蓄積。

デコード速度カードの「1日平均」は、リセット時刻 (既定 00:00、ローカル時刻) を
起点とする窓のアクティブ (処理中) リクエスト本数の平均。サーバーの /metrics は
瞬間値だけで 60 秒履歴も tok/s しか持たないため、launcher が数秒毎にアクティブ数を
サンプリング (背景ループ + 中継ポーリング) し、1 分バケットに蓄積する。

- 1 バケット = 1 分: [アクティブ数の合計, サンプル数]。バケット値 = その分の平均。
- 1日平均 = 観測できた各分のバケット値の平均 (分単位の時間加重)。サンプルの
  無い分 (サーバー停止 / launcher 停止中) は平均に含めない。
- リセット時刻を跨ぐと窓が自動で進み、古いバケットは捨てる。手動リセットは
  その時点から新しい窓を開始する (次の自動リセットは次のリセット時刻)。
- セッションではなくプロファイル鍵: サーバー再起動で日の記録が消えない。
- data/parallel_daily.json に永続化 (launcher 再起動を跨ぐ)。書き込みは
  dirty かつ前回保存から SAVE_INTERVAL_S 以上経過したときだけ。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Iterable

from .store import JsonStore

DEFAULT_RESET_TIME = "00:00"
BUCKET_S = 60
SAVE_INTERVAL_S = 30.0

_RESET_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def valid_reset_time(v: Any) -> bool:
    """HH:MM (00:00–23:59) 形式かどうか。"""
    return isinstance(v, str) and bool(_RESET_RE.match(v))


def window_start_for(reset_time: str, now_ts: float) -> float:
    """now_ts 以前で最新のリセット時刻 (ローカル時刻) の UNIX 秒。"""
    if not valid_reset_time(reset_time):
        reset_time = DEFAULT_RESET_TIME
    h, m = int(reset_time[:2]), int(reset_time[3:])
    lt = time.localtime(now_ts)
    start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
    if start > now_ts:                       # 今日のリセット時刻がまだ来ていない
        y = time.localtime(now_ts - 86400)
        start = time.mktime((y.tm_year, y.tm_mon, y.tm_mday, h, m, 0, 0, 0, -1))
    return start


class DailyActiveStore:
    """プロファイル毎の 1 日窓アクティブ数蓄積 (JsonStore 永続化付き)。"""

    def __init__(self, path: Path) -> None:
        self._store = JsonStore(path, {"profiles": {}})
        self._dirty = False
        self._last_save = 0.0

    # ------------------------------------------------------------ 内部

    def _entries(self) -> dict[str, Any]:
        doc = self._store.load()
        if not isinstance(doc, dict) or not isinstance(doc.get("profiles"), dict):
            doc = self._store.mutate(lambda _: {"profiles": {}})
        return doc["profiles"]

    def _roll(self, entry: dict[str, Any], now_ts: float, reset_time: str) -> None:
        """リセット時刻を跨いでいたら窓を進め、窓より古いバケットを捨てる。"""
        start = window_start_for(reset_time, now_ts)
        if start > entry.get("window_start", 0):
            entry["window_start"] = start
            cut = int(start // BUCKET_S)
            entry["buckets"] = {k: v for k, v in entry["buckets"].items() if int(k) >= cut}
            self._dirty = True

    def _entry(self, pid: str, now_ts: float, reset_time: str) -> dict[str, Any]:
        entries = self._entries()
        e = entries.get(pid)
        if not isinstance(e, dict) or not isinstance(e.get("buckets"), dict):
            e = entries[pid] = {"window_start": window_start_for(reset_time, now_ts),
                                "buckets": {}}
            self._dirty = True
        self._roll(e, now_ts, reset_time)
        return e

    # ------------------------------------------------------------ 操作

    def add(self, pid: str, ts: float, active: Any,
            reset_time: str = DEFAULT_RESET_TIME) -> None:
        """1 サンプル (瞬間のアクティブ数) をその分のバケットへマージする。"""
        if not pid or not isinstance(active, (int, float)) or isinstance(active, bool):
            return
        e = self._entry(pid, ts, reset_time)
        if ts < e["window_start"]:           # 窓より前の遅延サンプルは入れない
            return
        key = str(int(ts // BUCKET_S))
        b = e["buckets"].get(key)
        if isinstance(b, list) and len(b) == 2:
            b[0] += float(active)
            b[1] += 1
        else:
            e["buckets"][key] = [float(active), 1]
        self._dirty = True

    def reset(self, pid: str, now_ts: float) -> None:
        """手動リセット: 今この時点から新しい窓を開始する。"""
        self._entries()[pid] = {"window_start": now_ts, "buckets": {}}
        self._dirty = True
        self.save()

    def snapshot(self, pid: str, now_ts: float,
                 reset_time: str = DEFAULT_RESET_TIME) -> dict[str, Any]:
        """窓の開始 / 1日平均 / 観測分数 / サンプル数 / 1 分バケット列。

        バケットは [分頭の UNIX 秒, その分の平均, サンプル数] の時刻順。
        平均は「観測できた分の平均」の平均 (サンプルの無い分は含めない)。"""
        e = self._entry(pid, now_ts, reset_time)
        rows: list[tuple[int, float, int]] = []
        for k, b in e["buckets"].items():
            if isinstance(b, list) and len(b) == 2 and b[1] > 0:
                rows.append((int(k) * BUCKET_S, b[0] / b[1], int(b[1])))
        rows.sort()
        avg = sum(r[1] for r in rows) / len(rows) if rows else None
        return {
            "window_start": e["window_start"],
            "avg": avg,
            "minutes": len(rows),
            "samples": sum(r[2] for r in rows),
            "buckets": [[t, round(a, 3), n] for t, a, n in rows],
        }

    def prune(self, pids: Iterable[str]) -> None:
        """削除されたプロファイルの蓄積を捨てる (背景ループが毎周呼ぶ)。"""
        entries = self._entries()
        keep = set(pids)
        for pid in list(entries):
            if pid not in keep:
                del entries[pid]
                self._dirty = True

    # ------------------------------------------------------------ 永続化

    def load(self) -> None:
        self._store.load()

    def save(self) -> None:
        self._store.save()
        self._dirty = False
        self._last_save = time.time()

    def save_if_dirty(self, now_ts: float | None = None) -> None:
        now_ts = time.time() if now_ts is None else now_ts
        if self._dirty and now_ts - self._last_save >= SAVE_INTERVAL_S:
            self.save()
