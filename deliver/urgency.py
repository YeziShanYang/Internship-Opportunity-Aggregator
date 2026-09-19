"""Which rows go in ACT NOW rather than WORTH A LOOK.

A pure function rather than a property on `Judgment`, because three of its four inputs
live on the `Change` and were never properties of the model's answer -- and because
urgency is a presentation decision. The same judgment could reasonably be urgent in one
surface and not in another; the classifier has no opinion about it.
"""
from __future__ import annotations

import re

from core import clock, models

# The word the model returns for "reviews on a rolling basis / closes when full". One
# constant because two modules compare against it and a typo in either would silently
# stop promoting the most time-critical rows there are.
ROLLING = "rolling"

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


def targets_underclassmen(judgment: models.Judgment) -> bool:
    """Whether this posting is aimed at first- or second-years.

    Read off `Judgment.class_year` and the row's own title -- never off the section
    heading the row sits under. That distinction was asked for directly on 2026-09-18
    and it is the difference between a usable block and an undeliverable one: three of
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
    if UNDERCLASSMAN.search(judgment.class_year):
        return True
    return bool(UNDERCLASSMAN.search(judgment.change.key))


def is_rolling(judgment: models.Judgment) -> bool:
    """Whether the posting itself says it closes when full."""
    return judgment.deadline.strip().lower() == ROLLING


def is_urgent(judgment: models.Judgment) -> bool:
    """ACT NOW is only useful while it stays short, and only honest while it is not empty.

    This used to mean "relevant and confidently classified", which worked while only
    high-signal sources were classified at all. Once every source was classified that
    rule promoted every generic-but-eligible job-board row: a measured run put 22 of
    them in ACT NOW and left WORTH A LOOK empty, burying the two or three items that
    actually needed same-day attention. So it was narrowed to two specific things -- a
    firm on the rolling list, or a row that reads as aimed at first-years.

    Narrowing it that way overshot, and the 2026-09-13 digest is the measurement: 27
    rows in the block, 18 of them real opportunities, and **not one** of the 18 urgent.
    Only the nine blind sources were there. The reason is that both surviving tests read
    regexes over the *snapshot row text* -- a title, a location and some links -- while
    the deadline lives in the posting body that `enrich` had already fetched and the
    model had already read. On the source that supplies nearly all the volume, 7 of 592
    rows mention a class year at all, so `is_discovery_candidate` is about 1% there and
    `rolling` only fires for five named firms. ACT NOW could not have promoted an
    opportunity on that source however urgent it was.

    So the rule now asks the evidence: a firm on the rolling list, a posting that says
    it reviews on a rolling basis, or a stated close date inside three weeks. Merely
    being eligible for something is still WORTH A LOOK -- the block has to stay short,
    and every test here is a *dated* reason rather than a quality judgment.

    A deadline the codec could not parse never promotes a row. It is still printed in
    the Notes cell, so the case degrades to the owner reading the date himself rather
    than to a row quietly going missing. (Failing sources are escalated into the same
    block separately, by the renderer.)
    """
    change = judgment.change
    if not judgment.relevant:
        return False
    if targets_underclassmen(judgment):  # standing fact, not a dated one; see pins below
        return True
    if change.rolling:  # on the named list; time-critical regardless of stage
        return True
    if is_rolling(judgment):  # the posting says so itself
        return True
    days = clock.days_until(judgment.deadline)
    if days is not None and 0 <= days <= URGENT_WITHIN_DAYS:
        return True
    if not change.is_discovery_candidate:
        return False
    return judgment.classified and judgment.confidence in ("medium", "high")


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
        if row.source_id in checked:
            if row.change_id not in live.get(row.source_id, set()):
                continue  # checked, and the board no longer lists it
            row.last_seen = today
        kept.append(row)
    kept.sort(key=lambda r: (r.first_pinned, r.company.lower(), r.position.lower()))
    return kept
