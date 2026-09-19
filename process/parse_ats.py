"""Turn ATS payloads into canonical snapshot rows, screen them, and diff.

Four findings from measuring the live boards shape everything here:

* **Filtering on the word "intern" alone is wrong.** Jane Street's board has 48 student
  roles and *none* of them says "intern" in the title -- they are "Machine Learning
  Researcher", "Data Engineer", "Quantitative Trader", with the student-ness recorded in
  a `metadata` entry named `Employment Type`. Title-only filtering silently drops the
  single most important firm in the system.
* **`Employment Type` is not universal either.** It is a customer-defined Greenhouse
  field: present on janestreet, jumptrading, akunacapital, oldmissioncapital,
  fiveringsllc and sevenresearch; absent on imc, virtu, transmarketgroup and aquatic --
  whose titles do say "intern". The two signals cover different boards, so take either.
* **One firm is mostly one role posted per city.** Jane Street's 48 are 21 distinct
  titles across New York, London, Hong Kong and Singapore. The owner is US-based, so a
  location filter halves the whole tier: 144 postings to 69.
* **Every screen here reports.** Two of them used to report through a bare integer in
  `extra`, and the Workday plan screen -- which runs inside the fetcher -- showed "0
  suppressed" in HEALTH for a 152-posting board for a while, which is exactly the
  silent filter this project has been bitten by twice.

Pure. No network: the payloads and the Workday detail documents arrive from
`gather.ats`, and `previous` arrives as an argument.
"""
from __future__ import annotations

import html
import re

from core import clock, models
from gather import ats
from core import text as coretext
from process import snapshot

# Either signal is enough. See the module docstring for why neither alone suffices.
#
# `(?!a)` is load-bearing. A bare "intern" matches "Internal Audit Analyst",
# "Internal Sales Manager" and "International Fund Administration" -- no English word
# starts "interna" except those -- harmless on the quant boards, which have none, but
# Capital Group's Workday board alone has 28 such titles and they would all be
# classified as student roles. The lookahead is (?!a), not (?!al): "International" is
# intern + *at*, so excluding only "al" still let RBC's "International Equity Fund
# Analyst" and every "Internationa..." title through. The winternship alternative is
# there because Virtu really does run one and the word buries "intern" mid-token, so a
# plain word-boundary fix would drop a real programme.
#
# Measured 2026-09-11 against every Greenhouse/Lever/Ashby board on the watchlist: the
# word "intern" alone found 140 US student rows and missed 60 more. AQR was invisible
# entirely -- all 54 of its postings carry an empty employment_type and its whole 2027
# programme is titled "2027 Engineering Summer Analyst" and so on. This is the Jane
# Street trap in a third form, so the screen covers the vocabularies these firms
# actually use: Summer/Winter Analyst, Campus, Academy, Graduate Programme, University
# Hire, and a cycle year in the title.
STUDENT_TITLE = re.compile(
    r"\bintern(?!a)|winternship|co.?op"
    r"|\b(summer|winter|spring|fall)\s+(analyst|associate|intern)"
    r"|\bcampus\b|\bacademy\b|university hire|new grad"
    r"|graduate (programme|program|scheme|developer|trader|researcher|engineer)"
    r"|\b20(2[6-9]|3\d)\b",
    re.IGNORECASE,
)
STUDENT_TYPE = re.compile(r"\bintern(?!a)|co.?op", re.IGNORECASE)

# Jobs *about* early-career hiring are not early-career jobs. "Campus Recruiter",
# "Campus Relations & Events Associate" and "Campus Recruiting Coordinator" all match
# the widened screen above and are all full-time staff roles. Checked before the
# positive screen so it cannot be out-voted by a year in the title.
NOT_A_STUDENT_ROLE = re.compile(
    r"recruit|talent acquisition|campus relations", re.IGNORECASE
)

