"""Stripping HTML to readable text, and how much of it the classifier is given.

Pure, and in `core` rather than next to the fetcher because `process` needs it: both
the page normaliser and every ATS description parser reduce markup to text, and a stage
may only import at or below its own level. It used to live in `sources/postings.py`,
which meant the parsing layer imported the fetching layer to borrow a regex.
"""
from __future__ import annotations

import re

# What the classifier is given. Postings run to ~34K characters and the tail is
# boilerplate (benefits, EEO statements, "about us"), while the requirements block sits
# near the top. Measured: trimming this is worth 8% of the bill, because almost all of
# the cost is reasoning tokens rather than prompt size.
MAX_TEXT_CHARS = 12_000

_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
_ENTITIES = {"&#x27;": "'", "&amp;": "&", "&quot;": '"', "&lt;": "<", "&gt;": ">",
             "&nbsp;": " "}


def extract_text(html: str) -> str:
    """Strip a page to readable text. Crude on purpose -- the model tolerates noise."""
    text = _SCRIPT_OR_STYLE.sub(" ", html)
    text = _TAG.sub(" ", text)
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    return _WHITESPACE.sub(" ", text).strip()


# --- cross-source posting identity ---------------------------------------------------
#
# The watchlist overlaps on purpose -- several aggregators plus ~70 employer boards -- so
# one new posting at a well-covered firm arrives as three or four changes on the same
# morning. To the reader that is one thing to go and do.
#
# The identity has to come from the link and not from the text. Measured on one real
# PIMCO posting, the three sources that carried it that morning:
#
#   simplify        PIMCO / Software Engineering Intern - Technology Analyst @ Austin, TX
#   zshah           PIMCO / 2027 Summer Intern - Technology Analyst, Software Engineering
#   pimco-workday   2027 Summer Intern - Technology Analyst, Software Engineering
#
# Aggregators rewrite the title and the location string, so no text comparison joins
# those. All three preserved the employer's apply URL ending `..._R106745`. The clearest
# case in the corpus is nuft-2027's "Aquatic / QR" against the Greenhouse board's
# "Quantitative Researcher, Intern (Summer 2027)" -- same posting, no shared words.
_POSTING_URL = re.compile(r"https?://[^\s)\]<>]+")
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# Workday puts the requisition at the end of a long human-readable slug, and it is the
# only part two sites agree on: the tenant path segment differs between the employer's
# own board and an aggregator's copy of the link.
_REQUISITION = re.compile(r"_(R-?\d{3,})\b", re.IGNORECASE)
_NUMERIC_ID = re.compile(r"^\d{5,}$")


def _host_key(host: str) -> str:
    """Normalise the hosts that are the same board under different names.

    Greenhouse serves one job id from `boards`, `job-boards`, `boards-api` and their
    `.eu` variants, and an aggregator will have linked whichever one it saw. Every other
    vendor is left alone, because there the subdomain is the tenant: a Workday
    requisition is unique per tenant and not globally, so collapsing
    `pimco.wd1.myworkdayjobs.com` to `myworkdayjobs.com` would risk joining two
    employers' postings that happen to share a requisition number.
    """
    host = host.lower().removeprefix("www.")
    return "greenhouse.io" if host.endswith("greenhouse.io") else host


def posting_identities(*texts: str) -> frozenset[str]:
    """Every stable posting id findable in some text. Empty when there is none.

    Deliberately conservative: only a link that carries a *posting* identifier counts,
    never one that merely names the employer. A Simplify row holds both
    `simplify.jobs/c/PIMCO` and `simplify.jobs/p/<uuid>`, and keying on the first would
    merge every PIMCO posting into one row. Absence of an id means the change is never
    deduplicated -- two rows are only ever called the same posting on positive evidence.
    """
    found: set[str] = set()
    for text in texts:
        for raw in _POSTING_URL.findall(text or ""):
            url = raw.rstrip(".,;:)]}\"'")
            match = re.match(r"https?://([^/]+)(/.*)?$", url)
            if not match:
                continue
            host, path = _host_key(match.group(1)), match.group(2) or ""
            uuid = _UUID.search(path)
            if uuid:
                found.add(f"{host}:{uuid.group(0).lower()}")
                continue
            requisition = _REQUISITION.search(path)
            if requisition:
                found.add(f"{host}:{requisition.group(1).upper()}")
                continue
            segments = [s for s in path.split("/") if s]
            if segments and _NUMERIC_ID.match(segments[-1]):
                found.add(f"{host}:{segments[-1]}")
    return frozenset(found)
