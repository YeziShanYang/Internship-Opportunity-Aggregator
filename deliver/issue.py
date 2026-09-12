"""Deciding whether to send, and opening the issue.

Issues rather than email: GitHub emails the owner when an issue is opened in their own
repo, so there is no SMTP, no API key that expires silently, and no deliverability
problem. The issue *is* the email, which is also what makes the open issues the delivery
log every run shares.

Separate from the renderer because `digest.render` used to query the GitHub API from
inside rendering. That is the concrete version of "deliver must be allowed to import
httpx": the POST and the exactly-once check are I/O, the body is not, and mixing them
meant a render could not be done offline.
"""
from __future__ import annotations

import os

import httpx

from core import models, paths

GITHUB_API = "https://api.github.com"


def should_send(
    judgments: list[models.Judgment],
    results: list[models.SourceMetrics],
    already_delivered_today: bool = False,
) -> tuple[bool, bool]:
    """Return (send, status_only).

    Exactly one digest a day, every day. There is no "nothing to say" branch any more:
    a reminder the owner can set their morning around is worth more than an inbox saved
    from a short status mail, and it removes the one genuinely bad state the change-only
    cadence had -- a silent morning that could mean either "quiet day" or "broken job".

    `already_delivered_today` is the whole of the "exactly once" guarantee and it is
    absolute: it suppresses real changes and failing sources too, which the old
    change-only cadence deliberately did not. That is the point. A retry firing after a
    successful delivery has nothing new to tell the owner that the morning's issue did
    not already contain, and mailing the same date twice is the fatigue failure this
    cadence exists to avoid. The digest is not the only record -- the state commit and
    data/proposals.log still capture everything the retry saw.
    """
    if already_delivered_today:
        return False, False
    has_news = (
        bool(judgments)
        or any(not result.ok for result in results)
        or any(result.baseline for result in results)
    )
    return True, not has_news


def already_sent(date: str) -> bool | None:
    """Has a digest for `date` already been opened? None when GitHub cannot be asked.

    data/last_delivered.txt cannot answer this on its own. A run reads that marker from
    the commit it checked out, and the SHA is pinned when GitHub *dispatches* the run --
    which, given ticks that arrive 3-5 hours late, is not necessarily in cron order. A
    tick nominally scheduled first can dispatch second and check out a tree from before
    the other tick pushed its state, see a stale marker, and mail a duplicate.

    The issue list has no such problem. The issue *is* the email, so the open issues are
    the delivery log itself, and every run sees the same one regardless of what it
    checked out. Matching on the date in the title covers all three title shapes.

    Returns None rather than False when the question cannot be answered, so the caller
    can fall back to the marker instead of treating "I could not check" as "not yet
    delivered" and mailing twice.
    """
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_PAT")
    if not repo or not token:
        return None
    try:
        response = httpx.get(
            f"{GITHUB_API}/repos/{repo}/issues",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": paths.USER_AGENT,
            },
            # Sorted newest first, so 50 covers any plausible backlog of same-day ticks
            # without paginating.
            params={
                "state": "all",
                "sort": "created",
                "direction": "desc",
                "per_page": 50,
            },
            timeout=paths.HTTP_TIMEOUT_SECONDS,
        )
        if response.status_code >= 300:
            return None
        issues = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(issues, list):
        return None
    return any(
        date in (issue.get("title") or "")
        for issue in issues
        # The issues endpoint returns pull requests too; they are not digests.
        if isinstance(issue, dict) and "pull_request" not in issue
    )


def deliver(title: str, body: str) -> str:
    """Open an issue. Returns a human-readable description of what happened."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_PAT")
    if not repo or not token:
        return (
            "not delivered: GITHUB_REPOSITORY and GITHUB_TOKEN must both be set "
            "(they are, automatically, inside GitHub Actions)"
        )
    response = httpx.post(
        f"{GITHUB_API}/repos/{repo}/issues",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": paths.USER_AGENT,
        },
        json={"title": title, "body": body},
        timeout=paths.HTTP_TIMEOUT_SECONDS,
    )
    if response.status_code >= 300:
        raise RuntimeError(
            f"could not open issue: HTTP {response.status_code} {response.text[:300]}"
        )
    return f"opened issue {response.json().get('html_url')}"
