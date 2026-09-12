"""Where a fetch landed is part of whether it succeeded.

Both HTTP clients follow redirects, so a page that has been retired 302s to a friendly
error page and returns HTTP 200 -- and a friendly error page is full of prose, so it
sails past every content floor and reports success forever. Measured 2026-09-12:
`aqr-internship-program` had been doing exactly that, yielding 4,683 characters at ratio
0.0929 from `aqr.com/404`.

This is the blind spot the two content floors cannot see. They ask whether a page has
*enough text*, which catches a JavaScript shell and is blind to the opposite failure --
a marketing or error page, full of prose, containing no listings.
"""
from __future__ import annotations

import re
import urllib.parse

# A final URL that looks like an error page.
_ERROR_PATH = re.compile(r"/(?:404|410|error|not.?found|page.?not.?found|gone)(?:/|$|\.)", re.I)


def _comparable_url(url: str) -> tuple[str, str]:
    """(host, path) with only the cosmetic differences removed.

    Scheme, a leading `www.` and a trailing slash are noise: measured across the 65
    watched pages, two of the five redirects were nothing but that
    (`osqf.org`->`www.osqf.org`, `www.tower-research.com`->apex). Reporting those would
    be 40% noise in a check whose whole value is that it is quiet until it matters.
    """
    parts = urllib.parse.urlsplit(url.strip())
    host = (parts.netloc or "").lower().removeprefix("www.")
    path = (parts.path or "/").rstrip("/") or "/"
    return host, path


def verdict(configured: str, final: str) -> tuple[str, str]:
    """Compare the URL we asked for with the one we got. Returns (severity, message).

    Severity is "" (nothing to say), "warn" (the page loads but is not the page this
    row was configured to watch) or "fail" (it landed on an error page).

    The distinction is deliberate. A row redirected from a students page to a generic
    careers page still returns readable text, so calling it a failed fetch would be a
    lie -- but it is no longer watching what its `program_names` claims, which is a
    coverage hole that has to be said out loud every morning until someone fixes the
    URL. A row redirected to /404 is simply broken.
    """
    if not configured or not final:
        return "", ""
    want_host, want_path = _comparable_url(configured)
    got_host, got_path = _comparable_url(final)
    if (want_host, want_path) == (got_host, got_path):
        return "", ""
    if _ERROR_PATH.search(got_path):
        return "fail", (
            f"redirected to what looks like an error page: {configured} -> {final}. "
            "The page this row watched is gone; the text it is still returning is the "
            "error page's own prose, which is why the content floors did not catch it."
        )
    if want_path != got_path:
        return "warn", (
            f"redirected to a different path: {configured} -> {final}. The fetch "
            "succeeded, but this row is no longer watching the page it was configured "
            "for. Update the url in sources.csv or retire the row."
        )
    return "", ""
