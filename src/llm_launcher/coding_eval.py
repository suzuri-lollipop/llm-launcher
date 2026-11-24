"""コーディングベンチの AI 評価 (2 実行の定量診断)。

選択された 2 つの実行履歴について、メトリクス (時間 / トークン / ターン / ツールミス)
と実装ファイル内容を収集し、judge モデル (稼働中プロファイルのサーバーまたは外部 API)
に渡して構造化 JSON の評価 (軸別スコア / 欠陥 / 判定) を得る。結果は履歴として保存。

- judge がローカルプロファイルの場合は GPU 実走排他ロックを bench と共有する
  (外部 API judge はロック不要)。
- 外部 API キーは履歴に残さない (マスク保存 + 実行中メモリのみ)。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .coding import (CodingError, CodingManager, QWEN_THINKING_RE, external_probe,
                     file_tree, mask_secret, model_efforts, normalize_external_base,
                     safe_join)
from .store import JsonStore, new_id, now

MAX_EVALS = 60
# judge に渡す実装内容の上限 (文字数)。超えたら先頭ファイルから切り詰める
MATERIAL_CHARS_PER_RUN = 160_000
FILE_CHARS_MAX = 60_000
RAW_RESPONSE_MAX = 200_000

# 評価軸 (0-100)。UI のスコア表と judge プロンプトの単一の源
EVAL_AXES: list[dict[str, str]] = [
    {"key": "requirements", "label": "要件充足",
     "desc": "プロンプトの要件 (API 仕様・永続化・静的配信・パストラバーサル対策・相対パス・フロントエンド機能) をどれだけ満たしたか"},
    {"key": "correctness", "label": "正確性・堅牢性",
     "desc": "バグ・エッジケース・並行アクセス・エラー処理・入力検証の正しさ"},
    {"key": "code_quality", "label": "コード品質",
     "desc": "構造・可読性・保守性・重複の少なさ・適切な抽象化"},
    {"key": "frontend", "label": "フロントエンド品質",
     "desc": "UI 要件の充足・デザイン・レスポンシブ・アクセシビリティ (バックエンドのみの実装は frontend/ の有無で判断)"},
    {"key": "verification", "label": "自己検証",
     "desc": "自分で動作確認 (サーバー起動・API テスト・停止) をどれだけ丁寧に行ったか (メトリクスと出力履歴から判断)"},
    {"key": "efficiency", "label": "効率",
     "desc": "所要時間・ターン数・トークン量・ツールミスの少なさに対して成果物がどれだけ良かったか"},
]

JUDGE_SYSTEM = (
    "あなたはコーディングエージェントのベンチマーク審判です。"
    "2 つの実装 (A と B) を同じ課題に対して比較し、定量的に診断します。"
    "推測ではなく、提示された実装コードとメトリクスに基づいて評価してください。"
    "出力は指定された JSON スキーマのみ (前後の説明文・コードフェンス禁止)。"
)


def _fmt_metrics(m: dict[str, Any]) -> str:
    def f(v: Any, suffix: str = "") -> str:
        return f"{v}{suffix}" if v is not None else "-"
    return (
        f"所要 {f(m['elapsed_secs'], 's')} (backend {f(m['backend_secs'], 's')} / "
        f"frontend {f(m['frontend_secs'], 's')}) / ターン {m['turns']} / "
        f"ツール呼び出し {m['tool_calls']} (ミス {m['tool_errors']}) / "
        f"入力 {m['tokens_in']} tok / 出力 {m['tokens_out']} tok / "
        f"デコード {f(m['decode_tok_per_sec'], ' tok/s')} / 平均TTFT {f(m['avg_ttft_secs'], 's')} / "
        f"平均生成 {f(m['avg_gen_secs_per_turn'], 's/ターン')} / ファイル {m['files']}"
    )


def _judge_prompt(mat_a: dict[str, Any], mat_b: dict[str, Any]) -> str:
    """judge 用の評価プロンプト。実装は JSON ネストではなく Markdown の
    コードブロックで渡す (エスケープだらけの JSON 文字列はモデルが読み誤り、
    思考が空転する要因になるため)。"""
    axes = "\n".join(f"- {a['key']} (0-100): {a['label']} — {a['desc']}" for a in EVAL_AXES)
    schema = {
        "scores": {a["key"]: {"a": "0-100の整数", "b": "0-100の整数",
                               "reason": "A/B それぞれの根拠を日本語で簡潔に"} for a in EVAL_AXES},
        "defects": {"a": [{"severity": "high|medium|low", "file": "パス", "detail": "内容"}],
                    "b": [{"severity": "high|medium|low", "file": "パス", "detail": "内容"}]},
        "highlights": {"a": ["長所"], "b": ["長所"]},
        "verdict": {"winner": "A|B|tie", "confidence": "0-100の整数", "reason": "総合判定の根拠"},
        "summary": "全体の所見 (日本語で数文)",
    }

    def impl(mat: dict[str, Any], side: str) -> str:
        files = "\n\n".join(
            f"### {f['path']} ({f['chars']} 文字)\n````\n{f['content']}\n````"
            for f in mat["files"]) or "(ファイルなし)"
        trunc = "\n**注意: 実装は容量上限で切り詰められています。見えない部分を断定的に評価しないでください。**" \
            if mat["files_truncated"] else ""
        warn = f"\n- 警告: {mat['metrics']['warning']}" if mat["metrics"]["warning"] else ""
        err = f"\n- エラー: {mat['metrics']['error']}" if mat["metrics"]["error"] else ""
        return f"""# 実装 {side}: {mat['name']}
