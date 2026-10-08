"""Which rows are High rather than Medium, and why; and which are for underclassmen.

The tiers were ACT NOW and WORTH A LOOK until 2026-10-08. They are now High and Medium,
each row carries the reason it got its tier, and postings aimed at first-years have
their own block at the top of the digest instead of competing for the High tier.

A pure function rather than a property on `Judgment`, because three of its four inputs
live on the `Change` and were never properties of the model's answer -- and because
urgency is a presentation decision. The same judgment could reasonably be urgent in one
surface and not in another; the classifier has no opinion about it.
"""
from __future__ import annotations

import datetime
import re
from urllib.parse import urlsplit

from core import clock, models
from screen import rules as screen_rules

# The word the model returns for "reviews on a rolling basis / closes when full". One
# constant because two modules compare against it and a typo in either would silently
# stop promoting the most time-critical rows there are.
ROLLING = "rolling"

HIGH = "High"
MEDIUM = "Medium"

# A stated deadline this close is same-day news. Three weeks is the window in which the
# owner still has to write an application rather than merely note one: shorter and a
# posting found on a Friday would be WORTH A LOOK until the Monday it expired, longer
# and every Summer 2027 posting with an autumn close date lands in ACT NOW at once,
# which is the flood this rule already had to be narrowed once to prevent.
URGENT_WITHIN_DAYS = 21

# Who the posting is *for*. Deliberately narrower than `snapshot.DISCOVERY_PATTERN`,
# which also matches insight/discovery/ignite/launch and exists to spot a candidate new
# *programme*; this one has to be true of the role itself, because a pin lasts until the
# posting comes down and a wrong one is a wrong row every morning rather than once.
UNDERCLASSMAN = re.compile(
    r"first.?year|1st.?year|freshman|freshmen|sophomore|second.?year|2nd.?year"
    r"|underclass(?:man|men)?|rising sophomore",
    re.IGNORECASE,
)


# A class-year phrase that names first- or second-years but is not *aimed* at them. Added
# 2026-09-30: the pins had filled with rows like GE Healthcare's "at
# least sophomore standing" and Gilead's "freshman, sophomore, junior, or senior", which
# mention the word while being a floor or an open door. A posting is told once when it
# appears, and pinned only when it is a real match -- a programme for
# first- and second-years. A floor ("at least", "minimum", "completed") and an
# enumeration that runs on to juniors or seniors are both general postings.
NOT_TARGETED = re.compile(
    r"at\s+least|minimum|\bmin\.?\s|or\s+(?:higher|above|beyond)|and\s+(?:up|above)"
    r"|complet\w*|\+|junior|senior|third|fourth|3rd|4th|all\s+(?:class\s+)?years"
    r"|any\s+(?:class\s+)?year|graduate\s+student",
    re.IGNORECASE,
)


def _aimed_at_underclassmen(class_year: str, title: str) -> bool:
    """The shared test for a live judgment and a carried pin.

    The title is decisive on its own ("First-Year Insight"): an employer that names the
    year in the job title has built the role for it. The class-year phrase has to both
    name the year and not be a floor or a wider list.

    Sophomores alone are not a match. The owner is a first-year, so "Sophomore Intern"
    is a class-year rule-out and not a pin; Thrivent's sat at the top of the digest on
    2026-10-08 until this check existed. Re-asked of carried pins, so that one retires.
    """
    if screen_rules.sophomore_only(title) or screen_rules.sophomore_only(class_year):
        return False
    if UNDERCLASSMAN.search(title):
        return True
    return bool(UNDERCLASSMAN.search(class_year)) and not NOT_TARGETED.search(class_year)


def _deadline_passed(deadline: str) -> bool:
    days = clock.days_until(deadline)
    return days is not None and days < 0


