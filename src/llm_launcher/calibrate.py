"""ワンクリック・キャリブレーション: Maya / Strata / FreeToken の「この PC 向け調整」を launcher から実行する。

背景:
  Maya (module/maya) と Strata (module/strata) はどちらも「この PC でモデルを最速にする設定」を
  実測で決めるキャリブレーション機能を持つが、どちらも CLI の手順を伴う:

  Maya   `maya.sh --calibrate --no-start --config <maya-<model>.json>`
         → 起動せずに設定を実測調整し、そのまま config へ書き込んで終了する (10-15 分 / DS4 は 25-40 分)。

  Strata `strata.py engine calibrate --profile <名前> --measure --exclusive --out <report.json>`
         → `--review-result <report.json>` → `--apply-result <report.json> --exclusive` の順に実行する。
         measure / apply はどちらもエンジン起動を伴い、GPU を独占していなければならない
         (保存済み profile が必須。launcher のプロファイルから同名の Strata profile を作って使う)。

  FreeToken (module/fork/freetoken) には MTP ゲートの CLI キャリブレータが無く、launcher 自身が
  「候補パラメータごとにエンジン起動 → greedy プローブ → 停止」を繰り返す。対象は env で
  切り替えられるエンジン内部パラメータ (MTP ゲート 8 種 + mtp プロファイルでは chain 2 種)
  だけで、ユーザビリティに関わるもの (tp / コンテキスト長 / メモリ比率など) は対象外。
  勝者は profile の env、または起動 JSON 主体モード (Strata 方式: values.config の JSON が
  argv/env の正 = fork の calibrate_lookup spec と同一形式) ではその JSON の env に
  書き戻すので次回起動から効く (Strata の --apply-result 相当)。座標降下 1 パス・
  約 15-18 セルで 40-60 分。レイテンシ内訳は fork 側の [latency] プローク
  (FREETOKEN_LATENCY) が情報源で、計測中だけ有効にしてレポートに抄録を残す
  (profile には書かない)。--speculative lookup のプロファイルは fork 同梱の
  python -m freetoken.calibrate_lookup を CLI 実行し (起動 JSON モードではその JSON を
  --config にそのまま渡す)、勝者 4 キーを同じ要領で書き戻す。

  launcher 側はこれを「1 ボタン」で実行できるようにする:
    - バックエンドごとのコマンド列をここで組み立てる (UI/API は backend とプロファイルだけ指定する)
    - GPU 独占が必要なので、同じプロファイルのサーバーが稼働中なら先に停止してから実行する
    - 10-40 分の長時間処理なので、subprocess をバックグラウンドで走らせ、ログを逐行取り込み、
      進行状況を live で返す (ベンチマークと同じ fire-and-forget + poll の形)
    - 実行は 1 件ずつ (GPU 独占 / エンジン起動が衝突するため)。キャンセルはプロセスグループごと SIGTERM
    - 結果は data/calibrations.json に履歴として残す

  失敗の見分け方 (どちらも実機で確認した本物の CLI の癖):
    Maya   「エンジンが起動できなかった」場合でも **終了コードは 0** のまま (警告を出して config を
           書き換えない)。終了コードだけで失敗を見ると「成功」と偽ることになるので、調整の証拠
           (config の installer.calibration) を確認して、無ければ理由付きで失敗にする (_verify_maya_tuned)。
    Strata measure は 15-30 分のエンジン起動を伴うので、最初に読み取り専用の `--plan` 事前検査を走らせ、
           コンポーネント欠損 (例: 視覚エンコーダ vision/mmproj) を時間の無駄の前に止める (_plan_failure)。

  CLI の細部 (引数名・--output json の形・終了コード) は module 側に依存するため、
  コマンド組み立てをこのファイルに集め、tests/test_calibrate.py が本物の CLI と照合する。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import signal
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import bench
from .backends import (BACKENDS, backend_for, launch_json_path, load_launch_json,
                       replace_arg)
from .oom import MemoryGuardError, apply_score, meminfo
from .process import pid_alive
from .store import JsonStore, new_id, now

# キャリブレーションを提供するバックエンド (fork も基底と同じ手段を使う。
# FreeToken だけ CLI が無く、launcher が in-process で計測する)
CAL_BACKENDS = {"maya": "maya", "strata": "strata", "strata-fork": "strata",
                "freetoken": "freetoken", "freetoken-fork": "freetoken"}

# ログはデータベース (calibrations.json) に保持するが、長い実行では件数が増えるので
# 直近 LOG_TAIL 行だけ残す (cursor は「残っている行の添字」= UI は len から取り直す)。
LOG_TAIL = 400

# ---------------------------------------------------------------- FreeToken 計測グリッド
# 座標降下で sweep するのは MTP ゲート (エンジン内部パラメータ) だけ。デフォルト値は
# fork 側のコードが正 (python/freetoken/scheduler/scheduler.py / engine/config.py)。
# 候補は env で切り替えられるものに限る — tp / コンテキスト / メモリ等の
# ユーザビリティ項目は対象外 (ユーザーが決める範囲)。
FREETOKEN_MTP_DEFAULTS: dict[str, str] = {
    "FREETOKEN_MTP_MIN_P": "0",
    "FREETOKEN_MTP_MIN_ACCEPTANCE": "0.35",
    "FREETOKEN_MTP_REJECT_STREAK": "3",
    "FREETOKEN_MTP_SKIP_STEPS": "8",
    "FREETOKEN_MTP_RESYNC_LAG": "8",
    "FREETOKEN_MTP_COLD_START": "0",
    "FREETOKEN_MTP_ACCEPT_WINDOW": "64",
    # 未設定時の実効値は ACCEPT_WINDOW 追従 (scheduler.py)。 winner 正規化で実効値を明示する
    "FREETOKEN_MTP_RESUME_AFTER": "64",
    "FREETOKEN_MTP_LOOKUP_CHAIN": "0",
    "FREETOKEN_MTP_CHAIN_K": "2",
}
FREETOKEN_MTP_GRID: list[tuple[str, list[str]]] = [
    ("FREETOKEN_MTP_MIN_P", ["0.3", "0.5"]),            # K=1 の推定妥当性しきい値 p*≈0.3-0.5
    ("FREETOKEN_MTP_MIN_ACCEPTANCE", ["0.5", "0.6"]),   # 発動判定をもっと厳しく (EV の勝ち窓)
    ("FREETOKEN_MTP_REJECT_STREAK", ["2", "5"]),        # 連続失敗で休む閾値 (既定 3) を両方向
    ("FREETOKEN_MTP_SKIP_STEPS", ["4", "16"]),          # 休止後の再試行間隔 (既定 8) を両方向
    ("FREETOKEN_MTP_RESYNC_LAG", ["16"]),
    ("FREETOKEN_MTP_COLD_START", ["1"]),                # ゲート発動を最初から許可
    ("FREETOKEN_MTP_ACCEPT_WINDOW", ["24", "128"]),     # 発動判定窓 (既定 64) を両方向
    ("FREETOKEN_MTP_RESUME_AFTER", ["32", "128"]),      # 窓失敗後の停止ステップ数 (既定=窓長)
]
# chain (--speculative mtp 専用): MTP ドラフトに lookup の続きを連鎖させる v1 移植のゲート。
# 効果はコピ型テキストで出るため、chain を測る run は copy プローブを 1 本追加する
FREETOKEN_MTP_CHAIN_GRID: list[tuple[str, list[str]]] = [
    ("FREETOKEN_MTP_LOOKUP_CHAIN", ["1"]),              # 連鎖を有効化 (既定 0 = off)
    ("FREETOKEN_MTP_CHAIN_K", ["1", "3"]),              # 連鎖長 (既定 2) を両方向
]
FT_IMPROVE = 0.02             # 現ベストより 2% 超速いときだけ採用 (実行間ノイズ未満は無視)
FT_AUTO_PAR_LOSS = 0.02        # auto: 単流最優でも並行上限でこの率を超えて負けたら並行最優に差し替え
# auto フェーズ D: 勝者構成に対するマイクロ調整 (品質中立の A/B ノブのみ —
# 品質トレードオフ系 (FP8_DECODE_LINEAR / moe_skip_minw 等) と安全マージン系
# (VRAM_START_MARGIN 等) は意図的に対象外)。**この機材で no-op のノブも含める**:
# 様々な PC で 1 回だけ校正する運用なので、hybrid / CPU 実行系など他の構成で効く
# ものも全部測る (no-op セルは差が出ず不採用になるだけ)。
FT_MICRO_GRID: list[tuple[str, list[str]]] = [
    # 転送 / all-reduce 系 (既定: SHM_AR 160, DUAL_STREAM 0, FETCH_OVERLAP_MIN_ROWS 2,
    # DUAL/OVERLAP_GATHER_BPB 2, H2D_BLOCKS auto=8, TP_BANK_STAGGER 1)
    ("FREETOKEN_SHM_AR_MAX_KIB", ["320"]),
    ("FREETOKEN_DUAL_STREAM_DECODE", ["1"]),
    ("FREETOKEN_FETCH_OVERLAP_MIN_ROWS", ["1"]),
    ("FREETOKEN_DUAL_GATHER_BPB", ["1", "4"]),
    ("FREETOKEN_OVERLAP_GATHER_BPB", ["4"]),
    ("FREETOKEN_H2D_BLOCKS_PER_BANK", ["4", "16"]),
    ("FREETOKEN_TP_BANK_STAGGER", ["0"]),
    # prefill ペーシング / ステージング系 (既定: DEBT_S 2.0, CHUNK_TARGET 0=off,
    # STAGING_ROWS 64, AUTO_PROMOTE 0)。並行プローブは時間差到着なので混在負荷で測れる
    ("FREETOKEN_PREFILL_DEBT_S", ["1.0", "4.0"]),
    ("FREETOKEN_ADAPTIVE_CHUNK_TARGET_S", ["0.05"]),
    ("FREETOKEN_PREFILL_STAGING_ROWS", ["32", "128"]),
    ("FREETOKEN_PREFILL_AUTO_PROMOTE", ["1"]),
    # CPU 実行系 (locked/disk 層が発動する低 RAM 機で有効。既定: SPIN=50ms park,
    # LOCKED_FETCH_THREADS=max(2,min(4,cpus/4)))
    ("FREETOKEN_CPU_MOE_SPIN", ["0"]),
    ("FREETOKEN_LOCKED_FETCH_THREADS", ["1", "8"]),
    # hybrid バックエンド系 (auto 選択される機材で有効。既定: MIN_BS 2 (tp>1),
    # OVERLAP 1, FETCH recency, PREFILL_FETCH_FRACTION は policy 解決)
    ("FREETOKEN_HYBRID_MIN_BS", ["1"]),
    ("FREETOKEN_HYBRID_OVERLAP", ["0"]),
    ("FREETOKEN_HYBRID_FETCH", ["lowest_id"]),
    ("FREETOKEN_PREFILL_FETCH_FRACTION", ["0.5"]),
    # モデル固有のドラフトヘッド配線 (qwen4_exp 以外は no-op。drafter 側の変更なので
    # greedy 出力は verify が保証し、変わるのは acceptance だけ)
    ("FREETOKEN_MTP_WIRING", ["norm_mix_fc"]),
]
# 勝者モードで論理的に絶対に効かないノブだけスキップする (セル 1 本 = 起動 1 回の無駄防止)。
# 「この機材では不要」を理由にしたスキップはしない (他 PC での 1 回校正が前提のため)
FT_MICRO_MODES: dict[str, set[str]] = {
    "FREETOKEN_MTP_WIRING": {"mtp"},
}
FT_MICRO_IMPROVE = 0.02       # 単流 +2% 超だけ採用 (sweep と同じノイズ帯)
FT_MICRO_PAR_REGRESS = 0.02   # 採用で並行スコアを 2% 超悪化させない (差し替え規則のミラー)
FT_PAR_STAGGER_S = 0.75       # 並行プローブのレーン時間差 (prefill/decode 混在を作る)
FT_PROBE_WORDS = (64, 512)    # 短文 (デコード主軸) + 長文 (prefill 混在)。合算 tok/s をスコアに
FT_PROBE_COPY_WORDS = 256     # chain/lookup 発動用の逐語コピプローブ (mtp run だけ追加)
FT_PROBE_MAX_TOKENS = 128     # 全セル同一の greedy 出力長 (MTP ゲートは greedy 専用 = temp 0 必須)
FT_PROBE_SEED = 0x50EC7       # プロンプトは全セル同一シードで同条件比較にする
FT_PROBE_TIMEOUT = 300.0
FT_READY_TIMEOUT = 900.0      # エンジン起動 (モデルロード) 上限。FT_CAL_READY_TIMEOUT_S で変更可
FT_WARM_RETRY_S = 180.0       # warmup 中の一時 503 (loading/rebuild) を待つ猶予
# 計測中だけ fork 側の [latency] プロークを有効化 (レイテンシ内訳の抄録用。profile には書かない)
FT_LATENCY_ENV = {"FREETOKEN_LATENCY": "1", "FREETOKEN_LATENCY_EVERY_S": "5"}
# 起動ログの disk tier / locked 層 発動行 (banks はランク実体のサイズ)
FT_FIT_PAT = re.compile(r"banks ([0-9.]+) GiB (?:exceed the|> pin budget) ([0-9.]+) GiB")


def ft_short(param: str) -> str:
    return param.removeprefix("FREETOKEN_MTP_").lower()


def ft_candidates(param: str, env: dict[str, str]) -> list[str]:
    """現在の有効値 (設定値 > コード既定) に対して試す候補値。同じ値はスキップ。"""
    cur = str(env.get(param) or "").strip() or FREETOKEN_MTP_DEFAULTS.get(param, "")
    if (param == "FREETOKEN_MTP_CHAIN_K"
            and str(env.get("FREETOKEN_MTP_LOOKUP_CHAIN") or "0").strip() != "1"):
        return []   # chain が無効なうちは連鎖長を測っても全セル同一結果になる
    all_grid = dict(FREETOKEN_MTP_GRID + FREETOKEN_MTP_CHAIN_GRID)
    return [v for v in all_grid.get(param, []) if v != cur]


def ft_grid(speculative: str) -> list[tuple[str, list[str]]]:
    """プロファイルの speculative モードに対する実測グリッド。

    chain の 2 キーは --speculative mtp のリクエストでしか発動しない
    (FREETOKEN_MTP_LOOKUP_CHAIN は MTP verify 専用) ので、mtp 以外では測らない。"""
    if speculative == "mtp":
        return FREETOKEN_MTP_GRID + FREETOKEN_MTP_CHAIN_GRID
    return list(FREETOKEN_MTP_GRID)


def ft_health_status(url: str, auth: dict[str, str] | None = None) -> tuple[bool, dict | None]:
    """/health を GET して (http_ok, JSON or None)。

    freetoken は uvicorn が先に縛るため loading 中も HTTP 200 を返す設計。準備完了の
    真実は body の status ("loading" / "ok" / "error") なので status_check は
    bench._health_ok (200 だけ見る) では足りない。"""
    try:
        req = urllib.request.Request(url, headers=auth or {})
        with urllib.request.urlopen(req, timeout=4.0) as r:
            try:
                doc = json.loads(r.read(65536))
            except ValueError:
                doc = None
            return 200 <= r.status < 400, doc if isinstance(doc, dict) else None
    except OSError:
        return False, None


class CalibrateError(Exception):
    """ユーザーに提示するキャリブレーション開始 / 実行失敗。"""


class _Cancelled(Exception):
    """キャンセル検知 (内部シグナル)。"""


# ----------------------------------------------------------------- 名前 / パス


def strata_profile_name(profile: dict[str, Any]) -> str:
    """launcher のプロファイル → Strata の保存 profile 名 (小文字 a-z0-9_- のみ)。

    Strata の profile 名は小文字始まりの a-z0-9_- のみ。launcher の id (p_xxxxxxxx) は
    そのままだと使えないため正規化する。同じ id は常に同じ名前 → 何度実行しても同じ profile を再利用可。"""
    raw = str(profile.get("id") or "")
    safe = re.sub(r"[^a-z0-9_-]", "-", raw.lower())
    safe = re.sub(r"-+", "-", safe).strip("-_")
    if not safe or not safe[0].islower():
        safe = "llm-" + (safe or "profile")
    return safe[:60]


def _resolve_config(root: Path, module_dir: Path, profile: dict[str, Any]) -> str:
    """プロファイルの values.config を実在する絶対パスに解決する。

    相対パスの基底: バックエンドは module ディレクトリを cwd に起動するので
    (process.start: profile.cwd 空 = root だが Maya/Strata は module 側の相対を読む)、
    root と module ディレクトリの両方を試す (プロファイル実例: "maya-ds4.json" =
    module/maya/maya-ds4.json)。"""
    raw = str((profile.get("values") or {}).get("config") or "").strip()
    if not raw:
        return ""
    p = Path(raw)
    cands = [p] if p.is_absolute() else [root / p, module_dir / p]
    for c in cands:
        if c.is_file():
            return str(c.resolve())
    return ""


def maya_config_path(root: Path, profile: dict[str, Any]) -> str:
    """Maya のキャリブレーション対象 config。

    プロファイルに --config がある場合はそれが実在しないと "" (開始時に明確に失敗する —
    別の config を黙って調整するとユーザーの意図したモデル設定が変わってしまうため)。
    --config 未指定 (送信 OFF) なら module/maya の最新 maya-*.json を対象にする。"""
    raw = str((profile.get("values") or {}).get("config") or "").strip()
    if raw:
        return _resolve_config(root, root / "module" / "maya", profile)
    cands = sorted((root / "module" / "maya").glob("maya-*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return str(cands[0]) if cands else ""


def strata_config_path(root: Path, profile: dict[str, Any]) -> str:
    """Strata のキャリブレーション対象 config (プロファイルの --config 必須・実在必須)。"""
    return _resolve_config(root, root / "module" / "strata", profile)


# ----------------------------------------------------------------- コマンド


def build_maya_command(root: Path, profile: dict[str, Any], python: str) -> list[str]:
    """Maya: `sh maya.sh --calibrate --no-start --config <cfg>`。

    --no-start: 調整だけしてダッシュボードを起動しない (launcher が管理するサーバーと衝突しない)。
    --config:   対象 config を明示する (対話なし, 最近使った config の選択を避ける)。
    maya.sh は module/maya/.venv を用意して maya.py を exec する (GUI .venv を pip install で汚さない)。
    python 引数は使わない (maya.sh 自身が python を探す) が、シグネチャは Strata と揃えておく。"""
    cfg = maya_config_path(root, profile)
    argv = ["sh", str(root / "module" / "maya" / "maya.sh"), "--calibrate", "--no-start"]
    if cfg:
        argv += ["--config", cfg]
    return argv


def build_strata_commands(root: Path, profile: dict[str, Any], python: str,
                          report: str, profile_name: str) -> dict[str, list[str]]:
    """Strata: profile create → measure → review → apply。

    profile create: launcher プロファイルの config から Strata の保存 profile を作る
                    (同じ id なら同じ名前なので、既に保存済みなら create は idempotent に失敗せず再利用する
                     … 実は既存なら "profile ... already saved" で失敗するので、実行前に存在確認を行う)。
    --exclusive:    measure と apply はエンジンを起動して GPU を独占する宣言。launcher が同じプロファイルの
                    サーバーを止めた状態でだけ実行するので付けてよい (ユーザーの明示操作 1 クリック = この宣言)。
    --output json:  機械可読 ({"schema_version":1,"ok":...}) で結果を受け取る。"""
    cfg = strata_config_path(root, profile)
    script = str(root / "module" / "strata" / "strata.py")
    return {
        "create": [python, script, "profile", "create", profile_name,
                   "--from-config", cfg, "--output", "json"],
        # --plan: 読み取り専用の事前検査。エンジンを起動せずに、コンポーネントの欠損
        #         (例: 視覚エンコーダ mmproj が無い) を 15-30 分の測定の前に見つける。
        "plan": [python, script, "engine", "calibrate", "--profile", profile_name,
                 "--plan", "--output", "json"],
        "measure": [python, script, "engine", "calibrate", "--profile", profile_name,
                    "--measure", "--exclusive", "--out", report, "--output", "json"],
        "review": [python, script, "engine", "calibrate", "--profile", profile_name,
                   "--review-result", report, "--output", "json"],
        "apply": [python, script, "engine", "calibrate", "--profile", profile_name,
                  "--apply-result", report, "--exclusive", "--output", "json"],
    }


# ----------------------------------------------------------------- メモリガード


# Maya の DS4 系キャリブレーションは最後に `--mmap-experts off` を試す。これはエンジンの
# 専門家 (expert) の重みのうち VRAM に載らない分を **RAM へまるごとコピーする**。
#
# 実測 (2026-10-24 03:22 / 2026-10-25 06:58, 121 GB のマシン):
#   `11724 routed experts on the CPU lane (81.0 GB, in place in the model's files)` の 81 GB を
#   そのまま RAM にコピーし、エンジン自身の重み (~9 GB) と launcher の常駐分と合わせて
#   117.2 GB に達した。このとき MemAvailable は 118.8 GB あった (ギリギリ足りる計算だった) が、
#   ユーザースライスのメモリ圧迫が 65.8% > 50% を 20 秒超えたため **systemd-oomd** が
#   launcher の cgroup を kill した (`Failed with result 'oom-kill'`)。
#
# 「空きが足りている」と実際に使えるのは同じではない (page cache / GPU のピン留め分 / 断片化)。
# 余裕を厚めに取る: コピー 81 GB + エンジン 9 GB + 余裕 20 GB = 110 GB。
DS4_MMAP_OFF_GB = 81.0     # 実測 75.5〜81.0 GB (CPU lane の専門家の総量)
DS4_ENGINE_RESIDENT_GB = 9.0   # エンジン自身の重み (CUDA0 4.48 + CUDA1 4.73 GB)
DS4_MMAP_MARGIN_GB = 20.0      # page cache / GPU ピン留め / カーネル断片化の余裕


def _is_ds4(config_path: str) -> bool:
    """Maya の DS4 (DeepSeek-V4.1-Flash-1017) 系の config かを、モデル名で判定する。"""
    try:
        cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    blob = json.dumps(cfg, ensure_ascii=False).lower()
    return "ds4" in blob or "deepseek-v4" in blob


def memory_guard(config_path: str) -> dict[str, Any]:
    """キャリブレーション開始前のメモリガード。

    DS4 系 (`--mmap-experts off`) は CPU lane の専門家 81 GB を RAM へコピーし、エンジン自身の
    重み 9 GB と合わせて 90 GB を使う。page cache / GPU ピン留め / 断片化の余裕 20 GB を見て
    **110 GB** を要求する (実測では MemAvailable 118.8 GB でもピークは 117.2 GB になり、
    systemd-oomd に launcher を kill された)。

    swap は「RAM の空き」ではなく「ページの退避先」なので MemAvailable には含まれない。
    しかし 81 GB のコピーは常駐しない (エンジンは専門家を逐次使う) ので、十分な swap があれば
    ページ退避で成立する。そこで **MemAvailable + SwapFree** で判定し、RAM だけで足りない場合は
    「swap に退避して遅くなる」と理由に明記する (黙って 40 分の遅い実行にしない)。
    他の (Maya / Strata) は従来の 8 GB 予約で足る。
    例外は投げず dict で返す (呼び出し側が CalibrateError にする)。"""
    mi = meminfo()
    avail_gb = mi.get("MemAvailable", 0) / (1 << 30)
    swapfree_gb = mi.get("SwapFree", 0) / (1 << 30)
    total_gb = avail_gb + swapfree_gb
    if _is_ds4(config_path):
        need_gb = DS4_MMAP_OFF_GB + DS4_ENGINE_RESIDENT_GB + DS4_MMAP_MARGIN_GB
        why = (f"この調整は最後に --mmap-experts off を試し、VRAM に載らない専門家 "
               f"(実測 {DS4_MMAP_OFF_GB:.0f} GB) をエンジンの重み ({DS4_ENGINE_RESIDENT_GB:.0f} GB) と"
               f"一緒に RAM へコピーします")
    else:
        need_gb = 8.0
        why = "この調整はエンジンを起動します"
    ok = total_gb >= need_gb
    note = "" if avail_gb >= need_gb else " (RAM だけでは足りず、swap へ退避するため遅くなります)"
    return {"ok": ok, "available_gb": round(avail_gb, 1), "swap_free_gb": round(swapfree_gb, 1),
            "need_gb": need_gb, "note": note,
            "reason": "" if ok else
            (f"空きメモリ {avail_gb:.1f} GB + swap {swapfree_gb:.1f} GB しかありません ({why}。"
             f"ページキャッシュ等の余裕を含め必要な空き: {need_gb:.0f} GB)。"
             "他のサーバー / Maya を止めるか、swap を増やしてから実行してください")}




# ----------------------------------------------------------------- マネージャ


class CalibrationManager:
    """1 件ずつのキャリブレーション実行 + 履歴。"""

    def __init__(self, root: Path, profiles_store: JsonStore, store: JsonStore,
                 manager: Any):
        self.root = root
        self.profiles = profiles_store
        self.store = store
        self.manager = manager            # ProcessManager (サーバー停止に使う)
        self.jobs: dict[str, dict[str, Any]] = {j["id"]: j for j in store.load()}
        self.active_id: str | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._cancel: set[str] = set()
        self._extra: set[asyncio.Task] = set()    # キャンセル猶予後の SIGKILL など短命 task

    # ------------------------------------------------------------ 再起動時の回復

    def on_startup(self) -> list[str]:
        """launcher の再起動 (OOM kill / 手動 restart) で実行中だったジョブの実体は失われている。

        asyncio の task も子プロセスも前のプロセスと一緒に死ぬので、status を "running" のまま
        残すと UI は「実行中」を永遠に表示し、active_id も再設定されず、ユーザーは完了したのか
        失敗したのか判断できなくなる (実測: systemd-oomd が launcher を kill すると Maya の
        `--calibrate` 子プロセスも巻き添えで死に、ジョブは 23:20 開始のまま "running" で残った)。
        bench.BenchManager.on_startup() と同じ形で "lost" + 理由 + finished_at を書き、ログは
        取り込まれた分を残す (ログ末尾は履歴で読める)。復旧したジョブ id の一覧を返す。"""
        changed = False
        lost: list[str] = []
        for job in self.jobs.values():
            if job.get("status") in ("queued", "running"):
                job["status"] = "lost"
                job["error"] = ("llm-launcher の再起動 (OOM kill / restart) で実行が中断されました — "
                                "調整結果は保存されていません。ログ末尾に中断直前までの出力が残っています")
                job["finished_at"] = job.get("finished_at") or now()
                self._append_log(job, f"中断: {job['error']}")
                changed = True
                lost.append(job["id"])
        # active_id は再起動後に受け継がない (前のプロセスの task が動かしていた実行だけ)。
        # ただし回復対象が無い場合は None のまま (正常)。
        self.active_id = None
        if changed:
            self._save()
        return lost

    # ------------------------------------------------------------ 公開状態

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in job.items() if not k.startswith("_")}

    def list_jobs(self) -> list[dict[str, Any]]:
        return [self._public(j) for j in sorted(self.jobs.values(),
                                                key=lambda x: x.get("started_at") or 0)]

    def get(self, jid: str) -> dict[str, Any] | None:
        job = self.jobs.get(jid)
        return self._public(job) if job else None

    def live_log(self, jid: str, cursor: int = 0) -> dict[str, Any]:
        """ジョブのログを cursor 以降で返す (cursor = 前回の start + len(lines))。"""
        job = self.jobs.get(jid)
        if not job:
            raise KeyError(jid)
        logs = job.get("logs") or []
        cursor = max(0, min(int(cursor or 0), len(logs)))
        return {"lines": logs[cursor:], "start": cursor, "total": len(logs)}

    def supports(self, backend_id: str) -> bool:
        return backend_id in CAL_BACKENDS

    # ------------------------------------------------------------ 実行

    def _save(self) -> None:
        self.store._cache = [self.jobs[j] for j in sorted(self.jobs, key=lambda x: self.jobs[x]["started_at"])]
        self.store.save()

    def _append_log(self, job: dict[str, Any], line: str) -> None:
        strip = line.rstrip("\r\n")
        if not strip:
            return
        logs = job.setdefault("logs", [])
        logs.append(strip)
        if len(logs) > LOG_TAIL:
            del logs[:len(logs) - LOG_TAIL]
        self._save()

    async def start(self, profile: dict[str, Any], backend_id: str,
                    *, restart_after: bool = False,
                    actor: dict[str, Any] | None = None,
                    cal_mode: str = "current") -> dict[str, Any]:
        """キャリブレーションを 1 件開始する (キューせず、同時に 1 件だけ)。

        cal_mode="auto" (FreeToken のみ): 投機モード自体 (none/mtp+chain/lookup) を
        共通プローブで決勝トーナメントして勝者を起動構成に書き戻す。"""
        if self.active_id:
            raise CalibrateError("別のキャリブレーションが実行中です。完了を待つか中断してください")
        kind = CAL_BACKENDS.get(backend_id)
        if not kind:
            raise CalibrateError(f"{BACKENDS.get(backend_id, {}).get('name', backend_id)} は"
                                 "キャリブレーションに対応していません")
        backend = backend_for(self.root, backend_id)
        module_dir = self.root / backend["module_path"]
        if not module_dir.is_dir():
            raise CalibrateError(f"{backend['module_path']} がありません (submodule を init してください)")

        python = self.manager.resolve_python(backend_id)
        report_dir = self.root / "data" / "calibrations"
        report_dir.mkdir(parents=True, exist_ok=True)
        job_id = new_id("cal")
        job_mode = ""

        if kind == "maya":
            cfg = maya_config_path(self.root, profile)
            if not cfg:
                raise CalibrateError("Maya の config (maya-<model>.json) が見つかりません。"
                                     "プロファイルの --config に指定するか、先にセットアップしてください")
            steps: list[dict[str, Any]] = [
                # verify: Maya は失敗しても exit 0 なので、調整が実際に config へ書かれたかを後で確認する
                {"name": "calibrate", "argv": build_maya_command(self.root, profile, python),
                 "cwd": str(module_dir), "verify": cfg}]
        elif kind == "freetoken":
            cfg = ""
            try:
                spec_mode = self._ft_speculative(profile)
            except ValueError as e:
                raise CalibrateError(str(e))
            if cal_mode == "auto":
                # 投機モード自体の自動選定 (mtp sweep → lookup CLI → 共通プローブ決勝)。
                # 現モードが lookup でも mtp でも、まず auto が最上位の分岐
                job_mode = "auto"
                steps = [{"name": "auto"}]
            elif spec_mode == "lookup":
                # --speculative lookup: fork 同梱の Strata 式 calibrator をそのまま使う
                # (env 家族 FREETOKEN_LOOKUP_*、コピ型プロンプト + 対照、並行上限判定は
                # ツール側の実装)。起動 JSON モードではその JSON 自体 (calibrate_lookup
                # spec と同一形式) を --config に渡す = 校正対象と起動構成が同一ファイル。
                job_mode = "cli"
                steps = [self._ft_lookup_step(job_id, profile, module_dir, report_dir, python)]
            else:
                # CLI の無い MTP 系: launcher が in-process でエンジン起動→greedy 計測→停止
                # (_run_freetoken)。config ファイルは使わないのでメモリガードは汎用の 8 GB。
                job_mode = "sweep"
                steps = [{"name": "sweep"}]
        else:
            cfg = strata_config_path(self.root, profile)
            if not cfg:
                raise CalibrateError("Strata の config (strata-<model>.json) をプロファイルの --config に"
                                     "指定してください (ファイルが存在しません)")
            name = strata_profile_name(profile)
            try:
                existing = await self._strata_profiles(python, module_dir)
            except CalibrateError:
                existing = []
            report = str((report_dir / f"calibration-{job_id}.json").resolve())
            cmds = build_strata_commands(self.root, profile, python, report, name)
            # 既に保存済みで同じ config なら create は不要 (Strata は同名の保存に失敗する)
            have = next((p for p in existing if p.get("id") == name), None)
            if have is None:
                steps = [{"name": "create", "argv": cmds["create"], "cwd": str(module_dir)}]
            elif str(Path(have.get("source_config") or cfg).resolve()) != str(Path(cfg).resolve()):
                raise CalibrateError(
                    f"Strata の保存 profile {name} が別の config を指しています "
                    f"({have.get('source_config')}) — このプロファイル用の config を揃えるか、"
                    f"Strata 側で profile use-config {name} を実行してください")
            else:
                steps = []
            steps += [{"name": k, "argv": cmds[k], "cwd": str(module_dir)}
                      for k in ("plan", "measure", "review", "apply")]

        # GPU 独占: 同じプロファイルで稼働中のサーバーは全部止めてから実行する。
        # (キャリブレーションはエンジンを起動して GPU を独占するので、同居は絶対に不可)
        mine = [s for s in self.manager.sessions.values() if s["profile_id"] == profile["id"]]
        live = [s for s in mine if s["status"] in ("running", "starting")]
        for s in live:
            await self.manager.stop(s["id"])
        # stop() は「停止中 (= エンジンが arena 解放中)」を待って返す。それを過ぎても
        # 止まらない場合だけ実行しない (OS 過コミット凍結防止のため、launcher は待つ側に倒す)
        if any(s["status"] == "stopping" for s in mine):
            raise CalibrateError("サーバーの停止が完了していません (エンジン解放中)。数秒後に再試行してください")

        # メモリガード: DS4 の `--mmap-experts off` は 80 GB を要求するので、空きが無ければ
        # 40 分走らせてから OOM kill される前に、理由付きで開始を拒否する。
        # (サーバー停止後に行う — 停止して解放された RAM を数えるため)
        guard = memory_guard(cfg)
        if not guard["ok"]:
            raise CalibrateError(guard["reason"])

        job: dict[str, Any] = {
            "id": job_id,
            "profile_id": profile["id"],
            "profile_name": profile.get("name", ""),
            "backend": backend_id,
            "backend_name": backend["name"],
            "kind": kind,
            "mode": job_mode,
            "status": "queued",
            "step": "",
            "steps": [s["name"] for s in steps],
            "report": None,
            "error": "",
            "started_at": now(),
            "finished_at": None,
            "started_by": (actor or {}).get("id", "") or "",
            "logs": [],
            "restart_after": restart_after,
        }
        self.jobs[job_id] = job
        self.active_id = job_id
        self._save()
        self._tasks[job_id] = asyncio.create_task(self._run(job, steps, profile))
        return self._public(job)

    # ---------------------------------------------------------- FreeToken (in-process)

    async def _run_freetoken(self, job: dict[str, Any], profile: dict[str, Any], *,
                             apply: bool = True,
                             spec_override: str | None = None) -> dict[str, Any]:
        """MTP ゲート 8 パラメータを座標降下 1 パスで sweep する (約 15 セル・40-55 分)。

        各セル = env を差し替えてエンジンを起動 → greedy プローブ (短文+長文) →
        合算 tok/s をスコアに、+2% 超の改善だけ採用 → Winner (8 キー全部を明示値) を
        プロファイルの env へ書き戻す。Strata の --apply-result と同じく「次起動から効く」。
        GPU 独占は start() がこのプロファイルのサーバーを止めてくれている。

        apply=False は書き戻しを止める (auto モードのフェーズ A: 勝者はトーナメントで
        決着してから 1 回だけ適用する)。spec_override はセルの --speculative を強制する
        (auto が lookup プロファイルからも mtp 最良構成を作るため)。"""
        launch = None
        with contextlib.suppress(ValueError):
            launch = load_launch_json(self.root, profile)   # 適用先ログの分岐にだけ使う
        base_env = self._ft_base_env(profile)
        spec_mode = spec_override or self._ft_speculative(profile)
        grid = ft_grid(spec_mode)
        copy_probe = spec_mode == "mtp"   # chain はコピ型テキストでしか発動しない
        winner = {param: (str(base_env.get(param) or "").strip() or FREETOKEN_MTP_DEFAULTS[param])
                  for param, _vals in grid}
        if not str(base_env.get("FREETOKEN_MTP_RESUME_AFTER") or "").strip():
            # 未設定時の実効値は ACCEPT_WINDOW 追従なので、実効値で正規化する
            winner["FREETOKEN_MTP_RESUME_AFTER"] = winner["FREETOKEN_MTP_ACCEPT_WINDOW"]
        cells: list[dict[str, Any]] = []

        async def cell(label: str, overrides: dict[str, str]) -> dict[str, Any]:
            if job["id"] in self._cancel:
                raise _Cancelled()
            job["step"] = label
            out = await self._ft_run_cell(job, profile, base_env, label, overrides,
                                          copy=copy_probe, spec=spec_override)
            out["label"] = label
            cells.append(out)
            return out

        base = await cell("baseline", {})
        if not base.get("ok"):
            raise CalibrateError(f"基準 (現在の設定) のエンジンが起動 / 計測できませんでしたが: "
                                 f"{base.get('error', '')}")
        best_score = float(base["score"])
        best_latency = base.get("latency", "")
        self._append_log(job, f"[cell] baseline: {best_score:.2f} tok/s")
        self._ft_machine_fit(job, profile, self._ft_read_log(base.get("log", "")))

        for param, _vals in grid:
            for val in ft_candidates(param, winner):
                trial = {**winner, param: val}
                r = await cell(f"{ft_short(param)}={val}", trial)
                if not r.get("ok"):
                    self._append_log(job, f"[cell] {r['label']}: 失敗 ({r.get('error', '')}) — 継続")
                    continue
                score = float(r["score"])
                delta = (score / best_score - 1) * 100
                if score > best_score * (1 + FT_IMPROVE):
                    winner, best_score = trial, score
                    best_latency = r.get("latency", best_latency)
                    self._append_log(job, f"[cell] {r['label']}: {score:.2f} tok/s "
                                          f"({delta:+.1f}%) ← 採用")
                else:
                    self._append_log(job, f"[cell] {r['label']}: {score:.2f} tok/s ({delta:+.1f}%)")

        if apply:
            self._ft_write_env(profile, winner)
            self._append_log(job, f"{'起動 JSON' if launch is not None else 'profile env'} に適用: "
                             + ", ".join(f"{ft_short(k)}={v}" for k, v in winner.items()))

        report: dict[str, Any] = {
            "schema_version": 1, "backend": "freetoken",
            "baseline_score": base["score"], "best_score": round(best_score, 2),
            "improvement_pct": round((best_score / float(base["score"]) - 1) * 100, 1),
            "winner": winner, "cells": cells, "latency": best_latency,
        }
        with contextlib.suppress(OSError):
            (self.root / "data" / "calibrations"
             / f"calibration-{job['id']}.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        return report

    def _ft_speculative(self, profile: dict[str, Any]) -> str:
        """プロファイルの現在の --speculative モード (起動 JSON が argv の正)。"""
        launch = load_launch_json(self.root, profile)
        if launch is not None:
            argv = [str(a) for a in launch.get("argv") or []]
            for i, tok in enumerate(argv):
                if tok == "--speculative" and i + 1 < len(argv):
                    return argv[i + 1].strip().lower()
            return ""
        return str((profile.get("values") or {}).get("speculative") or "").strip().lower()

    def _ft_launch_update(self, profile: dict[str, Any], fn) -> bool:
        """起動 JSON を read-modify-write (原子的差し替え)。config 主体モードでなければ False。"""
        path = launch_json_path(self.root, profile)
        if path is None or not path.is_file():
            return False
        try:
            doc = json.loads(path.read_text("utf-8"))
        except ValueError as e:
            raise CalibrateError(f"起動 JSON を解析できません ({path.name}): {e}")
        fn(doc)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", "utf-8")
        tmp.replace(path)
        return True

    def _ft_write_env(self, profile: dict[str, Any], updates: dict[str, str]) -> None:
        """勝者の env を書き戻す。起動 JSON 主体モードでは JSON の env へ (校正の正)、
        レガシーではプロファイル store へ (env と _resolved_env 両方: process 側は
        _resolved_env を優先するため)。"""
        done = self._ft_launch_update(
            profile,
            lambda doc: doc.setdefault("env", {}).update(
                {str(k): str(v) for k, v in updates.items()}))
        if done:
            return

        def patch(data: list[Any]) -> bool:
            for p in data:
                if isinstance(p, dict) and p.get("id") == profile["id"]:
                    p.setdefault("env", {}).update(updates)
                    if isinstance(p.get("_resolved_env"), dict):
                        p["_resolved_env"].update(updates)
                    return True
            return False

        if not self.profiles.mutate(patch):
            raise CalibrateError("計測中にプロファイルが消えたため、結果を保存できませんでした")

    def _ft_read_log(self, rel: str, limit: int = 400_000) -> str:
        """root 相対ログ路径の末尾を読む (machine-fit は起動行だけ必要)。"""
        if not rel:
            return ""
        with contextlib.suppress(OSError):
            with (self.root / rel).open("rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - limit))
                return fh.read().decode("utf-8", "replace")
        return ""

    def _ft_machine_fit(self, job: dict[str, Any], profile: dict[str, Any], log_text: str) -> None:
        """機械適合: 実測 sweep の対象外 (単調でピークが無い) の計算設定。

        起動ログに disk tier / locked 層の発動行があった時だけ --moe-ram-budget を
        MemTotal-40 GiB (負荷時の working set 分 40) で固定し、RAM 多寡で起動挙動が
        変わるブレを無くす。余白が足りなければ書かない (auto のまま安全)。"""
        m = FT_FIT_PAT.search(log_text or "")
        if not m:
            self._append_log(job, "machine-fit: disk 層は発動していません (pin に余白あり) — やらない")
            return
        banks = float(m.group(1))
        total_gib = meminfo().get("MemTotal", 0) / 2**30
        budget = int(total_gib) - 40
        # ログの banks は発信ランクの shard 実体。明示 cap はランク数で割られるので、
        # 全ランクが自分の shard + 作業代を pin できるかを rank 数で検査する
        launch = None
        try:
            launch = load_launch_json(self.root, profile)
        except ValueError as e:
            self._append_log(job, f"machine-fit: 起動 JSON を読めません ({e}) — profile 側に記録します")
        if launch is not None:
            argv = [str(a) for a in launch.get("argv") or []]
            tp_v = next((argv[i + 1] for i, t in enumerate(argv)
                         if t == "--tp-size" and i + 1 < len(argv)), "")
        else:
            tp_v = str((profile.get("values") or {}).get("tp_size") or "")
        try:
            size = max(1, int(tp_v or 1))
        except ValueError:
            size = 1
        if budget < size * banks + 8:
            self._append_log(job, f"machine-fit: banks {banks:.1f}GiB/rank × tp{size} に対し"
                                  f"機械 {total_gib:.0f}GiB — 固定する余白がないため auto のまま")
            return
        if launch is not None:
            self._ft_launch_update(
                profile,
                lambda doc: doc.__setitem__(
                    "argv", replace_arg([str(a) for a in doc.get("argv") or []],
                                        "--moe-ram-budget", str(budget))))
        else:
            def patch(data: list[Any]) -> bool:
                for p in data:
                    if isinstance(p, dict) and p.get("id") == profile["id"]:
                        p.setdefault("values", {})["moe_ram_budget"] = str(budget)
                        return True
                return False

            if not self.profiles.mutate(patch):
                raise CalibrateError("計測中にプロファイルが消えたため、machine-fit を保存できませんでした")
            profile.setdefault("values", {})["moe_ram_budget"] = str(budget)
        self._append_log(job, f"machine-fit: disk 層発動を検出 (banks {banks:.1f}GiB, tp{size}) → "
                              f"{'起動 JSON' if launch is not None else 'profile'} の "
                              f"--moe-ram-budget {budget} GiB (次回起動から)")

    def _ft_lookup_step(self, job_id: str, profile: dict[str, Any], module_dir: Path,
                        report_dir: Path, python: str) -> dict[str, Any]:
        """calibrate_lookup の CLI ステップを組み立てる (cli モードと auto フェーズ B 共用)。

        起動 JSON 主体モードではその JSON 自体を --config に渡す (同一形式)。"""
        port_v = str((profile.get("values") or {}).get("port") or "").strip()
        launch_path = launch_json_path(self.root, profile)
        if launch_path is not None and launch_path.is_file():
            config_arg = str(launch_path)
        else:
            full = self.manager.build_argv(profile)
            spec_env = {**{k: str(v) for k, v in (profile.get("env") or {}).items()},
                        **{k: str(v) for k, v in (profile.get("_resolved_env") or {}).items()}}
            spec = {"python": full[0], "argv": full[1:], "cwd": str(module_dir),
                    "env": {k: v for k, v in spec_env.items()
                            if not k.startswith("FREETOKEN_LOOKUP_")},
                    "port": int(port_v or "1919"), "model_name": profile.get("name", "")}
            spec_path = report_dir / f"calibration-{job_id}.spec.json"
            spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), "utf-8")
            config_arg = str(spec_path)
        return {"name": "calibrate-lookup", "cwd": str(module_dir),
                "argv": [python, "-m", "freetoken.calibrate_lookup",
                         "--config", config_arg, "--out-dir", str(report_dir),
                         "--tag", f"cal-{job_id}",
                         *(["--port", port_v] if port_v else [])]}

    def _ft_lookup_doc(self, job: dict[str, Any]) -> dict[str, Any]:
        """fork CLI が書いた lookup レポート (--tag の sanitize 規則で job id の _ は -)。"""
        tag = f"cal-{job['id']}".replace("_", "-")
        path = self.root / "data" / "calibrations" / f"freetoken-lookup-{tag}.json"
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as e:
            raise CalibrateError(f"lookup のキャリブレーションレポートが読めません "
                                 f"({path.name}): {e}")

    def _ft_base_env(self, profile: dict[str, Any]) -> dict[str, str]:
        """セルの基準 env: 起動 JSON の env が正、profile.env がユーザー上書き。"""
        try:
            launch = load_launch_json(self.root, profile)
        except ValueError as e:
            raise CalibrateError(str(e))
        base_env = {str(k): str(v) for k, v in
                    ((profile.get("_resolved_env") or profile.get("env")) or {}).items()}
        if launch is not None:
            base_env = {**{str(k): str(v) for k, v in (launch.get("env") or {}).items()},
                        **base_env}
        return base_env

    def _ft_par_n(self, profile: dict[str, Any]) -> int:
        """並行プローブの本数 = プロファイルの --max-running-requests (ユーザー固定値)。"""
        raw = ""
        with contextlib.suppress(ValueError, CalibrateError):
            launch = load_launch_json(self.root, profile)
            if launch is not None:
                argv = [str(a) for a in launch.get("argv") or []]
                raw = next((argv[i + 1] for i, t in enumerate(argv)
                            if t == "--max-running-requests" and i + 1 < len(argv)), "")
        if not raw:
            raw = str((profile.get("values") or {}).get("max_running_requests") or "")
        try:
            return max(1, min(16, int(raw or 4)))
        except ValueError:
            return 4

    def _apply_freetoken_lookup(self, job: dict[str, Any],
                                profile: dict[str, Any]) -> dict[str, Any]:
        """fork 同梱 calibrate_lookup の出力 JSON を適用する。

        採用判定 (基準比 MIN_GAIN・並行上限で lookup が素の batch に勝つか) はツール
        側の契約。launcher は規則を足さず、負けたときは applied:False + note で正直に
        残す (env は書かない)。"""
        doc = self._ft_lookup_doc(job)
        chosen = doc.get("chosen") or {}
        winner = {str(k): str(v) for k, v in (chosen.get("env") or {}).items()
                  if str(k).startswith("FREETOKEN_LOOKUP_")}
        applied = bool(doc.get("lookup_enabled") and winner)
        if applied:
            self._ft_write_env(profile, winner)
            self._append_log(job, "profile env に適用: "
                             + ", ".join(f"{k[17:].lower()}={v}" for k, v in winner.items()))
        else:
            self._append_log(job, "lookup は並行上限で素の batch 推論に負けたため env は"
                             "書き戻しません (単一ストリーム利得はレポートに残っています)")
        # ツールが起動した各腕のエンジンログ (data/calibrations/logs/) から disk 層発動を探す
        fit_text = ""
        logs_dir = self.root / "data" / "calibrations" / "logs"
        t0 = float(job.get("started_at") or 0.0) - 1.0
        with contextlib.suppress(OSError):
            cand = sorted(logs_dir.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)[:12]
            for p in cand:
                if p.stat().st_mtime < t0:
                    continue
                fit_text = self._ft_read_log(str(p.relative_to(self.root)), 160_000)
                if FT_FIT_PAT.search(fit_text):
                    break
        self._ft_machine_fit(job, profile, fit_text)
        gain = chosen.get("gain_at_parallel")
        if gain is None:
            gain = chosen.get("gain") or 0.0
        return {
            "tuned": True, "backend": "freetoken-lookup", "applied": applied,
            "settings": winner if applied else {},
            "improvement_pct": round(float(gain) * 100, 1),
            "latency": (f"parallel={chosen.get('parallel')} "
                        f"tok_s={chosen.get('tok_s_at_parallel')} "
                        f"acceptance={chosen.get('acceptance')}"),
        }

    async def _ft_probe_parallel(self, job: dict[str, Any], sess: dict[str, Any],
                                 n: int, words: int = 512) -> dict[str, Any]:
        """n 並行のスループット計測。レーンは別シード (各自 prefill を払う) かつ
        FT_PAR_STAGGER_S 刻みの時間差到着 — prefill/decode 混在を作るので、
        prefill ペーシング系ノブの実効差もこのスコアに載る。"""
        base = f"http://127.0.0.1:{sess.get('port')}"
        auth = bench.auth_headers(sess)
        model = await asyncio.to_thread(bench._first_model, base, auth) or "default"

        def one(i: int) -> dict[str, Any]:
            if i:
                time.sleep(i * FT_PAR_STAGGER_S)
            payload = {"model": model,
                       "messages": [{"role": "user",
                                     "content": bench.make_prompt(words, FT_PROBE_SEED + 100 + i)}],
                       "temperature": 0.0, "max_tokens": FT_PROBE_MAX_TOKENS, "stream": False}
            return bench._one_request(base, "/v1/chat/completions", payload, False,
                                      FT_PROBE_TIMEOUT, auth)

        t0 = time.monotonic()
        rs = await asyncio.gather(*(asyncio.to_thread(one, i) for i in range(max(1, n))))
        wall = time.monotonic() - t0
        if any(not r.get("ok") for r in rs):
            bad = next(r for r in rs if not r.get("ok"))
            return {"ok": False, "error": f"並行プローブ失敗: {bad.get('error', '')}"[:400]}
        tokens = sum(int(r.get("out_tokens") or 0) for r in rs)
        if tokens <= 0 or wall <= 0:
            return {"ok": False, "error": "並行プローブが 0 トークンでした"}
        return {"ok": True, "score": round(tokens / wall, 2), "tokens": tokens,
                "wall": round(wall, 2), "n": len(rs)}

    async def _ft_final_cell(self, job: dict[str, Any], profile: dict[str, Any],
                             base_env: dict[str, str], label: str,
                             overrides: dict[str, str], spec: str,
                             par_n: int) -> dict[str, Any]:
        """決勝トーナメント 1 セル: 1 回の起動で単流 (copy 込み) と並行の両方を測る。"""
        clone, tmp_cfg = self._ft_cell_clone(job, profile, base_env, label, overrides, spec)
        try:
            sess = await self._ft_start(job, clone)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:200]}
        sid = sess["id"]
        try:
            await self._ft_wait_ready(job, sess)
            out = await self._ft_probe(job, sess, copy=True)
            if not out.get("ok"):
                return {"ok": False, "error": str(out.get("error", ""))[:400]}
            par = await self._ft_probe_parallel(job, sess, par_n)
            return {"ok": True, "single": out["score"],
                    "par": par.get("score") if par.get("ok") else None,
                    "par_error": par.get("error", ""),
                    "latency": self._ft_latency_excerpt(sess)}
        except RuntimeError as e:
            return {"ok": False, "error": str(e)[:400]}
        finally:
            with contextlib.suppress(Exception):
                await self.manager.stop(sid)
            if tmp_cfg is not None:
                with contextlib.suppress(OSError):
                    tmp_cfg.unlink()

    async def _run_freetoken_auto(self, job: dict[str, Any],
                                  profile: dict[str, Any]) -> dict[str, Any]:
        """投機モード自動選定: mtp sweep → lookup CLI → 共通プローブの決勝トーナメント。

        「mtp(+chain) / lookup / none のどれがこの箱で最速か」は誰も実測していなかった
        (プロファイルの現在値が校正経路を分岐させるだけだった) ので、1 ジョブで決め、
        勝者の --speculative と env ファミリーをまとめて適用する。判定: 単流スコア最優、
        ただし並行上限で並行最優に FT_AUTO_PAR_LOSS を超えて負ける構成は差し替え
        (fork ツールの MAX_PARALLEL_LOSS と同じ哲学)。勝者には品質中立の転送/グラフ系
        env 微調整 (フェーズ D) も行う。所要 ≈ 2-3 時間。"""
        # ---- フェーズ A: mtp(+chain) sweep。適用はまだ (決着はトーナメント)。
        # MTP ヘッドの無いモデルでは基準セルが起動失敗する → mtp を外して続行する
        job["step"] = "phase-mtp"
        self._append_log(job, "[auto] フェーズ A: MTP (chain 込み) を実測します…")
        mtp_env: dict[str, str] | None = None
        mtp_imp: float | None = None
        try:
            mtp = await self._run_freetoken(job, profile, apply=False, spec_override="mtp")
            mtp_env = {k: str(v) for k, v in (mtp.get("winner") or {}).items()}
            mtp_imp = mtp.get("improvement_pct")
        except CalibrateError as e:
            self._append_log(job, f"[auto] フェーズ A: MTP sweep 失敗 ({str(e)[:140]})"
                                  " — mtp はトーナメントから除外")
        if job["id"] in self._cancel:
            raise _Cancelled()

        # ---- フェーズ B: lookup CLI。失敗してもジョブは落とさずトーナメントから外す
        job["step"] = "phase-lookup"
        lookup_env: dict[str, str] | None = None
        backend_id = job.get("backend") or profile.get("backend") or "freetoken-fork"
        module_dir = self.root / backend_for(self.root, backend_id)["module_path"]
        report_dir = self.root / "data" / "calibrations"
        python = self.manager.resolve_python(backend_id)
        self._append_log(job, "[auto] フェーズ B: lookup (fork CLI) を実測します…")
        try:
            step = self._ft_lookup_step(job["id"], profile, module_dir, report_dir, python)
            rc, _raw = await self._run_step(job, step)
            if job["id"] in self._cancel:
                raise _Cancelled()
            if rc != 0:
                lookup_note = f"lookup CLI が失敗 (exit {rc}) — トーナメントから除外"
            else:
                doc = self._ft_lookup_doc(job)
                chosen = doc.get("chosen") or {}
                env4 = {str(k): str(v) for k, v in (chosen.get("env") or {}).items()
                        if str(k).startswith("FREETOKEN_LOOKUP_")}
                if env4:
                    lookup_env = env4
                    lookup_note = ("ツール判定: 並行上限で lookup は worth-keep 未満"
                                   if not doc.get("lookup_enabled")
                                   else "ツール判定: lookup 有効")
                else:
                    lookup_note = "lookup レポートに勝者 env が無い — トーナメントから除外"
        except _Cancelled:
            raise
        except (CalibrateError, OSError, ValueError) as e:
            lookup_note = f"lookup 結果を読めません ({e}) — トーナメントから除外"
        self._append_log(job, f"[auto] フェーズ B: {lookup_note}")
        if job["id"] in self._cancel:
            raise _Cancelled()

        # ---- フェーズ C: 決勝トーナメント (共通プローブ: 単流 copy 込み + 並行)
        base_env = self._ft_base_env(profile)
        par_n = self._ft_par_n(profile)
        configs: list[tuple[str, dict[str, str], str]] = [("none", {}, "none")]
        if mtp_env is not None:
            configs.append(("mtp", mtp_env, "mtp"))
        if lookup_env is not None:
            configs.append(("lookup", lookup_env, "lookup"))
            if par_n > 2:
                # LOOKUP_MAX_BATCH 既定 2: 並行 n 本でも 2 本しか draft できない。
                # ツールが sweep しないこの上限を並行プローブで A/B する
                configs.append(("lookup-mb",
                                {**lookup_env, "FREETOKEN_LOOKUP_MAX_BATCH": str(par_n)},
                                "lookup"))
        tour: list[dict[str, Any]] = []
        for name, env_over, spec in configs:
            if job["id"] in self._cancel:
                raise _Cancelled()
            job["step"] = f"final-{name}"
            r = await self._ft_final_cell(job, profile, base_env, f"final-{name}",
                                          env_over, spec, par_n)
            r["mode"] = name
            tour.append(r)
            if r.get("ok"):
                self._append_log(job, f"[auto] {name}: 単流 {r['single']} tok/s / "
                                      f"{par_n}並行 {r['par']} tok/s")
            else:
                self._append_log(job, f"[auto] {name}: 失敗 ({r.get('error', '')}) — 除外")
        ok = [t for t in tour if t.get("ok") and t.get("single")]
        if not ok:
            raise CalibrateError("決勝トーナメントの全セルが計測できませんでした")
        best_single = max(ok, key=lambda t: float(t["single"]))
        with_par = [t for t in ok if t.get("par")]
        best_par = max(with_par, key=lambda t: float(t["par"])) if with_par else best_single
        winner, rule = best_single, "単流スコア最優"
        if (best_par is not best_single and winner.get("par") is not None
                and float(winner["par"]) < float(best_par["par"]) * (1 - FT_AUTO_PAR_LOSS)):
            winner = best_par
            rule = (f"単流最優 ({best_single['mode']}) は {par_n}並行で {best_par['mode']} に "
                    f"{FT_AUTO_PAR_LOSS * 100:.0f}% 超負けるため並行最優に差し替え")
        mode = winner["mode"]
        spec_mode_now = mode.split("-")[0]        # lookup-mb → lookup
        # 勝者セルの env は configs 定義が正 (セル記録には持たせていない)
        winner_env = dict(next((e for n, e, _s in configs if n == mode), {}))

        # ---- フェーズ D: 勝者構成に対する転送/グラフ系 env の微調整 (座標降下)。
        # 基準スコアは勝者セル自身 (同一プローブ) なので追加の基準起動は要らない
        job["step"] = "phase-micro"
        micros: list[dict[str, Any]] = []
        cur_single = float(winner["single"])
        cur_par = float(winner["par"]) if winner.get("par") is not None else None
        for knob, vals in FT_MICRO_GRID:
            allowed = FT_MICRO_MODES.get(knob)
            if allowed is not None and spec_mode_now not in allowed:
                continue        # 勝者モードで論理的に効かないノブだけ飛ばす
            cur_v = str(base_env.get(knob) or winner_env.get(knob) or "").strip()
            for val in [v for v in vals if v != cur_v]:
                if job["id"] in self._cancel:
                    raise _Cancelled()
                label = f"{knob.removeprefix('FREETOKEN_').lower()}={val}"
                job["step"] = f"micro-{label}"
                r = await self._ft_final_cell(job, profile, base_env, f"micro-{label}",
                                              {**winner_env, knob: val}, spec_mode_now, par_n)
                if not r.get("ok"):
                    micros.append({"label": label, "ok": False, "error": r.get("error", "")})
                    self._append_log(job, f"[micro] {label}: 失敗 ({r.get('error', '')}) — スキップ")
                    continue
                s = float(r["single"])
                p = float(r["par"]) if r.get("par") is not None else None
                gain = (s / cur_single - 1) if cur_single else 0.0
                par_ok = (p is None or cur_par is None
                          or p >= cur_par * (1 - FT_MICRO_PAR_REGRESS))
                adopted = gain > FT_MICRO_IMPROVE and par_ok
                if adopted:
                    winner_env[knob] = val
                    cur_single, cur_par = s, (p if p is not None else cur_par)
                    self._append_log(job, f"[micro] {label}: 単流 {s} tok/s "
                                          f"({gain * 100:+.1f}%) ← 採用")
                else:
                    why = (f"並行 {p} が {FT_MICRO_PAR_REGRESS * 100:.0f}% 超の悪化"
                           if not par_ok else f"{gain * 100:+.1f}% が +2% 未満")
                    self._append_log(job, f"[micro] {label}: 単流 {s} tok/s ({why}) — 不採用")
                micros.append({"label": label, "ok": True, "single": s, "par": p,
                                "adopted": adopted})

        none_single = next((float(t["single"]) for t in ok if t["mode"] == "none"), None)
        improvement = (round((cur_single / none_single - 1) * 100, 1)
                       if none_single else None)

        # ---- 適用: --speculative (起動 JSON argv / values) + 勝者 env ファミリー
        launch_path = launch_json_path(self.root, profile)
        spec_mode_now = mode.split("-")[0]        # lookup-mb → lookup
        if launch_path is not None and launch_path.is_file():
            self._ft_launch_update(
                profile,
                lambda doc: doc.__setitem__(
                    "argv", replace_arg([str(a) for a in doc.get("argv") or []],
                                        "--speculative", spec_mode_now)))
        else:
            def patch(data: list[Any]) -> bool:
                for p in data:
                    if isinstance(p, dict) and p.get("id") == profile["id"]:
                        p.setdefault("values", {})["speculative"] = spec_mode_now
                        return True
                return False

            if not self.profiles.mutate(patch):
                raise CalibrateError("計測中にプロファイルが消えたため、モードを保存できませんでした")
            profile.setdefault("values", {})["speculative"] = spec_mode_now
        if winner_env:
            self._ft_write_env(profile, winner_env)
        self._append_log(job, f"[auto] 勝者: --speculative {spec_mode_now}"
                              + (f" + env {len(winner_env)} キー" if winner_env else "")
                              + f" ({rule})")

        report: dict[str, Any] = {
            "schema_version": 1, "backend": "freetoken-auto", "rule": rule, "par_n": par_n,
            "tournament": tour, "micro": micros,
            "mtp_sweep_improvement_pct": mtp_imp,
            "lookup_phase": lookup_note,
            "winner": {"mode": spec_mode_now, "env": winner_env},
            "improvement_pct": improvement, "latency": winner.get("latency", ""),
        }
        with contextlib.suppress(OSError):
            (report_dir / f"calibration-{job['id']}.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        return {"tuned": True, "backend": "freetoken-auto", "applied": True,
                "settings": {"--speculative": spec_mode_now, **winner_env},
                "improvement_pct": improvement, "latency": winner.get("latency", ""),
                "rule": rule}

    async def _ft_start(self, job: dict[str, Any], clone: dict[str, Any]) -> dict[str, Any]:
        """セル用サーバーを起動。直前のエンジン解放直後 (lingering) はガードに弾かれるため、
        その原因なら少し待って再試行する (force=True は過コミット保護を無視するので使わない)。"""
        for attempt in range(4):
            try:
                return await self.manager.start(clone, force=False)
            except MemoryGuardError as e:
                self._append_log(job, f"起動ガード: {str(e)[:120]} — 15 秒後に再試行")
                await asyncio.sleep(15)
        raise CalibrateError("エンジン起動がメモリガードで繰り返し拒否されました")

    async def _ft_wait_ready(self, job: dict[str, Any], sess: dict[str, Any]) -> None:
        """/health の body が status "ok" (モデルロード完了) になるまで待つ。

        freetoken は loading 中も HTTP 200 を返す設計 (uvicorn が先に縛る) なので、
        200 だけ見る bench._health_ok で抜けると warmup が 503 にぶつかる。
        status "error" (ワーカー死) は即失敗。"""
        sid = sess["id"]
        base = f"http://127.0.0.1:{sess.get('port')}"
        url = base + (sess.get("health_path") or "/health")
        auth = bench.auth_headers(sess)
        timeout = float(os.environ.get("FT_CAL_READY_TIMEOUT_S", "") or FT_READY_TIMEOUT)
        t0 = time.monotonic()
        last_log = 0.0
        while time.monotonic() - t0 < timeout:
            if job["id"] in self._cancel:
                raise _Cancelled()
            status = (self.manager.sessions.get(sid) or {}).get("status")
            if status in ("failed", "exited"):
                raise RuntimeError("エンジンが起動中に終了しました:\n" + self._ft_log_tail(sess, 15))
            http_ok, doc = await asyncio.to_thread(ft_health_status, url, auth)
            if http_ok:
                hst = (doc or {}).get("status")
                if hst == "error":
                    raise RuntimeError("/health が error を返しました: "
                                       f"{str((doc or {}).get('message'))[:200]}\n"
                                       + self._ft_log_tail(sess, 15))
                if hst in (None, "ok"):
                    return                          # 非 JSON (旧版) は 200 = 準備完了扱い
                tnow = time.monotonic()
                if tnow - last_log >= 30.0:         # loading: 進捗を ~30 秒ごとに残す
                    lp = (doc or {}).get("progress") or {}
                    done = int(lp.get("done_bytes") or 0) // 2 ** 30
                    tot = int(lp.get("total_bytes") or 0) // 2 ** 30
                    self._append_log(job, f"モデルをロード中です: phase={(doc or {}).get('phase', '?')} "
                                          f"{done}GB/{tot}GB ({tnow - t0:.0f}秒経過)")
                    last_log = tnow
            await asyncio.sleep(2)
        raise RuntimeError(f"エンジンが {timeout:.0f} 秒以内に起動しませんでした")

    async def _ft_probe(self, job: dict[str, Any], sess: dict[str, Any],
                        copy: bool = False) -> dict[str, Any]:
        """greedy プローブ (warmup 1 本を捨てて短文/長文 1 本ずつ)。スコアは合算 tok/s。

        temp 0 固定 — MTP ゲートは greedy 専用で、ランダムサンプリングでは何も測れない。
        プロンプトはシード固定で全セル同一 (セル間の比較可能性が命)。
        copy=True (mtp run) は逐語コピの 1 本を追加する — chain/lookup 系のドラフトは
        履歴の繰り返しでしか発動しないため、コピ型が無いと chain セルが常に無差分になる。"""
        base = f"http://127.0.0.1:{sess.get('port')}"
        auth = bench.auth_headers(sess)
        model = await asyncio.to_thread(bench._first_model, base, auth) or "default"
        path = "/v1/chat/completions"

        def call(words: int, seed: int, cp: bool = False) -> dict[str, Any]:
            maker = bench.make_copy_prompt if cp else bench.make_prompt
            payload = {"model": model,
                       "messages": [{"role": "user", "content": maker(words, seed)}],
                       "temperature": 0.0, "max_tokens": FT_PROBE_MAX_TOKENS, "stream": False}
            return bench._one_request(base, path, payload, False, FT_PROBE_TIMEOUT, auth)

        warm = await asyncio.to_thread(call, 32, FT_PROBE_SEED)
        tw = time.monotonic()
        while (not warm.get("ok") and "503" in str(warm.get("error", ""))
               and time.monotonic() - tw < FT_WARM_RETRY_S):
            await asyncio.sleep(5)              # 直後の一時 503 (rebuild 等の maintenance) を待つ
            if job["id"] in self._cancel:
                raise _Cancelled()
            warm = await asyncio.to_thread(call, 32, FT_PROBE_SEED)
        if not warm.get("ok"):
            return {"ok": False, "error": f"warmup 失敗: {warm.get('error', '')}"}
        tokens, secs, probes = 0, 0.0, []
        seq = [(w, False) for w in FT_PROBE_WORDS]
        if copy:
            seq.append((FT_PROBE_COPY_WORDS, True))
        for i, (words, cp) in enumerate(seq):
            r = await asyncio.to_thread(call, words, FT_PROBE_SEED + i, cp)
            if not r.get("ok"):
                return {"ok": False,
                        "error": f"プローブ ({words} words{'/copy' if cp else ''}) 失敗: "
                                 f"{r.get('error', '')}"}
            probes.append({"words": words, "copy": cp,
                           "out_tokens": r["out_tokens"], "secs": round(r["secs"], 2)})
            tokens += int(r["out_tokens"] or 0)
            secs += float(r["secs"])
        if tokens <= 0 or secs <= 0:
            return {"ok": False, "error": "プローブが 0 トークンでした"}
        return {"ok": True, "score": round(tokens / secs, 2), "tokens": tokens,
                "secs": round(secs, 2), "probes": probes}

    def _ft_log_tail(self, sess: dict[str, Any], lines: int) -> str:
        """セルのサーバーログ末尾 (起動失敗の理由をジョブログへ残すため)。"""
        try:
            data = (self.root / sess["log"]).read_bytes()[-16384:]
        except (OSError, KeyError):
            return "(ログが読めません)"
        return "\n".join(data.decode("utf-8", "replace").splitlines()[-lines:])

    def _ft_latency_excerpt(self, sess: dict[str, Any]) -> str:
        """サーバーログ末尾から最後の [latency] 行を 1 行拾う (レイテンシ内訳の抄録)。"""
        for line in reversed(self._ft_log_tail(sess, 400).splitlines()):
            if "[latency]" in line:
                return line.strip()[:300]
        return ""

    def _ft_cell_clone(self, job: dict[str, Any], profile: dict[str, Any],
                       base_env: dict[str, str], label: str,
                       overrides: dict[str, Any],
                       spec: str | None) -> tuple[dict[str, Any], "Path | None"]:
        """セル用 clone (env 差し替え + 任意で --speculative 強制) を作る。

        起動 JSON 主体モードでは argv の差し替えに実ファイルが要るので、一時 JSON を
        data/calibrations/ に書く (呼び出し側が finally で削除する)。"""
        env = {**base_env, **{k: str(v) for k, v in overrides.items()}, **FT_LATENCY_ENV}
        clone = {**profile, "env": env}
        if profile.get("_resolved_env"):
            clone["_resolved_env"] = env   # 解決済み env が env より優先される (process.py)
        tmp_cfg: Path | None = None
        if spec is not None:
            launch = None
            with contextlib.suppress(ValueError):
                launch = load_launch_json(self.root, profile)
            if launch is not None:
                doc = dict(launch)
                doc["argv"] = replace_arg([str(a) for a in launch.get("argv") or []],
                                          "--speculative", spec)
                tmp_cfg = (self.root / "data" / "calibrations"
                           / f"cell-{job['id']}-{label}.json")
                tmp_cfg.write_text(json.dumps(doc, ensure_ascii=False), "utf-8")
                clone["values"] = {**(profile.get("values") or {}), "config": str(tmp_cfg)}
            else:
                clone["values"] = {**(profile.get("values") or {}), "speculative": spec}
        return clone, tmp_cfg

    async def _ft_run_cell(self, job: dict[str, Any], profile: dict[str, Any],
                           base_env: dict[str, str], label: str,
                           overrides: dict[str, Any], copy: bool = False,
                           spec: str | None = None) -> dict[str, Any]:
        """1 セル = 起動 → ready 待ち → greedy 計測 → 停止。失敗は ok:False で返す
        (基準セルだけが致命的で、候補セルの失敗は「その値は除外」で足りるため)。"""
        clone, tmp_cfg = self._ft_cell_clone(job, profile, base_env, label, overrides, spec)
        try:
            sess = await self._ft_start(job, clone)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:200]}
        sid = sess["id"]
        try:
            await self._ft_wait_ready(job, sess)
            out = await self._ft_probe(job, sess, copy=copy)
            if out.get("ok"):
                out["latency"] = self._ft_latency_excerpt(sess)
                out["log"] = sess.get("log", "")   # machine-fit が起動行を読む
            return out
        except RuntimeError as e:
            return {"ok": False, "error": str(e)[:400]}
        finally:
            with contextlib.suppress(Exception):
                await self.manager.stop(sid)
            if tmp_cfg is not None:
                with contextlib.suppress(OSError):
                    tmp_cfg.unlink()

    async def _strata_profiles(self, python: str, module_dir: Path) -> list[dict[str, Any]]:
        """保存済み Strata profile の一覧 (JSON)。"""
        rc, raw = await self._exec([python, str(self.root / "module" / "strata" / "strata.py"),
                                    "profile", "list", "--output", "json"], module_dir)
        payload = self._parse_json(raw)
        if rc != 0:
            raise CalibrateError((payload or {}).get("error", {}).get("message")
                                 or f"strata profile list が失敗しました (exit {rc})")
        items = (payload or {}).get("profiles")
        return items if isinstance(items, list) else []

    def cancel(self, jid: str) -> dict[str, Any]:
        job = self.jobs.get(jid)
        if not job:
            raise KeyError(jid)
        if job["status"] not in ("queued", "running"):
            raise CalibrateError("このキャリブレーションは既に終了しています")
        self._cancel.add(jid)
        proc = self._procs.get(jid)
        if proc is not None and proc.returncode is None:
            pid = proc.pid
            self._signal_group(pid, signal.SIGTERM)
            # エンジンは SIGTERM で arena 解放に時間がかかる (launcher 自身の stop と同じ事情)。
            # 待っても終わらない場合に備えて、猶予後 SIGKILL を送る task を出す
            self._kill_later(jid, pid)
        self._save()
        return self._public(job)

    def _kill_later(self, jid: str, pid: int) -> None:
        grace = float(os.environ.get("LLM_LAUNCHER_CAL_KILL_GRACE_S", "240"))

        async def wait_kill() -> None:
            t0 = time.monotonic()
            while time.monotonic() - t0 < grace:
                if not pid_alive(pid):
                    return
                await asyncio.sleep(1.0)
            self._signal_group(pid, signal.SIGKILL)

        task = asyncio.create_task(wait_kill())
        self._extra.add(task)                     # 参照を保持 (GC されると task が消える)
        task.add_done_callback(self._extra.discard)

    def delete(self, jid: str) -> None:
        """履歴を削除する (実行中は呼び出し側が確認済みであること)。"""
        self.jobs.pop(jid, None)
        self._save()

    @staticmethod
    def _signal_group(pid: int, sig: signal.Signals) -> None:
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    async def _exec(self, argv: list[str], cwd: Path) -> tuple[int, str]:
        """1 コマンドを実行して (終了コード, stdout + stderr の末尾) を返す (ログには取り込まない)。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        except FileNotFoundError:
            return 127, ""
        except OSError:
            return 1, ""
        raw, _ = await proc.communicate()
        return proc.returncode or 0, raw.decode("utf-8", "replace")

    async def _run_step(self, job: dict[str, Any], step: dict[str, Any]) -> tuple[int, str]:
        """1 コマンドを走らせ、ログを逐行取り込み、終了コードと出力を返す。"""
        job["step"] = step["name"]
        self._append_log(job, f"===== {step['name']} {time.strftime('%F %T')} =====")
        self._append_log(job, shlex.join(step["argv"]))
        env = {**os.environ}
        try:
            proc = await asyncio.create_subprocess_exec(
                *step["argv"], cwd=step["cwd"], env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            self._append_log(job, f"コマンドが見つかりません: {e}")
            return 127, ""
        except OSError as e:
            self._append_log(job, f"起動失敗: {e}")
            return 1, ""
        self._procs[job["id"]] = proc
        # OOM 保護: キャリブレーションの子プロセス (maya.sh → maya.py → engine) は、このツリーを
        # ProcessManager.apply_oom が扱わないため **oom_score_adj が 0 のまま**になる。launcher 本体
        # だけが -700 で守られているので、systemd-oomd は launcher を kill してしまう (実測 03:22:
        # 88.3 GB ピーク → launcher 死亡)。Maya は実行中に engine を子として増やすので、1 回だけでは
        # 足りない — 定期的にツリーへ同じ score を掛け直す (apply_score は root + 全子孫を対象にする)。
        protect = asyncio.create_task(self._protect_tree(proc.pid))
        self._extra.add(protect)
        protect.add_done_callback(self._extra.discard)
        out: list[str] = []
        kept = 0
        try:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace")
                self._append_log(job, text)
                if kept < 96 * 1024:       # JSON の 1 行はこの範囲に必ず入る (行は正規の JSON 出力)
                    out.append(text)
                    kept += len(text)
            rc = proc.returncode if proc.returncode is not None else await proc.wait()
        finally:
            self._procs.pop(job["id"], None)
        return rc or 0, "".join(out)

    async def _protect_tree(self, root_pid: int, interval: float = 20.0) -> None:
        """キャリブレーションの子プロセスツリーへ oom_score_adj を定期適用する。

        Maya は実行中に engine (数十 GB を RAM へコピーする本体) を子として増やす。1 回だけの
        apply_score では後から増えた子を取りこぼすため、プロセスが消えるまで繰り返す。"""
        cfg = self.manager.oom_cfg() if hasattr(self.manager, "oom_cfg") else {"score_adj": -700}
        score = cfg.get("score_adj", -700)
        while pid_alive(root_pid):
            apply_score(root_pid, score)
            await asyncio.sleep(interval)

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any] | None:
        """CLI の --output json の行 (最後の有効な JSON オブジェクト) を返す。"""
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and "schema_version" in obj:
                    return obj
        return None

    async def _run(self, job: dict[str, Any], steps: list[dict[str, Any]],
                   profile: dict[str, Any]) -> None:
        job["status"] = "running"
        self._save()
        result: dict[str, Any] = {}
        try:
            if job["kind"] == "freetoken" and job.get("mode") == "auto":
                # FreeToken auto: 投機モード自体の選定 (mtp sweep → lookup CLI → 決勝)
                result["freetoken"] = await self._run_freetoken_auto(job, profile)
            elif job["kind"] == "freetoken" and job.get("mode") != "cli":
                # FreeToken: CLI 実行ではなく launcher 内でエンジン再起動 + greedy 計測
                result["freetoken"] = await self._run_freetoken(job, profile)
            else:
                for step in steps:
                    if job["id"] in self._cancel:
                        raise _Cancelled()
                    rc, raw = await self._run_step(job, step)
                    if job["id"] in self._cancel:
                        raise _Cancelled()
                    payload = self._parse_json(raw)
                    if rc != 0:
                        err = (payload or {}).get("error") if isinstance(payload, dict) else None
                        msg = err.get("message") if isinstance(err, dict) else ""
                        if step["name"] == "plan":
                            msg = _plan_failure(payload) or msg
                        raise CalibrateError(msg or f"{step['name']} が失敗しました (exit {rc})")
                    if step["name"] == "calibrate" and step.get("verify"):
                        # Maya は「エンジンが起動できなかった」場合でも exit 0 で終わる
                        # (警告を出して False を返し、config を書き換えない)。終了コードだけだと
                        # 失敗を「成功」と偽ることになるので、config が実際に調整されたかを確認する。
                        why = _verify_maya_tuned(job, step["verify"])
                        if why:
                            raise CalibrateError(why)
                    if step["name"] == "review" and isinstance(payload, dict):
                        # Strata の JSON: {"schema_version":1, "ok": <applicable>, "review": {...}}
                        review = payload.get("review") if isinstance(payload.get("review"), dict) else payload
                        applicable = review.get("applicable") if "applicable" in review else payload.get("ok")
                        if applicable is not True:
                            reasons = ", ".join(review.get("reasons") or []) or "review が適用不可と判定しました"
                            raise CalibrateError(f"review: 適用できません ({reasons})")
                    result[step["name"]] = payload or {}
                if job["kind"] == "freetoken":
                    # lookup: fork CLI の出力 JSON をそのまま適用 (勝ったときだけ env 書戻し)
                    result["freetoken"] = self._apply_freetoken_lookup(job, profile)
            job["status"] = "done"
            job["report"] = _summary(job["kind"], result)
        except _Cancelled:
            job["status"] = "cancelled"
            self._append_log(job, "キャンセルされました")
        except CalibrateError as e:
            job["status"] = "failed"
            job["error"] = str(e)
            self._append_log(job, f"失敗: {e}")
        except Exception as e:  # noqa: BLE001
            job["status"] = "failed"
            job["error"] = f"{type(e).__name__}: {e}"
            self._append_log(job, f"失敗: {job['error']}")
        finally:
            job["finished_at"] = now()
            self._tasks.pop(job["id"], None)
            if self.active_id == job["id"]:
                self.active_id = None
            self._save()

        # 調整後にサーバーを起動し直す (任意・失敗してもジョブ記録は残す)
        if job.get("restart_after") and job["status"] == "done":
            try:
                await self.manager.start(profile, force=True)
            except Exception:  # noqa: BLE001
                pass


