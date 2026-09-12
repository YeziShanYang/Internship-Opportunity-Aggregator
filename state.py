"""Shared paths, constants and CSV I/O for the canonical data files.

CSV is the source of truth (spec section 4), so every write goes through the helpers
here to keep column order and quoting byte-stable. An unstable writer would make every
run look like a whole-file change and destroy the `git log` audit trail that is the
entire reason state lives in the repo.
"""
from __future__ import annotations

import csv
import datetime
import hashlib
import pathlib
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
PROGRAMS_CSV = DATA / "programs.csv"
SOURCES_CSV = DATA / "sources.csv"
# The one method that is deliberately not fetched: these sources block automation or
# have nothing to diff, and they reach the owner as calendar reminders instead. It lives
# here rather than in check.py because build_xlsx.py needs it too -- a manual row must
# not count as coverage when deciding what stays on the owner's hand-check list.
UNWATCHED_METHOD = "manual"
SNAPSHOTS = DATA / "snapshots"
PROPOSALS_LOG = DATA / "proposals.log"
# Items the owner has ticked off in a delivered digest -- applied to, or not interested.
# Suppressed from every later digest. Keyed on a hash of source_id + row key, so next
# cycle's repost ("Summer 2028" rather than "Summer 2027") is a different key and
# resurfaces on its own, which is the intended behaviour rather than an accident.
APPLIED_TSV = DATA / "applied.tsv"
# Sources the weekly discovery pass has proposed. Committed, because without it the
# same candidates reappear every Monday and the section becomes noise. `status` is
# hand-edited: `rejected` is a permanent tombstone, never re-proposed.
DISCOVERED_CSV = DATA / "discovered.csv"
DISCOVERED_COLUMNS = [
    "first_proposed", "last_proposed", "kind", "key", "title", "url", "evidence", "status",
]
LAST_DISCOVERY = DATA / "last_discovery.txt"
# The date of the last digest actually delivered. The schedule fires several times
# each morning so that a dropped cron tick is not a missed day (see daily.yml), and the
# cadence is exactly one digest a day, so something has to stop ticks two and three from
# mailing again. This marker is the *fallback* half of that cap -- it is read from
# whatever commit the run checked out, so it can be stale. digest.delivered_issue_exists
# holds the authoritative answer.
LAST_DELIVERED = DATA / "last_delivered.txt"
# Two workbooks, with a deliberate split of audience (asked for by the owner
# 2026-09-11). programs.xlsx is *his* file: only the opportunities he has to chase
# himself. Anything he cannot apply to, and anything the tracker already watches, is
# bloat there and belongs in the other file. tracked.xlsx is the tool's own bookkeeping
# -- the full programme list plus the source, applied and discovery tables.
OUT_XLSX = ROOT / "out" / "programs.xlsx"
OUT_TRACKED_XLSX = ROOT / "out" / "tracked.xlsx"

# Spec section 11: identify ourselves, with a contact address.
USER_AGENT = "opportunity-tracker/1.0 (+mailto:jasonshi@stanford.edu)"
REQUEST_DELAY_SECONDS = 1.0
HTTP_TIMEOUT_SECONDS = 30.0

# Spec section 10.1: a source this broken is an emergency, not a footnote.
FAILURE_ESCALATION_THRESHOLD = 3

# A final URL that looks like an error page. Both HTTP clients are built with
# `follow_redirects=True`, so a page that has been retired 302s to a friendly error
# page and returns HTTP 200 -- and a friendly error page is full of prose, so it sails
# past MIN_ABSOLUTE_CHARS and MIN_TEXT_HTML_RATIO and reports success forever. Measured
# 2026-09-12: `aqr-internship-program` had been doing exactly that, yielding 4,683
# characters at ratio 0.0929 from `aqr.com/404`.
_ERROR_PATH = re.compile(r"/(?:404|410|error|not.?found|page.?not.?found|gone)(?:/|$|\.)", re.I)


def _comparable_url(url: str) -> tuple[str, str]:
    """(host, path) with only the cosmetic differences removed.

    Scheme, a leading `www.` and a trailing slash are noise: measured across the 65
    watched pages, two of the five redirects were nothing but that
    (`osqf.org`->`www.osqf.org`, `www.tower-research.com`->apex). Reporting those would
    be 40% noise in a check whose whole value is that it is quiet until it matters.
    """
    parts = urllib.parse.urlsplit(url.strip())
    host = (parts.netloc or "").lower().removeprefix("www.")
    path = (parts.path or "/").rstrip("/") or "/"
    return host, path


