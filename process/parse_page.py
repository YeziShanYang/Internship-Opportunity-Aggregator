"""Turn a fetched page into normalised lines, and judge whether the fetch was real.

Spec section 7's insight, unchanged: do not write a scraper that extracts postings from
these pages. Snapshot the readable text, diff it, and let the model read the diff. A
competition site that adds "2027 registration is open" is reporting itself; a parser
that tried to understand the page would break on the next redesign.

Two content floors keep a broken page from reading as a quiet one (spec 10.1), and the
ratio rule is the load-bearing one: three of the watched pages sit near 1,000
characters, close enough that a partial render could clear any sane absolute floor while
losing everything that matters.

Both floors ask whether a page has *enough text*, which is why they catch a JavaScript
shell and are blind to the opposite failure -- a marketing or error page, full of prose,
containing no listings. That blind spot is `redirect.verdict`'s job, and it is checked
here before the floors, because a page that landed somewhere else is wrong regardless
of how much prose it returned.

Pure. No network, no disk: `previous` arrives as an argument, which is what lifted
`read_snapshot` out of the fetch path.
"""
from __future__ import annotations

import difflib
import re

from core import clock, models
from gather import page
from process import redirect
from core import text as coretext
from process import snapshot

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

# No single diff line may be longer than this. Several watched sources are vendor JSON
# feeds (Workable, Rippling, Teamtailor, Pinpoint) served as one unbroken line, and
# Wolverine's is 147,741 characters. Emitting that verbatim would put a single line past
# GitHub's 65,536-character issue-body limit and fail delivery outright -- turning a
# one-byte upstream edit into a missed digest.
MAX_DIFF_LINE_CHARS = 400

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
    text = coretext.extract_text(text) if "<" in text else text
    # Vendor JSON feeds (Workable, Rippling, Teamtailor, Pinpoint) arrive as one line, so
    # a line differ can only ever say "the whole feed changed". Breaking on record
    # boundaries makes a new posting show up as one added line instead.
    if text.lstrip()[:1] in ("{", "["):
        text = text.replace("},{", "},\n{").replace("}, {", "},\n{")
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
) -> list[models.Change]:
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

    def clip(line: str) -> str:
        return line if len(line) <= MAX_DIFF_LINE_CHARS else (
            line[:MAX_DIFF_LINE_CHARS] + f" … [+{len(line) - MAX_DIFF_LINE_CHARS} chars]"
        )

    body = [f"+ {clip(line)}" for line in added[:MAX_DIFF_LINES]]
    body += [f"- {clip(line)}" for line in removed[:MAX_DIFF_LINES]]
    if len(added) > MAX_DIFF_LINES or len(removed) > MAX_DIFF_LINES:
        body.append(f"... {len(added)} added and {len(removed)} removed lines in total.")

    program_name = source.get("program_names", "")
    return [
        models.Change(
            source_id=source_id,
            kind="changed",
            # One change per page per run, so the page *is* the identity. Stable across
            # runs on purpose: `enrich` and `screen` look themselves up by this, and a
            # key that folded in the added/removed counts would change every morning.
            change_id=clock.change_id(source_id, "page"),
            key=f"{program_name or source_id} ({len(added)} added, {len(removed)} removed)",
            detail="\n".join(body),
            url=source.get("url", ""),
            program_name=program_name,
            # Added text only. A removed "Freshman" line is a programme going away, not
            # a discovery, and flagging it would push a closure into ACT NOW.
            is_discovery_candidate=bool(snapshot.DISCOVERY_PATTERN.search(blob)),
            rolling=bool(snapshot.ROLLING_PATTERN.search(blob)),
            posting_text=blob[: coretext.MAX_TEXT_CHARS],
        )
    ]


def assess(
    source: dict[str, str], fetched: page.PageFetch, previous: str | None
) -> models.SourceResult:
    """Decide what one page fetch means. Pure.

    `previous` is the last committed snapshot text, or None for "never seen". It is a
    parameter rather than a `read_snapshot` call because that is the whole point of the
    split: a stage that reads the database cannot be replayed from an artifact.
    """
    source_id = source["source_id"]
    attempt = fetched.attempt
    if not attempt.ok:
        return models.SourceResult(
            source_id=source_id, ok=False, error=attempt.error,
            content_length=attempt.size,
        )

    # Checked before the content floors: a page that landed somewhere else is wrong
    # however much prose it returned, and the floors are structurally unable to notice.
    severity, redirect_note = redirect.verdict(attempt.requested_url, attempt.final_url)
    if severity == "fail":
        return models.SourceResult(
            source_id=source_id, ok=False, error=redirect_note,
            content_length=attempt.size,
        )

    lines = normalise(fetched.html)
    text = "\n".join(lines)
    ratio = len(text) / max(attempt.size, 1)

    if len(text) < MIN_ABSOLUTE_CHARS:
        return models.SourceResult(
            source_id=source_id,
            ok=False,
            content_length=len(text),
            error=(
                f"page returned only {len(text)} characters of text "
                "(renders in JavaScript, or was blocked)"
            ),
        )
    if ratio < MIN_TEXT_HTML_RATIO:
        return models.SourceResult(
            source_id=source_id,
            ok=False,
            content_length=len(text),
            error=(
                f"page yielded {len(text)} characters of text from "
                f"{attempt.size} of HTML (ratio {ratio:.4f}) - this is a "
                "JavaScript shell, not a page with little on it"
            ),
        )
    if previous is not None and len(text) < SHRINK_RATIO * len(previous):
        return models.SourceResult(
            source_id=source_id,
            ok=False,
            content_length=len(text),
            error=(
                f"page shrank from {len(previous)} to {len(text)} characters - "
                "likely a redesign, a block, or a partial render"
            ),
        )

    extra = {"rows": len(lines), "chars": len(text), "ratio": round(ratio, 4)}
    # A "warn" redirect is not a failed fetch -- the page loads and the text is real --
    # but the row is no longer watching what it was configured for, so it rides along
    # in extra and HEALTH says so every morning until the url is corrected.
    if redirect_note:
        extra["redirected"] = redirect_note

    if previous is None:
        return models.SourceResult(
            source_id=source_id,
            ok=True,
            baseline=True,
            snapshot_text=text,
            snapshot_ext="txt",
            content_length=len(text),
            extra=extra,
        )

    return models.SourceResult(
        source_id=source_id,
        ok=True,
        changes=diff_pages(source_id, previous.splitlines(), lines, source),
        snapshot_text=text,
        snapshot_ext="txt",
        content_length=len(text),
        extra=extra,
    )
