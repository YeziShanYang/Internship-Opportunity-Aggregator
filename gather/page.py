"""Fetch a watched page, recording where the request actually landed.

The final URL is the reason this is its own function rather than a `client.get` at the
top of `page_watch.check`. Both clients follow redirects, so a retired page 302s to a
friendly error page and answers HTTP 200 -- and nothing in this codebase read
`response.url`, so `aqr-internship-program` reported success off `aqr.com/404`'s own
prose for weeks. Where a fetch landed is part of whether it worked, and a fetcher that
throws that away cannot be repaired downstream.

Measured before this tier was built: all ten seeded `page_text` sources returned real
text to a plain httpx GET -- 837 to 10,418 characters, zero JavaScript shells, zero
failures. That is why the project ships no browser, and it is a property of those ten
pages rather than a general claim, which is why `render_js=true` is refused loudly here
instead of being attempted.

Network only. The content floors, the redirect verdict and the diff are
`process.parse_page`'s job.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import httpx

from core import models


@dataclass
class PageFetch:
    """One page, as fetched. `html` is empty whenever `attempt.ok` is False."""

    attempt: models.FetchAttempt
    html: str = ""


def _attempt(source_id: str, url: str, **kwargs) -> models.FetchAttempt:
    return models.FetchAttempt(source_id=source_id, requested_url=url, **kwargs)


def unsupported(source: dict[str, str]) -> str:
    """Why this row cannot be fetched as written, or "" if it can.

    Refused at config time rather than discovered at runtime, which is what makes "a
    JavaScript shell is never a quiet day" true by construction. Both of these are the
    same class of bug as skipping an unknown method: the row looks watched, and is not
    watched as configured.
    """
    if (source.get("render_js") or "").strip().lower() == "true":
        return (
            "render_js=true is not supported (Phase 2 ships no browser); move this "
            "row to method=manual or accept that it is blind"
        )
    if (source.get("selector") or "").strip():
        return (
            "a CSS selector is configured but page_watch does not implement one; "
            "clear the column or add a parser"
        )
    return ""


def fetch(source: dict[str, str], client: httpx.Client) -> PageFetch:
    """GET one page. Never raises: a failed fetch is a result, not an exception."""
    source_id = source["source_id"]
    url = source.get("url") or ""

    refusal = unsupported(source)
    if refusal:
        return PageFetch(attempt=_attempt(source_id, url, ok=False, error=refusal))

    started = time.monotonic()
    try:
        response = client.get(url)
        response.raise_for_status()
    except Exception as exc:
        return PageFetch(attempt=_attempt(
            source_id, url, ok=False, error=f"{type(exc).__name__}: {exc}",
            status=getattr(getattr(exc, "response", None), "status_code", 0) or 0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        ))

    html = response.text
    return PageFetch(
        attempt=_attempt(
            source_id, url, ok=True, status=response.status_code,
            final_url=str(response.url), size=len(html),
            sha256=hashlib.sha256(html.encode("utf-8", "replace")).hexdigest(),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        ),
        html=html,
    )
