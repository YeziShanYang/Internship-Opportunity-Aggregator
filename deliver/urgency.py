"""Which rows go in ACT NOW rather than WORTH A LOOK.

A pure function rather than a property on `Judgment`, because three of its four inputs
live on the `Change` and were never properties of the model's answer -- and because
urgency is a presentation decision. The same judgment could reasonably be urgent in one
surface and not in another; the classifier has no opinion about it.
"""
from __future__ import annotations

from core import models


def is_urgent(judgment: models.Judgment) -> bool:
    """ACT NOW is only useful while it stays short.

    This used to mean "relevant and confidently classified", which worked while only
    high-signal sources were classified at all. Once every source was classified that
    rule promoted every generic-but-eligible job-board row: a measured run put 22 of
    them in ACT NOW and left WORTH A LOOK empty, burying the two or three items that
    actually needed same-day attention.

    So urgency means one of two specific things -- a firm that reviews on a rolling
    basis and closes when full, or a row that reads as aimed at first-years. Merely
    being eligible for something is WORTH A LOOK. (Failing sources are escalated into
    the same block separately, by the renderer.)
    """
    change = judgment.change
    if not judgment.relevant:
        return False
    if change.rolling:  # closes when full; time-critical regardless of stage
        return True
    if not change.is_discovery_candidate:
        return False
    return judgment.classified and judgment.confidence in ("medium", "high")