def redirect_verdict(configured: str, final: str) -> tuple[str, str]:
    """Compare the URL we asked for with the one we got. Returns (severity, message).

    Severity is "" (nothing to say), "warn" (the page loads but is not the page this
    row was configured to watch) or "fail" (it landed on an error page).

    The distinction is deliberate. A row redirected from a students page to a generic
    careers page still returns readable text, so calling it a failed fetch would be a
    lie -- but it is no longer watching what its `program_names` claims, which is a
    coverage hole that has to be said out loud every morning until someone fixes the
    URL. A row redirected to /404 is simply broken.
    """
    if not configured or not final:
        return "", ""
    want_host, want_path = _comparable_url(configured)
    got_host, got_path = _comparable_url(final)
    if (want_host, want_path) == (got_host, got_path):
        return "", ""
    if _ERROR_PATH.search(got_path):
        return "fail", (
            f"redirected to what looks like an error page: {configured} -> {final}. "
            "The page this row watched is gone; the text it is still returning is the "
            "error page's own prose, which is why the content floors did not catch it."
        )
    if want_path != got_path:
        return "warn", (
            f"redirected to a different path: {configured} -> {final}. The fetch "
            "succeeded, but this row is no longer watching the page it was configured "
            "for. Update the url in sources.csv or retire the row."
        )
    return "", ""

# Circuit breaker. A source that keeps failing is refetched on an exponential backoff
# rather than on every tick, borrowed from zshah101's health.py. Measured 2026-09-12:
# nine sources sit at exactly three consecutive failures with `last_success` empty --
# they have never worked once -- and each was being fetched three times a morning
# forever. Boards do come back (rate-limit storms, a page that was mid-deploy), so the
# window is capped rather than permanent and one success resets everything.
#
# The breaker changes *fetch* policy only. It must never change *reporting* policy: a
# quarantined source still appears in HEALTH and still escalates, because "we have
# stopped looking" is the most important version of "this source is blind, not quiet"
# (spec 10.1). Quarantining a source silently would be the exact failure this project
# has been bitten by twice.
QUARANTINE_AFTER_FAILURES = 3
QUARANTINE_HOURS = (6, 12, 24, 48)
QUARANTINE_CAP_HOURS = 72


def quarantine_hours(consecutive_failures: int) -> int:
    """How long to wait before retrying a source that has failed this many times."""
    step = consecutive_failures - QUARANTINE_AFTER_FAILURES
    if step < 0:
        return 0
    if step < len(QUARANTINE_HOURS):
        return QUARANTINE_HOURS[step]
    return QUARANTINE_CAP_HOURS


def quarantine_state(source: dict[str, str], now: datetime.datetime | None = None):
    """Return (skip_this_run, human explanation).

    Returns `(False, "")` for a healthy source, for one below the threshold, and for
    one whose window has expired -- an expired window is exactly how a recovered board
    gets retried without anyone intervening.
    """
    failures = int(source.get("consecutive_failures") or 0)
    hours = quarantine_hours(failures)
    if not hours:
        return False, ""
    last_attempt = (source.get("last_attempt") or "").strip()
    if not last_attempt:
        # No record of an attempt, so nothing says the window has started. Try it.
        return False, ""
    try:
        started = datetime.datetime.fromisoformat(last_attempt)
    except ValueError:
        return False, ""
    if started.tzinfo is None:
        started = started.replace(tzinfo=datetime.timezone.utc)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    until = started + datetime.timedelta(hours=hours)
    if now >= until:
        return False, ""
    never = not (source.get("last_success") or "").strip()
    return True, (
        f"quarantined for {hours}h after {failures} consecutive failures, retry after "
        f"{until.isoformat(timespec='minutes')}"
        + (" — it has never succeeded, so check the URL rather than waiting" if never else "")
    )

# Seeded from quant_math_cs_programs_freshman.xlsx, plus the tracking columns in
# spec section 4. Order is fixed; do not reorder without rewriting the whole file.
PROGRAM_COLUMNS = [
    "category",
    "name",
    "website",
    "applications_open",
    "target_years",
    "eligible",
    "notes",
    "source_id",
    "last_checked",
    "last_changed",
    "snapshot_hash",
    "status",
    "first_seen",
    "muted",
]

SOURCE_COLUMNS = [
    "source_id",
    "tier",
    "method",
    "url",
    "program_names",
    "selector",
    "render_js",
    "last_success",
    "consecutive_failures",
    # When we last actually tried. Distinct from last_success because the circuit
    # breaker needs to know when the backoff window started, and a quarantined run
    # is not an attempt.
    "last_attempt",
    # high | low. Low-signal sources are only classified when a row matches the
    # underclassman or rolling-firm filters; see classify.triage.
    "signal",
    "notes",
]

PROGRAM_STATUSES = ("dormant", "open", "closed", "unknown")


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def iso(when: datetime.datetime | None = None) -> str:
    """ISO8601 to whole seconds. Sub-second precision would churn the diff for nothing."""
    return (when or utcnow()).replace(microsecond=0).isoformat()


