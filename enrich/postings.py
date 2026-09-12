"""Fetch the real job posting behind a board row, so the classifier has something to judge.

A `simplify-2027` row is a title, a company and a location -- nothing a relevance filter
can act on. Measured on the snapshot: 7 of 592 rows mention any class-year word at all,
and all 7 are incidental matches on "Graduate Researcher". That is why the classifier
used to skip this source entirely: not because the rows were unimportant, but because
there was no information in them to be right or wrong about.

The posting page does carry it. Measured on a random sample of 22 live postings,
7 (~32%) state a class-year gate that excludes a first-year -- "rising junior",
"penultimate year", "third year", an expected-graduation window. Fetching turns a row
that could only ever be guessed at into one that can be decided.

Three findings that shape this module:

* **Only Simplify's own page is usable.** Rows carry two links: the employer's ATS
  (Workday, iCIMS, Greenhouse, Oracle HCM) and `simplify.jobs/p/{uuid}`. The ATS links
  are dead ends for a text fetch -- Workday returns literally zero characters, because
  the posting renders entirely in JavaScript. Simplify's page returns 13K-34K characters
  of real text including the requirements block.
* **The honest User-Agent works.** `paths.USER_AGENT` and a browser string return byte
  identical responses (140,325 bytes on the page tested), so there is no reason to
  pretend to be a browser. `robots.txt` allows `/p/` (`Allow: /`, and `/p` is absent
  from the disallow list) and sets no crawl-delay.
* **A short page is a failure, not an empty posting.** The JS-shell case returns HTTP
  200 with no content. Treating that as "posting has no requirements" would silently
  turn a fetch failure into a relevance judgment, which is the one thing this must not
  do -- see `fetch_text`.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import re
import time

import httpx

from core import paths, text as coretext
from gather import clients

# Rows put the ATS link and the Simplify link in the same `Application=` field, ATS
# first. Only the second one is fetchable, so match it specifically rather than taking
# the first URL in the row.
POSTING_URL = re.compile(r"https://simplify\.jobs/p/[0-9a-fA-F-]+")

CACHE_DIR = paths.DATA / "postings_cache"

# Below this, the response is a JavaScript shell rather than a posting. The real pages
# measured 13K-34K characters; the Workday shells measured 0.
MIN_USEFUL_CHARS = 500

# Re-exported so callers have one name for it; it lives in core.text because `process`
# needs it and may not import this layer.
MAX_TEXT_CHARS = coretext.MAX_TEXT_CHARS

# Politeness. robots.txt sets no crawl-delay, so this is self-imposed.
MAX_CONCURRENCY = 6
PER_REQUEST_DELAY_SECONDS = 0.2

# A hung host must never stall the daily run. Whatever has not been fetched when this
# expires is reported as a fetch failure, which degrades to unclassified rather than
# to a relevance decision.
TOTAL_BUDGET_SECONDS = 240.0

def recover_posting_url(*haystacks: str) -> str:
    """Best-effort: find a fetchable posting link in already-rendered row text.

    A fallback, not the main path. The producer that built the row knows which of its
    links is the posting page and records it on `Change.posting_url`; this only runs for
    a row recovered from a committed TSV, where the typed roles were never stored
    because the snapshot format is frozen. Returns "" rather than None so callers do
    not need two absent values.
    """
    for haystack in haystacks:
        if not haystack:
            continue
        match = POSTING_URL.search(haystack)
        if match:
            return match.group(0)
    return ""


def _cache_path(url: str):
    return CACHE_DIR / f"{hashlib.sha1(url.encode()).hexdigest()}.txt"


extract_text = coretext.extract_text


def fetch_text(client: httpx.Client, url: str) -> tuple[str, str]:
    """Return (text, error). Exactly one is non-empty. Never raises.

    An error must stay an error all the way to the digest. If a failed fetch were
    returned as empty text, the classifier would read "no stated requirements" and
    could rule the posting out -- inventing a relevance decision from a network
    problem.
    """
    cached = _cache_path(url)
    if cached.exists():
        try:
            return cached.read_text(), ""
        except OSError:
            pass  # fall through and refetch

    try:
        response = client.get(url)
        response.raise_for_status()
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"

    text = extract_text(response.text)
    if len(text) < MIN_USEFUL_CHARS:
        return "", (
            f"page returned only {len(text)} characters of text "
            "(renders in JavaScript, or was blocked)"
        )

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cached.write_text(text)
    except OSError:
        pass  # the cache is an optimisation; failing to write it is not an error

    return text, ""


build_client = clients.build_web_client


def fetch_many(
    urls: list[str], client: httpx.Client | None = None
) -> dict[str, tuple[str, str]]:
    """Fetch every URL concurrently. Returns {url: (text, error)}.

    Renamed from `fetch_for_changes`, which took a list of Changes and did the
    URL-derivation itself -- so the fetching layer had to know how a row stores its
    links. It takes URLs now and `enrich.bodies` owns the derivation.
    """
    if not urls:
        return {}

    owned = client is None
    client = client or build_client()
    deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
    results: dict[str, tuple[str, str]] = {}

    def one(url: str) -> tuple[str, tuple[str, str]]:
        if time.monotonic() > deadline:
            return url, ("", "skipped: the run's total fetch budget was exhausted")
        text, error = fetch_text(client, url)
        time.sleep(PER_REQUEST_DELAY_SECONDS)
        return url, (text, error)

    try:
        with concurrent.futures.ThreadPoolExecutor(MAX_CONCURRENCY) as pool:
            for url, outcome in pool.map(one, urls):
                results[url] = outcome
    finally:
        if owned:
            client.close()
    return results
