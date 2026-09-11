"""Render the digest and deliver it as a GitHub Issue (spec section 9).

Issues rather than email: GitHub emails the owner when an issue is opened in their own
repo, so there is no SMTP, no API key that expires silently, and no deliverability
problem. It also doubles as a lightweight application tracker -- comment and close.

Cadence is exactly one digest a day, every day -- no more and no less. The owner asked
for a reminder they can rely on, and a fixed daily arrival is what makes silence
diagnostic: no issue on a given morning means the job is broken, with no "maybe nothing
changed" ambiguity to explain it away. The cost is that quiet days mail too, so a quiet
day says so in the title ("(no changes)") and stays short enough to archive in a glance.

The "no more" half is load-bearing in the other direction. The morning schedule fires
three ticks so that a dropped cron tick is not a missed day, and every one of those
ticks would otherwise be free to open its own issue. Two digests for the same date is
the same notification-fatigue failure as a daily "0 changes" email, so delivery is
capped -- see should_send and delivered_issue_exists.
"""
from __future__ import annotations

import datetime
import os

import httpx

import calendar_reminders
import classify
import state
from classify import Judgment

GITHUB_API = "https://api.github.com"


def _health_lines(results: list[state.SourceResult], sources: dict[str, dict[str, str]]) -> tuple[list[str], list[str]]:
    """Return (health block lines, escalated-failure lines for ACT NOW)."""
    healthy = [r for r in results if r.ok]
    failing = [r for r in results if not r.ok]
    lines = [
        f"{len(results)} sources checked · {len(healthy)} healthy · "
        f"{len(failing)} FAILING"
        if failing
        else f"{len(results)} sources checked · {len(healthy)} healthy"
    ]
    escalated: list[str] = []
    for result in failing:
        source = sources.get(result.source_id, {})
        count = int(source.get("consecutive_failures") or 0)
        last_success = source.get("last_success") or "never"
        detail = (
            f"{result.source_id}: {count} consecutive failure"
            f"{'s' if count != 1 else ''}, last success {last_success}. {result.error}"
        )
        lines.append(f"⚠ {detail}")
        if count >= state.FAILURE_ESCALATION_THRESHOLD:
            escalated.append(
                f"**{result.source_id} has failed {count} times running** — this source "
                f"is blind, not quiet. Last success {last_success}. {result.error}"
            )
    for result in results:
        if result.baseline:
            lines.append(
                f"· {result.source_id}: first run, recorded "
                f"{result.extra.get('rows', 0)} rows as the baseline."
            )
    return lines, escalated


def _stale_profile_line() -> str | None:
    """Nag when the owner profile has not been reviewed in a while.

    Every relevance call in the digest is made against `classify.OWNER_PROFILE`. When
    it drifts out of date nothing breaks visibly -- the digest still renders, the
    judgments still look confident, they are just answering last year's question. The
    CALENDAR block is the right home because it already carries recurring
    human-action reminders, and this line costs nothing on the days it does not fire.
    """
    try:
        reviewed = datetime.date.fromisoformat(classify.PROFILE_LAST_REVIEWED)
    except ValueError:
        return None
    days = (datetime.date.fromisoformat(state.today_iso()) - reviewed).days
    if days < classify.PROFILE_REVIEW_AFTER_DAYS:
        return None
    return (
        f"Your interest profile was last reviewed {reviewed.isoformat()} "
        f"({days // 30} months ago) - are quant and maths still the priority? Every "
        "relevance call in this digest assumes so. Edit OWNER_PROFILE in classify.py "
        "and bump PROFILE_LAST_REVIEWED."
    )


def _judgment_line(judgment: Judgment, suppress_reason: bool = False) -> str:
    change = judgment.change
    head = f"**{judgment.program_name or change.source_id} — {change.key}**"
    bits = [head]
    if judgment.why and not (suppress_reason and not judgment.classified):
        bits.append(judgment.why)
    elif change.kind == "added":
        bits.append("New row appeared.")
    elif change.kind == "removed":
        bits.append("Row disappeared.")
    else:
        bits.append("Row changed.")
    if change.rolling:
        bits.append("ROLLING review — closes when full.")
    if judgment.suggested_action:
        bits.append(judgment.suggested_action)
    if not judgment.classified:
        bits.append("_Unverified — open the page to confirm._")
    if judgment.confidence == "low" and judgment.classified:
        bits.append("_Low confidence — surfaced deliberately rather than dropped._")
    line = " ".join(bits)
    if change.url:
        line += f" → {change.url}"
    return line


