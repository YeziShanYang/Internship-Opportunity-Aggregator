"""Render the digest body. Delivery itself is `deliver.issue`.

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

The "no more" half is load-bearing in the other direction. The schedule fires one tick
now, but a manual `workflow_dispatch` can still run alongside it -- that happened on
2026-09-14, when a late tick and a hand-dispatched run overlapped by twenty-one seconds
-- and either would otherwise be free to open its own issue. Two digests for the same
date is the same notification-fatigue failure as a daily "0 changes" email, so delivery
is capped -- see should_send and delivered_issue_exists.
"""
from __future__ import annotations

import datetime
import re

import calendar_reminders
from core import clock, models, paths, profile
from deliver import health, urgency
from enrich import bodies as enrich_bodies

def _stale_profile_line() -> str | None:
    """Nag when the owner profile has not been reviewed in a while.

    Every relevance call in the digest is made against `core.profile.OWNER_PROFILE`. When
    it drifts out of date nothing breaks visibly -- the digest still renders, the
    judgments still look confident, they are just answering last year's question. The
    CALENDAR block is the right home because it already carries recurring
    human-action reminders, and this line costs nothing on the days it does not fire.
    """
    try:
        reviewed = datetime.date.fromisoformat(profile.PROFILE_LAST_REVIEWED)
    except ValueError:
        return None
    days = (datetime.date.fromisoformat(clock.today_iso()) - reviewed).days
    if days < profile.PROFILE_REVIEW_AFTER_DAYS:
        return None
    return (
        f"Your interest profile was last reviewed {reviewed.isoformat()} "
        f"({days // 30} months ago) - are quant and maths still the priority? Every "
        "relevance call in this digest assumes so. Edit OWNER_PROFILE in core/profile.py "
        "and bump PROFILE_LAST_REVIEWED."
    )


# Nothing in the table is truncated. It used to be: a 96-character budget on Notes and
# 70 on Position, on the theory that GitHub wraps a long cell rather than scrolling it
# and an unbounded `why` turns four tidy rows into a wall of text.
#
# That traded the wrong thing away. The owner's objection, on the 2026-09-13 digest,
# was that every Notes cell ended in an ellipsis two lines in -- and because the model
# writes `why` as one sentence beginning with the posting's requirement, the clause
# that got cut was reliably the decisive one ("...and is a Summer 2027 role, so a
# first-year" — then nothing). A clipped sentence is worse than a tall cell: it ends
# mid-clause and the only way to recover the rest is to open the posting, which is the
# work the digest exists to save. Height is now managed by splitting the cell into
# labelled bullets instead, so a long note is scannable rather than a paragraph.
#
# Bullets inside a table cell are `<br>`-joined: GitHub renders a line break there but
# does not render a markdown list, so "- " per line would print literal hyphens.
BULLET_JOIN = "<br>"
BULLET = "• "

ROLLING_NOTE = "rolling — closes when full"

# Past this many sources, a filter line reports the count instead of the names.
MAX_FILTER_SOURCES = 6

URGENCY_ACT_NOW = "**ACT NOW**"
URGENCY_WORTH_A_LOOK = "Worth a look"


def _cell(text: str) -> str:
    """Flatten arbitrary text into something safe inside a markdown table cell.

    Two hazards, both of which have to be handled here rather than at the call sites: a
    literal pipe ends the cell, and a newline ends the whole row. Length is deliberately
    not one of them -- see the note on the bullet constants above.
    """
    return re.sub(r"\s+", " ", (text or "").replace("|", "\\|")).strip()


def _bullets(items: list[tuple[str, str]]) -> str:
    """(label, value) pairs as a bulleted list inside one table cell.

    An empty value is dropped rather than printed as "Year: not specified": a bullet
    that costs a line and says only that the posting was silent is noise, and its
    absence already carries the same information. An empty *label* is allowed, for the
    caveats, which read as sentences rather than as fields.
    """
    lines = []
    for label, value in items:
        value = _cell(value).rstrip(" .;,")
        if not value:
            continue
        lines.append(f"{BULLET}**{label}:** {value}" if label else f"{BULLET}{value}")
    return BULLET_JOIN.join(lines)


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


def _company_and_position(judgment: models.Judgment) -> tuple[str, str]:
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


def _deadline_note(judgment: models.Judgment) -> str:
    """The deadline bullet's text: the rolling sentinel spelled out, or the date.

    A value the model returned that is neither `rolling` nor an ISO date is still
    shown, verbatim. It just never counts as urgent -- see `deliver.urgency`. Showing
    it is the point: a deadline the code could not parse is exactly the one the owner
    needs to read for himself.
    """
    if judgment.change.rolling or urgency.is_rolling(judgment):
        return ROLLING_NOTE
    return judgment.deadline