def targets_underclassmen(judgment: models.Judgment) -> bool:
    """Whether this posting is aimed at first- or second-years.

    Read off `Judgment.class_year` and the row's own title -- never off the section
    heading the row sits under. That distinction dates from 2026-09-18 and it is the
    difference between a usable block and an undeliverable one: three of
    the watched repos (`luisae`, `underclassmen-cruz`, `underclassmen-zapply`) are
    underclassman trackers end to end, so their section headings carry the word on
    every row. `Row.identity` is section-qualified, so matching the whole snapshot line
    put 114 rows in scope -- 110 of them from those three repos, and at 577 bytes a
    table row that is ~63KB against GitHub's 65,536-character issue limit. The digest
    would have failed to send rather than merely read badly.

    `Change.key` is the row's own key, not the identity, so it is safe to read here;
    `Change.detail` is not, because on a `changed` row it carries the whole before and
    after including the heading.
    """
    if judgment.change.structural:
        # A collapse notice or a new-section row. Its `key` is the source's own prose,
        # not a job title: "underclassmen-cruz: 78 of 107 rows changed at once" matched
        # this rule and pinned a board restructure into ACT NOW, where it would have sat
        # until someone noticed. Measured on a forced re-baseline, 2026-09-18.
        return False
    return _aimed_at_underclassmen(judgment.class_year, judgment.change.key)


def is_stanford(judgment: models.Judgment) -> bool:
    """Whether the change comes from a Stanford page (stanford.edu, SLAC included).

    Added 2026-10-08: Stanford's own research programmes -- CURIS, SURIM, SIEPR, the
    VPUE index, SOLO's first-year filter -- are open to its first-years almost without
    exception, so they go in the top block with the postings built for them. Read off
    the host rather than a sources.csv column, because the host is the fact and a column
    would be one more thing to remember to set. Unlike an underclassman posting this is
    never pinned: a programme page stays up all year, so a pin would never retire, and a
    page change is news on the morning it happens.
    """
    host = (urlsplit(judgment.change.url or "").hostname or "").lower()
    return host == "stanford.edu" or host.endswith(".stanford.edu")


def in_top_block(judgment: models.Judgment) -> bool:
    """Relevant, and either built for underclassmen or a Stanford programme."""
    return judgment.relevant and (targets_underclassmen(judgment) or is_stanford(judgment))


def is_rolling(judgment: models.Judgment) -> bool:
    """Whether the posting itself says it closes when full."""
    return judgment.deadline.strip().lower() == ROLLING


def _closes(deadline: str, days: int) -> str:
    """"closes Oct 10 (2 days)" inside the High window; past it, only the date."""
    date = datetime.date.fromisoformat(deadline)
    if days > URGENT_WITHIN_DAYS:
        return f"closes {date:%b} {date.day}"
    when = "today" if days == 0 else "1 day" if days == 1 else f"{days} days"
    return f"closes {date:%b} {date.day} ({when})"


def rank(judgment: models.Judgment) -> tuple[str, str]:
    """(tier, reason) for a relevant judgment. The reason is printed beside the tier.

    High is only useful while it stays short, so every High reason is a *dated* one,
    never a quality judgement: a stated close date inside `URGENT_WITHIN_DAYS`, a firm on
    the named rolling list, or a discovery programme. Underclassman postings are not
    ranked here at all -- they have their own block; see `targets_underclassmen`.

    **A posting that merely says it reviews on a rolling basis is Medium.** It was High
    from 2026-09-13 until 2026-10-08, when it accounted for 10 of the 18 opportunities in
    the block -- National Life, Centene, TikTok -- because most postings say it. A rule
    every posting satisfies sorts nothing. The named firms on `ROLLING_PATTERN` stay
    High, because there the claim is measured (Jane Street has filled by late October).
    Within Medium a rolling posting still sorts first; see `medium_order`.

    A deadline the codec could not parse never promotes a row. It is still printed in
    the Notes cell, so it can still be read by hand.
    """
    change = judgment.change
    days = clock.days_until(judgment.deadline)
    if days is not None and 0 <= days <= URGENT_WITHIN_DAYS:
        return HIGH, _closes(judgment.deadline, days)
    if change.rolling:
        return HIGH, "rolling firm, fills early"
    if change.is_discovery_candidate and judgment.classified \
            and judgment.confidence in ("medium", "high"):
        return HIGH, "discovery programme"
    if not judgment.classified:
        return MEDIUM, "unverified"
    if is_rolling(judgment):
        return MEDIUM, "rolling, no close date"
    if days is not None and days > URGENT_WITHIN_DAYS:
        return MEDIUM, _closes(judgment.deadline, days)
    if days is not None:
        return MEDIUM, "deadline passed"
    return MEDIUM, "no deadline stated"


