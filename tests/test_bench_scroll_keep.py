"""Regression tests for scroll stability inside the periodically re-rendered panels.

The bench tab re-renders every 2.5 s (``benchTimer`` -> ``bmPoll`` -> ``renderBench`` ->
``renderBenchActive`` / ``renderBenchHistory``), and every render swaps the container's
whole ``innerHTML``. Rebuilding the DOM throws away the state of nested scroll boxes, so
the stage table — ``.bm-wrap`` is ``overflow-x:auto`` and 12 columns wide, so it is
*always* horizontally scrolled on a phone or a narrow window — snapped back to its left
edge twice per minute while the user was reading the TTFT / 最大停止 / 停止数 columns.

The fix keys every scroll box (``data-scrollkey``) and every collapsible block
(``data-detailkey``) and restores them around the swap via ``logScrollSnap`` /
``logScrollBack`` — the same pair the calibration log boxes already relied on, widened
from "vertical only, ``data-logkey`` only" to "vertical + horizontal + details state".

These tests pin the contract at source level (the repo has no browser test harness):
a future table renderer that forgets its key would silently bring the jump back.

A second, related complaint is the *hitch* felt while dragging a table's horizontal
scrollbar. Restoring the position is not enough there: the ``innerHTML`` swap destroys
the thumb mid-drag (and kills wheel / touch momentum), so a poll that changes nothing
must not touch the DOM at all, and a poll that does change something must wait for the
gesture to end. Hence two more rules, following the pattern ``renderRunning`` /
``renderHistory`` already use (``runningSig`` / ``histSig`` + ``_ssSig`` / ``_hsSig``):

* identical HTML  -> no swap (the history lists only differ via ``fmtAgo``, so this
  drops the rebuild rate from ~1.3 s to once a minute)
* ``uiScrolling()`` -> ``deferRender(key, fn)`` postpones the swap (one timer per slot,
  never for the first paint, so the panel cannot stay blank)
"""
import re
from pathlib import Path

def _frontend_src() -> str:
    """index.html は分割された (css/ + js/**)。アサーションは全体を結合した文字列に対して行う。"""
    base = Path(__file__).resolve().parents[1] / "src" / "llm_launcher" / "static"
    parts = [(base / "index.html").read_text(encoding="utf-8")]
    parts += [p.read_text(encoding="utf-8") for p in sorted((base / "css").glob("*.css"))]
    parts += [p.read_text(encoding="utf-8") for p in sorted((base / "js").rglob("*.js"))]
    return "\n".join(parts)

SRC = _frontend_src()


def body(fn: str) -> str:
    """The source of a top-level function (declared at column 0, closed by "}" at column 0)."""
    m = re.search(rf"^function {re.escape(fn)}\(.*?^\}}", SRC, re.S | re.M)
    assert m, f"{fn} not found in index.html"
    return m.group(0)


def test_every_bench_table_box_is_keyed():
    """A .bm-wrap without data-scrollkey cannot be restored — it resets to scrollLeft=0."""
    assert '<div class="bm-wrap">' not in SRC
    boxes = re.findall(r'<div class="bm-wrap" [^>]*', SRC)
    assert boxes, "expected the bench / calibration stage tables to still exist"
    for box in boxes:
        assert "data-scrollkey=" in box, box


def test_table_renderers_take_and_pass_a_key():
    assert re.search(r"function bmStageTable\(stages, key\)", SRC)
    assert re.search(r"function calSettingsTable\(rep, key\)", SRC)
    # no call site may drop the key argument
    assert not re.search(r"bmStageTable\([A-Za-z.]+\.stages\)", SRC)
    assert not re.search(r"calSettingsTable\(run\.report\)", SRC)
    # run ids are stable across polls, so they are the key; the prefixes keep them apart
    # from the log boxes' data-logkey="k:<id>" (a clash would restore the wrong box)
    assert 'bmStageTable(run.stages, "a:" + run.id)' in SRC
    assert 'bmStageTable(r.stages, "k:" + r.id)' in SRC
    assert 'calSettingsTable(run.report, "a:" + run.id)' in SRC
    assert 'data-detailkey="${esc(key)}:err"' in SRC


def test_snapshot_pair_covers_horizontal_and_details():
    snap = body("logScrollSnap")
    back = body("logScrollBack")
    assert "[data-logkey],[data-scrollkey]" in snap
    assert "[data-logkey],[data-scrollkey]" in back
    assert "scrollLeft" in snap and "scrollLeft" in back
    assert "[data-detailkey]" in snap and "[data-detailkey]" in back
    # log boxes must keep sticking to the tail when the user was already there ...
    assert "s.stick ? p.scrollHeight : s.top" in back
    # ... but a stage table is never "at the tail" (it does not overflow vertically), so
    # stickiness may only be computed for log boxes, otherwise it is meaningless state
    assert 'p.hasAttribute("data-logkey")' in snap


def test_restore_does_not_rewrite_unchanged_positions():
    """Writing the same scrollTop / scrollLeft every poll is what *feels* like a pull-back."""
    back = body("logScrollBack")
    assert "Math.abs(p.scrollTop - top) > 0.5" in back
    assert "Math.abs(p.scrollLeft - s.left) > 0.5" in back


