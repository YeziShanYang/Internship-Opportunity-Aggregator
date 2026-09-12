"""The gather stage: fetch every watched source and write the bodies to `.run/raw/`.

This is one of only three packages allowed to make a network request, and the only one
that makes ~157 of them. Nothing here parses, filters or diffs; the bodies land on disk
exactly as fetched and `process` decides what they mean.

Two clients, not one, and that is a credential boundary rather than a style choice.
`github_readme.build_client` puts `Authorization: Bearer <GH_PAT>` on every request it
makes. While only GitHub was contacted that was harmless; the moment a job board or a
careers page shares the client, the PAT is sent to boards-api.greenhouse.io,
api.lever.co, api.ashbyhq.com and every firm's marketing site.

Three things that are *not* failures and must each stay distinguishable from one, and
from each other, all the way to HEALTH (spec 10.1):

* `method=manual` -- deliberately not fetched, no attempt recorded at all. These reach
  the owner as calendar reminders.
* quarantined -- the breaker skipped the fetch, so nothing was learned and no counter
  may move. Still reported, because "we have stopped looking" is the strongest form of
  blind-not-quiet.
* an unrecognised method -- a configuration error. This used to be a bare `continue`,
  which produced no result at all: no health line, no failure count, and a silently
  unwatched source indistinguishable from a quiet one.
"""
from __future__ import annotations

import json
import os
import sys
import time

from core import models, paths
from gather import ats, breaker, github_readme, page
from persist import artifacts
from sources import postings

GITHUB_CLIENT = "github"
WEB_CLIENT = "web"

# method -> which client it needs. The companion table in `process.build` maps the same
# methods to their assessors, and `tests/test_stages.py` asserts the two agree -- a
# method in one and not the other is a source that fetches and is never read, or is
# read and never fetched.
CLIENT_FOR = {
    "github_readme": GITHUB_CLIENT,
    "greenhouse": WEB_CLIENT,
    "lever": WEB_CLIENT,
    "ashby": WEB_CLIENT,
    "workday": WEB_CLIENT,
    "phenom": WEB_CLIENT,
    "eightfold": WEB_CLIENT,
    "page_text": WEB_CLIENT,
}

UNWATCHED = paths.UNWATCHED_METHOD


def github_token() -> str | None:
    token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print(
            "note: no GH_PAT/GITHUB_TOKEN set; using unauthenticated GitHub API "
            "(60 requests/hour instead of 5,000)",
            file=sys.stderr,
        )
    return token


def _encode_body(method: str, fetched) -> bytes:
    """The bytes to store for this method's fetch.

    JSON for the ATS feeds, because their "one fetch" is a list of documents plus a map
    of Workday detail documents, and a faithful replay needs both. Text otherwise.
    """
    if method in ats.ENDPOINTS:
        return json.dumps(
            {"pages": fetched.pages, "details": fetched.details},
            ensure_ascii=False, default=str,
        ).encode("utf-8")
    text = getattr(fetched, "html", None)
    if text is None:
        text = getattr(fetched, "text", "")
    return text.encode("utf-8")


def fetch_one(source: dict[str, str], clients: dict) -> tuple[models.FetchAttempt, bytes]:
    """Fetch one source and return (attempt, body). Never raises."""
    source_id = source["source_id"]
    method = (source.get("method") or "").strip()
    client = clients[CLIENT_FOR[method]]

    if method == "page_text":
        fetched = page.fetch(source, client)
    elif method == "github_readme":
        fetched = github_readme.fetch(source, client)
    else:
        # Imported lazily so `gather` never imports `process` at module scope; the plan
        # predicate is data flowing downhill, not a dependency going uphill.
        from process.parse_ats import wants_workday_detail

        fetched = ats.fetch(source, client, wants_detail=wants_workday_detail)

    attempt = fetched.attempt
    if attempt.source_id != source_id:
        attempt = models.FetchAttempt(**{**vars(attempt), "source_id": source_id})
    return attempt, (_encode_body(method, fetched) if attempt.ok else b"")


def collect(sources: list[dict[str, str]], only: str | None) -> list[models.FetchAttempt]:
    """Fetch every watched source, writing each body to `.run/raw/{source_id}.body`.

    Returns one FetchAttempt per source that was considered -- including the quarantined
    ones and the misconfigured ones, because a source that produced no record is a
    source nobody will notice has gone blind.
    """
    attempts: list[models.FetchAttempt] = []
    token = github_token()
    with github_readme.build_client(token) as github, postings.build_client() as web:
        clients = {GITHUB_CLIENT: github, WEB_CLIENT: web}
        for source in sources:
            source_id = source["source_id"]
            if only and source_id != only:
                continue
            method = (source.get("method") or "").strip()
            if method == UNWATCHED:
                continue

            if method not in CLIENT_FOR:
                attempts.append(models.FetchAttempt(
                    source_id=source_id, ok=False,
                    error=f"sources.csv sets method={method!r}, which no module handles"))
                continue

            # `--only` is an explicit instruction to look at this source now, so it
            # bypasses the breaker; that is the affordance you want when you are
            # debugging the source that is quarantined.
            if not only:
                skip, why = breaker.quarantine_state(source)
                if skip:
                    attempts.append(models.FetchAttempt(
                        source_id=source_id, ok=False, quarantined=True, error=why))
                    continue

            attempt, body = fetch_one(source, clients)
            if attempt.ok:
                artifacts.write_body(source_id, body)
            attempts.append(attempt)
            time.sleep(paths.REQUEST_DELAY_SECONDS)  # spec section 11: be a good citizen
    artifacts.write(artifacts.RAW_INDEX, "raw-index", attempts)
    return attempts
