"""Regression test for the playground tok/s overestimate.

The playground accumulates a token estimate from streamed SSE deltas while
waiting for usage. The old code summed estTok() per delta; estTok rounds
each call up to at least 1, and servers like FreeToken emit roughly one
delta per token, so Japanese text was overcounted (+12% total, 1.4x in
short windows) and the live tok/s showed ~70 where the engine did ~45.
The fix adds estTokFrac(): the fractional score is summed across deltas
and rounded once, so per-delta boundaries cannot inflate the count.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "llm_launcher" / "static" / "index.html"
CHUNKS_FIXTURE = Path(__file__).resolve().parent / "data" / "playground_jp_token_chunks.txt"

# Kanji-dense Japanese; tokenized by the serving model to 594 tokens (0.53 tok/char).
JP_UNIT = (
    "日本語の文章生成では、トークン化の粒度が生成速度と出力文字数の関係を左右する。"
    "漢字はひとつで一つ以上のトークンになることもあり、ひらがなは数文字がまとめて"
    "一つのトークンに符号化される。そのため文字数をそのままトークン数とみなすと"
    "過大評価になりやすい。推論エンジンは各ステップで一トークンを生成するので、"
    "生成速度はトークン毎のデルタ到着間隔を平均して測るのが最も正確である。"
)
JP_TEXT = JP_UNIT * 6
JP_TRUE_TOKENS = 594

# The serving model's per-token decode of JP_TEXT, unit-separator joined.
# FreeToken's detokenizer emits roughly one SSE delta per token, so these
# chunks reproduce what the playground accumulator actually sees in a stream.
def jp_token_chunks():
    raw = CHUNKS_FIXTURE.read_text(encoding="utf-8")
    chunks = raw.split("\u241f")
    assert "".join(chunks) == JP_TEXT and len(chunks) == JP_TRUE_TOKENS
    return chunks

FUNC_RE = re.compile(
    r"function estTokFrac\(s\) \{.*?\n\}\nfunction estTok\(s\) \{.*?\n\}", re.S
)

NODE_DRIVER = """
{functions}
const chunks = JSON.parse(require("fs").readFileSync(0, "utf8"));
let frac = 0, perDelta = 0;
for (const c of chunks) { frac += estTokFrac(c); perDelta += estTok(c); }
console.log(JSON.stringify({streamed: frac, whole: estTok(chunks.join("")), perDelta: perDelta}));
"""


def _run_estimate(chunks):
    src = STATIC.read_text(encoding="utf-8")
    match = FUNC_RE.search(src)
    assert match, "estTokFrac/estTok not found in index.html"
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    script = NODE_DRIVER.replace("{functions}", match.group(0))
    out = subprocess.run(
        [node, "-e", script],
        input=json.dumps(chunks, ensure_ascii=False),
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def test_japanese_delta_stream_count_stays_near_true_tokens():
    # one delta per token, exactly as FreeToken's detokenizer emits for CJK
    chunks = jp_token_chunks()
    est = _run_estimate(chunks)
    # 0.6 tok/char is a coarse constant (true here: 0.53); tolerate calibration
    # drift but catch per-delta rounding, which pushed the old sum to 1.25x truth
    assert abs(est["streamed"] / JP_TRUE_TOKENS - 1) < 0.20, est
    assert abs(est["whole"] / JP_TRUE_TOKENS - 1) < 0.20, est
    # the old per-delta formula must still be measurably worse on this input,
    # otherwise this test no longer protects the regression
    assert est["perDelta"] / est["streamed"] > 1.05, est


def test_streamed_sum_is_delta_split_invariant():
    # summing per-delta fractions must equal the whole-text score whatever
    # the CJK chunk boundaries are; per-delta rounding (the old bug) broke this
    base = "日本語の文章生成ではトークン化の粒度が速度を左右する。"
    ref = _run_estimate([base])["streamed"]
    for step in (1, 2, 3, 5):
        got = _run_estimate([base[i:i + step] for i in range(0, len(base), step)])["streamed"]
        assert abs(got - ref) < 1e-6, (step, got, ref)


def test_english_words_not_inflated():
    text = "The quick brown fox jumps over the lazy dog while iterating tokens. " * 20
    words = len(text.split())
    est = _run_estimate([text])
    assert abs(est["whole"] / words - 1) < 0.10, est
    # whitespace-delimited word chunks are the realistic SSE split
    chunks = re.findall(r"\S+\s*", text)
    est2 = _run_estimate(chunks)
    assert abs(est2["streamed"] / words - 1) < 0.10, est2