def _notes(judgment: models.Judgment, suppress_reason: bool = False) -> str:
    """The Notes cell: four labelled facts, then the caveats.

    Bullets rather than one run-on clause, because this column answers four separate
    questions and the owner reads them in this order: how long do I have (Deadline),
    can I even apply (Year), where is it (Location), and why did this reach me (Why).
    All four used to be crushed into a single semicolon-joined sentence, which is what
    made the truncation so costly -- the class year and the location were inside the
    model's one prose sentence, so they were both only ever present by luck.

    Ordered most-decision-relevant first. A rolling deadline changes what the owner
    does today; a confidence caveat only changes how much he trusts a row he is already
    reading, so it goes last.
    """
    change = judgment.change
    items: list[tuple[str, str]] = [
        ("Deadline", _deadline_note(judgment)),
        ("Year", judgment.class_year),
        ("Location", judgment.location),
    ]
    if judgment.why and not (suppress_reason and not judgment.classified):
        items.append(("Why", judgment.why))
    elif change.kind == "removed":
        items.append(("Why", "row disappeared from the source"))
    elif change.kind == "changed":
        items.append(("Why", "row changed on the source"))
    if not judgment.classified:
        items.append(("", "⚠ unverified — open the page"))
    elif judgment.confidence == "low":
        items.append(("", "low confidence, kept deliberately"))
    return _bullets(items)


VALUE = "value"
ELIGIBILITY = "eligibility"

# The phrase a rule-out quoted from the posting. For an eligibility call that phrase is
# the entire evidence -- "rising junior", a graduation window, a clearance requirement --
# and the sentence wrapped around it repeats what the block header already says. Keeping
# only the quote took the block from 274 characters a row to about 90 on the morning it
# had grown to 123 rows and 52% of the whole digest, which is what pushed nine real
# opportunities out of the table for want of room.
_QUOTED = re.compile(r"[\"\u201c\u2018']([^\"\u201c\u201d\u2018\u2019']{3,140})[\"\u201d\u2019']")


def _ruled_out_line(judgment: models.Judgment) -> str:
    """One RULED OUT entry. Compact for eligibility, in full for a value judgement.

    Anything that is not explicitly an eligibility call is printed in full, including a
    row whose `ruled_out_by` is missing entirely. That is the conservative direction: an
    unlabelled rule-out might be the arguable kind, and shortening it would hide exactly
    the reasoning worth reading.
    """
    why = judgment.why or "not relevant"
    if judgment.ruled_out_by == ELIGIBILITY:
        quoted = _QUOTED.search(why)
        if quoted:
            why = f"\u201c{quoted.group(1).strip()}\u201d"
    return f"- **{judgment.change.key}** — {why}"


def _table_row(urgency: str, company: str, position: str, notes: str, url: str = "") -> str:
    company = _cell(company)
    position = _cell(position)
    if url:
        # Any remaining brackets would terminate the link text early. By here the
        # markdown links are already reduced to their labels by `_unlink`, so this is
        # a backstop for a literal bracket in a job title.
        label = position.replace("[", "(").replace("]", ")")
        position = f"[{label}]({url})"
    return f"| {urgency} | {company} | {position} | {notes} |"


def _judgment_row(judgment: models.Judgment, suppress_reason: bool = False) -> str:
    company, position = _company_and_position(judgment)
    return _table_row(
        URGENCY_ACT_NOW if urgency.is_urgent(judgment) else URGENCY_WORTH_A_LOOK,
        company,
        position,
        _notes(judgment, suppress_reason),
        judgment.change.url,
    )


