"""デコード速度グラフの正規化: Strata 系 JSON /metrics 以外のバックエンド担当。

Strata / Maya / Basalt は /metrics が JSON (live.slots / history) なので app.py が
そのまま中継する。一方 vLLM / SGLang / llama.cpp は Prometheus テキスト、
FreeToken 系は /v1/stats JSON で、形式も単位も違う:

  vLLM      /metrics  vllm:generation_tokens_total (counter) の差分で tok/s を出す
  SGLang    /metrics  sglang:gen_throughput (gauge, 要 --enable-metrics)
  llama.cpp /slots    スロット毎の n_decoded 差分でスロット別 tok/s を出す (要 --slots)
            /metrics  llamacpp:tokens_predicted_total (counter, 要 --metrics)
  FreeToken /v1/stats throughput.decode_tps (5 秒スライディング窓)

counter / n_decoded の差分は「前回のポーリング」を覚えている必要があるので、
セッション毎の RateTracker を app.py が持って 1 秒ポーリングごとに渡す。
サーバー再起動で counter が巻き戻ったら (dv < 0) その 1 点は None にする。
"""
from __future__ import annotations

import re
from collections import deque
from typing import Any

# Prometheus テキスト形式の 1 行: name{labels} value [timestamp]
_PROM_LINE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([^\s]+)")
_PROM_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_prometheus(text: str) -> tuple[dict[str, float], dict[str, str]]:
    """Prometheus テキストを (メトリクス名ごとの合計, 代表ラベル) にする。

    counter / gauge は engine や TP ランク毎に系列が分かれるので、グラフが欲しい
    「サーバー全体」の値はラベルを畳んで合計する。model_name だけは最初に見つかった
    系列の値を拾う (モデル表示用)。NaN / Inf は合計に入れない。
    """
    sums: dict[str, float] = {}
    labels: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _PROM_LINE.match(line)
        if not m:
            continue
        name, label_str, raw = m.groups()
        try:
            value = float(raw)
        except ValueError:
            continue
        if value != value or value in (float("inf"), float("-inf")):  # NaN / Inf
            continue
        sums[name] = sums.get(name, 0.0) + value
        if label_str and "model_name" not in labels:
            for k, v in _PROM_LABEL.findall(label_str):
                if k == "model_name":
                    labels["model_name"] = v.replace('\\"', '"').replace("\\\\", "\\")
    return sums, labels


def _num(v: Any) -> float | int | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class RateTracker:
    """セッション 1 本分の差分計算状態 (app.py が sid 毎に保持する)。

    now は単調時計 (time.monotonic)。counter_rate / slot_rate は呼ばれた時点で
    前回値を覚えるので、同じポーリングで 2 回呼ばないこと。
    """

    def __init__(self) -> None:
        self._counters: dict[str, tuple[float, float]] = {}
        self._slots: dict[Any, tuple[float, int]] = {}
        self.history: deque[float | None] = deque(maxlen=60)
        # /v1/models や /props の初回取得結果 (model / max_context)。毎秒叩かない。
        self.model_info: dict[str, Any] | None = None

    def counter_rate(self, name: str, value: float, now: float) -> float | None:
        prev = self._counters.get(name)
        self._counters[name] = (now, value)
        if prev is None:
            return None
        dt, dv = now - prev[0], value - prev[1]
        if dt <= 0 or dv < 0:          # 同時刻 / サーバー再起動での巻き戻し
            return None
        return dv / dt

    def slot_rate(self, key: Any, n_decoded: int, now: float) -> float | None:
        prev = self._slots.get(key)
        self._slots[key] = (now, n_decoded)
        if prev is None:
            return None
        dt, dv = now - prev[0], n_decoded - prev[1]
        if dt <= 0 or dv < 0:          # 新しいタスクで n_decoded が 0 に戻った
            return None
        return dv / dt

    def prune_slots(self, active_keys: set) -> None:
        for key in list(self._slots):
            if key not in active_keys:
                del self._slots[key]

    def push_history(self, tok_s: float | None) -> None:
        self.history.append(tok_s)


def _live(state: str, tok_s: Any, *, running: Any = None, active: Any = None,
          waiting: Any = None, queued: Any = None, parallel: Any = None,
          prefill: Any = None) -> dict[str, Any]:
    """app.py の Strata 系中継と同じ live キー一式 (フロントエンドが読む形)。"""
    return {
        "state": state, "tok_s": _num(tok_s), "tok_s_mean": None,
        "prefill_tok_s_mean": _num(prefill), "parallel": _num(parallel),
        "running": _num(running), "active": _num(active), "writing": None,
        "waiting": _num(waiting), "admitting": None, "outside_slots": None,
        "queued": _num(queued),
    }


def normalize_freetoken(stats: dict[str, Any]) -> dict[str, Any]:
    """/v1/stats (StatsTracker のスライディング窓) → グラフ形式。

    リクエスト毎の内訳は出していない (decode_tps は全部合算) ので slots は空。
    フロントエンドは合算グラフ + 「内訳なし」注記を描く。"""
    tp = stats.get("throughput") if isinstance(stats.get("throughput"), dict) else {}
    req = stats.get("requests") if isinstance(stats.get("requests"), dict) else {}
    model = stats.get("model") if isinstance(stats.get("model"), dict) else {}
    active = _num(req.get("active"))
    return {
        "server": {"model": model.get("id") or "", "max_context": _num(model.get("ctx"))},
        "live": _live("generating" if active else "idle", tp.get("decode_tps"),
                      running=active, active=active, prefill=tp.get("prefill_tps")),
        "slots": [],
    }


