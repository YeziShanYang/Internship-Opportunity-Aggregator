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


def change_id(source_id: str, identity: str) -> str:
    """Opaque stable id for one changed row, computed once and used as the join key.

    Not to be confused with `change_key` above, and the two are easy to confuse, so:

    * `change_key` is the *mute* key. It is cycle-scoped and hashes the human-readable
      row text, because a dismissal should lapse when next season's repost arrives.
      It is what data/applied.tsv stores, and re-keying it invalidates that file.
    * `change_id` is the *join* key. It is cycle-independent and hashes the row's
      section-qualified identity, because `enrich`, `screen` and `classify` all need to
      look up their own contribution for one row and must agree on the name of it
      within a single run.

    It exists to delete a re-parse. `job_boards` used to recover the employer from a
    rendered diff key with `key.split(" @ ")[0].split(" #")[0]` -- taking a string the
    code had assembled two frames earlier back apart, and getting it wrong whenever a
    title contained " @ " or the row had picked up an ordinal.
    """
    return hashlib.sha1(f"{source_id}|{identity}".encode()).hexdigest()[:12]


def days_until(date: str, today: str | None = None) -> int | None:
    """Whole days from today to an ISO `YYYY-MM-DD`, or None if it is not one.

    None means "this is not a date", and every caller has to decide what to do about
    that rather than being handed a number that silently reads as "no time left" or as
    "plenty". The value comes from a language model, so unparseable is a normal case
    and not an error: `deliver.urgency` treats it as "not urgent" and the digest still
    prints the raw text so the owner can read it himself.

    Negative is returned rather than clamped. A deadline that has already passed is a
    different fact from one closing today, and squashing the two would let a stale
    posting sit in ACT NOW indefinitely.
    """
    try:
        when = datetime.date.fromisoformat((date or "").strip())
    except ValueError:
        return None
    return (datetime.date.fromisoformat(today or today_iso()) - when).days * -1
