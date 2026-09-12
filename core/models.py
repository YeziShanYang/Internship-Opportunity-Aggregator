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


@dataclass(frozen=True)
class FilterReport:
    """What one filter removed, in the one shape all of them use.

    Every filter reports what it removed. That rule is older than this dataclass and
    has been broken twice, both times the same way: the filter was added, the HEALTH
    line was not, and the digest stayed plausible while a source went blind. Two of the
    six filter sites reported through *nothing at all* until this shape existed -- the
    `muted` filter, and github_repos' section_include/section_exclude/ignore_columns.

    With one shape, "this filter removed rows and emitted no FilterReport" becomes a
    testable assertion instead of an invisible hole.
    """

    stage: str
    filter_id: str
    considered: int = 0
    removed: int = 0
    source_id: str = ""  # "" means run-wide rather than per-source
    reason: str = ""  # one human sentence, for HEALTH. NEVER parsed
    samples: tuple[str, ...] = ()  # up to 5 removed keys, so a bad filter can be audited


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
    # What each of this source's own filters removed. Separate from `extra` because a
    # filter report is a fact the digest is obliged to print, while `extra` is a loose
    # bag of counters -- and "the filter was added, the HEALTH line was not" is exactly
    # how this rule has been broken twice.
    filters: list[FilterReport] = field(default_factory=list)
    # True when the circuit breaker skipped the fetch. Neither a success nor a new
    # failure: the counters must not move, or a quarantine would inflate itself.
    quarantined: bool = False


@dataclass
class SourceMetrics:
    """Everything one source check reported, except the payload.

    The snapshot text is deliberately absent. It is 22KB for one repo, its home is
    data/snapshots/ where git already tracks it, and carrying it through the stage
    handoff as well would make .run/changes.json unreadable in exactly the way these
    artifacts exist to prevent.

    `ok=False` and `ok=True, change_count=0` stay different facts here, the same way
    they are on SourceResult (spec 10.1).
    """

    source_id: str
    ok: bool
    error: str = ""
    quarantined: bool = False
    baseline: bool = False
    content_length: int = 0
    snapshot_ext: str = "tsv"
    change_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def of(cls, result: SourceResult) -> SourceMetrics:
        return cls(
            source_id=result.source_id,
            ok=result.ok,
            error=result.error,
            quarantined=result.quarantined,
            baseline=result.baseline,
            content_length=result.content_length,
            snapshot_ext=result.snapshot_ext,
            change_count=len(result.changes),
            extra=dict(result.extra),
        )


@dataclass
class ChangeSet:
    """The `.run/changes.json` payload: what moved, and what every filter removed.

    Written after suppression rather than before, because a change that was muted is
    not a change this run is acting on -- but the count of them is, which is what the
    FilterReports carry.
    """

    changes: list[Change] = field(default_factory=list)
    metrics: list[SourceMetrics] = field(default_factory=list)
    filters: list[FilterReport] = field(default_factory=list)


@dataclass
class FetchAttempt:
    """What one network fetch did, without its payload.

    The payload goes to `.run/raw/{source_id}.body` as bytes; this is the row in
    `.run/raw/index.json`. Separating them is what lets `run.py process --only X` read
    one source instead of parsing every body, and it keeps the index small enough to
    scan in one look for all ~157 sources.

    `requested_url` and `final_url` are both here because the difference between them
    is part of whether the fetch succeeded -- see `process.redirect`. `requests` is
    greater than one for the methods whose single logical fetch is several round trips:
    Workday pages a board and then fetches a description per survivor.
    """

    source_id: str
    ok: bool
    error: str = ""
    status: int = 0
    requested_url: str = ""
    final_url: str = ""
    size: int = 0
    sha256: str = ""
    elapsed_ms: int = 0
    requests: int = 1
