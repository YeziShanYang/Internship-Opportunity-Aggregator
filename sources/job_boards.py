"""Tier 2: postings pulled straight from firms' applicant tracking systems.

Phase 1 could only see an opportunity once a volunteer had typed it into a markdown
table. This sees it the moment the firm creates it, days to weeks earlier, and with the
full description attached.

Measured on the live boards while this was written:

* **Greenhouse carries almost everything.** 11 boards answered for the tracked firms --
  janestreet 230 jobs, imc 170, jumptrading 106, virtu 50, akunacapital 38,
  oldmissioncapital 38, transmarketgroup 19, fiveringsllc 16, aquatic 8, sevenresearch
  8. Fingerprint mining the snapshots found more (drweng 157, worldquant 103, schonfeld
  71). Lever and Ashby contribute a handful each. Citadel, HRT, SIG, Tower, Two Sigma,
  AQR and XTX are on no public ATS at all and can only ever be Tier 3 or manual.
* **Filtering on the word "intern" alone is wrong.** Jane Street's board has 48 student
  roles and *none* of them says "intern" in the title -- they are "Machine Learning
  Researcher", "Data Engineer", "Quantitative Trader", with the student-ness recorded in
  a `metadata` entry named `Employment Type`. Title-only filtering silently drops the
  single most important firm in the system. Hence `is_student_posting`, which reads
  whichever signal a board actually provides.
* **`Employment Type` is not universal either.** It is a customer-defined Greenhouse
  field: present on janestreet, jumptrading, akunacapital, oldmissioncapital,
  fiveringsllc and sevenresearch; absent on imc, virtu, transmarketgroup and aquatic --
  whose titles do say "intern". The two signals cover different boards, so take either.
* **One firm is mostly one role posted per city.** Jane Street's 48 are 21 distinct
  titles across New York, London, Hong Kong and Singapore. The owner is US-based, so a
  location filter halves the whole tier: 144 postings to 69.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

import httpx

import state
from sources import postings, snapshot

GREENHOUSE, LEVER, ASHBY = "greenhouse", "lever", "ashby"

ENDPOINTS = {
    GREENHOUSE: "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    LEVER: "https://api.lever.co/v0/postings/{slug}?mode=json",
    ASHBY: "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}

# Either signal is enough. See the module docstring for why neither alone suffices.
STUDENT_TITLE = re.compile(r"intern", re.IGNORECASE)
STUDENT_TYPE = re.compile(r"intern|co.?op", re.IGNORECASE)

# The owner is a US citizen, US-based (spec section 8 profile). Jane Street alone posts
# the same internship in four cities. Matching is deliberately generous -- an
# unrecognised location is kept, because dropping a real US role to tidy the digest is
# the expensive error and the suppressed count is reported in HEALTH either way.
US_LOCATION = re.compile(
    r"united states|\bu\.?s\.?a?\b|remote"
    r"|new york|chicago|austin|boston|san francisco|seattle|houston|miami|atlanta"
    r"|philadelphia|dallas|denver|minneapolis|charlotte|washington|los angeles"
    r"|,\s*(NY|CA|IL|TX|NJ|MA|WA|PA|FL|CT|MO|GA|MN|CO|OH|AZ|UT|NC|VA|MD|WI|IA|DC)\b",
    re.IGNORECASE,
)

# A board with more changes than this has been restructured, not restocked. Collapsing
# is what stands between a Greenhouse schema tweak and a 1,400-item digest.
MAX_CHANGES_PER_BOARD = 25


@dataclass(frozen=True)
class Posting:
    title: str
    location: str
    department: str
    employment_type: str
    url: str
    text: str  # description; goes to the classifier, never into the snapshot


def is_student_posting(posting: Posting) -> bool:
    return bool(
        STUDENT_TITLE.search(posting.title)
        or STUDENT_TYPE.search(posting.employment_type or "")
    )


def is_us(posting: Posting) -> bool:
    return not posting.location or bool(US_LOCATION.search(posting.location))


def _clean(raw: str) -> str:
    return postings.extract_text(html.unescape(raw or ""))[: postings.MAX_TEXT_CHARS]


def parse_greenhouse(payload: dict) -> list[Posting]:
    out = []
    for job in payload.get("jobs", []) or []:
        employment_type = ""
        for meta in job.get("metadata") or []:
            if (meta.get("name") or "").strip().lower() in ("employment type", "job type"):
                value = meta.get("value")
                if isinstance(value, str):
                    employment_type = value
        departments = job.get("departments") or []
        out.append(
            Posting(
                title=(job.get("title") or "").strip(),
                location=((job.get("location") or {}).get("name") or "").strip(),
                department=(departments[0].get("name") if departments else "") or "",
                employment_type=employment_type,
                url=job.get("absolute_url") or "",
                text=_clean(job.get("content") or ""),
            )
        )
    return out


def parse_lever(payload: list) -> list[Posting]:
    out = []
    for job in payload or []:
        categories = job.get("categories") or {}
        out.append(
            Posting(
                title=(job.get("text") or "").strip(),
                location=(categories.get("location") or "").strip(),
                department=(categories.get("department") or categories.get("team") or ""),
                employment_type=categories.get("commitment") or "",
                url=job.get("hostedUrl") or "",
                text=_clean(
                    (job.get("descriptionPlain") or "")
                    + "\n"
                    + (job.get("additionalPlain") or "")
                ),
            )
        )
    return out


def parse_ashby(payload: dict) -> list[Posting]:
    out = []
    for job in payload.get("jobs", []) or []:
        if job.get("isListed") is False:
            continue
        out.append(
            Posting(
                title=(job.get("title") or "").strip(),
                location=(job.get("location") or "").strip(),
                department=(job.get("department") or job.get("team") or ""),
                employment_type=job.get("employmentType") or "",
                url=job.get("jobUrl") or "",
                text=_clean(job.get("descriptionPlain") or ""),
            )
        )
    return out


PARSERS = {GREENHOUSE: parse_greenhouse, LEVER: parse_lever, ASHBY: parse_ashby}


def to_snapshot(kept: list[Posting]) -> snapshot.Snapshot:
    """Canonicalise into the shared TSV shape so the Tier 1 diff applies unchanged.

    Deliberately absent from the row value: `updated_at`, any hash of the description,
    and salary metadata. Greenhouse bumps `updated_at` on trivial edits, so including it
    would mark all 230 Jane Street rows as changed every morning and exhaust the
    classification budget on a copy-edit.

    Keying on the folded title rather than the ATS job id means an annual repost reads
    as one changed row -- "they reposted the QT internship" -- instead of a removed row
    plus an added one.
    """
    snap = snapshot.Snapshot()
    seen: dict[str, int] = {}
    # Sorted before the ordinals are handed out. Two postings with the same title and
    # location need a "#2" to tell them apart, and assigning that in whatever order the
    # API happened to return would let them swap suffixes between runs and churn a
    # spurious pair of changes every morning. Measured: only 1 row of 188 collides
    # today, so this is insurance rather than a fix.
    for posting in sorted(kept, key=lambda p: (p.department, p.title, p.location, p.url)):
        section = posting.department.strip() or "(no department)"
        title = snapshot.fold(posting.title)
        key = f"{title} @ {posting.location}" if posting.location else title
        count = seen.get(f"{section}::{key}", 0) + 1
        seen[f"{section}::{key}"] = count
        if count > 1:
            key = f"{key} #{count}"
        value = "; ".join(
            f"{label}={text}"
            for label, text in (("Type", posting.employment_type), ("URL", posting.url))
            if text
        )
        snap.sections.append(section)
        snap.rows.append(snapshot.Row(section=section, key=key, value=value, url=posting.url))
    return snap


def check(source: dict[str, str], client: httpx.Client) -> state.SourceResult:
    """Check one ATS board. Never raises: a failure is a result, not an exception."""
    source_id = source["source_id"]
    method = (source.get("method") or "").strip()
    slug = (source.get("url") or "").strip()
    parser = PARSERS.get(method)
    if parser is None or not slug:
        return state.SourceResult(
            source_id=source_id,
            ok=False,
            error=f"no parser for method={method!r} or empty slug",
        )

    try:
        response = client.get(ENDPOINTS[method].format(slug=slug))
        response.raise_for_status()
        parsed = parser(response.json())
    except Exception as exc:
        return state.SourceResult(
            source_id=source_id, ok=False, error=f"{type(exc).__name__}: {exc}"
        )

    students = [p for p in parsed if is_student_posting(p)]
    kept = [p for p in students if is_us(p)]
    suppressed_not_student = len(parsed) - len(students)
    suppressed_not_us = len(students) - len(kept)

    previous_text = state.read_snapshot(source_id, ext="tsv")
    previous = snapshot.parse_snapshot(previous_text) if previous_text else None

    # Spec 10.1. A slug that still resolves but returns nothing is the Tier 2 version of
    # a README restructure: HTTP 200, no exception, and "no open roles" indistinguishable
    # from "we have gone blind". Self-calibrating against yesterday rather than a
    # configured floor, so there is no constant to rot.
    if previous is not None and previous.rows and not parsed:
        return state.SourceResult(
            source_id=source_id,
            ok=False,
            error=f"board returned 0 postings, down from {len(previous.rows)} tracked rows",
        )

    current = to_snapshot(kept)
    text = snapshot.render_snapshot(current)
    extra = {
        "rows": len(current.rows),
        "postings": len(parsed),
        "suppressed_not_student": suppressed_not_student,
        "suppressed_not_us": suppressed_not_us,
    }

    if previous is None:
        return state.SourceResult(
            source_id=source_id,
            ok=True,
            baseline=True,
            snapshot_text=text,
            content_length=len(text),
            extra=extra,
        )

    changes = snapshot.diff_snapshots(source_id, previous, current)
    by_key = {p.title: p for p in kept}
    for change in changes:
        posting = by_key.get(change.key.split(" @ ")[0].split(" #")[0])
        if posting is not None:
            change.posting_text = posting.text
        change.program_name = change.program_name or source.get("program_names", "")

    if len(changes) > MAX_CHANGES_PER_BOARD:
        detail = "\n".join(f"- {c.kind}: {c.key}" for c in changes[:10])
        extra["collapsed"] = len(changes)
        changes = [
            state.Change(
                source_id=source_id,
                kind="changed",
                key=f"{slug}: {len(changes)} rows changed at once",
                detail=(
                    f"{len(changes)} rows moved in a single run, which reads as a board "
                    f"restructure rather than news. First ten:\n{detail}"
                ),
                url=ENDPOINTS[method].format(slug=slug),
                program_name=source.get("program_names", ""),
            )
        ]

    return state.SourceResult(
        source_id=source_id,
        ok=True,
        changes=changes,
        snapshot_text=text,
        content_length=len(text),
        extra=extra,
    )