def medium_order(judgment: models.Judgment) -> int:
    """Sort key within Medium: rolling first, then dated, then undated, then unverified."""
    reason = rank(judgment)[1]
    if reason.startswith("rolling"):
        return 0
    if reason.startswith("closes"):
        return 1
    if reason == "unverified":
        return 3
    return 2


def is_urgent(judgment: models.Judgment) -> bool:
    """High tier: relevant, not in the top block, and a dated reason."""
    if not judgment.relevant or in_top_block(judgment):
        return False
    return rank(judgment)[0] == HIGH


def live_change_ids(
    previous: dict[str, set[str]], changes: list[models.Change], checked: set[str]
) -> dict[str, set[str]]:
    """Which rows are on their board *now*, per source.

    Derived from yesterday's snapshots plus today's diff rather than from the new
    snapshots directly, because at pin-refresh time the new ones have not been written
    yet -- and because the subtraction is exactly what the diff already computed. A
    source that was not checked this run keeps yesterday's set verbatim; see
    `refresh_pins` for why that matters.
    """
    live = {source_id: set(ids) for source_id, ids in previous.items()}
    for change in changes:
        if change.source_id not in checked:
            continue
        bucket = live.setdefault(change.source_id, set())
        if change.kind == "removed":
            bucket.discard(change.change_id)
        else:
            bucket.add(change.change_id)
    return live


def refresh_pins(
    judgments: list[models.Judgment],
    existing: list[models.PinnedRow],
    live: dict[str, set[str]],
    checked: set[str],
    today: str,
    describe,
) -> list[models.PinnedRow]:
    """Today's pinned set: yesterday's, minus the ones that came down, plus new ones.

    Two asymmetries are load-bearing, and both are the same instinct the rest of this
    codebase follows -- a wrong keep costs one line the owner skims, a wrong drop hides
    an opportunity.

    A pin is dropped **only** when the source that carries it was checked successfully
    this run and no longer lists the row. A failing source, a quarantined one, or a
    source skipped by `--only` keeps every pin it has: "we have stopped looking" must
    never be rendered as "it closed", which is the same rule the circuit breaker
    follows when it insists a quarantined source still appears in HEALTH.

    And only a *relevant* judgment earns a pin. A row the screen or the classifier
    ruled out is not pinned, so the block does not accumulate the non-US, junior-only
    and off-field postings that the underclassman trackers carry alongside the real
    ones. It still prints once, in RULED OUT, on the morning it moved.
    """
    by_id = {row.change_id: row for row in existing}

    for judgment in judgments:
        if not judgment.relevant or not targets_underclassmen(judgment):
            continue
        change = judgment.change
        if change.kind == "removed":
            by_id.pop(change.change_id, None)
            continue
        company, position = describe(judgment)
        was = by_id.get(change.change_id)
        by_id[change.change_id] = models.PinnedRow(
            change_id=change.change_id,
            source_id=change.source_id,
            company=company,
            position=position,
            url=change.posting_url or change.url,
            deadline=judgment.deadline,
            class_year=judgment.class_year,
            location=judgment.location,
            first_pinned=was.first_pinned if was else today,
            last_seen=today,
        )

    kept = []
    for row in by_id.values():
        # Re-asked of every carried pin, not just new ones, so a rule change retires the
        # pins the old rule made instead of leaving them until their postings close.
        if not _aimed_at_underclassmen(row.class_year, row.position):
            continue
        # A stated close date that has passed ends the pin even while the page is up:
        # Jane Street's Bridge sat pinned three days past its 2026-09-27 deadline.
        if _deadline_passed(row.deadline):
            continue
        if row.source_id in checked:
            if row.change_id not in live.get(row.source_id, set()):
                continue  # checked, and the board no longer lists it
            row.last_seen = today
        kept.append(row)
    kept.sort(key=lambda r: (r.first_pinned, r.company.lower(), r.position.lower()))
    return kept