- モデル: {mat['model']} / 対象: {mat['target']} / thinking: {mat['metrics']['thinking']}
- メトリクス: {_fmt_metrics(mat['metrics'])}{warn}{err}
{trunc}
## ファイル
{files}

## 最終発言 (参考)
{mat['final_text'] or '(なし)'}"""

    return f"""# 課題 (両実装とも同じ)
## バックエンド作成プロンプト
{mat_a['requirements']['backend_prompt']}

## フロントエンド作成プロンプト
{mat_a['requirements']['frontend_prompt']}

# 評価軸
{axes}

{impl(mat_a, 'A')}

{impl(mat_b, 'B')}

# 出力形式 (この JSON のみ。値は日本語で)
{json.dumps(schema, ensure_ascii=False, indent=1)}

注意:
- scores は各軸独立に採点 (合計点ではない)。メトリクス (時間/ターン/トークン) は efficiency と verification の参考に。
- defects は実装コードから確認できる具体的欠陥のみ (ファイルパス付き)。無い場合は空配列。
- winner はスコアと欠陥の重さで総合判断。僅差なら tie。
- 同じ分析を繰り返さないこと。結論が出たら次へ進むこと。"""


def _metrics(run: dict[str, Any]) -> dict[str, Any]:
    """実行レコードから judge 用の定量メトリクスを作る (UI の cbAgg と同じ集計)。"""
    phs = [p for p in ((run.get("phases") or {}).get("backend"),
                       (run.get("phases") or {}).get("frontend")) if p]

    def s(k: str) -> float:
        return sum(float(p.get(k) or 0) for p in phs)

    tout, gen_sum, gen_n = s("tokens_out"), s("gen_secs_sum"), s("gen_n")
    ttft_sum, ttft_n = s("ttft_sum"), s("ttft_n")
    calls, errs = s("tool_calls"), s("tool_errors")
    secs = None
    if run.get("started_at") is not None and run.get("finished_at") is not None:
        secs = round(run["finished_at"] - run["started_at"], 1)
    return {
        "status": run.get("status"),
        "elapsed_secs": secs,
        "backend_secs": (run.get("phases") or {}).get("backend", {}).get("secs"),
        "frontend_secs": (run.get("phases") or {}).get("frontend", {}).get("secs"),
        "turns": int(s("turns")), "tool_calls": int(calls), "tool_errors": int(errs),
        "tokens_in": int(s("tokens_in")), "tokens_out": int(tout),
        "decode_tok_per_sec": round(tout / gen_sum, 2) if gen_sum else None,
        "avg_ttft_secs": round(ttft_sum / ttft_n, 2) if ttft_n else None,
        "avg_gen_secs_per_turn": round(gen_sum / gen_n, 2) if gen_n else None,
        "files": (run.get("files") or {}).get("count", 0),
        "warning": run.get("warning", ""), "error": run.get("error", ""),
        "thinking": (run.get("params") or {}).get("thinking", ""),
    }


def collect_materials(coding: CodingManager, run: dict[str, Any]) -> dict[str, Any]:
    """1 実行分の評価材料: メトリクス + 要件 + 実装ファイル内容 (上限付き)。"""
    ws = coding.run_dir(run["id"]) / "workspace"
    files: list[dict[str, Any]] = []
    budget = MATERIAL_CHARS_PER_RUN
    truncated = False
    entries = [f for f in file_tree(ws) if f["path"] != "AGENTS.md"]
    # ソースらしいファイルを優先 (小さいファイルからだと水増しされるため大きい順)
    entries.sort(key=lambda f: -int(f.get("size") or 0))
    for f in entries:
        if budget <= 0:
            truncated = True
            break
        try:
            p = safe_join(ws, f["path"])
            text = p.read_text(encoding="utf-8", errors="replace")
        except (OSError, CodingError):
            continue
        if len(text) > FILE_CHARS_MAX:
            text = text[:FILE_CHARS_MAX]
            truncated = True
        if len(text) > budget:
            text = text[:budget]
            truncated = True
        budget -= len(text)
        files.append({"path": f["path"], "chars": len(text), "content": text})
    params = run.get("params") or {}
    return {
        "name": run.get("name", ""),
        "model": run.get("model", ""),
        "target": run.get("profile_name") or (run.get("external") or {}).get("base_url", ""),
        "metrics": _metrics(run),
        "requirements": {"backend_prompt": params.get("backend_prompt", ""),
                         "frontend_prompt": params.get("frontend_prompt", "")},
        "files": files,
        "files_truncated": truncated,
        "final_text": (run.get("progress") or {}).get("text", "")[:2000],
    }


def _parse_judge_json(text: str) -> dict[str, Any]:
    """judge 応答から JSON を取り出す (コードフェンス / 前後文を許容)。"""
    t = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if fence:
        t = fence.group(1)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        return json.loads(t[start:end + 1])
    raise ValueError("judge 応答から JSON を解析できません")


def _validate_result(res: dict[str, Any]) -> dict[str, Any]:
    """スコアを 0-100 に量子化し、欠陥/判定の形を整える (judge の出力揺れを吸収)。"""
    def clamp(v: Any) -> int:
        try:
            return max(0, min(100, int(round(float(v)))))
        except (TypeError, ValueError):
            return 0

    scores = {}
    raw_scores = res.get("scores") or {}
    for ax in EVAL_AXES:
        it = raw_scores.get(ax["key"]) or {}
        scores[ax["key"]] = {"a": clamp(it.get("a")), "b": clamp(it.get("b")),
                             "reason": str(it.get("reason") or "")[:2000]}
    defects = {}
    for side in ("a", "b"):
        items = []
        for d in ((res.get("defects") or {}).get(side) or [])[:30]:
            if isinstance(d, dict):
                sev = str(d.get("severity") or "medium").lower()
                items.append({"severity": sev if sev in ("high", "medium", "low") else "medium",
                              "file": str(d.get("file") or "")[:300],
                              "detail": str(d.get("detail") or "")[:1000]})
        defects[side] = items
    highlights = {side: [str(x)[:500] for x in ((res.get("highlights") or {}).get(side) or [])[:10]]
                  for side in ("a", "b")}
    v = res.get("verdict") or {}
    winner = str(v.get("winner") or "tie").upper()
    verdict = {"winner": winner if winner in ("A", "B", "TIE") else "tie",
               "confidence": clamp(v.get("confidence")),
               "reason": str(v.get("reason") or "")[:2000]}
    if verdict["winner"] == "TIE":
        verdict["winner"] = "tie"
    return {"scores": scores, "defects": defects, "highlights": highlights,
            "verdict": verdict, "summary": str(res.get("summary") or "")[:4000]}


def _judge_thinking_extra(base: str, auth: dict[str, str], model: str,
                          thinking: str, local: bool) -> dict[str, Any]:
    """judge の thinking 配線 (ベンチの _prepare_pi_home と同じ系統)。
    テンプレート既定の thinking ON を確実に切る必要がある (Qwen3.8-Flash-Next は
    enable_thinking トグル + effort 語彙、Inkling-2 は reasoning_effort="none")。
    /v1/models が effort 語彙を公開するサーバー (= テンプレート probe 済み =
    chat_template_kwargs を解する) には語彙に応じた配線を使う。語彙の無い
    外部 API には OpenAI 標準 reasoning_effort のみ (kwargs は OpenAI が拒否する)。"""
    efforts, _ = model_efforts(base, auth)
    qwen = bool(QWEN_THINKING_RE.search(model or ""))
    if qwen and efforts:
        kw: dict[str, Any] = {"enable_thinking": thinking != "off", "preserve_thinking": True}
        if thinking != "off":
            kw["reasoning_effort"] = thinking if thinking in efforts else efforts[0]
        return {"chat_template_kwargs": kw}
    if efforts and "none" in efforts:
        return {"reasoning_effort": thinking if thinking != "off" else "none"}
    if local and qwen:
        return {"chat_template_kwargs": {"enable_thinking": thinking != "off",
                                         "preserve_thinking": True}}
    return {"reasoning_effort": thinking} if thinking != "off" else {}


def _chat_call(base: str, auth: dict[str, str], model: str, system: str, user: str,
               thinking: str, max_tokens: int, timeout: float,
               local: bool = False) -> dict[str, Any]:
    """judge への 1 回の chat completions (同期・スレッドで呼ぶ)。"""
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        # temperature=0 の貪欲デコードは量子化モデル + 長文プロンプトで反復ループに
        # 陥ることがある (Qwen3.8 NVFP4 の judge 実測)。適度な温度で回避する
        "max_tokens": max_tokens, "temperature": 0.3, "top_p": 0.9, "stream": False,
    }
    body.update(_judge_thinking_extra(base, auth, model, thinking, local))
    req = urllib.request.Request(
        f"{base}/v1/chat/completions", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(auth or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read().decode("utf-8", "replace"))
    msg = (resp.get("choices") or [{}])[0].get("message") or {}
    return {"text": msg.get("content") or "",
            "reasoning_chars": len(msg.get("reasoning_content") or ""),
            "usage": resp.get("usage") or {},
            "finish_reason": (resp.get("choices") or [{}])[0].get("finish_reason", "")}


class CodingEvalManager:
    """AI 評価ジョブの起動・履歴管理。coding の実行履歴を読み取り専用で参照する。"""

    def __init__(self, store: JsonStore, coding: CodingManager, manager: Any):
        self.store = store
        self.coding = coding
        self.manager = manager
        self.exclusive: Any = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._ext_keys: dict[str, str] = {}

    def attach_exclusive(self, lock: Any) -> None:
        self.exclusive = lock

    def list_evals(self) -> list[dict[str, Any]]:
        return sorted(self.store.load(), key=lambda e: e.get("created_at") or 0, reverse=True)

    def on_startup(self) -> list[str]:
        """launcher 再起動で死んだ評価ジョブを failed に回復 (coding と同じ)。"""
        lost: list[str] = []
        for e in self.store.load():
            if e.get("status") == "running":
                e["status"] = "failed"
                e["error"] = (e.get("error") or "") + " / launcher の再起動で中断されました"
                e["finished_at"] = e.get("finished_at") or now()
                lost.append(e["id"])
        if lost:
            self.store.save()
        return lost

    def get(self, eid: str) -> dict[str, Any] | None:
        return next((e for e in self.store.load() if e.get("id") == eid), None)

    def delete(self, eid: str) -> None:
        ev = self.get(eid)
        if not ev:
            raise KeyError(eid)
        if ev.get("status") == "running":
            raise CodingError("実行中の評価は削除できません")

        def _d(lst: list) -> None:
            lst[:] = [e for e in lst if e.get("id") != eid]
        self.store.mutate(_d)

    async def start(self, body: dict[str, Any]) -> dict[str, Any]:
        run_a = self.coding.get(body["run_a"])
        run_b = self.coding.get(body["run_b"])
        if not run_a or not run_b:
            raise CodingError("評価対象の実行履歴が見つかりません")
        if body["run_a"] == body["run_b"]:
            raise CodingError("異なる 2 つの実行を選んでください")
        for r in (run_a, run_b):
            if r.get("status") in ("queued", "running"):
                raise CodingError("実行中のベンチマークは評価できません (完了を待ってください)")
        judge = body.get("judge") or {}
        ext = judge.get("external")
        profile = None
        if ext:
            base = normalize_external_base(ext.get("base_url", ""))
            if not base.startswith(("http://", "https://")):
                raise CodingError("外部 API の base_url が不正です")
            judge_desc = {"kind": "external", "name": ext.get("name") or ext.get("model") or base,
                          "model": ext.get("model", ""), "base_url": base,
                          "api_key": mask_secret(ext.get("api_key", ""))}
        else:
            pid = judge.get("profile_id", "")
            profile = self.manager.find_profile(pid)
            if not profile:
                raise CodingError("judge プロファイルが見つかりません")
            judge_desc = {"kind": "profile", "name": profile.get("name", ""),
                          "profile_id": pid, "model": ""}
        thinking = str(body.get("thinking") or "off")
        if thinking not in ("off", "minimal", "low", "medium", "high", "xhigh", "max"):
            raise CodingError("thinking は off / minimal / low / medium / high / xhigh / max です")
        eid = new_id("ev")
        ev: dict[str, Any] = {
            "id": eid, "created_at": now(), "finished_at": None,
            "status": "running", "error": "",
            "run_a": {"id": run_a["id"], "name": run_a.get("name", ""),
                      "model": run_a.get("model", ""), "profile_name": run_a.get("profile_name", "")},
            "run_b": {"id": run_b["id"], "name": run_b.get("name", ""),
                      "model": run_b.get("model", ""), "profile_name": run_b.get("profile_name", "")},
            "judge": judge_desc, "thinking": thinking,
            "timeout": int(body.get("timeout") or 900),
            "materials": None, "result": None, "raw": "", "secs": None,
        }
        if ext:
            self._ext_keys[eid] = ext.get("api_key", "")
        locked = False
        if not ext and self.exclusive is not None:
            if not self.exclusive.acquire(f"eval:{eid}"):
                raise CodingError("ベンチマーク / キャリブレーションが実行中です。完了を待つか中断してください")
            locked = True
        try:
            self.store.mutate(lambda lst: lst.append(ev))
            self._trim()
            self._tasks[eid] = asyncio.create_task(self._job(ev, run_a, run_b, profile, ext, locked))
        except BaseException:
            locked and self.exclusive and self.exclusive.release()
            self._ext_keys.pop(eid, None)
            raise
        return ev

    def _trim(self) -> None:
        def _t(lst: list) -> None:
            if len(lst) > MAX_EVALS:
                keep = {e["id"] for e in sorted(lst, key=lambda e: e.get("created_at") or 0,
                                                reverse=True)[:MAX_EVALS]}
                keep |= {e["id"] for e in lst if e.get("status") == "running"}
                lst[:] = [e for e in lst if e["id"] in keep]
        self.store.mutate(_t)

    def _save(self, ev: dict[str, Any]) -> None:
        def _patch(lst: list) -> None:
            for i, e in enumerate(lst):
                if e.get("id") == ev["id"]:
                    lst[i] = ev
                    return
        self.store.mutate(_patch)

    async def _job(self, ev: dict[str, Any], run_a: dict[str, Any], run_b: dict[str, Any],
                   profile: dict[str, Any] | None, ext: dict[str, Any] | None,
                   locked: bool) -> None:
        t0 = time.monotonic()
        session_id = ""
        try:
            mat_a = await asyncio.to_thread(collect_materials, self.coding, run_a)
            mat_b = await asyncio.to_thread(collect_materials, self.coding, run_b)
            ev["materials"] = {
                "a": {"files": len(mat_a["files"]), "chars": sum(f["chars"] for f in mat_a["files"]),
                      "truncated": mat_a["files_truncated"]},
                "b": {"files": len(mat_b["files"]), "chars": sum(f["chars"] for f in mat_b["files"]),
                      "truncated": mat_b["files_truncated"]},
            }
            self._save(ev)
            if ext:
                base = normalize_external_base(ext.get("base_url", ""))
                key = self._ext_keys.get(ev["id"], "")
                auth = {"Authorization": f"Bearer {key}"} if key else {}
                probe = await asyncio.to_thread(external_probe, base, auth, 20.0)
                if not probe["ok"]:
                    raise CodingError(f"judge 外部 API に接続できません: {probe['error']}")
                model = ext.get("model") or (probe["models"][0] if probe["models"] else "")
                if not model:
                    raise CodingError("judge のモデル名を解決できません (明示指定してください)")
            else:
                live = next((s for s in self.manager.sessions.values()
                             if s["profile_id"] == profile["id"]
                             and s["status"] in ("running", "starting")), None)
                if not live:
                    raise CodingError("judge プロファイルが起動していません。先に起動してください")
                session_id = live["id"]
                port = live.get("port")
                if not port:
                    raise CodingError("judge セッションのポートを解決できません")
                base = f"http://127.0.0.1:{port}"
                from .backends import api_key_from_argv
                k = api_key_from_argv(live.get("argv") or [])
                auth = {"Authorization": f"Bearer {k}"} if k else {}
                from .bench import _first_model
                model = await asyncio.to_thread(_first_model, base, auth)
                if not model:
                    raise CodingError("judge サーバーからモデル名を解決できません")
            ev["judge"]["model"] = model
            self._save(ev)
            prompt = _judge_prompt(mat_a, mat_b)
            if ev["thinking"] == "off":
                # テンプレートで thinking を切っても複雑な課題では自発的に think ブロックを
                # 開くモデルがいる (Qwen3.8 実測)。明示指示で compliance を上げる
                prompt += ("\n\n注意: thinking は無効です。think ブロックを書かず、"
                           "ただちに JSON 本体のみを出力してください。")
            # thinking off でも自発思考で枠を食うことがあるためエンジン既定上限まで確保
            call = await asyncio.to_thread(
                _chat_call, base, auth, model, JUDGE_SYSTEM, prompt,
                ev["thinking"], 32768, float(ev["timeout"]), not ext)
            ev["raw"] = call["text"][:RAW_RESPONSE_MAX]
            if call["finish_reason"] == "length":
                raise CodingError("judge の出力がトークン上限で打ち切られました (thinking を下げるか timeout を確認)")
            res = _validate_result(_parse_judge_json(call["text"]))
            ev["result"] = res
            ev["usage"] = call["usage"]
            ev["status"] = "completed"
        except CodingError as e:
            ev["status"] = "failed"
            ev["error"] = str(e)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            ev["status"] = "failed"
            detail = ""
            if isinstance(e, urllib.error.HTTPError):
                with contextlib.suppress(Exception):
                    detail = e.read().decode("utf-8", "replace")[:300]
            ev["error"] = f"judge 呼び出し失敗: {e} {detail}".strip()
        except (ValueError, json.JSONDecodeError) as e:
            ev["status"] = "failed"
            ev["error"] = f"judge 応答の解析失敗: {e}"
        except asyncio.CancelledError:
            ev["status"] = "failed"
            ev["error"] = "中断されました"
        except Exception as e:  # noqa: BLE001
            ev["status"] = "failed"
            ev["error"] = f"{type(e).__name__}: {e}"
        finally:
            ev["finished_at"] = now()
            ev["secs"] = round(time.monotonic() - t0, 1)
            self._save(ev)
            self._tasks.pop(ev["id"], None)
            self._ext_keys.pop(ev["id"], None)
            locked and self.exclusive and self.exclusive.release()
