#!/usr/bin/env python3
"""llm-launcher OOM 保護ヘルパー (root 実行専用・引数を取らない設計)。

起動元 (llm-launcher) が stdin に 2 行で渡す:
    1 行目: ルート PID (モデルサーバーのプロセス)
    2 行目: 設定する oom_score_adj (負値 = 殺されにくい)

その PID から子・孫へプロセスツリーを辿って /proc/<pid>/oom_score_adj に書き込む。
* 書き込むのは /proc/<pid>/oom_score_adj だけ (他のシステム設定には触れない)
* 対象は root PID とその子孫のみ。PID 1 / カーネルスレッド / 自分自身は書かない
  (親方向へ遡ると sshd やユーザーセッションまで書き換えてしまうためやめる。
   launcher 本体は llm_launcher.oom.protect_self が別途自分で守る)
* 処理する PID 数に上限 (暴走防止)
* 権限が足りていない場合は exit 1 + stderr に理由 (親が UI へ表示する)

sudoers 側で「引数なしでこのパスだけ」許可する前提なので、引数は受け取らない。
"""

from __future__ import annotations

import os
import sys

MAX_PIDS = 4000


def child_pids(pid: int) -> list[int]:
    """/proc/<pid>/task/*/children を辿って子 PID 群を返す (無ければ空)。"""
    kids: list[int] = []
    tdir = f"/proc/{pid}/task"
    try:
        tids = os.listdir(tdir)
    except OSError:
        return kids
    for tid in tids:
        try:
            with open(f"{tdir}/{tid}/children", "r", encoding="ascii") as fh:
                kids += [int(x) for x in fh.read().split()]
        except (OSError, ValueError):
            continue
    return kids


def ppid_of(pid: int) -> int | None:
    """Status の PPid を返す。読めなければ None。"""
    try:
        with open(f"/proc/{pid}/status", "r", encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def all_ppids() -> dict[int, list[int]]:
    """CONFIG_PROC_METRICS children が無い環境向けのフォールバック (ppid -> children)。"""
    out: dict[int, list[int]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        ppid = ppid_of(int(name))
        if ppid:
            out.setdefault(ppid, []).append(int(name))
    return out


def tree(root: int) -> list[int]:
    """root 起点のプロセスツリー (mp-spawn される scheduler / worker を拾う)。"""
    out: list[int] = []
    seen: set[int] = set()
    me = os.getpid()

    def add(p: int) -> None:
        if p > 1 and p != me and p not in seen:
            seen.add(p)
            out.append(p)

    add(root)
    queue = [root]
    scan: dict[int, list[int]] | None = None
    while queue and len(out) < MAX_PIDS:
        cur = queue.pop(0)
        kids = child_pids(cur)
        if not kids and scan is None:
            scan = all_ppids()          # children ファイルが無い環境は 1 回だけ全スキャン
        for kid in (kids or scan.get(cur, [])):
            if kid not in seen:
                add(kid)
                queue.append(kid)
    return out


def main() -> int:
    raw = sys.stdin.read().split()
    if len(raw) < 2:
        sys.stderr.write("usage: pid score  (stdin)\n")
        return 2
    try:
        root, score = int(raw[0]), int(raw[1])
    except ValueError:
        sys.stderr.write("pid / score が数値ではありません\n")
        return 2
    if not -1000 <= score <= 1000:
        sys.stderr.write(f"score が範囲外です: {score}\n")
        return 2
    if root <= 1:
        sys.stderr.write(f"対象 PID が不正です: {root}\n")
        return 2
    if os.geteuid() != 0:
        sys.stderr.write("root として実行してください (sudoers 経由を想定)\n")
        return 1

    pids = tree(root)
    if not pids:
        sys.stderr.write(f"PID {root} は存在しないか読み取れません\n")
        return 1
    ok, failed = 0, []
    for pid in pids:
        try:
            with open(f"/proc/{pid}/oom_score_adj", "w", encoding="ascii") as fh:
                fh.write(str(score))
            ok += 1
        except OSError as e:
            failed.append(f"{pid}:{e.__class__.__name__}")
    print(f"applied={ok}/{len(pids)} score={score} pids={','.join(map(str, pids[:16]))}")
    if failed:
        sys.stderr.write("skip: " + ",".join(failed[:10]) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
