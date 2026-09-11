"""Tier 3: watch a page's text and report what moved.

Spec section 7's insight, unchanged: do not write a scraper that extracts postings from
these pages. Snapshot the readable text, diff it, and let the model read the diff. A
competition site that adds "2027 registration is open" is reporting itself; a parser
that tried to understand the page would break on the next redesign.

Measured before building: all ten seeded `page_text` sources return real text to a
plain httpx GET -- 837 to 10,418 characters, zero JavaScript shells, zero failures. So
Phase 2 ships with no browser. That is a property of these ten pages, not a general
claim, which is why `render_js=true` is refused loudly rather than attempted.

Two floors keep a broken page from reading as a quiet one (spec 10.1). The ratio rule
is the load-bearing one: three of the ten sit near 1,000 characters, close enough that
a partial render could clear any sane absolute floor while losing everything that
matters.
"""
from __future__ import annotations

import difflib
import re

import httpx

import state
from sources import postings, snapshot

# Below this a response is a JavaScript shell or a block page, not a short page.
# Calibrated against measurements, not guessed: the thinnest real page in the watchlist
# is hedgewestsf at 837 characters, while SIG's careers page yields 221 from 402KB of
# HTML. An earlier value of 200 let SIG through.
MIN_ABSOLUTE_CHARS = 500

# The stronger signal, because it does not care how big the page is. A shell is mostly
# markup: SIG measures 0.0005 text-to-HTML, while the thinnest genuine page in the
# watchlist (imc-launchpad-us, 2,472 chars from 678KB) measures 0.0036 -- seven times
# higher. Everything else measured between 0.01 and 0.31.
MIN_TEXT_HTML_RATIO = 0.0015

# Losing this much of a page is a redesign, a block, or a partial render. Self
# calibrating against the last good fetch, so there is no per-source constant to rot.
SHRINK_RATIO = 0.4

# A nav change can move dozens of lines; the digest only needs enough to judge.
MAX_DIFF_LINES = 12

_DROP = re.compile(r"(?is)<(script|style|noscript|svg|iframe|template)\b.*?</\1>")
_HTML_COMMENT = re.compile(r"(?s)<!--.*?-->")
_BLOCK_END = re.compile(
    r"(?is)</(p|div|li|tr|h[1-6]|section|article|header|footer|nav)\s*>|<br\s*/?>"
)
_TAG = re.compile(r"(?s)<[^>]+>")

# Per-request noise. Replaced with a placeholder rather than deleted so that a
# genuinely new element still shows up as an added line instead of vanishing into a
# collapse.
_NOISE = (
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hex>"),
    (re.compile(r"[?&](v|ver|t|ts|cb|nocache|_)=[^\s&\"'<>]+"), ""),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}T[\d:.+Z-]+"), "<timestamp>"),
    (re.compile(r"(?i)\bcsrf[-_]?token\S*"), "<csrf>"),
)


def normalise(raw_html: str) -> list[str]:
    """Readable text as lines.

    Line-preserving rather than one blob, so the committed snapshot diffs readably in
    `git log` the way the Tier 1 TSVs do. Deliberately not sorted: unlike a table, a
    page's order carries meaning, and sorting would turn a moved section into a pile of
    spurious additions.
    """
    text = _DROP.sub(" ", raw_html)
    text = _HTML_COMMENT.sub(" ", text)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = postings.extract_text(text) if "<" in text else text
    for pattern, placeholder in _NOISE:
        text = pattern.sub(placeholder, text)
    lines = []
    for line in text.split("\n"):
        collapsed = " ".join(line.split())
        if collapsed:
            lines.append(collapsed)
    return lines