def _verify_maya_tuned(job: dict[str, Any], cfg_path: str) -> str:
    """Maya の `--calibrate` が本当に調整したかを確かめ、失敗なら理由を返す (成功なら "")。

    maya.py は「エンジンが起動できなかった」場合に警告を出して False を返すだけで、
    終了コードは 0 のまま (config は書き換えない)。終了コードだけを見ると失敗を成功と
    取り違えるので、成功時の証拠 (config の installer.calibration) を確認し、無ければ
    ログ末尾から Maya 自身の警告 / エンジンの理由を拾って返す。"""
    try:
        cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return f"Maya の config を確認できません ({cfg_path}): {e}"
    cal = (cfg.get("installer") or {}).get("calibration") if isinstance(cfg, dict) else None
    if isinstance(cal, dict) and cal.get("date"):
        return ""
    lines = (job.get("logs") or [])[-40:]
    for line in reversed(lines):
        low = line.lower()
        if "not tuned" in low or "did not finish" in low:
            return "Maya の調整は完了しませんでした: " + line.strip()
    for line in reversed((job.get("logs") or [])[-40:]):
        if "the engine said:" in line.lower():
            return "Maya のエンジンが起動できませんでした: " + line.split(":", 1)[1].strip()
    return "Maya の調整は完了しませんでした (config に調整結果が書かれていません — ログ参照)"


