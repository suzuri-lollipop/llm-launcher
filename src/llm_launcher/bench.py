"""組み込みベンチマーク: プロファイル指定のサーバー (OpenAI 互換 API) へ
launcher から負荷をかけ、スループット / TTFT / レイテンシ分布を計測する。

追加依存なし (urllib + スレッドプール)。イベントループ上のワーカー協調体が
HTTP 実体をスレッドで走らせ、並列度リストの各値を 1 ステージとして逐次実行する。
各ステージの前に warmup リクエスト (統計外) を置ける。結果は data/bench.json に
履歴として永続化し、実行中は progress をライブ更新する。"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import math
import random
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .store import JsonStore, new_id, now

MAX_RUNS = 50        # 履歴として保持する最大件数 (完了分のみ整理)
SAVE_THROTTLE = 2.0  # 進行状況の永続化間隔 (秒)


class BenchError(Exception):
    """ユーザーに提示するベンチマーク失敗。"""


class _Cancelled(Exception):
    """キャンセル検出 (内部シグナル)。"""


# ------------------------------------------------------------------ prompt

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


def _health_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2.0) as r:
            return 200 <= r.status < 400
    except OSError:
        return False


def _first_model(base: str) -> str | None:
    try:
        with urllib.request.urlopen(f"{base}/v1/models", timeout=8) as r:
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
                 stream: bool, timeout: float) -> dict[str, Any]:
    """OpenAI 互換リクエストを 1 本実行し、所要時間・TTFT・トークン数を返す。

    例外は投げず ok/error で結果を返す (ワーカー側を集計しやすくするため)。"""
    t0 = time.perf_counter()
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream" if stream else "application/json"},
        method="POST",
    )

    def _fail(msg: str) -> dict[str, Any]:
        return {"ok": False, "error": msg[:300], "secs": time.perf_counter() - t0}

    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        snippet = ""
        with contextlib.suppress(OSError):
            snippet = e.read().decode("utf-8", "replace")[:200].replace("\n", " ")
        return _fail(f"HTTP {e.code} {snippet}".strip())
    except OSError as e:
        return _fail(f"{type(e).__name__}: {e}")

    ttf: float | None = None
    usage: dict[str, Any] = {}
    text_parts: list[str] = []
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
            return {"ok": True, "secs": time.perf_counter() - t0, "ttft": None,
                    "out_tokens": int(out) if out is not None else est,
                    "in_tokens": usage.get("prompt_tokens"),
                    "estimated": "completion_tokens" not in usage or "prompt_tokens" not in usage}
        for raw in resp:  # SSE 行読み (タイムアウトはソケット読み取りに適用)
            line = raw.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
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
                if ttf is None:
                    ttf = time.perf_counter() - t0
                text_parts.append(piece)
    est = est_tokens("".join(text_parts))
    out = usage.get("completion_tokens")
    out = int(out) if out is not None else est
    if not out and est:
        out = est
    return {"ok": True, "secs": time.perf_counter() - t0, "ttft": ttf,
            "out_tokens": out, "in_tokens": usage.get("prompt_tokens"),
            "estimated": "completion_tokens" not in usage or "prompt_tokens" not in usage}


# ------------------------------------------------------------------ stats


def _pct(vals: list[float], q: float) -> float:
    k = max(0, min(len(vals) - 1, math.ceil(q * len(vals)) - 1))
    return vals[k]


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

    # ------------------------------------------------------------ public

    def on_startup(self) -> None:
        """launcher 再起動で実行中だったジョブは実体を失っているので印を付ける。"""
        changed = False
        for r in self.store.load():
            if r.get("status") in ("running", "queued"):
                r["status"] = "lost"
                r["error"] = "llm-launcher の再起動中に実行が中断されました"
                r["finished_at"] = r.get("finished_at") or now()
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
            "params": params,
            "progress": {"stage": 0, "stages": list(params["concurrency"]), "phase": "queued",
                         "done": 0, "total": params["num_prompts"], "ok": 0, "fail": 0,
                         "out_tokens": 0, "elapsed": 0},
            "stages": [],
        }
        self.store.mutate(lambda lst: lst.append(run))
        self._trim()
        self.active_id = run["id"]
        self._tasks[run["id"]] = asyncio.create_task(self._job(run, profile, params))
        return run

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
        self.store.mutate(_d)

    async def shutdown(self) -> None:
        for rid in list(self._tasks):
            self._cancel.add(rid)
            self._tasks[rid].cancel()

    # ------------------------------------------------------------- internals

    def _trim(self) -> None:
        def _t(lst: list) -> None:
            if len(lst) <= MAX_RUNS:
                return
            keep = {r["id"] for r in sorted(lst, key=lambda r: r.get("created_at") or 0,
                                            reverse=True)[:MAX_RUNS]}
            keep |= {r["id"] for r in lst if r.get("status") in ("queued", "running")}
            lst[:] = [r for r in lst if r["id"] in keep]
        self.store.mutate(_t)

    def _save(self, run: dict[str, Any], force: bool = False) -> None:
        t = time.monotonic()
        if not force and t - self._last_save.get(run["id"], 0.0) < SAVE_THROTTLE:
            return
        self._last_save[run["id"]] = t
        with contextlib.suppress(OSError):
            self.store.save()

    def _progress(self, run: dict[str, Any], **kw: Any) -> None:
        run["progress"].update(kw)

    def _check_cancel(self, run: dict[str, Any]) -> None:
        if run["id"] in self._cancel:
            raise _Cancelled()

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
            if (params.get("stop_after") and run.get("auto_started")
                    and run.get("session_id")):
                with contextlib.suppress(Exception):
                    await self.manager.stop(run["session_id"])
                    run["stopped_after"] = True
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
        run["port"] = port
        self._progress(run, phase="waiting")
        await self._wait_ready(run, base, sess.get("health_path") or "/health",
                               int(params["ready_timeout"]), sess["id"])
        model = params.get("model") or await self._resolve_model(base)
        run["model"] = model
        path = "/v1/chat/completions" if params["endpoint"] == "chat" else "/v1/completions"
        seed0 = int(hashlib.md5(run["id"].encode()).hexdigest()[:8], 16)
        for si, conc in enumerate(params["concurrency"]):
            self._check_cancel(run)
            stage = await self._stage(run, si, base, path, model, params, conc,
                                      seed0 + si * 1_000_003)
            run["stages"].append(stage)
            self._save(run, force=True)
            if stage["ok"] == 0:
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
                live = await self.manager.start(profile)
                run["auto_started"] = True
            except RuntimeError as e:
                raise BenchError(f"サーバーの起動に失敗しました: {e}")
        run["session_id"] = live["id"]
        return live

    async def _wait_ready(self, run: dict[str, Any], base: str, health: str,
                          timeout: int, sid: str) -> None:
        t0 = time.monotonic()
        while True:
            self._check_cancel(run)
            s = self.manager.sessions.get(sid)
            if s and s.get("status") not in ("running", "starting"):
                raise BenchError("サーバーが起動途中で終了しました。サーバータブのログを確認してください")
            if await asyncio.to_thread(_health_ok, f"{base}{health}"):
                return
            if time.monotonic() - t0 > timeout:
                raise BenchError(f"{timeout}s 経ってもサーバーが health に応答しません。"
                                 "サーバータブのログを確認してください")
            await asyncio.sleep(1.5)

    async def _resolve_model(self, base: str) -> str:
        for i in range(3):
            mid = await asyncio.to_thread(_first_model, base)
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
                     params: dict[str, Any], conc: int, seed: int) -> dict[str, Any]:
        n = int(params["num_prompts"])
        warm = int(params["warmup"])
        stream = bool(params["stream"])
        timeout = float(params["request_timeout"])
        loop = asyncio.get_running_loop()
        self._progress(run, stage=si, phase="warmup", done=0, total=n, ok=0, fail=0,
                       out_tokens=0, elapsed=0)
        self._save(run, force=True)

        def make_body(i: int) -> dict[str, Any]:
            prompt = make_prompt(int(params["input_len"]), seed + i)
            return self._payload(params, model, prompt)

        if warm:
            ex = ThreadPoolExecutor(max_workers=min(warm, 4), thread_name_prefix="bench-w")
            try:
                for i in range(warm):
                    self._check_cancel(run)
                    await loop.run_in_executor(
                        ex, functools.partial(_one_request, base, path, make_body(i),
                                              stream, timeout))
            finally:
                ex.shutdown(wait=False)

        self._check_cancel(run)
        idx = [0]
        results: list[dict[str, Any]] = []
        aborted = {"v": False}
        ex = ThreadPoolExecutor(max_workers=min(conc, 64), thread_name_prefix="bench")
        t0 = time.perf_counter()
        self._progress(run, stage=si, phase="measure", done=0, total=n, ok=0, fail=0,
                       out_tokens=0, elapsed=0)
        self._save(run, force=True)

        async def worker() -> None:
            while True:
                if run["id"] in self._cancel:
                    aborted["v"] = True
                    return
                i = idx[0]
                idx[0] += 1
                if i >= n:
                    return
                r = await loop.run_in_executor(
                    ex, functools.partial(_one_request, base, path, make_body(i),
                                          stream, timeout))
                results.append(r)
                prog = run["progress"]
                prog["done"] += 1
                if r.get("ok"):
                    prog["ok"] += 1
                    prog["out_tokens"] += int(r.get("out_tokens") or 0)
                else:
                    prog["fail"] += 1
                prog["elapsed"] = time.perf_counter() - t0
                self._save(run)

        try:
            await asyncio.gather(*(worker() for _ in range(conc)))
        finally:
            ex.shutdown(wait=False)
        wall = time.perf_counter() - t0

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
        e2e = [r["out_tokens"] / r["secs"] for r in okres
               if r.get("out_tokens") and r["secs"] > 0]
        out_tok = sum(int(r.get("out_tokens") or 0) for r in okres)
        in_tok = sum(int(r.get("in_tokens") or 0) for r in okres)
        return {
            "concurrency": conc, "num_prompts": n, "warmup": warm,
            "ok": len(okres), "fail": len(results) - len(okres), "aborted": aborted["v"],
            "wall_secs": round(wall, 2),
            "rps": _div(len(okres), wall),
            "out_tps": _div(out_tok, wall),
            "total_tps": _div(out_tok + in_tok, wall),
            "out_tokens": out_tok, "in_tokens": in_tok,
            "tokens_estimated": any(r.get("estimated") for r in okres),
            "latency": _stats(lat), "ttft": _stats(ttfts),
            "decode_tps": _stats(decodes), "e2e_tps": _stats(e2e),
            "errors": errors,
        }
