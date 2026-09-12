"""Tier 3 compatibility shim. The real code is `gather.page` + `process.parse_page`.

`check` did a fetch, a normalise, two content floors, a redirect verdict, a shrink
check, a disk read and a diff in one function, which meant there was nowhere to put a
new step and no way to inspect what any step produced. It is five lines over a fetcher
and a pure assessor now.

Kept as a shim so `check.CHECKERS` and every `FakeHTMLClient` test in the suite carry
on working untouched while the internals move -- which is the point of doing it this
way round rather than rewriting the callers at the same time.
"""
from __future__ import annotations

import httpx

from core import models
from gather import page
from persist import store
from process import parse_page

# Re-exported for callers that predate the split: the test suite reads the floors and
# calls `normalise`/`diff_pages` directly.
from process.parse_page import (  # noqa: F401
    MAX_DIFF_LINE_CHARS,
    MAX_DIFF_LINES,
    MIN_ABSOLUTE_CHARS,
    MIN_TEXT_HTML_RATIO,
    SHRINK_RATIO,
    diff_pages,
    normalise,
)


def check(source: dict[str, str], client: httpx.Client) -> models.SourceResult:
    """Check one page. Never raises: a failure is a result, not an exception."""
    fetched = page.fetch(source, client)
    previous = store.read_snapshot(source["source_id"], ext="txt")
    return parse_page.assess(source, fetched, previous)