def render(
    judgments: list[Judgment],
    results: list[state.SourceResult],
    sources: dict[str, dict[str, str]],
    status_only: bool = False,
) -> tuple[str, str]:
    """Return (issue title, issue body).

    `status_only` says there was no news -- it only picks the title. The body is the
    same shape every day: on a quiet day that is the calendar block and the health
    block, which is exactly what a reminder with nothing to report should look like.
    """
    today = state.today_iso()
    health_lines, escalated = _health_lines(results, sources)

    act_now = [j for j in judgments if j.urgent]
    worth_a_look = [j for j in judgments if j.relevant and not j.urgent]
    ruled_out = [j for j in judgments if not j.relevant]

    if escalated:
        title = f"Opportunity digest — {today} (source failing)"
    elif status_only:
        # Quiet days mail now, so the title has to carry the whole message for an owner
        # triaging a notification list without opening anything.
        title = f"Opportunity digest — {today} (no changes)"
    else:
        title = f"Opportunity digest — {today}"

    body: list[str] = [f"Opportunity digest — {today}", ""]

    # If every unclassified item shares one reason, say it once rather than on every
    # line. Repeating a 90-character disclaimer per row buries the actual content.
    reasons = {j.why for j in judgments if not j.classified and j.why}
    suppress_reason = len(reasons) == 1
    if suppress_reason:
        body.append(f"> Note: {reasons.pop()}")
        body.append("")

    if escalated or act_now:
        body.append(f"## ■ ACT NOW ({len(escalated) + len(act_now)})")
        for line in escalated:
            body.append(f"- {line}")
        for judgment in act_now:
            body.append(f"- {_judgment_line(judgment, suppress_reason)}")
        body.append("")

    if worth_a_look:
        body.append(f"## ■ WORTH A LOOK ({len(worth_a_look)})")
        for judgment in worth_a_look:
            body.append(f"- {_judgment_line(judgment, suppress_reason)}")
        body.append("")

    if ruled_out:
        body.append(f"<details><summary>■ RULED OUT ({len(ruled_out)})</summary>")
        body.append("")
        body.append(
            "_Read and judged out of scope - usually a class-year gate on the posting, "
            "or a role outside quant/maths/software. Expand to audit; a wrong call here "
            "is the expensive kind, so the reasons are shown rather than hidden._"
        )
        body.append("")
        for judgment in ruled_out:
            body.append(
                f"- **{judgment.change.key}** — {judgment.why or 'not relevant'}"
            )
        body.append("")
        body.append("</details>")
        body.append("")

    month, reminders = calendar_reminders.for_month()
    body.append(f"## ■ CALENDAR ({month})")
    for reminder in reminders or ["Nothing scheduled for this month."]:
        body.append(f"- {reminder}")
    for reminder in calendar_reminders.ALWAYS:
        body.append(f"- {reminder}")
    stale = _stale_profile_line()
    if stale:
        body.append(f"- {stale}")
    body.append("")

    body.append("## ■ HEALTH")
    for line in health_lines:
        body.append(f"- {line}")
    if ruled_out:
        # Surfaced here as well as in the collapsed block: the filter silently eating
        # real opportunities is the failure mode worth noticing, and an implausible
        # count is the cheapest signal that it is happening.
        body.append(
            f"- {len(ruled_out)} of {len(judgments)} changes were filtered out by the "
            "classifier (see RULED OUT above)."
        )

    return title, "\n".join(body)


def should_send(
    judgments: list[Judgment],
    results: list[state.SourceResult],
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


def delivered_issue_exists(date: str) -> bool | None:
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
                "User-Agent": state.USER_AGENT,
            },
            # Sorted newest first, so 50 covers any plausible backlog of same-day ticks
            # without paginating.
            params={
                "state": "all",
                "sort": "created",
                "direction": "desc",
                "per_page": 50,
            },
            timeout=state.HTTP_TIMEOUT_SECONDS,
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
            "User-Agent": state.USER_AGENT,
        },
        json={"title": title, "body": body},
        timeout=state.HTTP_TIMEOUT_SECONDS,
    )
    if response.status_code >= 300:
        raise RuntimeError(
            f"could not open issue: HTTP {response.status_code} {response.text[:300]}"
        )
    return f"opened issue {response.json().get('html_url')}"