# Workday's list response carries no employment type, so the title is all there is at
# list time. A future year in the title is the signal that catches the campus programmes
# whose names avoid the word entirely -- Capital Group's is "CAP Associate - US (2027)".
STUDENT_TITLE_WORKDAY = re.compile(
    r"\bintern(?!a)|winternship|co.?op|campus|\b20(2[6-9]|3\d)\b", re.IGNORECASE
)

# The owner is a US citizen, US-based (spec section 8 profile). Jane Street alone posts
# the same internship in four cities. Matching is deliberately generous -- an
# unrecognised location is kept, because dropping a real US role to tidy the digest is
# the expensive error and the suppressed count is reported in HEALTH either way.
US_LOCATION = re.compile(
    r"united states|\bu\.?s\.?a?\b|remote"
    r"|new york|chicago|austin|boston|san francisco|seattle|houston|miami|atlanta"
    r"|philadelphia|dallas|denver|minneapolis|charlotte|washington|los angeles"
    r"|,\s*(NY|CA|IL|TX|NJ|MA|WA|PA|FL|CT|MO|GA|MN|CO|OH|AZ|UT|NC|VA|MD|WI|IA|DC)\b"
    # Spelled-out state names, not just the postal abbreviations. Voloridge posts the
    # same Jupiter office as both "Jupiter, FL" and "Jupiter, Florida", and the one
    # spelled out was its Ascend Program -- a programme explicitly open to first- and
    # second-year undergraduates, i.e. the single most relevant posting on the board.
    # It was being dropped as non-US. Swept across every ATS board afterwards: the only
    # remaining unmatched locations are genuinely foreign (London, Shanghai, Budapest).
    r"|\b(alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware"
    r"|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana"
    r"|maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana"
    r"|nebraska|nevada|new hampshire|new jersey|new mexico|north carolina|north dakota"
    r"|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota"
    r"|tennessee|texas|utah|vermont|virginia|west virginia|wisconsin|wyoming"
    r"|district of columbia)\b",
    re.IGNORECASE,
)

# Clearly-foreign locations, used to skip a detail fetch at list time. This is the
# mirror image of US_LOCATION and its bias runs the other way on purpose: US_LOCATION
# keeps anything it does not recognise, so it cannot narrow a Workday board at all --
# "TORONTO, Ontario, Canada" is simply unrecognised. Only an explicit country match
# drops a row, so an unfamiliar location is still fetched and still judged.
#
# Measured on RBC Early Talent, which is why this exists: 37 of 40 titles pass the
# Workday title filter and all but three are Canadian. Without this the board exceeds
# WORKDAY_MAX_DETAILS and is reported as failing, which is correct but useless.
NON_US_LOCATION = re.compile(
    r"\b(canada|ontario|quebec|alberta|manitoba|saskatchewan|british columbia"
    r"|nova scotia|new brunswick|newfoundland"
    r"|toronto|montr[eé]al|vancouver|calgary|ottawa|halifax|winnipeg|edmonton"
    r"|united kingdom|england|scotland|ireland|london, (uk|england)|dublin|edinburgh|glasgow"
    r"|india|bengaluru|bangalore|mumbai|hyderabad|chennai|pune|gurgaon|gurugram|noida"
    r"|china|hong kong|singapore|japan|tokyo|australia|sydney|melbourne"
    r"|germany|frankfurt|munich|berlin|france|paris|netherlands|amsterdam"
    r"|switzerland|zurich|geneva|poland|warsaw|krak[oó]w|spain|madrid|barcelona"
    r"|italy|milan|sweden|stockholm|denmark|copenhagen|norway|oslo|finland|helsinki"
    r"|belgium|brussels|luxembourg|austria|vienna|portugal|lisbon"
    r"|brazil|s[aã]o paulo|mexico|mexico city|argentina|chile|colombia"
    r"|israel|tel aviv|dubai|abu dhabi|united arab emirates|saudi arabia|qatar"
    r"|south africa|johannesburg|kenya|nigeria|egypt|turkey|istanbul"
    r"|malaysia|kuala lumpur|indonesia|jakarta|philippines|manila|thailand|bangkok"
    r"|vietnam|korea|seoul|taiwan|taipei|new zealand|auckland)\b",
    re.IGNORECASE,
)

