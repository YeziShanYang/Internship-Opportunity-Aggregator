"""The shapes every stage passes to the next one.

These carry no behaviour that touches the outside world, which is the point: a `Change`
can be built by `process`, read by `screen`, judged by `classify` and rendered by
`deliver` without any of those four needing to agree on anything else.

`SourceResult` is the load-bearing one. Spec section 10.1 says a failure must never be
indistinguishable from a quiet day, and this is where that is made structural rather
than remembered: `ok=False` and `ok=True, changes=[]` are different facts, and every
checker returns one of these whether it worked or not.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Change:
    """One observed upstream change, before classification."""

    source_id: str
    kind: str  # added | removed | changed
    key: str  # human-readable row identity, e.g. "Akuna Capital / QD"
    detail: str  # what changed, as text the classifier and the reader both see
    url: str = ""
    program_name: str = ""  # filled from sources.csv program_names
    is_discovery_candidate: bool = False
    rolling: bool = False
    # Some sources hand back the full posting body in the same response that lists it
    # -- Greenhouse `content=true`, Lever and Ashby `descriptionPlain`. Carrying it
    # here lets the classifier judge real requirements without a second HTTP request,
    # and without `enrich` having to guess a URL it cannot always construct.
    posting_text: str = ""


@dataclass
class SourceResult:
    """Outcome of checking one source.

    Spec section 10.1: a failure must never be indistinguishable from a quiet day, so
    every check returns one of these whether it worked or not. `ok=False` and
    `ok=True, changes=[]` are different facts and are reported differently.
    """

    source_id: str
    ok: bool
    changes: list[Change] = field(default_factory=list)
    error: str = ""
    content_length: int = 0
    snapshot_text: str | None = None  # new snapshot to persist, if the check succeeded
    baseline: bool = False  # first ever sight of this source: record, do not report
    # Tier 1 and 2 store a canonical TSV; Tier 3 stores normalised page text.
    snapshot_ext: str = "tsv"
    extra: dict[str, Any] = field(default_factory=dict)
    # True when the circuit breaker skipped the fetch. Neither a success nor a new
    # failure: the counters must not move, or a quarantine would inflate itself.
    quarantined: bool = False
