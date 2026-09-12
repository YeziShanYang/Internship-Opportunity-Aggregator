"""Fetch a repo's README: the repo metadata for its default branch, then the raw file.

Network only. Parsing the tables is `process.parse_readme`'s job, and the client that
carries the GitHub credential is `gather.clients`' -- see there for why there are two.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import httpx

from core import models, paths

GITHUB_API = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"


@dataclass
class ReadmeFetch:
    """One README, as fetched. `text` is empty whenever `attempt.ok` is False."""

    attempt: models.FetchAttempt
    text: str = ""
    branch: str = ""


def fetch_readme(client: httpx.Client, repo: str) -> tuple[str, str]:
    """Return (readme_text, default_branch).

    The branch comes from the API rather than a guess: LuisaE/opportunities is on
    `master` and SimplifyJobs is on `dev`.
    """
    meta = client.get(f"{GITHUB_API}/repos/{repo}")
    meta.raise_for_status()
    branch = meta.json().get("default_branch") or "main"
    time.sleep(paths.REQUEST_DELAY_SECONDS)
    readme = client.get(f"{RAW_BASE}/{repo}/{branch}/README.md")
    readme.raise_for_status()
    return readme.text, branch


def fetch(source: dict[str, str], client: httpx.Client) -> ReadmeFetch:
    """Two requests -- the repo metadata, then the raw README. Never raises."""
    source_id = source["source_id"]
    repo = source.get("url") or ""
    started = time.monotonic()

    def attempt(**kwargs) -> models.FetchAttempt:
        return models.FetchAttempt(
            source_id=source_id, requested_url=f"{GITHUB_API}/repos/{repo}",
            requests=2, elapsed_ms=int((time.monotonic() - started) * 1000), **kwargs)

    try:
        text, branch = fetch_readme(client, repo)
    except httpx.HTTPStatusError as exc:
        return ReadmeFetch(attempt=attempt(
            ok=False, status=exc.response.status_code,
            error=f"HTTP {exc.response.status_code} fetching {exc.request.url}"))
    except Exception as exc:  # network, DNS, timeout, decode
        return ReadmeFetch(attempt=attempt(
            ok=False, error=f"{type(exc).__name__}: {exc}"))

    return ReadmeFetch(
        attempt=attempt(
            ok=True, status=200, size=len(text),
            final_url=f"{RAW_BASE}/{repo}/{branch}/README.md",
            sha256=hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
            # The branch is not recoverable from the README bytes, and it is in the
            # failure message when a parse floor fires ("from N bytes on branch dev").
            meta={"branch": branch}),
        text=text,
        branch=branch,
    )
