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
from dataclasses import dataclass, field
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
PROGRAMS_CSV = DATA / "programs.csv"
SOURCES_CSV = DATA / "sources.csv"
SNAPSHOTS = DATA / "snapshots"
PROPOSALS_LOG = DATA / "proposals.log"
# The date of the last digest actually delivered. The schedule fires several times
# each morning so that a dropped cron tick is not a missed day (see daily.yml), and
# this is what stops the retries from mailing the same heartbeat two or three times.
LAST_DELIVERED = DATA / "last_delivered.txt"
OUT_XLSX = ROOT / "out" / "programs.xlsx"

# Spec section 11: identify ourselves, with a contact address.
USER_AGENT = "opportunity-tracker/1.0 (+mailto:jasonshi@stanford.edu)"
REQUEST_DELAY_SECONDS = 1.0
HTTP_TIMEOUT_SECONDS = 30.0

# Spec section 10.1: a source this broken is an emergency, not a footnote.
FAILURE_ESCALATION_THRESHOLD = 3

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
    extra: dict[str, Any] = field(default_factory=dict)