# Phenom carries a real recruiting category per posting, which is better evidence than
# any title regex and is why that method exists as its own fetcher. Measured on
# Susquehanna's 263 postings the field takes exactly four values: "Interns + Co-ops"
# (57), "New Graduates" (35), "Student Discovery Program" (10) and "Experienced
# Professionals" (161). Filtering on it needs no guessing at all.
#
# "Student Discovery Program" is the reason STUDENT_TYPE is not reused here: SIG's
# Discovery postings are titled "Discovery Program: Quantitative Trading" with no
# "intern" anywhere, exactly the trap Jane Street set on Greenhouse. New Graduates is
# kept because it is the full-time-out-of-university category -- the Jane Street FTTP
# equivalent, which this project treats as top priority.
PHENOM_STUDENT_CATEGORY = re.compile(
    r"\bintern(?!a)|co.?op|student|new graduate|campus|discovery", re.IGNORECASE
)

# A board with more changes than this has been restructured, not restocked. Collapsing
# is what stands between a Greenhouse schema tweak and a 1,400-item digest.
MAX_CHANGES_PER_BOARD = 25

# How many of a collapsed board's rows to name. The digest gets the count; these are
# for whoever goes and looks at why.
MAX_COLLAPSE_SAMPLES = 10

NOT_STUDENT = "ats-not-a-student-role"
NOT_US = "ats-not-us"
# The screens that run before this layer produces a Posting at all: Workday's
# "is this description worth a second request", and Phenom's recruiting category.
PRE_SCREENED = {
    ats.WORKDAY: ("ats-workday-detail-plan",
                  "listing did not look like a US student role, so no second "
                  "request was made for its description"),
    ats.PHENOM: ("ats-phenom-category",
                 "the site's own recruiting category is not a student, campus, "
                 "co-op, new-graduate or discovery one"),
}


def is_student_posting(posting: models.Posting) -> bool:
    if NOT_A_STUDENT_ROLE.search(posting.title):
        return False
    return bool(
        STUDENT_TITLE.search(posting.title)
        or STUDENT_TYPE.search(posting.employment_type or "")
    )


def is_us(posting: models.Posting) -> bool:
    return not posting.location or bool(US_LOCATION.search(posting.location))


def wants_workday_detail(job: dict) -> bool:
    """Workday's plan: is this listing worth a second request for its description?

    Passed into `gather.ats.fetch` rather than applied there, because deciding what to
    keep is this layer's job and a fetcher that made the decision itself would have to
    import it.
    """
    title = job.get("title") or ""
    return bool(
        STUDENT_TITLE_WORKDAY.search(title)
        and not NOT_A_STUDENT_ROLE.search(title)
        and not NON_US_LOCATION.search(job.get("locationsText") or "")
    )


def _clean(raw: str) -> str:
    return coretext.extract_text(html.unescape(raw or ""))[: coretext.MAX_TEXT_CHARS]


def parse_greenhouse(payload: dict) -> list[models.Posting]:
    out = []
    for job in ats._listing(payload, "jobs"):
        employment_type = ""
        for meta in job.get("metadata") or []:
            if (meta.get("name") or "").strip().lower() in ("employment type", "job type"):
                value = meta.get("value")
                if isinstance(value, str):
                    employment_type = value
        departments = job.get("departments") or []
        out.append(
            models.Posting(
                title=(job.get("title") or "").strip(),
                location=((job.get("location") or {}).get("name") or "").strip(),
                department=(departments[0].get("name") if departments else "") or "",
                employment_type=employment_type,
                url=job.get("absolute_url") or "",
                text=_clean(job.get("content") or ""),
            )
        )
    return out