def render(
    judgments: list[models.Judgment],
    results: list[models.SourceMetrics],
    sources: dict[str, dict[str, str]],
    suppressed_applied: int = 0,
    suppressed_muted: int = 0,
    discovery_lines: list[str] | None = None,
    status_only: bool = False,
    enriched: dict[str, enrich_bodies.PostingBody] | None = None,
    filters: list[models.FilterReport] | None = None,
    usage: models.Usage | None = None,
) -> tuple[str, str]:
    """Return (issue title, issue body).

    `status_only` says there was no news -- it only picks the title. The body is the
    same shape every day: on a quiet day that is the calendar block and the health
    block, which is exactly what a reminder with nothing to report should look like.
    """
    today = clock.today_iso()
    health_lines, escalated = health.source_lines(results, sources)

    act_now = [j for j in judgments if urgency.is_urgent(j)]
    worth_a_look = [j for j in judgments
                    if j.relevant and not urgency.is_urgent(j)]
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
    #
    # Collected as a list rather than appended straight onto the body, because the table
    # is the one block that can be trimmed if the whole digest will not fit. Built in
    # priority order, so trimming from the end always drops the least urgent row.
    table: list[str] = [
        _table_row(URGENCY_ACT_NOW, source_id, position, _cell(notes))
        for source_id, position, notes in escalated
    ]
    table += [_judgment_row(j, suppress_reason) for j in act_now]
    table += [_judgment_row(j, suppress_reason) for j in worth_a_look]
    table_index = None
    if table:
        body.append(f"## ■ OPPORTUNITIES ({len(table)})")
        body.append("")
        body.append("| Urgency | Company | Position | Notes |")
        body.append("|---|---|---|---|")
        table_index = len(body)
        body += table
        body.append("")

    if ruled_out:
        body.append(f"<details><summary>■ RULED OUT ({len(ruled_out)})</summary>")
        body.append("")
        body.append(
            "_Read and judged out of scope. Expand to audit; a wrong call here is the "
            "expensive kind, so the reasons are shown rather than hidden. A row ruled "
            "out on **value** — it could be applied to, but the role is not worth a "
            "morning — carries its full reasoning, because that is a judgement and it "
            "has been wrong. A row ruled out on **eligibility** carries the phrase from "
            "the posting that did it, which is the whole of the evidence._"
        )
        body.append("")
        # Value first: they are the arguable ones, and the reader who opens this block
        # is usually opening it to disagree with one.
        for judgment in sorted(ruled_out, key=lambda j: j.ruled_out_by != VALUE):
            body.append(_ruled_out_line(judgment))
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
    # Before the run-wide filter lines, because it comes first in the pipeline and
    # because a posting that could not be read is the reason a screen had no opinion
    # about it.
    enrich_line = enrich_bodies.health_line(enriched or {})
    if enrich_line:
        body.append(f"- {enrich_line}")
    # Every filter in the pipeline reports through one shape and is rendered by one
    # function, here: the per-source screens each checker applied, the two run-wide
    # mute filters, and the deterministic screen. The screen's tally used to be a
    # bespoke sentence read off a module global, which is why a stage could not be run
    # on its own -- `render` on a fresh process printed an empty line against
    # freshly-initialised counters.
    for line in health.filter_lines(filters or []):
        body.append(f"- {line}")
    spend = health.usage_line(usage or models.Usage())
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

    return title, _fit(body, table, table_index, len(judgments))


def _fit(
    body: list[str], table: list[str], table_index: int | None, total: int
) -> str:
    """Join the body, trimming the table if GitHub would refuse the whole thing.

    A 422 for an over-long body opens no issue at all, so without this a busy morning
    loses the entire digest rather than its overflow -- the calendar, the health block
    and every urgent row with it. That is the failure this project cares about most:
    silence is supposed to mean broken, and it did, but the reader has no way to know
    which morning it was or what it would have said.

    Trimming is the last resort and not a strategy. The digest is short because the
    screen and the classifier are selective; when it is still too long, the least urgent
    rows go, the count says exactly how many, and the state commit has all of them. It
    does nothing at all to a digest that already fits, so the byte-identical re-render
    property is untouched on every normal day.
    """
    rendered = "\n".join(body)
    if len(rendered) <= paths.MAX_ISSUE_BODY_CHARS or table_index is None:
        return rendered

    head, tail = body[:table_index], body[table_index + len(table):]
    # Recomputed inside the loop: the note's own length is part of what has to fit, and
    # its digits change as rows come off.
    for keep in range(len(table) - 1, -1, -1):
        note = (
            f"- _{len(table) - keep} of {len(table)} rows did not fit GitHub's "
            f"{paths.MAX_ISSUE_BODY_CHARS:,}-character issue body limit and were "
            "withheld, least urgent first. All of them are in this morning's state "
            "commit._"
        )
        candidate = "\n".join(head + table[:keep] + [""] + [note] + tail)
        if len(candidate) <= paths.MAX_ISSUE_BODY_CHARS:
            return candidate
    # Every row removed and it still does not fit, so the overflow is not the table.
    # Return the un-trimmed body rather than a silently mangled one: the delivery will
    # fail loudly, which is the correct outcome for a bug this is not equipped to fix.
    return rendered
