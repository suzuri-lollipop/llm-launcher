"""ダッシュボード用のホスト / GPU スナップショット収集。

Linux の /proc と nvidia-smi を直接読む (追加依存なし)。読めない項目は
キー欠損 / null になるだけで致命的にはならない。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import shutil
import socket
import time
from typing import Any

GPU_TTL = 4.0            # nvidia-smi 実行コストがあるので短めキャッシュ
GPU_TIMEOUT = 8.0
GPU_QUERY = ("index,name,utilization.gpu,memory.used,memory.total,"
             "temperature.gpu,power.draw,power.limit")


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def host_snapshot(root: str) -> dict[str, Any]:
    """ロードアベレージ / メモリ / ディスク / 稼働時間 (安価なので毎回計算)。"""
    h: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count() or 0,
    }
    parts = _read("/proc/uptime").split()
    if parts:
        with contextlib.suppress(ValueError):
            h["uptime"] = float(parts[0])
    la = _read("/proc/loadavg").split()
    if len(la) >= 3:
        with contextlib.suppress(ValueError):
            h["load"] = [float(la[0]), float(la[1]), float(la[2])]
    mem: dict[str, int] = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            cols = rest.split()
            if cols:
                with contextlib.suppress(ValueError):
                    mem[key] = int(cols[0]) * 1024          # kB -> bytes
    if mem.get("MemTotal"):
        total, avail = mem["MemTotal"], mem.get("MemAvailable", 0)
        swap_t, swap_f = mem.get("SwapTotal", 0), mem.get("SwapFree", 0)
        h["memory"] = {"total": total, "used": max(0, total - avail), "available": avail,
                       "swap_total": swap_t, "swap_used": max(0, swap_t - swap_f)}
    with contextlib.suppress(OSError):
        du = shutil.disk_usage(root)
        h["disk"] = {"total": du.total, "used": du.used, "free": du.free, "path": root}
    return h


def _parse_gpu_csv(out: str) -> list[dict[str, Any]]:
    gpus: list[dict[str, Any]] = []
    for line in out.strip().splitlines():
        p = [c.strip() for c in line.split(",")]
        if len(p) < 8:
            continue

        def num(i: int) -> float | None:
            with contextlib.suppress(ValueError):
                return float(p[i])
            return None
        gpus.append({
            "index": p[0], "name": p[1], "util": num(2),
            "mem_used_mib": num(3), "mem_total_mib": num(4),
            "temp": num(5), "power": num(6), "power_limit": num(7),
        })
    return gpus


_gpu_lock = asyncio.Lock()
_gpu_cache: dict[str, Any] = {"ts": 0.0, "gpus": None, "error": ""}


async def gpu_snapshot() -> dict[str, Any]:
    """nvidia-smi で GPU 一覧を採集 (TTL キャッシュ + single-flight)。

    返り値: {"gpus": [...]|None, "error": str} — 非 NVIDIA / 失敗時は gpus=None。"""
    if _gpu_cache["gpus"] is not None and time.monotonic() - _gpu_cache["ts"] < GPU_TTL:
        return {"gpus": _gpu_cache["gpus"], "error": _gpu_cache["error"]}
    async with _gpu_lock:
        # ロック待ち後に再チェック (同時リクエストは1回の実行に寄せる)
        if _gpu_cache["gpus"] is not None and time.monotonic() - _gpu_cache["ts"] < GPU_TTL:
            return {"gpus": _gpu_cache["gpus"], "error": _gpu_cache["error"]}
        exe = shutil.which("nvidia-smi")
        gpus: list[dict[str, Any]] | None = None
        err = ""
        if not exe:
            err = "nvidia-smi が見つかりません (NVIDIA 環境以外では GPU 欄は非表示)"
        else:
            try:
                proc = await asyncio.create_subprocess_exec(
                    exe, f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                out, b = await asyncio.wait_for(proc.communicate(), timeout=GPU_TIMEOUT)
                if proc.returncode == 0:
                    gpus = _parse_gpu_csv(out.decode("utf-8", "replace"))
                    if not gpus:
                        err = "nvidia-smi 出力を解析できませんでした"
                else:
                    err = f"nvidia-smi 失敗 (rc={proc.returncode}): {b.decode('utf-8', 'replace')[:160]}"
            except asyncio.TimeoutError:
                err = "nvidia-smi がタイムアウトしました"
            except OSError as e:
                err = f"nvidia-smi 起動失敗: {e}"
        _gpu_cache.update(ts=time.monotonic(), gpus=gpus, error=err)
        return {"gpus": gpus, "error": err}
