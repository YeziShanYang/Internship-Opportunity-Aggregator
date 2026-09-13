"""Which rows go in ACT NOW rather than WORTH A LOOK.

A pure function rather than a property on `Judgment`, because three of its four inputs
live on the `Change` and were never properties of the model's answer -- and because
urgency is a presentation decision. The same judgment could reasonably be urgent in one
surface and not in another; the classifier has no opinion about it.
"""
from __future__ import annotations

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
