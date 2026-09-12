"""Who the digest is for. The single place the owner's interests are encoded.

This is perishable input, not a constant, and that is the whole reason it has a module
of its own with a review date beside it. A stale profile does not fail loudly -- it
quietly mis-sorts every item in every digest, and the output still looks perfectly
well-formed while doing it. `PROFILE_LAST_REVIEWED` drives a reminder in the CALENDAR
block once it goes stale; bump it by hand whenever the text changes.

The date is a backstop, not a substitute for asking. Anyone working on code that reads
this, or having a conversation that touches what the owner is looking for, should ask
whether it still holds.

It lives in `core` because `classify` passes it verbatim to the model and `deliver`
renders the staleness nag, and neither may import the other.
"""
from __future__ import annotations

PROFILE_LAST_REVIEWED = "2026-09-11"
PROFILE_REVIEW_AFTER_DAYS = 183  # ~6 months

OWNER_PROFILE = (
    "First-year undergraduate at Stanford, class of 2030, studying math and/or CS. "
    "Does not meet the eligibility criteria for identity-restricted programmes "
    "-- those reserved for women, transgender or gender-expansive students, "
    "underrepresented racial minorities, or LGBTQIA+ students -- nor for \"barriers "
    "to access and opportunity\" criteria. US citizen, US-based. "
    "Regional St. Louis firms are viable summer options. "
    "Interests, in order: quantitative finance and mathematics first, software "
    "engineering second. General finance roles are adjacent and acceptable."
)
