"""Time, hashing and the identity of one digest line.

Grouped together because they are the run's only sources of non-determinism that are
not I/O, which makes them the things a test most often needs to pin. Kept out of
`paths.py` so that importing a path does not drag in the clock.
"""
from __future__ import annotations

import datetime
import hashlib


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def iso(when: datetime.datetime | None = None) -> str:
    """ISO8601 to whole seconds. Sub-second precision would churn the diff for nothing."""
    return (when or utcnow()).replace(microsecond=0).isoformat()


def today_iso() -> str:
    return utcnow().date().isoformat()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def recruiting_cycle(date: str | None = None) -> int:
    """Which hiring season a date belongs to.

    Summer 2027 internships are advertised from roughly July 2026, so the cycle rolls
    over mid-year rather than in January: September 2026 is part of the 2027 cycle.
    """
    day = datetime.date.fromisoformat(date or today_iso())
    return day.year + 1 if day.month >= 7 else day.year


def change_key(source_id: str, key: str, cycle: int | None = None) -> str:
    """Short id for one digest line in one hiring cycle, for the applied.tsv mute list.

    The cycle is part of the hash on purpose, so a dismissal is scoped to the season it
    was made in and next year's repost is simply a different item. Relying on the title
    to carry the year does not work: measured across the live boards, 136 of 188 rows
    say "Summer 2027" somewhere and 52 do not -- Jane Street titles every student role
    plainly and records the season in metadata. Tagging the key means those come back
    too, without an expiry clock that would also lapse mid-season.
    """
    cycle = recruiting_cycle() if cycle is None else cycle
    return hashlib.sha1(f"{source_id}|{key}|{cycle}".encode()).hexdigest()[:10]
