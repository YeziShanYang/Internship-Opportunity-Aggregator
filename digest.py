"""Render the digest and deliver it as a GitHub Issue (spec section 9).

Issues rather than email: GitHub emails the owner when an issue is opened in their own
repo, so there is no SMTP, no API key that expires silently, and no deliverability
problem.

The body is one table -- urgency, company, position, notes -- and nothing else but the
calendar and health footers. It used to be two prose sections of checkbox list items,
which read as an explanation of the project rather than a list of things to go and do.
The checkboxes went with it: a GitHub task list only renders as a tickable box in a list
item, never inside a table cell, so the table and tick-to-dismiss were mutually
exclusive. `data/applied.tsv` survives as a hand-editable mute list.

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
import re

import httpx

import calendar_reminders
import classify
import state
from classify import Judgment

GITHUB_API = "https://api.github.com"


def _health_lines(
    results: list[state.SourceResult], sources: dict[str, dict[str, str]]
) -> tuple[list[str], list[tuple[str, str, str]]]:
    """Return (health block lines, escalated failures as ACT NOW table rows).

    The escalated half is returned as (company, position, notes) rather than a rendered
    sentence because it shares the opportunities table: a source that has gone blind is
    the most actionable thing the digest can carry, and burying it under the table in a
    prose block is how it gets skimmed past.
    """
    healthy = [r for r in results if r.ok]
    failing = [r for r in results if not r.ok]
    lines = [
        f"{len(results)} sources checked · {len(healthy)} healthy · "
        f"{len(failing)} FAILING"
        if failing
        else f"{len(results)} sources checked · {len(healthy)} healthy"
    ]
    escalated: list[tuple[str, str, str]] = []
    quarantined = [r for r in failing if r.quarantined]
    if quarantined:
        # Reported as its own fact. "We have stopped looking at this source" is a
        # different and more serious statement than "this fetch failed", and it is the
        # one a reader is most likely to assume did not happen.
        lines.append(
            f"⚠ {len(quarantined)} source(s) were not fetched at all — the circuit "
            f"breaker has them quarantined: "
            + ", ".join(sorted(r.source_id for r in quarantined))
        )
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
                (
                    result.source_id,
                    f"SOURCE BLIND — {count} failures running",
                    f"not quiet, blind. Last success {last_success}. {result.error}",
                )
            )
    for result in results:
        if result.baseline:
            lines.append(
                f"· {result.source_id}: first run, recorded "
                f"{result.extra.get('rows', 0)} rows as the baseline."
            )
    # Every filter reports what it removed. A filter that hides silently is how a
    # source goes blind without anyone noticing, and Tier 2 applies two of them.
    not_student = sum(r.extra.get("suppressed_not_student", 0) for r in results)
    not_us = sum(r.extra.get("suppressed_not_us", 0) for r in results)
    if not_student or not_us:
        lines.append(
            f"· job boards: {not_student} postings were not student roles and "
            f"{not_us} were outside the US."
        )
    for result in results:
        # Not a failure -- the fetch worked -- but the row is watching a page it was
        # not configured for, which is a coverage hole rather than an outage. Said out
        # loud every morning until the url is corrected, because the failure mode it
        # replaces was silence.
        redirected = result.extra.get("redirected")
        if redirected:
            lines.append(f"⚠ {result.source_id}: {redirected}")
    for result in results:
        collapsed = result.extra.get("collapsed")
        if collapsed:
            lines.append(
                f"⚠ {result.source_id}: {collapsed} rows changed at once and were "
                "collapsed into one item — that reads as a board restructure."
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


# Column budgets for the opportunities table. GitHub wraps a long cell rather than
# scrolling it, so an unbounded `why` turns four tidy rows into a wall of text and
# defeats the point of the table. These are the widths at which a row still reads as a
# row on a phone.
POSITION_CHARS = 70
COMPANY_CHARS = 34
NOTE_CHARS = 96

URGENCY_ACT_NOW = "**ACT NOW**"
URGENCY_WORTH_A_LOOK = "Worth a look"


def _cell(text: str, limit: int = 0) -> str:
    """Flatten arbitrary text into something safe inside a markdown table cell.

    Three hazards, all of which have to be handled here rather than at the call sites:
    a literal pipe ends the cell, a newline ends the whole row, and an over-long value
    wraps the table into unreadability.
    """
    text = re.sub(r"\s+", " ", (text or "").replace("|", "\\|")).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;.:-—–") + "…"
    return text


# An aggregator row's key carries the real employer, as a markdown link:
# "[InfiniteQuant](https://simplify.jobs/c/InfiniteQuant) / Quantitative Trader @ NYC".
# Matching the link shape rather than splitting every key on " / " is deliberate: a
# job-board key is "Title @ Location", and a title containing a slash ("Software
# Engineer / Backend") would otherwise be read as a company called "Software Engineer".
_LINKED_ENTITY = re.compile(r"^\[([^\]]+)\]\([^)]*\)\s*/\s*(.+)$")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _unlink(text: str) -> str:
    """Markdown link syntax down to its label.

    Escaping the brackets instead, which is what the first version did, rendered
    "[InfiniteQuant](url) / Quantitative Trader" as "(InfiniteQuant)(url) /
    Quantitative Trader" inside the cell -- the URL printed twice and the employer in
    parentheses.
    """
    return _MD_LINK.sub(r"\1", text)


def _company_and_position(judgment: Judgment) -> tuple[str, str]:
    """Split one change into the table's company and position columns.

    Job-board rows are already keyed "Title @ Location", which is exactly the position
    column. Page-text rows are keyed on the programme name instead -- "Optiver
    FutureFocus (3 added, 1 removed)" -- so the company name would otherwise be printed
    twice and waste the only two columns that carry the identity of the thing.
    """
    change = judgment.change
    company = judgment.program_name or change.program_name or change.source_id
    position = change.key

    # Prefer the employer named in the row over the aggregator that carried it. For a
    # Simplify or zshah row, `program_names` is the list's own name, which is the same
    # useless string on every one of its 500+ rows.
    linked = _LINKED_ENTITY.match(position)
    if linked:
        return _unlink(linked.group(1)), _unlink(linked.group(2))

    position = _unlink(position)
    if company and position.lower().startswith(company.lower()):
        position = position[len(company):].lstrip(" -–—:·,").strip()
        # What is left of a page-text key is the bare change count, "(2 added, 0
        # removed)", which is not a position and reads as a typo as a link label.
        if position.startswith("("):
            position = f"page updated {position}"
    return company, position or "page updated"


def _notes(judgment: Judgment, suppress_reason: bool = False) -> str:
    """The short clause at the end of a row.

    Ordered most-decision-relevant first, because this is the column the width budget
    truncates. A rolling deadline changes what the owner does today; a confidence
    caveat only changes how much he trusts the row he is already reading.
    """
    change = judgment.change
    bits: list[str] = []
    if change.rolling:
        bits.append("ROLLING — closes when full")
    if judgment.why and not (suppress_reason and not judgment.classified):
        bits.append(judgment.why)
    elif change.kind == "removed":
        bits.append("row disappeared")
    elif change.kind == "changed":
        bits.append("row changed")
    if judgment.suggested_action:
        bits.append(judgment.suggested_action)
    if not judgment.classified:
        bits.append("unverified — open the page")
    elif judgment.confidence == "low":
        bits.append("low confidence, kept deliberately")
    # The model ends `why` with a full stop, so joining raw gives "...dropped.; low
    # confidence" -- two marks of punctuation in a row inside a 96-character budget.
    return _cell("; ".join(bit.rstrip(" .") for bit in bits if bit.strip()), NOTE_CHARS)


def _table_row(urgency: str, company: str, position: str, notes: str, url: str = "") -> str:
    company = _cell(company, COMPANY_CHARS)
    position = _cell(position, POSITION_CHARS)
    if url:
        # Any remaining brackets would terminate the link text early. By here the
        # markdown links are already reduced to their labels by `_unlink`, so this is
        # a backstop for a literal bracket in a job title.
        label = position.replace("[", "(").replace("]", ")")
        position = f"[{label}]({url})"
    return f"| {urgency} | {company} | {position} | {notes} |"


def _judgment_row(judgment: Judgment, suppress_reason: bool = False) -> str:
    company, position = _company_and_position(judgment)
    return _table_row(
        URGENCY_ACT_NOW if judgment.urgent else URGENCY_WORTH_A_LOOK,
        company,
        position,
        _notes(judgment, suppress_reason),
        judgment.change.url,
    )


def render(
    judgments: list[Judgment],
    results: list[state.SourceResult],
    sources: dict[str, dict[str, str]],
    suppressed_applied: int = 0,
    suppressed_muted: int = 0,
    discovery_lines: list[str] | None = None,
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

    # One table rather than two sections, sorted so every ACT NOW row sits above every
    # WORTH A LOOK row. The urgency column carries the distinction the headings used to.
    if escalated or act_now or worth_a_look:
        body.append(
            f"## ■ OPPORTUNITIES ({len(escalated) + len(act_now) + len(worth_a_look)})"
        )
        body.append("")
        body.append("| Urgency | Company | Position | Notes |")
        body.append("|---|---|---|---|")
        for source_id, position, notes in escalated:
            body.append(
                _table_row(URGENCY_ACT_NOW, source_id, position, _cell(notes, NOTE_CHARS))
            )
        for judgment in act_now:
            body.append(_judgment_row(judgment, suppress_reason))
        for judgment in worth_a_look:
            body.append(_judgment_row(judgment, suppress_reason))
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

    if discovery_lines:
        body.append("## ■ DISCOVERED")
        body += discovery_lines
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
    # What the run cost, in the run's own report. A cost that only appears on a billing
    # page a day later is a cost nobody notices drifting upward.
    # Before the spend line, because it explains part of it: a change the screen
    # settled is a model call that did not happen.
    screened = classify.screen_line()
    if screened:
        body.append(f"- {screened}")
    spend = classify.usage_line()
    if spend:
        body.append(f"- {spend}")
    if suppressed_applied:
        body.append(
            f"- {suppressed_applied} item(s) are muted in data/applied.tsv and were "
            "hidden. Delete the line to bring one back."
        )
    if suppressed_muted:
        # This filter reported through nothing at all until 2026-09-12, which is how it
        # went unnoticed that it was not filtering either.
        body.append(
            f"- {suppressed_muted} item(s) were hidden because every programme their "
            "source informs is muted=true in data/programs.csv."
        )
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
