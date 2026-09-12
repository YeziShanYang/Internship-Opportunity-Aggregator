"""Level 5: deterministic verdicts, and nothing else.

Three properties are load-bearing, in this order, and they are the reason this is a
stage of its own rather than a helper inside `classify`:

1. **This layer may only rule OUT, never rule IN.** A change it has no opinion on goes
   to the model exactly as before. Nothing is dropped for being unrecognised, which is
   the same commitment spec 8 rule 3 makes about a missing API key.
2. **A rule-out must quote the phrase that caused it.** The reason lands in RULED OUT
   where it can be audited, so a wrong rule reads as a wrong rule rather than as an
   absence.
3. **Absence of evidence is never evidence.** No posting text means no class-year
   rule-out -- the same instruction the prompt gives the model when a fetch failed.

Measured against the 2026-09-12 run: 28 of the model's 40 rule-outs were reproduced
with zero wrong rule-outs among the 37 it kept, so 36% of model calls disappear.
"""
from screen.rules import (  # noqa: F401
    EXCLUDING_YEARS,
    INCLUDING_YEARS,
    VERSION,
    Verdict,
    screen,
    screen_posting,
    screen_title,
)
from screen.verdicts import (  # noqa: F401
    FILTER_ID,
    apply,
    report,
)