def _plan_failure(payload: dict[str, Any] | None) -> str:
    """Strata `engine calibrate --plan` の失敗理由 (読み取り専用の事前検査)。

    出力に plan.component_preflight.issues / missing があれば、欠損コンポーネント名を
    列挙する (例: 視覚エンコーダ mmproj が無い → measure は 15-30 分走った末に失敗するので、
    その前に止める)。"""
    plan = (payload or {}).get("plan") if isinstance(payload, dict) else None
    if not isinstance(plan, dict):
        return ""
    pre = plan.get("component_preflight") if isinstance(plan.get("component_preflight"), dict) else {}
    issues = pre.get("issues") if isinstance(pre.get("issues"), list) else []
    if issues:
        parts = []
        for it in issues:
            if isinstance(it, dict):
                comp = it.get("component") or ""
                msg = it.get("message") or ""
                parts.append(f"{comp}: {msg}" if comp else msg)
        if parts:
            return "事前検査で不足あり: " + "; ".join(parts[:5])
    missing = [c.get("name") for c in (pre.get("components") or [])
               if isinstance(c, dict) and c.get("required") and c.get("status") != "ok"]
    if missing:
        return "必須コンポーネントが見つかりません: " + ", ".join(missing[:8])
    if plan.get("tool_available") is False:
        return "Strata の tools/calibrate.py が無い (Terminal アーカイブではエンジン calibrate 不可)"
    return ""


def _summary(kind: str, result: dict[str, Any]) -> dict[str, Any]:
    """ジョブ内の CLI 結果を UI 用の短い要約にする。"""
    if kind == "maya":
        return {"tuned": True, "backend": "maya"}
    if kind == "freetoken":
        rep = result.get("freetoken") or {}
        if rep.get("backend") in ("freetoken-lookup", "freetoken-auto"):
            return rep          # lookup (fork CLI) / auto (トーナメント) は UI 形のまま返す
        return {"tuned": True, "backend": "freetoken", "applied": True,
                "settings": rep.get("winner") or {},
                "improvement_pct": rep.get("improvement_pct"),
                "latency": rep.get("latency", "")}
    # Strata の JSON は {"schema_version":1, "ok":true, "review": {...}} / {"... ","application": {...}}
    review = (result.get("review") or {}).get("review") or result.get("review") or {}
    apply = result.get("apply") or {}
    app = apply.get("application") or {}
    return {
        "settings": review.get("settings") or {},
        "before_args": review.get("before_args"),
        "after_args": review.get("after_args"),
        "application_record": app.get("application_record"),
        "applied": bool(apply.get("ok") is True or app),
    }