def normalize_vllm(sums: dict[str, float], labels: dict[str, str],
                   tracker: RateTracker, now: float) -> dict[str, Any]:
    """vLLM の Prometheus /metrics。tok/s は generation_tokens_total の差分。

    初回ポーリングは差分が取れないので tok_s=None (フロントは "-" 表示)。"""
    tok = prefill = None
    if "vllm:generation_tokens_total" in sums:
        tok = tracker.counter_rate("vllm:generation_tokens_total",
                                   sums["vllm:generation_tokens_total"], now)
    if "vllm:prompt_tokens_total" in sums:
        prefill = tracker.counter_rate("vllm:prompt_tokens_total",
                                       sums["vllm:prompt_tokens_total"], now)
    running = sums.get("vllm:num_requests_running")
    waiting = sums.get("vllm:num_requests_waiting")
    return {
        "server": {"model": labels.get("model_name") or "", "max_context": None},
        "live": _live("generating" if running else "idle", tok,
                      running=running, active=running, waiting=waiting,
                      queued=waiting, prefill=prefill),
        "slots": [],
    }


def normalize_sglang(sums: dict[str, float], labels: dict[str, str],
                     tracker: RateTracker, now: float) -> dict[str, Any]:
    """SGLang の Prometheus /metrics (--enable-metrics が要る)。

    gen_throughput はスケジューラが直近の decode 速度を gauge で出すので差分不要。
    古い版で gauge が無いときだけ generation_tokens_total の差分に落とす。"""
    tok = sums.get("sglang:gen_throughput")
    if tok is None and "sglang:generation_tokens_total" in sums:
        tok = tracker.counter_rate("sglang:generation_tokens_total",
                                   sums["sglang:generation_tokens_total"], now)
    running = sums.get("sglang:num_running_reqs")
    queued = sums.get("sglang:num_queue_reqs")
    return {
        "server": {"model": labels.get("model_name") or "", "max_context": None},
        "live": _live("generating" if running else "idle", tok,
                      running=running, active=running, waiting=queued, queued=queued),
        "slots": [],
    }


def normalize_llamacpp(slot_rows: list[Any], sums: dict[str, float],
                       tracker: RateTracker, now: float) -> dict[str, Any]:
    """llama-server の /slots (JSON) + /metrics (Prometheus)。

    スロット別 tok/s は next_token[0].n_decoded の差分。タスクが変わると
    n_decoded が 0 に戻るので id_task をキーにする (巻き戻りは None)。
    /slots が無効 (--slots なし) なら slot_rows=[] で合算だけ描く。"""
    slots: list[dict[str, Any]] = []
    active_keys: set = set()
    for i, row in enumerate(slot_rows):
        if not isinstance(row, dict):
            continue
        busy = bool(row.get("is_processing"))
        nt = row.get("next_token")
        first = nt[0] if isinstance(nt, list) and nt and isinstance(nt[0], dict) else {}
        n_decoded = _num(first.get("n_decoded"))
        n_prompt = _num(row.get("n_prompt_tokens"))
        n_read = _num(row.get("n_prompt_tokens_processed"))
        tok = 0.0
        if busy and n_decoded is not None:
            key = row.get("id_task", row.get("id", i))
            active_keys.add(key)
            rate = tracker.slot_rate(key, n_decoded, now)
            tok = rate if rate is not None else 0.0
        reading = (busy and n_prompt is not None and n_read is not None
                   and n_read < n_prompt)
        slots.append({
            "slot": row["id"] if isinstance(row.get("id"), int) else i,
            "state": ("reading" if reading else "generating") if busy else "idle",
            "tok_s": tok,
            "prompt_tokens": n_prompt,
            "prompt_read": n_read,
            "generated": n_decoded,
            "elapsed_s": None,
            "held_tokens": None,
            "n_ctx": _num(row.get("n_ctx")),
            "accept": None,
        })
    tracker.prune_slots(active_keys)

    tok = None
    if "llamacpp:tokens_predicted_total" in sums:
        tok = tracker.counter_rate("llamacpp:tokens_predicted_total",
                                   sums["llamacpp:tokens_predicted_total"], now)
    if tok is None:
        tok = sums.get("llamacpp:predicted_tokens_seconds")
    if tok is None and slots:
        # --metrics なし: スロット差分の合計で代用する (初回は 0)
        tok = sum(s["tok_s"] for s in slots)
    running = sums.get("llamacpp:requests_processing")
    if running is None:
        running = sum(1 for s in slots if s["state"] != "idle") if slots else None
    queued = sums.get("llamacpp:requests_deferred")
    return {
        "server": {"model": "", "max_context": None},
        "live": _live("generating" if running else "idle", tok,
                      running=running, active=running, queued=queued,
                      parallel=len(slots) if slots else None),
        "slots": slots,
    }
