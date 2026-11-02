"""Regression tests for the meta lines that embed an inline <svg> sprite.

Both the playground per-message meta line and the bench progress note are HTML
fragments (they carry an inline `<svg><use href=#i-bolt /></svg>` reference).
Both were fed through esc(), which escapes & < > " ' — so the markup could
never render and showed up verbatim on screen:

    2.4s · first 0.04s · 54→67 tok · <svg class=ic aria-hidden=true><use href=#i-bolt /></svg> 30.7 tok/s

The fix stops escaping those two fragments. Equally important, these tests pin
the *legitimate* esc() calls: a future sweep that removes escaping in the wrong
place would silently expose engine- and user-supplied text to markup injection.
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "llm_launcher" / "static" / "index.html"
SRC = STATIC.read_text(encoding="utf-8")


def test_playground_meta_is_not_escaped():
    # the shape that produced the raw tag on screen must be gone
    assert "bits.map(b => esc(b))" not in SRC
    # ...and the fragment is joined as markup
    assert re.search(r'class="pg-meta">\$\{bits\.join\(" · "\)\}', SRC)


def test_bench_progress_tps_note_is_not_escaped():
    assert "esc(tpsNote)" not in SRC
    assert re.search(r"\$\{esc\(doneNote\)\}\$\{tpsNote\}", SRC)


def test_usage_counts_are_number_normalised():
    # esc() no longer guards this interpolation, so engine values are coerced
    assert "Number(m.usage.completion_tokens)" in SRC
    assert "Number(m.usage.prompt_tokens)" in SRC


def test_legitimate_escaping_survives():
    """esc() removals must stay surgical — these carry untrusted text."""
    for needle in ("esc(m.reasoning)",        # assistant reasoning body
                   "esc(modelNote)",          # model name from the engine
                   "esc(doneNote)",
                   "esc(String(r.error)"):    # run error strings
        assert needle in SRC, needle
    assert SRC.count("esc(String(r.error)") >= 3


def _markup_fragment_names(src):
    """Identifiers holding a string that contains inline markup (<svg> ...).

    Every icon in this file is an inline <svg> on a single line, which keeps this
    deliberately simple: name := assignment target or push() receiver on a line
    that also contains '<svg'.
    """
    names = set()
    for line in src.splitlines():
        if "<svg" not in line:
            continue
        for pat in (r"(?:const|let|var)\s+(\w+)\s*=", r"(\w+)\.push\("):
            m = re.search(pat, line)
            if m:
                names.add(m.group(1))
    return names


def test_no_markup_fragment_is_passed_to_esc():
    """Guard against reintroducing the bug via a *new* fragment variable."""
    frag = _markup_fragment_names(SRC)
    assert frag, "fragment extraction found nothing - the check itself broke"

    offenders = set(re.findall(r"esc\(\s*([A-Za-z_$][\w$]*)\s*[,)]", SRC)) & frag
    # esc(IC(...)) style calls take a call, not a bare identifier, so they are
    # covered by the direct check below rather than by `offenders`.
    assert not offenders, f"markup fragment escaped: {sorted(offenders)}"

    for arr, param in re.findall(r"(\w+)\.map\(\s*(\w+)\s*=>\s*esc\(\2\)\)", SRC):
        assert arr not in frag, f"markup fragment array escaped via map: {arr}"

    assert not re.search(r'''esc\(\s*`[^`]*<svg''', SRC)
    assert not re.search(r'''esc\(\s*[A-Za-z_$][\w$]*\s*\+\s*[`'"]<[a-zA-Z/]''', SRC)