# The six panels that poll (bench / calibrate / setup, active card + history list) and the
# assignment that resets nested scroll boxes in each of them.
SWAP_SITES = {
    "renderBenchActive": "el.innerHTML = `<",
    "renderBenchHistory": "el.innerHTML = html;",
    "renderCalActive": "el.innerHTML = `<",
    "renderCalHistory": "el.innerHTML = html;",
    "renderInsActive": "el.innerHTML = html;",
    "renderInsHistory": "el.innerHTML = html;",
}

# ... and the slot that caches the last HTML written, so an identical poll is a no-op.
HTML_CACHES = {
    "renderBenchHistory": "S.bench._bh",
    "renderCalHistory": "S.cal._ch",
    "renderInsHistory": "S.ins._ih",
    "renderInsActive": "S.ins._ia",
}

# One deferred-render slot per panel (deferRender keys must not collide either).
DEFER_SLOTS = {
    "renderBenchActive": "bm-act",
    "renderBenchHistory": "bm-hist",
    "renderCalActive": "cal-act",
    "renderCalHistory": "cal-hist",
    "renderInsActive": "ins-act",
    "renderInsHistory": "ins-hist",
}


def test_every_periodic_swap_is_wrapped_by_snapshot_and_restore():
    """innerHTML is the reset point: snap must precede it, restore must follow it."""
    for fn, swap in SWAP_SITES.items():
        src = body(fn)
        assert "logScrollSnap(el)" in src, fn
        assert src.index("logScrollSnap(el)") < src.index(swap), fn
        assert src.rindex("logScrollBack(el, _ls)") > src.rindex(swap), fn
        # nothing may bail out between the snapshot and the restore
        tail = src[src.index("logScrollSnap(el)"):]
        for m in re.finditer(r"return;", tail):
            assert "logScrollBack(el, _ls)" in tail[:m.start()], fn


def test_poll_does_not_rebuild_identical_html():
    """Dragging a scrollbar while nothing changed must not lose the thumb (or a selection)."""
    for fn, cache in HTML_CACHES.items():
        src = body(fn)
        guard = re.search(re.escape(f"if (html === {cache} && el.childElementCount) return;"), src)
        assert guard, fn
        assert src.index("const html =") < guard.start(), fn
        # the cache is only allowed to advance together with an actual write
        assert src.index(f"{cache} = html;") < src.index("el.innerHTML = html;"), fn
        assert src.count(f"{cache} = html;") == 1, fn


def test_empty_placeholder_is_written_once():
    """The empty-state branch used to rewrite (and thus reset) the box on every poll."""
    for fn in ("renderBenchHistory", "renderCalHistory", "renderInsHistory"):
        src = body(fn)
        empty = r"if \((S\.\w+\._[a-z]{1,2}h) !== empty\) \{ \1 = empty; el\.innerHTML = empty; \}"
        m = re.search(empty, src)
        assert m, fn
        assert "logScrollSnap" not in src[:m.start()], fn      # nothing to preserve in an empty box


def test_cache_keys_are_seeded_in_the_state_objects():
    """A missing key means `undefined !== html` forever, i.e. the skip silently never fires."""
    for init, key in (("S.bench = {", "_bh"), ("S.cal = {", "_ch"), ("S.ins = {", "_ih"),
                      ("S.ins = {", "_ia")):
        m = re.search(re.escape(init) + r"[\s\S]*?\};", SRC)
        assert m, init
        assert f'{key}: ""' in m.group(0), f"{key} missing from {init}"


def test_rebuild_is_deferred_while_the_user_is_scrolling():
    for fn, slot in DEFER_SLOTS.items():
        src = body(fn)
        guard = re.search(r"if \(uiScrolling\(\) && el\.childElementCount\) \{ deferRender\("
                          + re.escape(f'"{slot}"'), src)
        assert guard, fn
        assert guard.start() < src.rindex("el.innerHTML = "), fn
        # the first paint may never be postponed, or the panel would render blank
        assert "el.childElementCount" in guard.group(0), fn


def test_scroll_guard_helpers_exist_and_cannot_stack_timers():
    assert re.search(r"const uiScrolling = \(\) => Date\.now\(\) < _scrollHoldUntil;", SRC)
    assert re.search(r'window\.addEventListener\("scroll", \(\) => \{ _scrollHoldUntil = '
                     r'Date\.now\(\) \+ 350; \},\s*\{passive: true, capture: true}\)', SRC)
    defer = body("deferRender")
    assert "if (_deferTimers[key]) return;" in defer        # one pending render per slot
    assert "if (uiScrolling()) { deferRender(key, fn); return; }" in defer
    assert "}, 400);" in defer


def test_helpers_are_declared_before_the_bench_section_uses_them():
    """The bench tab is rendered earlier in the file than the calibration section."""
    for decl in ("const uiScrolling", "function deferRender"):
        assert SRC.index(decl) < SRC.index("function renderBenchActive"), decl


def test_calibrate_panels_stay_guarded():
    """The widening must not undo the pre-existing log-box protection."""
    for fn in ("renderCalActive", "renderCalHistory"):
        src = body(fn)
        assert "logScrollSnap(el)" in src and "logScrollBack(el, _ls)" in src, fn
    assert 'data-logkey="k:${esc(' in SRC