def today_iso() -> str:
    return utcnow().date().isoformat()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_csv(path: pathlib.Path, columns: list[str]) -> list[dict[str, str]]:
    """Read a CSV, guaranteeing every configured column is present on every row."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        row.pop(None, None)  # tolerate stray trailing commas
        for column in columns:
            row.setdefault(column, "")
            if row[column] is None:
                row[column] = ""
    return rows


def write_csv(path: pathlib.Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=columns, extrasaction="ignore", lineterminator="\n"
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") or "" for column in columns})


def read_programs() -> list[dict[str, str]]:
    return read_csv(PROGRAMS_CSV, PROGRAM_COLUMNS)


def write_programs(rows: list[dict[str, str]]) -> None:
    write_csv(PROGRAMS_CSV, PROGRAM_COLUMNS, rows)


def read_sources() -> list[dict[str, str]]:
    return read_csv(SOURCES_CSV, SOURCE_COLUMNS)


def write_sources(rows: list[dict[str, str]]) -> None:
    write_csv(SOURCES_CSV, SOURCE_COLUMNS, rows)


def snapshot_path(source_id: str, ext: str = "txt") -> pathlib.Path:
    """Tier 1 stores canonical `.tsv` rows; Tier 3 will store `.txt` page text."""
    return SNAPSHOTS / f"{source_id}.{ext}"


def change_key(source_id: str, key: str, cycle: int | None = None) -> str:
    """Short id for one digest line in one hiring cycle, for tick-to-dismiss.

    The cycle is part of the hash on purpose, so a dismissal is scoped to the season it
    was made in and next year's repost is simply a different item. Relying on the title
    to carry the year does not work: measured across the live boards, 136 of 188 rows
    say "Summer 2027" somewhere and 52 do not -- Jane Street titles every student role
    plainly and records the season in metadata. Tagging the key means those come back
    too, without an expiry clock that would also lapse mid-season.
    """
    cycle = recruiting_cycle() if cycle is None else cycle
    return hashlib.sha1(f"{source_id}|{key}|{cycle}".encode()).hexdigest()[:10]


def recruiting_cycle(date: str | None = None) -> int:
    """Which hiring season a date belongs to.

    Summer 2027 internships are advertised from roughly July 2026, so the cycle rolls
    over mid-year rather than in January: September 2026 is part of the 2027 cycle.
    """
    day = datetime.date.fromisoformat(date or today_iso())
    return day.year + 1 if day.month >= 7 else day.year


def read_applied() -> dict[str, str]:
    """{key: marked_at}. Empty when nothing has ever been ticked."""
    if not APPLIED_TSV.exists():
        return {}
    applied: dict[str, str] = {}
    for line in APPLIED_TSV.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if not parts[0]:
            continue
        applied[parts[0]] = parts[1] if len(parts) > 1 else ""
    return applied


def write_applied(applied: dict[str, str], titles: dict[str, str] | None = None) -> None:
    titles = titles or {}
    lines = ["# key\tmarked_at\ttitle"]
    lines += [
        f"{key}\t{marked}\t{titles.get(key, '')}" for key, marked in sorted(applied.items())
    ]
    DATA.mkdir(parents=True, exist_ok=True)
    APPLIED_TSV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_discovered() -> list[dict[str, str]]:
    return read_csv(DISCOVERED_CSV, DISCOVERED_COLUMNS)


def write_discovered(rows: list[dict[str, str]]) -> None:
    write_csv(DISCOVERED_CSV, DISCOVERED_COLUMNS, rows)


def read_last_discovery() -> str:
    return LAST_DISCOVERY.read_text(encoding="utf-8").strip() if LAST_DISCOVERY.exists() else ""


def write_last_discovery(date: str | None = None) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    LAST_DISCOVERY.write_text((date or today_iso()) + "\n", encoding="utf-8")


def read_snapshot(source_id: str, ext: str = "txt") -> str | None:
    """None means "never seen"; "" means "seen, and it really was empty"."""
    path = snapshot_path(source_id, ext)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def write_snapshot(source_id: str, text: str, ext: str = "txt") -> None:
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    if not text.endswith("\n"):
        text += "\n"
    snapshot_path(source_id, ext).write_text(text, encoding="utf-8")


def append_proposal(line: str) -> None:
    """Append-only log. Spec section 8 rule 5: never silently rewrite `eligible`."""
    PROPOSALS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PROPOSALS_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{iso()}\t{line}\n")


def read_last_delivered() -> str:
    """The ISO date of the last delivered digest, or "" if none was ever delivered."""
    if not LAST_DELIVERED.exists():
        return ""
    return LAST_DELIVERED.read_text(encoding="utf-8").strip()


def write_last_delivered(date: str | None = None) -> None:
    LAST_DELIVERED.parent.mkdir(parents=True, exist_ok=True)
    LAST_DELIVERED.write_text(f"{date or today_iso()}\n", encoding="utf-8")


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
    # and without `postings` having to guess a URL it cannot always construct.
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