def parse_lever(payload: list) -> list[models.Posting]:
    out = []
    for job in ats._listing_root(payload):
        categories = job.get("categories") or {}
        out.append(
            models.Posting(
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


def parse_ashby(payload: dict) -> list[models.Posting]:
    out = []
    for job in ats._listing(payload, "jobs"):
        if job.get("isListed") is False:
            continue
        out.append(
            models.Posting(
                title=(job.get("title") or "").strip(),
                location=(job.get("location") or "").strip(),
                department=(job.get("department") or job.get("team") or ""),
                employment_type=job.get("employmentType") or "",
                url=job.get("jobUrl") or "",
                text=_clean(job.get("descriptionPlain") or ""),
            )
        )
    return out


def parse_workday(fetched: ats.AtsFetch, base: str) -> list[models.Posting]:
    """Only the planned postings become Postings: the rest were never fetched."""
    out = []
    for payload in fetched.pages:
        for job in ats._page_items(payload, "jobPostings"):
            if not wants_workday_detail(job):
                continue
            path = job.get("externalPath") or ""
            info = (fetched.details.get(path) or {}).get("jobPostingInfo") or {}
            out.append(
                models.Posting(
                    title=(job.get("title") or "").strip(),
                    location=(job.get("locationsText") or "").strip(),
                    department="(no department)",
                    employment_type=info.get("timeType") or "",
                    url=f"{base.split('/wday/')[0]}{path}",
                    text=_clean(info.get("jobDescription") or ""),
                )
            )
    return out


def _phenom_field(value) -> str:
    """Flatten a Phenom field to a string.

    Not defensive boilerplate: `category` really does arrive as a one-element list
    (`["Interns + Co-ops"]`) while `title`, `city` and `country` arrive as plain
    strings, and the first version of this fetcher called .strip() on the list and
    failed the whole board. Field types are per-field and undocumented, so coerce
    rather than assume.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(_phenom_field(item) for item in value if item).strip()
    if isinstance(value, dict):
        return _phenom_field(value.get("name") or value.get("value"))
    return str(value).strip()


def parse_phenom(fetched: ats.AtsFetch) -> list[models.Posting]:
    out = []
    for payload in fetched.pages:
        for job in ats._page_items(payload, "jobs"):
            data = job.get("data") or {}
            category = _phenom_field(data.get("category"))
            if not PHENOM_STUDENT_CATEGORY.search(category):
                continue
            if NOT_A_STUDENT_ROLE.search(_phenom_field(data.get("title"))):
                continue
            city = _phenom_field(data.get("city"))
            country = _phenom_field(data.get("country"))
            out.append(
                models.Posting(
                    title=_phenom_field(data.get("title")),
                    # City plus country, because the US screen keys off the country
                    # name and "Bala Cynwyd (Philadelphia Area)" matches no city list.
                    location=", ".join(part for part in (city, country) if part),
                    department=category or "(no department)",
                    employment_type=category,
                    url=_phenom_field(data.get("apply_url")),
                    text=_clean(
                        _phenom_field(data.get("description"))
                        + " "
                        + _phenom_field(data.get("qualifications"))
                    ),
                )
            )
    return out


def parse_eightfold(fetched: ats.AtsFetch) -> list[models.Posting]:
    """No screen of its own, deliberately.

    The board is campus-only by construction, and measured on all 59 of Millennium's
    postings every one of the 17 US titles already matches the shared STUDENT_TITLE
    screen with none caught by the NOT_A_STUDENT_ROLE veto. Adding a second private
    filter here would be the duplication that already caused one bug in this module.
    """
    out = []
    for payload in fetched.pages:
        for job in ats._page_items(payload, "positions"):
            out.append(
                models.Posting(
                    title=(job.get("name") or "").strip(),
                    location=(job.get("location") or "").strip(),
                    department=(job.get("department") or job.get("business_unit") or "").strip()
                    or "(no department)",
                    employment_type="",
                    # The canonical url is on the Eightfold host and is fetchable; the
                    # `type` field says "ATS" on every row and is not an employment type.
                    url=(job.get("canonicalPositionUrl") or "").strip(),
                    text="",
                )
            )
    return out


def parse(fetched: ats.AtsFetch, method: str, slug: str) -> list[models.Posting]:
    """Payloads to Postings, before any screen this layer applies."""
    if method == ats.WORKDAY:
        return parse_workday(fetched, slug.rstrip("/"))
    if method == ats.PHENOM:
        return parse_phenom(fetched)
    if method == ats.EIGHTFOLD:
        return parse_eightfold(fetched)
    payload = fetched.pages[0] if fetched.pages else None
    if method == ats.GREENHOUSE:
        return parse_greenhouse(payload)
    if method == ats.LEVER:
        return parse_lever(payload)
    return parse_ashby(payload)


def to_snapshot(
    kept: list[models.Posting],
) -> tuple[snapshot.Snapshot, dict[str, models.Posting]]:
    """Canonicalise into the shared TSV shape, and say which posting made each row.

    Returns (snapshot, {row identity: posting}). The second half is the fix for a
    re-parse: the old code rendered the row key, diffed it, and then recovered the
    employer from the rendered string with `key.split(" @ ")[0].split(" #")[0]` -- taking
    apart a string it had assembled two frames earlier, and getting it wrong for any
    title containing " @ " or any row that had picked up a collision ordinal. The
    posting is in hand right here, so it is handed over rather than reconstructed.

    Deliberately absent from the row value: `updated_at`, any hash of the description,
    and salary metadata. Greenhouse bumps `updated_at` on trivial edits, so including it
    would mark all 230 Jane Street rows as changed every morning and exhaust the
    classification budget on a copy-edit.

    Keying on the folded title rather than the ATS job id means an annual repost reads
    as one changed row -- "they reposted the QT internship" -- instead of a removed row
    plus an added one.
    """
    snap = snapshot.Snapshot()
    by_identity: dict[str, models.Posting] = {}
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
        # An ATS row's own url is the posting page, so the two agree here. They
        # differ only on the aggregator READMEs, which carry two links per cell.
        row = snapshot.Row(section=section, key=key, value=value,
                           url=posting.url, posting_url=posting.url)
        snap.sections.append(section)
        snap.rows.append(row)
        by_identity[row.identity] = posting
    return snap, by_identity


def _report(filter_id: str, source_id: str, considered: int, removed: int,
            reason: str, samples: tuple[str, ...] = ()) -> models.FilterReport:
    return models.FilterReport(
        stage="process", filter_id=filter_id, source_id=source_id,
        considered=considered, removed=removed, reason=reason, samples=samples)


def assess(
    source: dict[str, str], fetched: ats.AtsFetch, previous: str | None
) -> models.SourceResult:
    """Decide what one board fetch means. Pure.

    Three independent behaviours report to HEALTH from here and each one going quiet is
    the silent-filter failure this project has been bitten by twice: the plausibility
    check, the two posting screens, and the collapse.
    """
    source_id = source["source_id"]
    method = (source.get("method") or "").strip()
    slug = (source.get("url") or "").strip()

    if not fetched.attempt.ok:
        return models.SourceResult(
            source_id=source_id, ok=False, error=fetched.attempt.error)

    try:
        parsed = parse(fetched, method, slug)
    except Exception as exc:
        return models.SourceResult(
            source_id=source_id, ok=False, error=f"{type(exc).__name__}: {exc}")

    # Workday screens on the title and Phenom on the recruiting category, both while
    # parsing, so re-running the shared title/type screen here would drop rows those
    # methods deliberately kept -- "Discovery Program: Quantitative Trading" contains
    # no "intern" at all.
    if method in (ats.WORKDAY, ats.PHENOM):
        students = parsed
    else:
        students = [p for p in parsed if is_student_posting(p)]
    kept = [p for p in students if is_us(p)]

    # Rows the board had that never became a Posting. For the paged vendors a screen
    # runs before this point, so their count has to come from the board's own total or
    # it is lost -- which is exactly what happened for Phenom while this was being
    # split, and what the suite caught.
    pre_screened = (
        max(fetched.listed - len(parsed), 0) if method in ats.PAGED else 0
    )

    not_student = [p for p in parsed if p not in students]
    not_us = [p for p in students if p not in kept]
    filters = [
        _report(NOT_STUDENT, source_id, len(parsed), len(not_student),
                "title and employment type give no sign of a student or new-graduate "
                "role, or the role is about early-career hiring rather than being one",
                tuple(p.title for p in not_student[:5])),
        _report(NOT_US, source_id, len(students), len(not_us),
                "location names a country other than the United States; an "
                "unrecognised location is kept rather than dropped",
                tuple(f"{p.title} @ {p.location}" for p in not_us[:5])),
    ]
    if pre_screened and method in PRE_SCREENED:
        filter_id, reason = PRE_SCREENED[method]
        filters.append(
            _report(filter_id, source_id, fetched.listed, pre_screened, reason))

    previous_snapshot = snapshot.parse_snapshot(previous) if previous else None

    # Spec 10.1. A slug that still resolves but returns nothing is the Tier 2 version of
    # a README restructure: HTTP 200, no exception, and "no open roles" indistinguishable
    # from "we have gone blind". Self-calibrating against yesterday rather than a
    # configured floor, so there is no constant to rot.
    if (previous_snapshot is not None and previous_snapshot.rows
            and not parsed and not pre_screened):
        return models.SourceResult(
            source_id=source_id,
            ok=False,
            filters=filters,
            error=(
                f"board returned 0 postings, down from "
                f"{len(previous_snapshot.rows)} tracked rows"
            ),
        )

    current, by_identity = to_snapshot(kept)
    text = snapshot.render_snapshot(current)
    extra = {
        "rows": len(current.rows),
        "postings": len(parsed) + pre_screened,
        "suppressed_not_student": len(not_student) + pre_screened,
        "suppressed_not_us": len(not_us),
    }

    if previous_snapshot is None:
        return models.SourceResult(
            source_id=source_id,
            ok=True,
            baseline=True,
            snapshot_text=text,
            content_length=len(text),
            extra=extra,
            filters=filters,
        )

    changes = snapshot.diff_snapshots(source_id, previous_snapshot, current)
    # Joined on the identity the diff itself keyed on, via the change_id computed from
    # it. No string is taken apart to find out which posting a change came from.
    texts = {
        clock.change_id(source_id, identity): posting.text
        for identity, posting in by_identity.items()
    }
    for change in changes:
        change.posting_text = texts.get(change.change_id, "")
        change.program_name = change.program_name or source.get("program_names", "")

    collapsed = None
    if len(changes) > MAX_CHANGES_PER_BOARD:
        samples = tuple(f"{c.kind}: {c.key}" for c in changes[:MAX_COLLAPSE_SAMPLES])
        collapsed = models.BoardCollapse(total=len(changes), samples=samples)
        extra["collapsed"] = len(changes)
        changes = [
            models.Change(
                source_id=source_id,
                kind="changed",
                change_id=clock.change_id(source_id, "board-restructure"),
                structural=True,
                key=f"{slug}: {collapsed.total} rows changed at once",
                detail=(
                    f"{collapsed.total} rows moved in a single run, which reads as a "
                    "board restructure rather than news. First "
                    f"{len(samples)}:\n" + "\n".join(f"- {s}" for s in samples)
                ),
                url=ats.ENDPOINTS[method].format(slug=slug),
                program_name=source.get("program_names", ""),
            )
        ]

    return models.SourceResult(
        source_id=source_id,
        ok=True,
        changes=changes,
        snapshot_text=text,
        content_length=len(text),
        extra=extra,
        filters=filters,
        collapsed=collapsed,
    )
