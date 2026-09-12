"""The two run-wide mute filters, and the reports they owe HEALTH.

Both of these sat loose in `check.main` as a pair of inline comprehensions with a
`before - after` subtraction each. That is how one of them managed to be broken for
weeks: `program_names` in sources.csv is "|"-separated, the filter compared the whole
field against a single programme name, and so muting had no effect at all on any source
covering more than one programme -- while reporting through nothing, which is why
nobody noticed it was not filtering. The two halves of the same failure.

Every filter reports what it removed, so each one here returns a `FilterReport` even
when it removed nothing. Reporting zero is the useful case: it is the difference between
"this filter is quiet today" and "this filter is not running".
"""
from __future__ import annotations

from core import clock, models

MUTED = "muted-programme"
APPLIED = "applied-tsv"

# Enough removed keys to audit a filter that has started over-matching, few enough that
# the artifact stays readable.
MAX_SAMPLES = 5


def muted_programmes(programs: list[dict[str, str]]) -> set[str]:
    return {
        program["name"]
        for program in programs
        if (program.get("muted") or "").strip().lower() == "true"
    }


def _all_muted(change: models.Change, muted: set[str]) -> bool:
    """True only when EVERY programme this change's source informs is muted.

    Muting "Jane Street INSIGHT" must not also silence FTTP news arriving on the same
    row, and an empty `program_names` is a whole job board rather than a muted
    programme, so it is never muted.
    """
    names = [n.strip() for n in (change.program_name or "").split("|") if n.strip()]
    return bool(names) and all(name in muted for name in names)


def _report(filter_id: str, considered: int, removed: list[models.Change], reason: str):
    return models.FilterReport(
        stage="process",
        filter_id=filter_id,
        considered=considered,
        removed=len(removed),
        reason=reason,
        samples=tuple(change.key for change in removed[:MAX_SAMPLES]),
    )


def suppress(
    changes: list[models.Change],
    *,
    muted: set[str],
    applied: dict[str, str],
) -> tuple[list[models.Change], list[models.FilterReport]]:
    """Drop muted and already-actioned changes. Returns (kept, reports).

    Order matters only for the counts: a change that is both muted and in applied.tsv
    is attributed to the muted filter, because that is the more specific statement about
    why the owner is not seeing it.
    """
    reports: list[models.FilterReport] = []

    considered = len(changes)
    dropped = [change for change in changes if _all_muted(change, muted)]
    changes = [change for change in changes if not _all_muted(change, muted)]
    reports.append(_report(
        MUTED, considered, dropped,
        "every programme their source informs is muted=true in data/programs.csv",
    ))

    # Dropped before classification, which also saves the model call. This used to be
    # populated by ticking a checkbox in a delivered digest; the digest is a table now
    # and a table cell cannot hold a working checkbox, so the file is hand-edited.
    considered = len(changes)
    keys = {change.key: clock.change_key(change.source_id, change.key) for change in changes}
    dropped = [change for change in changes if keys[change.key] in applied]
    changes = [change for change in changes if keys[change.key] not in applied]
    reports.append(_report(
        APPLIED, considered, dropped,
        "muted in data/applied.tsv. Delete the line to bring one back",
    ))

    return changes, reports


def removed_by(reports: list[models.FilterReport], filter_id: str) -> int:
    """How many rows one filter removed, across every report it emitted."""
    return sum(r.removed for r in reports if r.filter_id == filter_id)
