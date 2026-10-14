"""OOM kill 対策: 起動ツリーの oom_score_adj 管理と起動前メモリガード。

Linux の OOM killer は「RSS が最大のプロセス」を犠牲者にするので、モデルインスタンス
(数十 GB) は常時まっ先に殺される。同じ PC で Cline 等の重い CLI を動かしていると、
空きが数 GB を切った瞬間にモデル側が落ちる — これがこのモジュールの動機。

やること (多層):
  1. 起動したサーバーツリー (と launcher 自身) の oom_score_adj を下げて kill 順を後回しにする
  2. 非特権では下げられない (CAP_SYS_RESOURCE が必要) ので、その事実を隠さず表示し、
     導入スクリプト (scripts/oom-protect-install.sh) への道を示す
     → 特権ヘルパーが導入済みなら sudo -n 経由で適用する
  3. 起動前に空きメモリ不足を検知して「カーネルに殺される前に」断る / 警告する
  4. cgroup の memory.events (oom_kill) と exit code から OOM kill だったかを推定する

権限に関する実測 (systemd 259 / 非特権 user session):
  - echo -700 > /proc/self/oom_score_adj        -> EACCES
  - systemd-run --user -p OOMScoreAdjust=-700   -> 受理されるが適用されない (score 不変)
  - systemd-run --user -p ManagedOOMPreference -> "Failed to set unit properties"
  なので「ユーザー権限でできること」は 3 と 4 と「特権を借りる」ことに限定している。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from typing import Any

PROC_ROOT = "/proc"
CGROUP_ROOT = "/sys/fs/cgroup"

# インストーラが置く特権ヘルパー (stdin で pid / score を受け取り、引数を取らない設計
# → sudoers を引数なし実行にピン留めできる)
HELPER_PATH = os.environ.get("LLM_LAUNCHER_OOM_HELPER",
                             "/usr/local/lib/llm-launcher/oom-protect")
INSTALL_HINT = ("sudo scripts/oom-protect-install.sh          # sudoers ヘルパー導入\n"
                "                     sudo scripts/oom-protect-install.sh --systemd  # 常駐サービス化")

DEFAULT_SCORE_ADJ = -700
DEFAULT_MIN_AVAILABLE_GB = 8.0
GUARD_MODES = ("block", "warn", "off")
CAP_SYS_RESOURCE_BIT = 24

DEFAULTS: dict[str, Any] = {
    "enabled": True,            # 起動ツリーの oom_score_adj を下げる
    "score_adj": DEFAULT_SCORE_ADJ,
    "min_available_gb": DEFAULT_MIN_AVAILABLE_GB,   # 起動を許可する最低 MemAvailable
    "guard_mode": "block",      # block / warn / off — 空き不足時の起動前ガード動作
    "reapply": True,            # 起動後に後から湧いた worker へ periodic 再適用
    "swift_stop": False,        # 逼迫時に launcher 側で自発的に安全停止する
}

OOMD_PROBE_TTL = 60.0
_OOMD_PROBE: dict[str, Any] = {"ts": 0.0, "value": False}


class MemoryGuardError(RuntimeError):
    """起動前メモリガードが拒否したことを示す (UI 側は force で再試行できる)。"""


# ------------------------------------------------------------------ settings


def merged_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    """settings.json の oom ブロックを既定値とマージし、値を範囲クリップする。"""
    out = dict(DEFAULTS)
    for k, v in (raw or {}).items():
        if k in out:
            out[k] = v
    out["enabled"] = bool(out["enabled"])
    out["reapply"] = bool(out["reapply"])
    out["swift_stop"] = bool(out["swift_stop"])
    try:
        # 既定 -700。負値を書くには CAP_SYS_RESOURCE が必要で、非特権 (通常ユーザー) では
        # EACCES になるため、その場合は status() / ログ / UI に理由を出し、sudo ヘルパー
        # 導入を案内する (0 以上を書きたい場合は明示的に 0 を設定する)。
        out["score_adj"] = max(-1000, min(1000, int(float(out["score_adj"]))))
    except (ValueError, TypeError):
        out["score_adj"] = DEFAULT_SCORE_ADJ
    try:
        out["min_available_gb"] = max(0.0, min(512.0, float(out["min_available_gb"])))
    except (ValueError, TypeError):
        out["min_available_gb"] = DEFAULT_MIN_AVAILABLE_GB
    if out["guard_mode"] not in GUARD_MODES:
        out["guard_mode"] = DEFAULTS["guard_mode"]
    return out


# ------------------------------------------------------------------- /proc


def _path(*parts: str) -> str:
    return os.path.join(PROC_ROOT, *parts)


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def meminfo() -> dict[str, int]:
    """/proc/meminfo をバイト単位で読む (読めないキーは欠損)。"""
    out: dict[str, int] = {}
    for line in _read(_path("meminfo")).splitlines():
        key, _, rest = line.partition(":")
        cols = rest.split()
        if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree") and cols:
            with contextlib.suppress(ValueError):
                out[key] = int(cols[0]) * 1024          # kB -> bytes
    return out


def available_bytes() -> int:
    return meminfo().get("MemAvailable", 0)


def effective_caps() -> int:
    """/proc/self/status の CapEff (読めなければ 0)。"""
    for line in _read(_path("self", "status")).splitlines():
        if line.startswith("CapEff:"):
            with contextlib.suppress(ValueError):
                return int(line.split()[1], 16)
            return 0
    return 0


def can_lower_score() -> bool:
    """oom_score_adj を **下げる** 権限があるか。

    カーネルは CAP_SYS_RESOURCE のないプロセスの negative 書き込みを EACCES で弾く。
    実際に書き込まず capability ビットで見るので副作用はない。
    """
    if os.geteuid() == 0:
        return True
    return bool(effective_caps() & (1 << CAP_SYS_RESOURCE_BIT))


def read_score(pid: int = 0) -> int | None:
    raw = _read(_path("self" if pid <= 0 else str(pid), "oom_score_adj")).strip()
    with contextlib.suppress(ValueError):
        return int(raw)
    return None


def write_score(pid: int, score: int) -> tuple[bool, str]:
    """1 プロセスの oom_score_adj を書く。失敗時は (False, 理由)。"""
    try:
        with open(_path("self" if pid <= 0 else str(pid), "oom_score_adj"),
                  "w", encoding="ascii") as fh:
            fh.write(str(int(score)))
        return True, ""
    except PermissionError as e:
        return False, f"permission denied ({e.strerror or e})"
    except OSError as e:
        return False, str(e)


def _stat_fields(pid: str) -> tuple[int, int] | None:
    """/proc/<pid>/stat から (ppid, pgid) を取り出す。comm に空白/括弧があっても壊れない。"""
    text = _read(_path(pid, "stat"))
    if not text:
        return None
    close = text.rfind(")")
    if close < 0:
        return None
    cols = text[close + 1:].split()
    if len(cols) < 3:
        return None
    with contextlib.suppress(ValueError, IndexError):
        return int(cols[1]), int(cols[2])      # ppid, pgid
    return None


def tree_pids(root_pid: int) -> list[int]:
    """root_pid とその全子孫 (mp-spawn された scheduler / tokenizer worker 等)。

    起動は start_new_session=True なので pgid == root_pid だが、後からグループを
    抜ける子も取りこぼさないよう ppid ツリーを BFS する。/proc が読めなければ
    [root_pid] にフォールバック。
    """
    children: dict[int, list[int]] = {}
    try:
        entries = os.listdir(PROC_ROOT)
    except OSError:
        return [root_pid]
    for name in entries:
        if not name.isdigit() or int(name) == root_pid:
            continue
        f = _stat_fields(name)
        if f:
            children.setdefault(f[0], []).append(int(name))
    out: list[int] = [root_pid]
    queue: list[int] = [root_pid]
    seen = {root_pid}
    while queue:
        for kid in children.get(queue.pop(0), []):
            if kid not in seen:
                seen.add(kid)
                out.append(kid)
                queue.append(kid)
    return out


# -------------------------------------------------------------- apply score


def helper_available() -> bool:
    """特権ヘルパー (sudo 経由) が導入済みか。"""
    return (os.path.isfile(HELPER_PATH) and os.access(HELPER_PATH, os.X_OK)
            and shutil.which("sudo") is not None)


def _run_helper(cmd: list[str], payload: bytes) -> "subprocess.CompletedProcess[bytes]":
    """ヘルパー実行 (stdin に payload を流す)。テストではここを差し替える。"""
    return subprocess.run(cmd, input=payload, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=8, check=False)


def apply_via_helper(root_pid: int, score: int) -> tuple[bool, str]:
    """sudo -n でヘルパーを呼び、ツリー全体に score を適用してもらう。

    ヘルパーは引数を取らず stdin で (pid, score) を読む。sudoers を
    「引数なしの実行」にピン留めするためで、注入余地を減らすためでもある。
    """
    if not helper_available():
        return False, "helper not installed"
    try:
        r = _run_helper(["sudo", "-n", HELPER_PATH],
                        f"{int(root_pid)}\n{int(score)}\n".encode())
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"helper 実行失敗: {e}"
    out = (r.stdout or b"").decode("utf-8", "replace").strip()
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        return False, err or out or f"helper rc={r.returncode}"
    return True, out


def apply_score(root_pid: int, score: int, pids: list[int] | None = None) -> dict[str, Any]:
    """起動ツリー全体へ score を適用する。駄目なときは理由を返す (例外は投げない)。"""
    pids = list(pids) if pids is not None else tree_pids(root_pid)
    denied: list[int] = []
    applied = 0
    last_err = ""
    for pid in pids:
        ok, err = write_score(pid, score)
        if ok:
            applied += 1
        else:
            denied.append(pid)
            last_err = err
    via = "direct" if applied and not denied else ""
    err = ""
    if denied:
        ok, msg = apply_via_helper(root_pid, score)
        if ok:
            via, err, last_err, denied = "helper", "", "", []
            applied = len(pids)
        else:
            err = last_err or msg
    return {"enabled": True, "score": int(score), "applied": applied,
            "denied": len(denied), "via": via, "error": err,
            "can_lower": can_lower_score(), "helper": helper_available()}


def protect_self(score: int) -> dict[str, Any]:
    """launcher 自身の oom_score_adj を下げる (起動時 / 設定保存時に呼ぶ)。"""
    ok, err = write_score(0, score)
    if ok:
        return {"enabled": True, "score": score, "applied": True, "via": "direct", "error": ""}
    ok_h, msg = apply_via_helper(os.getpid(), score)
    if ok_h:
        return {"enabled": True, "score": score, "applied": True, "via": "helper", "error": ""}
    return {"enabled": True, "score": score, "applied": False, "via": "", "error": err or msg}


# ---------------------------------------------------------------- oom traces


def self_unit() -> str:
    """/proc/self/cgroup から自分 (launcher) の unit 名を取り出す (診断表示用)。"""
    for line in _read(_path("self", "cgroup")).splitlines():
        _, _, path = line.partition("::")
        if path:
            name = path.rstrip("/").rsplit("/", 1)[-1]
            if name.endswith((".service", ".scope", ".slice")):
                return name
    return ""


def cgroup_dir() -> str:
    """自分 cgroup の /sys/fs/cgroup 直下パス (v2 前提、読めなければ '')。"""
    path = ""
    for line in _read(_path("self", "cgroup")).splitlines():
        head, _, p = line.partition("::")
        if head.strip() == "0" and p:
            path = p
            break
    if not path or not os.path.isdir(CGROUP_ROOT):
        return ""
    return os.path.join(CGROUP_ROOT, path.lstrip("/"))


def cgroup_oom_kills() -> int | None:
    """自 cgroup 以下降で起きた OOM kill 回数 (memory.events の oom_kill)。

    cgroup v2 では world-readable なのでユーザー権限で読める。読めなければ None。
    """
    d = cgroup_dir()
    if not d:
        return None
    fallback: int | None = None
    for line in _read(os.path.join(d, "memory.events")).splitlines():
        cols = line.split()
        if len(cols) != 2:
            continue
        with contextlib.suppress(ValueError):
            val = int(cols[1])
            if cols[0] == "oom_kill":          # 実際に kill された回数 (こっちを優先)
                return val
            if cols[0] == "oom":               #older kernel のフォールバック
                fallback = val
    return fallback


def oomd_running() -> bool:
    """systemd-oomd が動いているか (60 秒キャッシュで /proc スキャンを節約)。"""
    now = time.monotonic()
    if now - _OOMD_PROBE["ts"] < OOMD_PROBE_TTL:
        return bool(_OOMD_PROBE["value"])
    found = False
    try:
        for name in os.listdir(PROC_ROOT):
            if name.isdigit() and _read(_path(name, "comm")).strip() == "systemd-oomd":
                found = True
                break
    except OSError:
        found = False
    _OOMD_PROBE.update(ts=now, value=found)
    return found


def death_reason(exit_code: int | None, oom_delta: int | None,
                 available_gb: float | None = None) -> tuple[str, bool]:
    """終了コード + cgroup の oom_kill 増分から OOM kill だったかを推定する。

    戻り値: (日本語メッセージ, OOM 候補フラグ)。OOM じゃなさそうなら ("", False)。
    """
    avail = "" if available_gb is None else f" (終了時点の空き {available_gb:.1f}GB)"
    sigkill = exit_code is not None and exit_code in (-9, 137, -137)
    if not sigkill:
        return "", False
    if oom_delta and oom_delta > 0:
        return (f"OOM kill を検知しました (cgroup memory.events oom_kill +{oom_delta}){avail}。"
                "メモリ不足でカーネルに kill されました。OOM 対策の設定とモデルのメモリ上限を見直してください",
                True)
    return (f"SIGKILL (9) で終了しました{avail}。OOM Killer による kill の可能性があります。"
            "ホーム / バックエンド設定の OOM 対策カードで保護状態を確認してください", True)


# ------------------------------------------------------------- memory guard


def pressure(cfg: dict[str, Any]) -> dict[str, Any]:
    """今のメモリ逼迫度。warn = 予約値未満 / crit = その半分以下。"""
    reserve = float(cfg["min_available_gb"]) * (1 << 30)
    mi = meminfo()
    avail = mi.get("MemAvailable", 0)
    total = mi.get("MemTotal", 0) or 1
    state = "crit" if avail <= reserve / 2 else ("warn" if avail <= reserve else "ok")
    return {"state": state, "available": avail, "total": total, "reserve": int(reserve),
            "available_gb": round(avail / (1 << 30), 2),
            "used_pct": round((total - avail) / total * 100.0, 1),
            "swap_free": mi.get("SwapFree", 0)}


def guard(cfg: dict[str, Any]) -> dict[str, Any]:
    """起動前メモリガード。例外は投げずに dict で合否を返す (呼び出し側が判断)。

    ブロックするのは MemAvailable が予約値以下になるときだけ — そこまで行くと
    起動した時点でカーネルに殺される確率が高いから。warn / off にすれば止めない。
    """
    p = pressure(cfg)
    mode = cfg["guard_mode"]
    if mode == "off" or cfg["min_available_gb"] <= 0:
        return {"ok": True, "mode": "off", "message": "", **p}
    if p["state"] == "ok":
        return {"ok": True, "mode": mode, "message": "", **p}
    head = (f"空きメモリ {p['available_gb']:.1f}GB しかありません "
            f"(起動に必要な最低予約 {cfg['min_available_gb']:.1f}GB 未満)")
    msg = (head + " — この状態で起動すると OOM kill される可能性が高いです"
           if p["state"] == "crit" else head + " — 起動は可能ですが OOM 危険域に近いです")
    return {"ok": mode != "block", "mode": mode, "message": msg, **p}


# ------------------------------------------------------------------- status


def status(cfg: dict[str, Any], self_result: dict[str, Any] | None = None) -> dict[str, Any]:
    """OOM 対策パネル (API / UI) 用の状態スナップショット。"""
    p = pressure(cfg)
    can, helper = can_lower_score(), helper_available()
    score = int(cfg["score_adj"])
    applied = bool((self_result or {}).get("applied"))
    via = (self_result or {}).get("via") or ""
    err = (self_result or {}).get("error", "")
    if not cfg["enabled"]:
        verdict, level = "OOM 対策は無効です。有効にするとモデルの起動ツリーを保護します", "off"
    elif score == 0:
        verdict = ("oom_score_adj は変更していません (0)。モデルを実際に保護するには負の値 "
                   f"(例: -500) を設定し、次のコマンドで sudo ヘルパーを導入してください:\n{INSTALL_HINT}")
        level = "warn"
    elif applied:
        verdict = (f"保護適用中: oom_score_adj={score} "
                   f"({'sudo ヘルパー経由' if via == 'helper' else '直接書き込み'})")
        level = "ok"
    elif can or helper:
        verdict = f"適用可能です (未適用){': ' + err if err else ''}"
        level = "ok"
    else:
        verdict = ("権限不足 (CAP_SYS_RESOURCE なし) のため oom_score_adj を下げられません。"
                   "この状態ではメモリ最大のモデルインスタンスが OOM killer の最優先犠牲者になります。"
                   f" 導入方法:\n{INSTALL_HINT}" + (f"\nエラー: {err}" if err else ""))
        level = "warn"
    return {
        "enabled": cfg["enabled"],
        "score_adj": score,
        "min_available_gb": cfg["min_available_gb"],
        "guard_mode": cfg["guard_mode"],
        "reapply": cfg["reapply"],
        "swift_stop": cfg["swift_stop"],
        "can_lower": can,
        "helper": helper,
        "self_applied": applied,
        "self_via": via,
        "self_error": err,
        "self_score": read_score(0),
        "self_unit": self_unit(),
        "oomd": oomd_running(),
        "oom_kills": cgroup_oom_kills(),
        "verdict": verdict,
        "level": level,
        "install_hint": INSTALL_HINT,
        **p,
    }