def diff_pages(
    source_id: str, old: list[str], new: list[str], source: dict[str, str]
) -> list[state.Change]:
    """At most one Change per page per run.

    Page diffs are lumpy -- a nav tweak moves forty lines -- so one Change per changed
    line would drown the digest and burn the classification budget on boilerplate.
    """
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    added: list[str] = []
    removed: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(new[j1:j2])
        if tag in ("delete", "replace"):
            removed.extend(old[i1:i2])
    if not added and not removed:
        return []

    blob = " ".join(added)
    body = [f"+ {line}" for line in added[:MAX_DIFF_LINES]]
    body += [f"- {line}" for line in removed[:MAX_DIFF_LINES]]
    if len(added) > MAX_DIFF_LINES or len(removed) > MAX_DIFF_LINES:
        body.append(f"... {len(added)} added and {len(removed)} removed lines in total.")

    program_name = source.get("program_names", "")
    return [
        state.Change(
            source_id=source_id,
            kind="changed",
            key=f"{program_name or source_id} ({len(added)} added, {len(removed)} removed)",
            detail="\n".join(body),
            url=source.get("url", ""),
            program_name=program_name,
            # Added text only. A removed "Freshman" line is a programme going away, not
            # a discovery, and flagging it would push a closure into ACT NOW.
            is_discovery_candidate=bool(snapshot.DISCOVERY_PATTERN.search(blob)),
            rolling=bool(snapshot.ROLLING_PATTERN.search(blob)),
            posting_text=blob[: postings.MAX_TEXT_CHARS],
        )
    ]


def check(source: dict[str, str], client: httpx.Client) -> state.SourceResult:
    """Check one page. Never raises: a failure is a result, not an exception."""
    source_id = source["source_id"]

    # Refused at config time rather than discovered at runtime. This is what makes
    # "a JavaScript shell is never a quiet day" true by construction.
    if (source.get("render_js") or "").strip().lower() == "true":
        return state.SourceResult(
            source_id=source_id,
            ok=False,
            error=(
                "render_js=true is not supported (Phase 2 ships no browser); move this "
                "row to method=manual or accept that it is blind"
            ),
        )
    # Silently ignoring a configured selector is the same class of bug as skipping an
    # unknown method: the row looks watched and is not watched as configured.
    if (source.get("selector") or "").strip():
        return state.SourceResult(
            source_id=source_id,
            ok=False,
            error=(
                "a CSS selector is configured but page_watch does not implement one; "
                "clear the column or add a parser"
            ),
        )

    try:
        response = client.get(source["url"])
        response.raise_for_status()
    except Exception as exc:
        return state.SourceResult(
            source_id=source_id, ok=False, error=f"{type(exc).__name__}: {exc}"
        )

    lines = normalise(response.text)
    text = "\n".join(lines)
    previous_text = state.read_snapshot(source_id, ext="txt")
    ratio = len(text) / max(len(response.text), 1)

    if len(text) < MIN_ABSOLUTE_CHARS:
        return state.SourceResult(
            source_id=source_id,
            ok=False,
            content_length=len(text),
            error=(
                f"page returned only {len(text)} characters of text "
                "(renders in JavaScript, or was blocked)"
            ),
        )
    if ratio < MIN_TEXT_HTML_RATIO:
        return state.SourceResult(
            source_id=source_id,
            ok=False,
            content_length=len(text),
            error=(
                f"page yielded {len(text)} characters of text from "
                f"{len(response.text)} of HTML (ratio {ratio:.4f}) - this is a "
                "JavaScript shell, not a page with little on it"
            ),
        )
    if previous_text is not None and len(text) < SHRINK_RATIO * len(previous_text):
        return state.SourceResult(
            source_id=source_id,
            ok=False,
            content_length=len(text),
            error=(
                f"page shrank from {len(previous_text)} to {len(text)} characters - "
                "likely a redesign, a block, or a partial render"
            ),
        )

    extra = {"rows": len(lines), "chars": len(text), "ratio": round(ratio, 4)}
    if previous_text is None:
        return state.SourceResult(
            source_id=source_id,
            ok=True,
            baseline=True,
            snapshot_text=text,
            snapshot_ext="txt",
            content_length=len(text),
            extra=extra,
        )

    changes = diff_pages(source_id, previous_text.splitlines(), lines, source)
    return state.SourceResult(
        source_id=source_id,
        ok=True,
        changes=changes,
        snapshot_text=text,
        snapshot_ext="txt",
        content_length=len(text),
        extra=extra,
    )
