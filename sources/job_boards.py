"""Tier 2 compatibility shim. The real code is `gather.ats` + `process.parse_ats`.

`check` was fourteen distinct things in one function, including a disk read and a
markdown render. It is five lines now.

Kept as a shim because this is the riskiest module in the refactor: the plausibility
check, the two posting screens and the collapse are independent behaviours that each
report to HEALTH, and `discover.verify` plus four test classes drive `check` directly.
Moving the internals under an unchanged signature is what let all of them stay as they
were.
"""
from __future__ import annotations

import httpx

from core import models
from gather import ats
from gather.ats import (  # noqa: F401
    ASHBY,
    EIGHTFOLD,
    ENDPOINTS,
    GREENHOUSE,
    LEVER,
    PHENOM,
    WORKDAY,
    MalformedPayload,
)
from persist import store
from process import parse_ats
from process.parse_ats import (  # noqa: F401
    MAX_CHANGES_PER_BOARD,
    NOT_A_STUDENT_ROLE,
    NON_US_LOCATION,
    PHENOM_STUDENT_CATEGORY,
    STUDENT_TITLE,
    STUDENT_TITLE_WORKDAY,
    STUDENT_TYPE,
    US_LOCATION,
    _phenom_field,
    is_student_posting,
    is_us,
    parse_ashby,
    parse_greenhouse,
    parse_lever,
    to_snapshot,
)

# `discover` reads this to decide which methods it knows how to propose. It used to be
# the three single-shot parsers only, which is still what it means: a mined slug is a
# bare name, and the paged vendors take a whole tenant URL that cannot be guessed.
PARSERS = {GREENHOUSE: parse_greenhouse, LEVER: parse_lever, ASHBY: parse_ashby}

Posting = models.Posting


def check(source: dict[str, str], client: httpx.Client) -> models.SourceResult:
    """Check one ATS board. Never raises: a failure is a result, not an exception."""
    fetched = ats.fetch(source, client, wants_detail=parse_ats.wants_workday_detail)
    previous = store.read_snapshot(source["source_id"], ext="tsv")
    return parse_ats.assess(source, fetched, previous)
