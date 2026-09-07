"""Render the digest and deliver it as a GitHub Issue (spec section 9).

Issues rather than email: GitHub emails the owner when an issue is opened in their own
repo, so there is no SMTP, no API key that expires silently, and no deliverability
problem. It also doubles as a lightweight application tracker -- comment and close.

Cadence is change-only plus a Monday heartbeat. A daily "0 changes" email trains the
owner to archive unread, which is precisely when the FTTP notice arrives; but a silent
failure must never look like a quiet day, so if no email arrives on a Monday, something
is broken.
"""
from __future__ import annotations

import os

import httpx

import calendar_reminders
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
    health_only: bool = False,
) -> tuple[str, str]:
    """Return (issue title, issue body)."""
    today = state.today_iso()
    health_lines, escalated = _health_lines(results, sources)

    act_now = [j for j in judgments if j.urgent]
    worth_a_look = [j for j in judgments if j.relevant and not j.urgent]
    ruled_out = [j for j in judgments if not j.relevant]

    if health_only:
        title = f"Weekly health summary — {today}"
    elif escalated:
        title = f"Opportunity digest — {today} (source failing)"
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
    body.append("")

    body.append("## ■ HEALTH")
    for line in health_lines:
        body.append(f"- {line}")

    return title, "\n".join(body)


def should_send(
    judgments: list[Judgment],
    results: list[state.SourceResult],
    is_monday: bool,
    already_delivered_today: bool = False,
) -> tuple[bool, bool]:
    """Return (send, health_only).

    Send when there is something to say, or when it is Monday. The Monday heartbeat is
    the thing that makes silence diagnostic.

    `already_delivered_today` suppresses the heartbeat only. The morning schedule fires
    several times so that a dropped cron tick is not a missed day (see daily.yml), and
    the heartbeat is the one path with no natural interlock -- a change-driven digest
    cannot repeat, because the snapshots advance on the first success. Three identical
    health summaries every Monday would train the owner to archive the digest unread,
    which is the exact failure the change-only cadence exists to prevent. A retry that
    finds real changes still delivers: that is news, not a duplicate.
    """
    has_changes = bool(judgments)
    has_failures = any(not result.ok for result in results)
    has_baseline = any(result.baseline for result in results)
    if has_changes or has_failures or has_baseline:
        return True, False
    if is_monday and not already_delivered_today:
        return True, True
    return False, False


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
