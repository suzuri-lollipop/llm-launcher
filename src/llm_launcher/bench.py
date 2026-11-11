"""組み込みベンチマーク: プロファイル指定のサーバー (OpenAI 互換 API) へ
launcher から負荷をかけ、スループット / TTFT / レイテンシ分布を計測する。

追加依存なし (urllib + スレッドプール)。イベントループ上のワーカー協調体が
HTTP 実体をスレッドで走らせ、並列度リストの各値を 1 ステージとして逐次実行する。
各ステージの前に warmup リクエスト (統計外) を置ける。結果は data/bench.json に
履歴として永続化し、実行中は progress をライブ更新する。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import queue as _queue
import random
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .backends import api_key_from_argv
from .oom import MemoryGuardError
from .process import VIA_BENCH
from .store import JsonStore, new_id, now

MAX_RUNS = 50        # 履歴として保持する最大件数 (完了分のみ整理)
SAVE_THROTTLE = 2.0  # 進行状況の永続化間隔 (秒)
CANCEL_JOIN_S = 5.0  # 中断がテスト/UI 上で「即座」とみなせる上限 (秒)
PROMPT_SEED_GAP = 1_000_003     # ステージ間でプロンプト列をずらす幅
WARMUP_SEED_OFFSET = 500_000    # warmup は測定本体と別のプロンプト列
MAX_STAGES = 8                  # 並列度ステージの上限
# 経時記録 (グラフ用): measure 中この間隔でサンプル点を 1 個積む。上限を超えたら
# 古い点を等間隔のまま半分に間引き、次のサンプルから間隔を 2 倍にする (段あたりの
# 記録点数を有界に保ちつつ、長い実行も曲線として重ねられるようにする)。
SAMPLE_EVERY = 1.0
MAX_SAMPLES = 180
MAX_REQS = 256        # フェーズ記録 (reqs) の段あたり保存点数上限 (超えら等間隔間引き)
MAX_STORE_BYTES = 8_000_000  # bench.json の概算上限 (超えたら古い完了分から整理)

# 生成中の「人間の体感停止」の閾値: ストリームのチャンク間隔がこれを超えたら
# 1 回と数える (stall_count)。最長停止 (stall_worst_s / gap_max) は閾値と無関係に
# 常に記録する。目標は「人間が関知できない待ち時間」= これを 0 に保つこと。
STALL_S = 0.5


class BenchError(Exception):
    """ユーザーに提示するベンチマーク失敗。"""


class _Cancelled(Exception):
    """キャンセル検出 (内部シグナル)。"""


class BenchProfileError(Exception):
    """ベンチマークプロファイルの保存・更新の失敗 (ユーザーに提示する)。"""


class BenchProfiles:
    """Named, reusable benchmark settings (`data/bench_profiles.json`).

    A profile stores a whole benchmark body (the target launch profile, mode, stages and the decode
    controls), so the same run can be repeated from the UI or the API.  The app validates the body
    with BenchBody before it gets here; this class only owns names, ids and the store."""

    MAX = 100
    NAME_MAX = 80

    def __init__(self, store: JsonStore):
        self.store = store

    def list(self) -> list[dict[str, Any]]:
        return sorted(self.store.load(), key=lambda p: p.get("updated_at") or 0, reverse=True)

    def get(self, pid: str) -> dict[str, Any] | None:
        return next((p for p in self.store.load() if p.get("id") == pid), None)

    def _name(self, name: Any, *, skip: str | None = None) -> str:
        text = str(name or "").strip()
        if not text:
            raise BenchProfileError("ベンチプロファイル名を入力してください")
        if len(text) > self.NAME_MAX:
            raise BenchProfileError(f"ベンチプロファイル名は {self.NAME_MAX} 文字までです")
        for p in self.store.load():
            if p.get("id") != skip and str(p.get("name") or "").casefold() == text.casefold():
                raise BenchProfileError(f"同名のベンチプロファイルがあります: {text}")
        return text

    def create(self, name: Any, params: dict[str, Any]) -> dict[str, Any]:
        data = self.store.load()
        if len(data) >= self.MAX:
            raise BenchProfileError(f"ベンチプロファイルは {self.MAX} 件までです。不要なものを削除してください")
        text = self._name(name)
        item = {"id": new_id("bp"), "name": text, "params": dict(params),
                "created_at": now(), "updated_at": now()}
        self.store.mutate(lambda lst: lst.append(item))
        return item

    def update(self, pid: str, name: Any, params: dict[str, Any]) -> dict[str, Any]:
        item = self.get(pid)
        if item is None:
            raise KeyError(pid)
        text = self._name(name, skip=pid)
        item = {**item, "name": text, "params": dict(params), "updated_at": now()}

        def patch(data: list[dict[str, Any]]) -> bool:
            for i, p in enumerate(data):
                if p.get("id") == pid:
                    data[i] = item
                    return True
            return False

        if not self.store.mutate(patch):
            raise KeyError(pid)
        return item

    def delete(self, pid: str) -> bool:
        data = self.store.load()
        keep = [p for p in data if p.get("id") != pid]
        if len(keep) == len(data):
            return False
        self.store.mutate(lambda lst: lst.__setitem__(slice(None), keep))
        return True


# ------------------------------------------------------------------ 実走の排他


class ExclusiveLock:
    """同じマシンの実走 (ベンチ / キャリブレーション) を 1 本に制限するロック。

    ベンチとキャリブレーションは同じ GPU を使い、しかもキャリブレーションは対象
    プロファイルのサーバーを止めてエンジンを起動するため、重なると双方の結果が
    壊れる (README の「同時実走は 1 件」はベンチ同士にしか効いていなかった)。
    owner はロック自身が持つ — 各 manager が別々に owner を持つと、共有ロックの
    取り合いで「自分が取った」と誤認するため。"""

    __slots__ = ("_rl", "owner")

    def __init__(self) -> None:
        self._rl = threading.RLock()
        self.owner: str = ""            # "" = 空き / "bench:<profile>" / "calibration"

    def acquire(self, owner: str) -> bool:
        if self.owner:
            return False
        if not self._rl.acquire(blocking=False):
            return False
        if self.owner:                  # 取るまでの間に誰かが入った
            self._rl.release()
            return False
        self.owner = owner
        return True

    def release(self) -> None:
        if not self.owner:
            return
        self.owner = ""
        with contextlib.suppress(RuntimeError):
            self._rl.release()

_WORD_BANK = (
    "the quick brown fox jumps over lazy dogs while a quiet system measures token "
    "throughput and latency across parallel workers using stable random text "
    "samples benchmarks reveal hidden bottlenecks under sustained concurrent load "
    "every run produces fresh measurements that operators compare across profiles "
    "backends engines quantizations kernels and scheduling policies memory pools "
    "cache pages decode steps prefill batches scheduler queues tokens per second"
).split()


def make_prompt(n_words: int, seed: int) -> str:
    """リクエスト毎に重複しない擬似ランダム文章を作る (prefix cache 対策でシード可変)。"""
    rnd = random.Random(seed)
    words = [rnd.choice(_WORD_BANK) for _ in range(max(1, n_words))]
    return " ".join(words) + "\n\n上の文章の内容を一文で要約してください。"


def make_copy_prompt(n_words: int, seed: int) -> str:
    """逐語コピさせるプロンプト (chain / lookup 系の測定用)。

    greedy 出力が入力自身の反復になるため、suffix ドラフトや MTP+lookup chain が
    実際に発動する条件 (履歴の繰り返し) を合成プロンプトで再現できる。"""
    rnd = random.Random(seed)
    words = [rnd.choice(_WORD_BANK) for _ in range(max(1, n_words))]
    passage = " ".join(words)
    return (passage + "\n\nCopy the passage above verbatim, word for word, "
                      "to the end. Do not add anything.")


_CJK_RE = re.compile(
    r"[\u1100-\u115f\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]")


def est_tokens(s: str) -> int:
    """usage を返さないサーバー向けのトークン数推定 (CJK≈0.6tok/字、他≈1tok/語)。"""
    if not s:
        return 0
    cjk = len(_CJK_RE.findall(s))
    rest = _CJK_RE.sub(" ", s)
    words = len(re.findall(r"[A-Za-z0-9_'-]+", rest))
    return max(1, round(cjk * 0.6 + words))


# ------------------------------------------------------------------- http


def auth_headers(sess: dict[str, Any]) -> dict[str, str]:
    """セッションの argv に --api-key があれば Bearer ヘッダを作る。
    バックエンド側で Bearer 認証を有効化している場合、launcher 内の
    ベンチマークが 401 で全滅するのを防ぐ。"""
    key = api_key_from_argv(sess.get("argv") or [])
    return {"Authorization": f"Bearer {key}"} if key else {}


def _health_ok(url: str, auth: dict[str, str] | None = None) -> bool:
    try:
        req = urllib.request.Request(url, headers=auth or {})
        with urllib.request.urlopen(req, timeout=2.0) as r:
            return 200 <= r.status < 400
    except OSError:
        return False


def _first_model(base: str, auth: dict[str, str] | None = None) -> str | None:
    try:
        req = urllib.request.Request(f"{base}/v1/models", headers=auth or {})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    for it in (data.get("data") or []):
        if isinstance(it, dict):
            mid = it.get("id") or it.get("model") or it.get("name")
            if mid:
                return str(mid)
    return None


def _one_request(base: str, path: str, payload: dict[str, Any],
                 stream: bool, timeout: float,
                 auth: dict[str, str] | None = None,
                 stop: threading.Event | None = None,
                 live: "dict[str, float] | None" = None) -> dict[str, Any]:
    """OpenAI 互換リクエストを 1 本実行し、所要時間・TTFT・トークン数を返す。

    例外は投げず ok/error で結果を返す (ワーカー側を集計しやすくするため)。
    stop は中断シグナル: SSE のイベント境界で見て、立っていれば接続を閉じて
    `cancelled` を付けて返す (GPU 上に前の負荷を残さないため)。
    live を渡すと、content が届くたびに est_tokens(piece) を即时加算する
    (単一書きのスレッド専用スロット)。サンプラーがこの合計の 1 秒あたりの
    増分を測ることで、「完了時にまとめて確定」ではなく「その秒に実際に生成された
    トークン」を経時記録できる。"""
    t0 = time.perf_counter()
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream" if stream else "application/json",
                 **(auth or {})},
        method="POST",
    )

    def _fail(msg: str) -> dict[str, Any]:
        _finish_timer()
        return {"ok": False, "error": msg[:300], "secs": time.perf_counter() - t0}

    def _stopped() -> bool:
        return stop is not None and stop.is_set()

    timer = None
    # 中断時に shutdown するソケットを監視スレッドへ渡す口。
    # 接続前は socketpair のダミー、接続後は resp のソケットを [0] に入れる
    # (urlopen の timeout は 1 回の recv にしか効かないため、shutdown しないと
    #  サーバーがトークンを止めたまま request_timeout まで固まる)。
    abort_sock: list[socket.socket] = list(socket.socketpair())
    timer_stop: threading.Event | None = None     # 監視スレッド専用の停止シグナル

    def _finish_timer() -> None:
        """監視スレッドを畳む (呼び出し側の stop は立てない — 成功でも「中断」扱いになる)。"""
        if timer is not None and timer_stop is not None:
            timer_stop.set()
            timer.join(timeout=0.2)
        with contextlib.suppress(OSError):
            abort_sock[0].close()
            abort_sock[1].close()

    if stop is not None and not stop.is_set():
        timer_stop = threading.Event()

        def _watch(stop: threading.Event = stop,
                   done: threading.Event = timer_stop,
                   sockbox: list = abort_sock) -> None:
            # stop (中断要求) か done (このリクエストの終了) のどちらかまで待つ。
            # どちらも既定引数で束縛する — 同じ関数が連続で呼ばれたときに、前の
            # 監視スレッドが後の呼び出しの stop を見てしまう事故を避ける。
            while not stop.is_set() and not done.wait(0.02):
                pass
            if stop.is_set():
                # 監視スレッド自身は resp を知らない — 接続できた側が
                # sockbox[0] に実ソケットを入れて渡す (接続前の中断に対応)。
                with contextlib.suppress(Exception):
                    sockbox[0].shutdown(socket.SHUT_RDWR)
        timer = threading.Thread(target=_watch, name="bench-abort", daemon=True)
        timer.start()

    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        snippet = ""
        with contextlib.suppress(OSError):
            snippet = e.read().decode("utf-8", "replace")[:200].replace("\n", " ")
        return _fail(f"HTTP {e.code} {snippet}".strip())
    except OSError as e:
        return _fail(f"{type(e).__name__}: {e}")
    with contextlib.suppress(Exception):
        abort_sock[0].close()
        abort_sock[0] = resp.fp.raw._sock    # 以後の中断はこのソケットを shutdown する
    # 監視スレッドが abort しても urlopen は例外にならない (接続確立後)。
    # ここで止まっていたら、以降の読みは必ず失敗するので待たずに戻る。
    if stop is not None and stop.is_set():
        return {**_fail("中断によりリクエストを打ち切りました"), "cancelled": True}

    ttf: float | None = None
    usage: dict[str, Any] = {}
    text_parts: list[str] = []
    # ストリーム停止の計測: 直前の content チャンク到着からの間隔を追い、
    # 最長間隔 (gap_max) と STALL_S 以上の間隔の回数 (stalls) を記録する。
    # 送信->最初のトークンは TTFT として別途記録されるため含めない。
    last_piece_t: float | None = None
    gap_max: float | None = None
    stalls = 0
    cancelled = False
    with resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if not stream or "text/event-stream" not in ctype:
            # 非ストリーム応答 (stream=true でも無視するサーバー向けフォールバック)
            try:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                return _fail(f"応答を解析できません: {e}")
            usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
            ch = (body.get("choices") or [{}])[0] if body.get("choices") else {}
            msg = ch.get("message") if isinstance(ch.get("message"), dict) else {}
            txt = ((msg.get("content") or ch.get("text") or "")
                   + (msg.get("reasoning_content") or msg.get("reasoning") or ""))
            est = est_tokens(txt)
            out = usage.get("completion_tokens")
            # TTFT は取れない (一括応答なので最初のトークン時刻が存在しない)。
            # ttft=None のまま decode 速度を割らないよう、呼び出し側が除外する。
            _finish_timer()
            if live is not None:
                # 非ストリームは「到着」が存在しないので、確定した全部を一度に加算する
                # (経時グラフ上は完了の瞬間のバースになる — それが実態)
                live["f"] += int(out) if out is not None else est
            return {"ok": True, "secs": time.perf_counter() - t0, "ttft": None,
                    "out_tokens": int(out) if out is not None else est,
                    "in_tokens": usage.get("prompt_tokens"),
                    "gap_max": None, "stalls": 0,
                    "estimated": "completion_tokens" not in usage or "prompt_tokens" not in usage}
        # SSE を「改行までのデータ」単位で読む。
        # - `for line in resp` (io のバッファ経由) はサーバーが接続を閉じるまで返らない
        # - resp.readline() は読み込みの途中で blocking に入ると、残りの行がバッファに
        #   あってもソケットに何か届くまで返らない (大きなストリームで中断できない)
        # ため、HTTPResponse にラップされる前の生ソケットから読み、行は自前で分割する。
        # shutdown (中断) で recv は即座に EOF を返し、届いている行は処理してから抜ける。
        sock = resp.fp.raw._sock
        buf = b""
        cancelled = False
        done = False
        while not done and not cancelled:
            if _stopped():
                cancelled = True
                break
            chunk = b""
            try:
                chunk = sock.recv(65536)
            except OSError:
                # タイムアウト (request_timeout 経過) や shutdown による中断
                if _stopped():
                    cancelled = True
                    break
                raise
            if not chunk:
                # EOF: サーバーが閉じた、または中断で shutdown した。中断なら
                # 「完了」ではなく cancelled として扱う (統計に完了扱いで入らないよう)
                cancelled = _stopped()
                break
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                line = raw.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    done = True
                    break
                try:
                    evt = json.loads(data.decode("utf-8", "replace"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(evt.get("usage"), dict):
                    usage = evt["usage"]
                ch = (evt.get("choices") or [{}])[0] if evt.get("choices") else {}
                delta = ch.get("delta") if isinstance(ch.get("delta"), dict) else None
                if delta is not None:
                    piece = (delta.get("content") or delta.get("reasoning_content")
                             or delta.get("reasoning") or "")
                else:
                    piece = ch.get("text") if isinstance(ch.get("text"), str) else ""
                if piece:
                    t_now = time.perf_counter()
                    if live is not None:
                        if live.get("a") is None:
                            live["a"] = t_now           # フェーズ: 最初のトークン (待ち→デコードの境界)
                        live["d"] = t_now               # フェーズ: 最後のトークン
                    if ttf is None:
                        ttf = t_now - t0
                    else:
                        gap = t_now - (last_piece_t if last_piece_t is not None else t_now)
                        if gap_max is None or gap > gap_max:
                            gap_max = gap
                        if gap >= STALL_S:
                            stalls += 1
                            # 停止区間そのものをスロットへ積む (スイムレーンに赤で重畳し、
                            # 比較バーの「停止」セグメントへ集計できるようにするため)。
                            # 書き手はこのストリーム読み手 1 スレッドだけ。
                            if live is not None:
                                gl = live.get("gl")
                                if gl is None:
                                    gl = live["gl"] = []
                                if len(gl) < 100:           # 1 リクエスト 100 区間上限
                                    gl.append((last_piece_t, t_now))
                                live["st"] = float(live.get("st") or 0.0) + gap
                    last_piece_t = t_now
                    text_parts.append(piece)
                    if live is not None:
                        live["f"] += est_tokens(piece)   # 到着した分をその場で数える
            if not chunk:
                break          # サーバーが閉じた (EOF)
    est = est_tokens("".join(text_parts))
    out = usage.get("completion_tokens")
    out = int(out) if out is not None else est
    if not out and est:
        out = est
    _finish_timer()
    res = {"ok": True, "secs": time.perf_counter() - t0, "ttft": ttf,
           "out_tokens": out, "in_tokens": usage.get("prompt_tokens"),
           "gap_max": round(gap_max, 4) if gap_max is not None else None,
           "stalls": stalls,
           "estimated": "completion_tokens" not in usage or "prompt_tokens" not in usage}
    if cancelled:
        # 中断で途中終了したリクエストは統計 (tok/s) に混ぜない
        res["cancelled"] = True
    return res


# ------------------------------------------------------------------ stats


def _pct(vals: list[float], q: float) -> float:
    k = max(0, min(len(vals) - 1, math.ceil(q * len(vals)) - 1))
    return vals[k]


def _fetch_phase_pool(base: str) -> "list[dict[str, Any]] | None":
    """サーバー側のリクエスト内訳 (到着時刻 / プレフィル秒 / サーバー側TTFT) を取りに行く。

    対応形式は strata 系の JSON /metrics ({"requests":[{time, prompt_ms,
    first_engine_token_s, prompt_tokens, ...}]})。非対応サーバー (Prometheus 生テキスト
    や 404) やタイムアウトは None = 「このサーバーは内訳を出さない」として扱う。
    ベンチの本体計測 (HTTP 200 の SSE) とは独立の補助情報なので、失敗しても何もしない。"""
    try:
        req = urllib.request.Request(base.rstrip("/") + "/metrics")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — 補助情報の取得失敗は常に「無し」に倒す
        return None
    ents = data.get("requests") if isinstance(data, dict) else None
    if not isinstance(ents, list):
        return None
    out = [e for e in ents
           if isinstance(e, dict) and isinstance(e.get("time"), (int, float))
           and isinstance(e.get("prompt_ms"), (int, float))]
    return out or None


def _attach_phase_metrics(snaps: "list[dict[str, Any]]", slots: "list[dict[str, Any]]",
                          pool: "list[dict[str, Any]]", t0_wall: float) -> int:
    """サーバー内訳エントリを各リクエスト行に突合して arr(到着)/q(キュー待ち)/p(プレフィル) を付与。

    snaps[i] と slots[i] は同一順。突合は送信実時刻 (slot["w"]) に近く、入力トークンが
    一致する未使用エントリを貪欲に (近い順)。キュー待ち = サーバー側TTFT − プレフィル。
    戻り値はマッチ件数 (0 = このサーバーでは内訳を描けない)。"""
    used: set[int] = set()
    hit = 0
    for i, sl in enumerate(slots):
        w = sl.get("w")
        if not isinstance(w, float) or snaps[i].get("a") is None:
            continue
        tin = sl.get("in")
        best: "tuple[int, float] | None" = None
        for j, e in enumerate(pool):
            if j in used:
                continue
            dt = float(e["time"]) - w
            if not (-1.0 <= dt <= 30.0):
                continue
            if tin is not None and e.get("prompt_tokens") not in (None, tin):
                continue
            if best is None or abs(dt) < best[1]:
                best = (j, abs(dt))
        if best is None:
            continue
        used.add(best[0])
        e = pool[best[0]]
        pm = max(0.0, float(e.get("prompt_ms") or 0.0) / 1000.0)
        ft = float(e.get("first_engine_token_s") or 0.0)
        snaps[i]["arr"] = round(float(e["time"]) - t0_wall, 2)
        snaps[i]["p"] = round(pm, 2)
        if ft > 0:
            snaps[i]["q"] = round(max(0.0, ft - pm), 2)
        hit += 1
    return hit


def _stats(vals: list[float]) -> dict[str, float | None]:
    if not vals:
        return {"avg": None, "p50": None, "p90": None, "p99": None, "min": None, "max": None}
    vs = sorted(vals)
    return {"avg": round(sum(vs) / len(vs), 4), "p50": round(_pct(vs, 0.5), 4),
            "p90": round(_pct(vs, 0.9), 4), "p99": round(_pct(vs, 0.99), 4),
            "min": round(vs[0], 4), "max": round(vs[-1], 4)}


def _div(a: float, b: float) -> float | None:
    return round(a / b, 3) if b > 0 else None


# ---------------------------------------------------------------- manager


class BenchManager:
    """ベンチマークジョブの作成・実行・履歴管理。同時実行走行は 1 つに制限する
    (同一 GPU 上の計測同士が互いの結果を狂わせるため)。"""

    def __init__(self, store: JsonStore, profiles_store: JsonStore, manager):
        self.store = store
        self.profiles = profiles_store
        self.manager = manager
        self.active_id: str | None = None
        self._cancel: set[str] = set()
        self._tasks: dict[str, asyncio.Task] = {}
        self._last_save: dict[str, float] = {}
        self._dead: set[str] = set()     # 履歴整理で消えた run の id (以後保存しない)
        # 同じマシン (GPU) を独占する他の実走との排他。キャリブレーションは
        # エンジンを起動して GPU を独占するので、ベンチと重なると互いの結果が壊れる。
        # app.py が CalibrationManager へ同じロックを渡す。
        self.exclusive = ExclusiveLock()

    @property
    def exclusive_owner(self) -> str:
        return self.exclusive.owner

    # ------------------------------------------------------------ public

    def on_startup(self) -> None:
        """launcher 再起動で実行中だったジョブは実体を失っているので印を付ける。

        ベンチのために自動起動したサーバーが動きっぱなしで残ると、次に計測したときに
        前の設定のサーバーを測ってしまう (自動停止の意思が失われる) ため、
        auto_started のセッションがまだ生きていれば stopped_after の代わりに
        「停止できなかった / 手動で止めてください」を理由に残す。停止そのものは
        非同期処理なので on_startup では印だけ付ける。"""
        changed = False
        for r in self.store.load():
            if r.get("status") in ("running", "queued"):
                r["status"] = "lost"
                r["error"] = "llm-launcher の再起動中に実行が中断されました"
                r["finished_at"] = r.get("finished_at") or now()
                if r.get("auto_started") and not r.get("stopped_after"):
                    sid = r.get("session_id")
                    sess = self.manager.sessions.get(sid) if sid else None
                    if sess and sess.get("status") in ("running", "starting", "stopping"):
                        r["error"] += (" / 自動起動したサーバーが残っています "
                                       "(必要ならサーバータブで停止してください)")
                        r["server_left_running"] = True
                changed = True
        if changed:
            self.store.save()

    def list_runs(self) -> list[dict[str, Any]]:
        return sorted(self.store.load(), key=lambda r: r.get("created_at") or 0, reverse=True)

    def get(self, rid: str) -> dict[str, Any] | None:
        return next((r for r in self.store.load() if r["id"] == rid), None)

    async def start(self, profile: dict[str, Any], params: dict[str, Any],
                    backend_name: str) -> dict[str, Any]:
        if self.active_id:
            raise BenchError("他のベンチマークが実行中です。完了を待つか取り消してください")
        if not self.claim_exclusive(f"bench:{profile['id']}"):
            raise BenchError("キャリブレーションが実行中です。完了を待つか中断してください"
                             " (同じ GPU を使うため同時に実走できません)")
        run = {
            "id": new_id("b"),
            "created_at": now(),
            "started_at": None,
            "finished_at": None,
            "status": "queued",
            "error": "",
            "profile_id": profile["id"],
            "profile_name": profile.get("name", ""),
            "backend": profile.get("backend", ""),
            "backend_name": backend_name,
            "model": params.get("model") or "",
            "session_id": None,
            "port": None,
            "auto_started": False,
            "stopped_after": False,
            "stop_error": "",
            "params": params,
            "progress": {"stage": 0, "stages": list(params["concurrency"]), "phase": "queued",
                         "done": 0, "total": params["num_prompts"], "ok": 0, "fail": 0,
                         "out_tokens": 0, "dec_sum": 0.0, "dec_n": 0, "elapsed": 0},
            "stages": [],
        }
        try:
            self.store.mutate(lambda lst: lst.append(run))
            self._trim()
            self.active_id = run["id"]
            self._tasks[run["id"]] = asyncio.create_task(self._job(run, profile, params))
        except BaseException:
            self.release_exclusive()
            raise
        return run

    def claim_exclusive(self, owner: str) -> bool:
        """このマシンの実走 (ベンチ / キャリブレーション) を 1 本に制限する。"""
        return self.exclusive.acquire(owner)

    def release_exclusive(self) -> None:
        self.exclusive.release()

    def cancel(self, rid: str) -> None:
        run = self.get(rid)
        if not run:
            raise KeyError(rid)
        if run.get("status") not in ("queued", "running"):
            raise BenchError("このベンチマークは取り消せません (完了済み)")
        self._cancel.add(rid)

    def delete(self, rid: str) -> None:
        run = self.get(rid)
        if not run:
            raise KeyError(rid)
        if run.get("status") in ("queued", "running"):
            raise BenchError("実行中のベンチマークは削除できません")

        def _d(lst: list) -> None:
            lst[:] = [r for r in lst if r["id"] != rid]
        self._dead.add(rid)
        self._last_save.pop(rid, None)
        self.store.mutate(_d)

    async def shutdown(self) -> None:
        for rid in list(self._tasks):
            self._cancel.add(rid)
            self._tasks[rid].cancel()

    # ------------------------------------------------------------- internals

    @staticmethod
    async def _one_in_thread(base: str, path: str, body: dict[str, Any], stream: bool,
                             timeout: float, auth: dict[str, str] | None,
                             stop: threading.Event,
                             cancelled: "Callable[[], bool] | None" = None,
                             live: "dict[str, float] | None" = None) -> dict[str, Any]:
        """_one_request を 1 本のスレッドで実行して結果を返す。

        stop は中断シグナル (ソケット shutdown で読みを起こす)。cancelled は
        「この run がキャンセル済みか」を返す述語で、True になったら stop を立てて
        ワーカー側の待ちを打ち切る (ワーカーは await 中でループ先頭に戻れないため)。
        ThreadPoolExecutor を使わないのは、中断時に「まだ動いているリクエスト」を
        待ち切る手段が必要だから。daemon スレッドなら stop で SSE の行境界から抜け、
        待ち切れなくてもプログラム終了を妨げない。"""
        q: "_queue.Queue[dict[str, Any] | BaseException]" = _queue.Queue(maxsize=1)

        def run() -> None:
            try:
                q.put(_one_request(base, path, body, stream, timeout, auth, stop, live=live))
            except BaseException as e:  # noqa: BLE001
                q.put(e)

        threading.Thread(target=run, name="bench-req", daemon=True).start()
        while True:
            try:
                r = q.get_nowait()
            except _queue.Empty:
                # 中断: スレッドはソケット shutdown で抜けるが、応答が来ない/遅い
                # サーバーでも「ワーカーは即座に止まる」必要があるため、ここでも
                # stop を見て打ち切る (スレッドは daemon なので残らない)。
                # cancelled() は run のキャンセル集合を直接見る述語で、_stage の
                # ワーカーが await 中でも中断を検出できるようにする。
                if stop.is_set() or (cancelled is not None and cancelled()):
                    stop.set()
                    return {"ok": False, "error": "中断", "secs": 0.0, "cancelled": True}
                await asyncio.sleep(0.005)      # 数 ms 粒度で待つ (トークン間隔を歪めない)
                continue
            if isinstance(r, BaseException):
                return {"ok": False, "error": f"{type(r).__name__}: {r}",
                        "secs": 0.0, "cancelled": True}
            return r

    async def _parallel(self, body_of, run: dict[str, Any], indexes, base: str, path: str,
                        conc: int, stream: bool, timeout: float,
                        auth: dict[str, str] | None) -> None:
        """warmup 用: 最大 conc 本の同時実行で全件を流す (結果は捨てる・統計外)。

        stop はこの呼び出し専用に作る (呼び出し元の測定用 Event を立てると、
        測定開始前に「中断済み」になる)。"""
        stop = threading.Event()
        idx = [0]
        seq = list(indexes)
        lock = threading.Lock()

        def take() -> int | None:
            with lock:
                i = idx[0]
                idx[0] += 1
            return seq[i] if i < len(seq) else None

        async def worker() -> None:
            while True:
                if run["id"] in self._cancel:
                    stop.set()
                    return
                i = take()
                if i is None:
                    return
                await self._one_in_thread(base, path, body_of(i), stream, timeout, auth, stop)

        try:
            await asyncio.gather(*(worker() for _ in range(max(1, conc))))
        finally:
            stop.set()
        self._check_cancel(run)

    def _trim(self) -> None:
        def _t(lst: list) -> None:
            if len(lst) > MAX_RUNS:
                keep = {r["id"] for r in sorted(lst, key=lambda r: r.get("created_at") or 0,
                                                reverse=True)[:MAX_RUNS]}
                keep |= {r["id"] for r in lst if r.get("status") in ("queued", "running")}
                gone = [r["id"] for r in lst if r["id"] not in keep]
                lst[:] = [r for r in lst if r["id"] in keep]
                # 消えた run は以後の _save で書き戻さない (進行中の task がまだ
                # その dict を持っていても、履歴に復活させない)
                self._dead.update(gone)
                for rid in gone:
                    self._last_save.pop(rid, None)
            # 経時記録 (series) で 1 件あたりの大きさがぶれるため、件数に加えて
            # 概算シリアライズサイズでも抑える (古い完了分から落とす)
            total = len(json.dumps(lst, ensure_ascii=False))
            old = [r for r in sorted(lst, key=lambda r: r.get("created_at") or 0)
                   if r.get("status") not in ("queued", "running")
                   and r.get("id") != self.active_id]
            dead: set[str] = set()
            for r in old:
                if total <= MAX_STORE_BYTES:
                    break
                rid = r.get("id")
                dead.add(rid)
                total -= len(json.dumps(r, ensure_ascii=False)) + 6
                self._dead.add(rid)
                self._last_save.pop(rid, None)
            if dead:
                lst[:] = [r for r in lst if r.get("id") not in dead]
        self.store.mutate(_t)

    def _save(self, run: dict[str, Any], force: bool = False) -> None:
        rid = run["id"]
        if rid in self._dead:
            return                       # 削除/整理済みの run はディスクへ戻さない
        t = time.monotonic()
        if not force and t - self._last_save.get(rid, 0.0) < SAVE_THROTTLE:
            return
        self._last_save[rid] = t
        with contextlib.suppress(OSError):
            self.store.save()

    def clear_all(self) -> int:
        """完了 / 失敗 / 中断 / lost の履歴を一括削除して削除件数を返す。

        実行中・待機中の run はメモリ上のタスクが保持し続けているので残す
        (消すと _save で復活し、進捗の書き込み先も失われる)。"""
        removed: list[str] = []

        def _c(lst: list) -> None:
            keep: list = []
            for r in lst:
                rid = r.get("id")
                if r.get("status") in ("queued", "running") or rid == self.active_id:
                    keep.append(r)
                else:
                    removed.append(rid)
            lst[:] = keep
            self._dead.update(removed)            # 生き返り (保存) 防止は delete と同じ
            for rid in removed:
                self._last_save.pop(rid, None)

        self.store.mutate(_c)
        return len(removed)

    def _progress(self, run: dict[str, Any], **kw: Any) -> None:
        run["progress"].update(kw)

    def _check_cancel(self, run: dict[str, Any]) -> None:
        """中断要求があれば _Cancelled を投げる (ステージ遷移の各所で使う)。"""
        if run["id"] in self._cancel:
            raise _Cancelled()

    def _cancelled(self, run: dict[str, Any]) -> bool:
        """この run が取り消し済みか (ワーカーの await 中チェック / 集計の門番)。"""
        return run["id"] in self._cancel

    async def _job(self, run: dict[str, Any], profile: dict[str, Any],
                   params: dict[str, Any]) -> None:
        try:
            await self._run(run, profile, params)
            run["status"] = "cancelled" if run["id"] in self._cancel else "completed"
        except _Cancelled:
            run["status"] = "cancelled"
        except asyncio.CancelledError:
            run["status"] = "cancelled"
        except BenchError as e:
            run["status"] = "failed"
            run["error"] = str(e)
        except Exception as e:  # noqa: BLE001
            run["status"] = "failed"
            run["error"] = f"{type(e).__name__}: {e}"
        finally:
            run["finished_at"] = now()
            self._progress(run, phase="done")
            self._save(run, force=True)
            self.active_id = None
            self._cancel.discard(run["id"])
            self._tasks.pop(run["id"], None)
            # 排他の解放は「停止処理より先」に行う。停止に時間がかかるバックエンド
            # (Strata のエンジン解放待ち等) の間、次のベンチ/キャリブレーションを
            # 待たせ続けないため (計測対象の GPU は停止で解放される前に空かない)。
            self.release_exclusive()
            # 自動起動したサーバーの停止: 完了時だけでなく中断/失敗時も、ユーザーが
            # 「完了後停止」を選んでいたら止める (残すと次回の計測が別設定のまま走る)。
            if ((params.get("stop_after") or run.get("status") in ("failed", "cancelled"))
                    and run.get("auto_started") and run.get("session_id")
                    and not run.get("stopped_after")):
                try:
                    await self.manager.stop(run["session_id"])
                    run["stopped_after"] = True
                except Exception as e:  # noqa: BLE001
                    # 握り潰すと「停止したつもり」で次回の計測が別設定のまま走る。
                    # 失敗理由を残して UI / JSON エクスポートに出す。
                    run["stop_error"] = f"{type(e).__name__}: {e}"
                self._save(run, force=True)

    async def _run(self, run: dict[str, Any], profile: dict[str, Any],
                   params: dict[str, Any]) -> None:
        run["started_at"] = now()
        sess = await self._resolve_session(run, profile, params)
        port = sess.get("port")
        if not port:
            raise BenchError("このセッションはポートを解決できていません "
                             "(プロファイルの port 設定を確認してください)")
        base = f"http://127.0.0.1:{port}"
        auth = auth_headers(sess)
        run["port"] = port
        self._progress(run, phase="waiting")
        await self._wait_ready(run, base, sess.get("health_path") or "/health",
                               int(params["ready_timeout"]), sess["id"], auth)
        model = params.get("model") or await self._resolve_model(base, auth)
        run["model"] = model
        path = "/v1/chat/completions" if params["endpoint"] == "chat" else "/v1/completions"
        seed0 = int(hashlib.md5(run["id"].encode()).hexdigest()[:8], 16)
        for si, conc in enumerate(params["concurrency"]):
            self._check_cancel(run)
            stage = await self._stage(run, si, base, path, model, params, conc,
                                      seed0 + si * PROMPT_SEED_GAP, auth)
            run["stages"].append(stage)
            self._save(run, force=True)
            if stage["ok"] == 0 and not stage.get("aborted"):
                # 中断 (キャンセル) による 0 件は失敗ではない (stage に aborted が立つ)
                errs = "; ".join(stage["errors"][:2])
                raise BenchError(f"並列度 {conc} で成功リクエストが 0 件です。"
                                 f"{(' ' + errs) if errs else ''}")
            if run["id"] in self._cancel:
                break

    async def _resolve_session(self, run: dict[str, Any], profile: dict[str, Any],
                               params: dict[str, Any]) -> dict[str, Any]:
        stopping = next((s for s in self.manager.sessions.values()
                         if s["profile_id"] == profile["id"] and s["status"] == "stopping"),
                        None)
        if stopping:
            raise BenchError("サーバーが停止中です。少し待ってから再実行してください")
        live = next((s for s in self.manager.sessions.values()
                     if s["profile_id"] == profile["id"] and s["status"] in ("running", "starting")),
                    None)
        if not live:
            if not params.get("auto_start", True):
                raise BenchError("プロファイルが起動していません。起動タブでスタートするか、"
                                 "「起動済みでない場合は自動起動」を ON にしてください")
            self._progress(run, phase="launching")
            self._save(run, force=True)
            try:
                live = await self.manager.start(profile, via=VIA_BENCH)
                run["auto_started"] = True
                # 起動直後から stopping の窓 (エンジン解放待ち等) があるので記録する
                run["session_id"] = live["id"]
            except MemoryGuardError as e:
                # メモリガード / 解放待ちの拒否はユーザーが対処できるよう理由をそのまま出す
                raise BenchError(f"サーバーの起動に失敗しました: {e}") from e
            except RuntimeError as e:
                raise BenchError(f"サーバーの起動に失敗しました: {e}") from e
        run["session_id"] = live["id"]
        return live

    async def _wait_ready(self, run: dict[str, Any], base: str, health: str,
                          timeout: int, sid: str,
                          auth: dict[str, str] | None = None) -> None:
        t0 = time.monotonic()
        while True:
            self._check_cancel(run)
            s = self.manager.sessions.get(sid)
            if s and s.get("status") not in ("running", "starting"):
                raise BenchError("サーバーが起動途中で終了しました。サーバータブのログを確認してください")
            if await asyncio.to_thread(_health_ok, f"{base}{health}", auth):
                return
            if time.monotonic() - t0 > timeout:
                raise BenchError(f"{timeout}s 経ってもサーバーが health に応答しません。"
                                 "サーバータブのログを確認してください")
            await asyncio.sleep(1.5)

    async def _resolve_model(self, base: str,
                             auth: dict[str, str] | None = None) -> str:
        for i in range(3):
            mid = await asyncio.to_thread(_first_model, base, auth)
            if mid:
                return mid
            if i < 2:
                await asyncio.sleep(2.0)
        raise BenchError("モデル名を解決できません (/v1/models 応答なし)。"
                         "モデル欄に明示指定してください")

    def _payload(self, params: dict[str, Any], model: str, prompt: str) -> dict[str, Any]:
        body: dict[str, Any] = {"max_tokens": params["output_len"], "stream": params["stream"]}
        if params.get("temperature") is not None:
            body["temperature"] = params["temperature"]
        # Decode-length controls.  min_tokens / ignore_eos are the OpenAI-compatible extensions most
        # servers take (vLLM, llama.cpp); a server that does not know them ignores the keys, and one
        # that refuses them fails the run with the server's own message.  0 / false sends nothing.
        if int(params.get("min_tokens") or 0) > 0:
            body["min_tokens"] = int(params["min_tokens"])
        if params.get("ignore_eos") is True:
            body["ignore_eos"] = True
        if params["stream"]:
            body["stream_options"] = {"include_usage": True}
        if model:
            body["model"] = model
        if params["endpoint"] == "chat":
            body["messages"] = [{"role": "user", "content": prompt}]
        else:
            body["prompt"] = prompt
        return body

    async def _stage(self, run: dict[str, Any], si: int, base: str, path: str, model: str,
                     params: dict[str, Any], conc: int, seed: int,
                     auth: dict[str, str] | None = None) -> dict[str, Any]:
        n = int(params["num_prompts"])
        warm = int(params["warmup"])
        stream = bool(params["stream"])
        timeout = float(params["request_timeout"])
        # 同時に張りすぎるとサーバー側の backlog や GIL 争奪で計測自体が歪む
        # (128 並列で偽サーバーが Connection reset を返した)。実運用の GPU サーバーも
        # これ以上は捌けないので、同時接続を 64 で頭打ちにする。
        conc = max(1, min(int(conc), 64))
        progress = run["progress"]
        self._progress(run, stage=si, phase="warmup", done=0, total=n, ok=0, fail=0,
                       out_tokens=0, dec_sum=0.0, dec_n=0, elapsed=0)
        self._save(run, force=True)

        def make_body(i: int, warmup: bool = False) -> dict[str, Any]:
            # warmup は測定本体と別のプロンプト列にする (同じだと 1 件目だけが
            # prefix cache に載って有利になり、README の「重複なし」と矛盾する)。
            base_seed = seed + (WARMUP_SEED_OFFSET if warmup else 0)
            prompt = make_prompt(int(params["input_len"]), base_seed + i)
            return self._payload(params, model, prompt)

        if warm:
            await self._parallel(lambda i: make_body(i, True), run, range(warm), base, path,
                                 min(warm, 4), stream, timeout, auth)

        self._check_cancel(run)
        idx = [0]
        results: list[dict[str, Any]] = []
        aborted = {"v": False}
        t0 = time.perf_counter()
        t0_wall = time.time()   # サーバー側の時刻 (epoch) と突き合わせる基準時計
        self._progress(run, stage=si, phase="measure", done=0, total=n, ok=0, fail=0,
                       out_tokens=0, dec_sum=0.0, dec_n=0, elapsed=0)
        self._save(run, force=True)
        # 測定用の stop は必ず新しい Event にする。_parallel (warmup) が finally で
        # 自分の stop を立てるので、それを共有すると測定開始時点で「中断済み」になる。
        # 束縛済みの既定引数 (k) で受けるのは、ワーカー/監視スレッドが閉包変数
        # `stop` の再束縛を読むタイミングで取り違えないようにするため。
        # stop は中断シグナル: ワーカーは待機中に、実行中のリクエストはソケットの
        # shutdown 監視スレッドがこれを見て、どちらも即座に抜ける。
        stop = threading.Event()

        # 経時記録: measure 中 1 秒ごとにサンプル点を積み、完了時に stage へ写す。
        # 各リクエストは自分のスロット (slots の 1 要素) をストリーム到着刻みに
        # 増やし、サンプラーはその合計の 1 秒あたり増分 = agg (生成 tok/s 合算) を
        # 記録する。従来の win (完了確定分の累積差分) は旧データ互換のため残す。
        series: list[dict[str, Any]] = []
        slots: list[dict[str, float]] = []   # リクエスト 1 本 = 1 スロット (単一書き手)
        live = {"inf": 0}                    # インフライト数 (ワーカーが更新)
        run["progress"]["series"] = series
        iv = {"v": SAMPLE_EVERY, "dec": False}

        mode = str(params.get("mode") or "loop")
        gap_s = float(params.get("gap_s") or 2.0)

        def _snap(sl: dict[str, Any]) -> dict[str, Any]:
            # フェーズ記録 (段開始からの秒)。未完成のキーは None のまま = ライブで伸びる行
            z = lambda v: round(v - t0, 2) if isinstance(v, float) else None
            out = {"s": z(sl.get("s")), "a": z(sl.get("a")), "d": z(sl.get("d")),
                   "e": z(sl.get("e")), "n": int(sl.get("f") or 0)}
            gl = sl.get("gl")
            if isinstance(gl, list) and gl:
                # 停止区間 (閾値以上のチャンク間ギャップ) — ライブでも増え続ける
                out["g"] = [[z(a), z(b)] for a, b in list(gl)[:100]]
            st = sl.get("st")
            if isinstance(st, float):
                out["st"] = round(st, 3)
            return out

        async def _sampler() -> None:
            last_t, last_tok, last_gen = 0.0, 0, 0.0
            while True:
                await asyncio.sleep(iv["v"])
                p = run["progress"]
                t = time.perf_counter() - t0
                tok = int(p.get("out_tokens") or 0)
                gen = sum(s["f"] for s in slots)   # 届いたトークンの総量 (ストリーム実測)
                dt = max(t - last_t, 0.2)
                dn = int(p.get("dec_n") or 0)
                series.append({"t": round(t, 2), "done": int(p.get("done") or 0),
                               "ok": int(p.get("ok") or 0), "fail": int(p.get("fail") or 0),
                               "tok": tok, "inf": live["inf"],
                               "win": round((tok - last_tok) / dt, 1),
                               "agg": round((gen - last_gen) / dt, 1),
                               "avg": round(tok / max(t, 0.2), 1),
                               "dec": round(float(p.get("dec_sum") or 0.0) / dn, 1) if dn else None})
                last_t, last_tok, last_gen = t, tok, gen
                p["reqs"] = [_snap(sl) for sl in slots]   # ライブのフェーズタイムライン
                if len(series) > MAX_SAMPLES:
                    del series[1::2]         # 偶数番目を残して等間隔のまま半分に間引く
                    iv["v"] *= 2.0
                    iv["dec"] = True
                self._save(run)              # throttle 越しに 2 秒刻みでディスクへ

        async def _do(i: int, k: threading.Event) -> None:
            # リクエスト 1 本を実行してスロット/結果/進捗を更新する (loop と stagger で共通)
            # リクエストは 1 本ずつ daemon スレッドで走らせる。中断は
            # _one_in_thread 側が run のキャンセル集合も見て打ち切る。
            live["inf"] += 1
            # フェーズ追跡スロット: s=送信 a=最初のトークン d=最後のトークン
            # e=確定(ストリームクローズ/応答読み切り) f=届いたトークン推定。
            # 1 スロットの各キーは単一の書き手だけが更新する (s/e=ワーカー, a/d/f/g=読み手)。
            slot: dict[str, Any] = {"f": 0.0, "s": time.perf_counter(), "w": time.time()}
            slots.append(slot)
            try:
                r = await self._one_in_thread(base, path, make_body(i), stream, timeout, auth, k,
                                              lambda: self._cancelled(run), live=slot)
            finally:
                live["inf"] -= 1
                slot["e"] = time.perf_counter()
            if r.get("ok") and r.get("in_tokens") is not None:
                slot["in"] = int(r["in_tokens"])   # サーバー内訳との突合キー
            # 中断後に戻ってきた結果 (打ち切り or たまたま完了) は集計に入れない。
            # ここを素通りさせると「中断したのに ok=N」が保存される。
            if r.get("cancelled") or self._cancelled(run):
                k.set()
                aborted["v"] = True
                return
            results.append(r)
            prog = run["progress"]
            prog["done"] += 1
            if r.get("ok"):
                prog["ok"] += 1
                prog["out_tokens"] += int(r.get("out_tokens") or 0)
                # decode tok/s のライブ値: 確定値と同じ「リクエスト毎の算術平均」を
                # 完了のつど積む (dec_sum/dec_n → stage 終了時の decode_tps.avg に収束)。
                # 出力 tok/s (累積) は prefill 待ち込みで過小になるため、
                # UI の主指標はこっち (生成中の実速度) を見せる
                dt = (r.get("secs") or 0.0) - (r.get("ttft") or 0.0)
                if r.get("ttft") is not None and dt > 0.05 and r.get("out_tokens"):
                    prog["dec_sum"] = float(prog.get("dec_sum") or 0.0) + int(r["out_tokens"]) / dt
                    prog["dec_n"] = int(prog.get("dec_n") or 0) + 1
            else:
                prog["fail"] += 1
            prog["elapsed"] = time.perf_counter() - t0
            self._save(run)

        async def worker(k: threading.Event = stop) -> None:
            while True:
                if aborted["v"] or self._cancelled(run):
                    k.set()
                    aborted["v"] = True
                    return
                i = idx[0]
                idx[0] += 1
                if i >= n:
                    return
                await _do(i, k)

        async def arrival(i: int, k: threading.Event = stop) -> None:
            # インタリーブ: i 本目を段開始 + i*間隔秒で送信する。先行する本がデコード中
            # に新しいプロンプトが到着する構成で、届いた待ち・走行中の停止という
            # 干渉そのものが計測対象。待機中は 50ms 刻みで中断を確認する。
            target = t0 + i * gap_s
            while True:
                if aborted["v"] or self._cancelled(run):
                    k.set()
                    aborted["v"] = True
                    return
                rem = target - time.perf_counter()
                if rem <= 0:
                    break
                await asyncio.sleep(min(rem, 0.05))
            await _do(i, k)

        samp = asyncio.create_task(_sampler())
        try:
            try:
                if mode == "stagger":
                    await asyncio.gather(*(arrival(i) for i in range(conc)))
                else:
                    await asyncio.gather(*(worker() for _ in range(conc)))
            except asyncio.CancelledError:
                # shutdown からのキャンセル: インフライトも一緒に畳んでから抜ける
                stop.set()
                raise
        finally:
            samp.cancel()
            run["progress"].pop("series", None)   # ライブ用の写しは stage 側に残す
            run["progress"].pop("reqs", None)
        wall = time.perf_counter() - t0

        # サーバー側の内訳 (キュー待ち / プレフィル) を突合して各行に付与する。
        # 対応サーバー (/metrics JSON) だけ。非対応は None のままで、待ちブロックは
        # 従来どおり琥珀の単一区間 (実測 TTFT) として正直に表示される。
        allsnaps = [_snap(sl) for sl in slots]
        pm_kind: str | None = None
        if stream:
            pool = await asyncio.to_thread(_fetch_phase_pool, base)
            if pool is not None:
                pm_kind = "strata"
                _attach_phase_metrics(allsnaps, slots, pool, t0_wall)

        okres = [r for r in results if r.get("ok")]
        errors: list[str] = []
        for r in results:
            if not r.get("ok"):
                msg = r.get("error") or ""
                if msg and msg not in errors:
                    errors.append(msg)
                if len(errors) >= 5:
                    break
        lat = [float(r["secs"]) for r in okres]
        ttfts = [float(r["ttft"]) for r in okres if r.get("ttft") is not None]
        decodes = [r["out_tokens"] / (r["secs"] - r["ttft"]) for r in okres
                   if r.get("ttft") is not None and (r["secs"] - r["ttft"]) > 0.05
                   and r.get("out_tokens")]
        # decode tok/s Σ (全並列合算): decode_tps.avg が「1 ストリームの速さ」なのに対し、
        # これは「ストリームが動いていた区間全体で合算何 tok/s 出ていたか」。
        # Σトークン ÷ (最初のトークン到着 → 最後のトークン到着) で、毎秒チャート
        # (全並列合算) と同じ軸の数字になる。avg × N は後続待ちのアイドルを無視した
        # 概算なので使わない (非 stream = a/d 無しは None)。
        ad = [(float(sl["a"]), float(sl["d"])) for sl in slots
              if isinstance(sl.get("a"), float) and isinstance(sl.get("d"), float)
              and sl["d"] > sl["a"]]
        dec_tok = sum(int(r.get("out_tokens") or 0) for r in okres if r.get("ttft") is not None)
        dec_span = (max(d for _, d in ad) - min(a for a, _ in ad)) if ad else None
        dec_sum_tps = round(dec_tok / dec_span, 3) if dec_span and dec_span > 0.05 and dec_tok else None
        e2e = [r["out_tokens"] / r["secs"] for r in okres
               if r.get("out_tokens") and r["secs"] > 0]
        gaps = [float(r["gap_max"]) for r in okres if r.get("gap_max") is not None]
        stall_count = sum(int(r.get("stalls") or 0) for r in okres)
        out_tok = sum(int(r.get("out_tokens") or 0) for r in okres)
        # 入力トークン: usage を返さないサーバーでは prompt_tokens が None になる。
        # 一部だけ 0 で埋めて合計すると total tok/s が過小に出るため、全件揃ったときだけ
        # 合計する (欠けていれば total_tps は None = 「測れない」)。
        ins = [r.get("in_tokens") for r in okres]
        in_tok = (sum(int(v) for v in ins)
                  if ins and all(isinstance(v, int) and v > 0 for v in ins) else None)
        return {
            "concurrency": conc, "num_prompts": n, "warmup": warm,
            # 送信方式: loop = 空いた分を即時送信 / stagger = デコード中に gap_s 間隔で送信
            "mode": mode, "gap_s": round(gap_s, 3) if mode == "stagger" else None,
            "ok": len(okres), "fail": len(results) - len(okres), "aborted": aborted["v"],
            "wall_secs": round(wall, 2),
            "rps": _div(len(okres), wall),
            "out_tps": _div(out_tok, wall),
            "total_tps": _div(out_tok + in_tok, wall) if in_tok is not None else None,
            "out_tokens": out_tok, "in_tokens": in_tok,
            "tokens_estimated": any(r.get("estimated") for r in okres),
            "latency": _stats(lat), "ttft": _stats(ttfts),
            # decode tok/s は「TTFT 後 ~ 終了」の実測区間で割る。非ストリーム
            # (ttft=None) は区間が無いので測れない (表では "-" になる)。
            "decode_tps": _stats(decodes), "decode_tps_sum": dec_sum_tps, "e2e_tps": _stats(e2e),
            "gaps": _stats(gaps),
            "stall_worst_s": round(max(gaps), 3) if gaps else None,
            "stall_count": stall_count,
            # 閾値以上の停止区間の合計秒 (reqs[].st の総和) — 比較バーの停止セグメントと同系統
            "stall_secs": round(sum(float(sl.get("st") or 0.0) for sl in slots), 2),
            "stall_threshold_s": STALL_S,
            "errors": errors,
            # 経時記録 (グラフ用)。samples 間隔は iv["v"] (上限で間引くと拡大)。
            "series": series,
            "series_decimated": iv["dec"],
            # リクエスト単位のフェーズ記録 (スイムレーン用)。send→first→last→確定。
            # 対応サーバーでは arr/q/p (到着・キュー待ち・プレフィル秒) が付く。
            # 多すぎる場合は等間隔に間引く (reqs_truncated)
            "reqs": allsnaps[::max(1, -(-len(allsnaps) // MAX_REQS))],
            "reqs_truncated": len(slots) > MAX_REQS,
            "phase_metrics": pm_kind,
        }
