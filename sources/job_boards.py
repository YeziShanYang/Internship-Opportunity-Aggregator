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

from core import models
from persist import store
from sources import postings, snapshot

GREENHOUSE, LEVER, ASHBY, WORKDAY, PHENOM, EIGHTFOLD = (
    "greenhouse", "lever", "ashby", "workday", "phenom", "eightfold",
)

ENDPOINTS = {
    GREENHOUSE: "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    LEVER: "https://api.lever.co/v0/postings/{slug}?mode=json",
    ASHBY: "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    # Workday's slug is the whole CXS base URL, because the tenant, the site and the
    # wd1/wd3/wd5 shard all vary independently and guessing any of them is guesswork.
    WORKDAY: "{slug}/jobs",
    # Phenom, like Workday, takes the whole endpoint URL as its slug.
    PHENOM: "{slug}",
    # Eightfold likewise: the tenant host varies per employer
    # (campusjobs.mlp.com, mlp.eightfold.ai, career.mlp.com all front the same board).
    # The entry is needed even though a fetcher handles the requests, because `check`
    # reads ENDPOINTS when it collapses a board restructure into one item.
    EIGHTFOLD: "{slug}",
}

# Workday rejects a limit above 20 (50 and 100 return an empty list), and it keeps
# serving rows past the end rather than stopping -- a naive "fetch until empty" loop
# pulled 412 rows from a 152-job board, cycling the same titles. Always stop at `total`.
WORKDAY_PAGE = 20
WORKDAY_MAX_PAGES = 40

# Descriptions are one extra request each, so they are fetched only for postings that
# already passed the title filter. Capital Group's board is 152 jobs and 1 survivor.
# The cap must not bind silently -- several boards hit an earlier value of 25 exactly,
# which is the signature of a truncation rather than a coincidence -- so overflow is
# counted and reported in HEALTH.
WORKDAY_MAX_DETAILS = 80

# Either signal is enough. See the module docstring for why neither alone suffices.
#
# `(?!a)` is load-bearing. A bare "intern" matches "Internal Audit Analyst",
# "Internal Sales Manager" and "International Fund Administration" -- no English word
# starts "interna" except those -- harmless on the
# quant boards, which have none, but Capital Group's Workday board alone has 28 such
# titles and they would all be classified as student roles. The lookahead is (?!a), not
# (?!al): "International" is intern + *at*, so excluding only "al" still let RBC's
# "International Equity Fund Analyst" and every "Internationa..." title through. The
# winternship alternative is there because Virtu really does run one and the word
# buries "intern" mid-token, so a plain word-boundary fix would drop a real programme.
# Measured 2026-09-11 against every Greenhouse/Lever/Ashby board on the watchlist: the
# word "intern" alone found 140 US student rows and missed 60 more. AQR was invisible
# entirely -- all 54 of its postings carry an empty employment_type and its whole 2027
# programme is titled "2027 Engineering Summer Analyst", "2027 Research Summer Analyst"
# and so on. This is the Jane Street trap in a third form, so the screen now covers the
# vocabularies these firms actually use: Summer/Winter Analyst, Campus, Academy,
# Graduate Programme, University Hire, and a cycle year in the title.
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
    r"|toronto|montr[e\u00e9]al|vancouver|calgary|ottawa|halifax|winnipeg|edmonton"
    r"|united kingdom|england|scotland|ireland|london, (uk|england)|dublin|edinburgh|glasgow"
    r"|india|bengaluru|bangalore|mumbai|hyderabad|chennai|pune|gurgaon|gurugram|noida"
    r"|china|hong kong|singapore|japan|tokyo|australia|sydney|melbourne"
    r"|germany|frankfurt|munich|berlin|france|paris|netherlands|amsterdam"
    r"|switzerland|zurich|geneva|poland|warsaw|krak[o\u00f3]w|spain|madrid|barcelona"
    r"|italy|milan|sweden|stockholm|denmark|copenhagen|norway|oslo|finland|helsinki"
    r"|belgium|brussels|luxembourg|austria|vienna|portugal|lisbon"
    r"|brazil|s[a\u00e3]o paulo|mexico|mexico city|argentina|chile|colombia"
    r"|israel|tel aviv|dubai|abu dhabi|united arab emirates|saudi arabia|qatar"
    r"|south africa|johannesburg|kenya|nigeria|egypt|turkey|istanbul"
    r"|malaysia|kuala lumpur|indonesia|jakarta|philippines|manila|thailand|bangkok"
    r"|vietnam|korea|seoul|taiwan|taipei|new zealand|auckland)\b",
    re.IGNORECASE,
)


# Phenom pages 100 at a time and reports `totalCount`, so pagination is bounded the
# same way Workday's is. Measured on Susquehanna: 263 postings over three pages.
PHENOM_PAGE = 100
PHENOM_MAX_PAGES = 30

# Eightfold ignores a larger `num` and serves ten at a time whatever you ask for, so
# the page size is descriptive rather than a request. 40 pages is 400 postings, well
# clear of Millennium's 59.
EIGHTFOLD_PAGE = 10
EIGHTFOLD_MAX_PAGES = 40

# Phenom carries a real recruiting category per posting, which is better evidence than
# any title regex and is why this method exists as its own fetcher. Measured on
# Susquehanna's 263 postings the field takes exactly four values: "Interns + Co-ops"
# (57), "New Graduates" (35), "Student Discovery Program" (10) and "Experienced
# Professionals" (161). Filtering on it needs no guessing at all.
#
# "Student Discovery Program" is the reason the shared STUDENT_TYPE regex is not reused
# here: SIG's Discovery postings are titled "Discovery Program: Quantitative Trading"
# with no "intern" anywhere, exactly the trap Jane Street set on Greenhouse. New
# Graduates is kept because it is the full-time-out-of-university category -- the
# Jane Street FTTP equivalent, which this project treats as top priority.
PHENOM_STUDENT_CATEGORY = re.compile(
    r"\bintern(?!a)|co.?op|student|new graduate|campus|discovery", re.IGNORECASE
)


# A board with more changes than this has been restructured, not restocked. Collapsing
# is what stands between a Greenhouse schema tweak and a 1,400-item digest.
MAX_CHANGES_PER_BOARD = 25


class MalformedPayload(RuntimeError):
    """HTTP 200, but the response shape says we cannot trust what it contains.

    This is the JSON counterpart of MIN_TEXT_HTML_RATIO, and it exists because the
    obvious reading of a broken payload is the dangerous one. Greenhouse answers a
    rate-limited request with `{"error": "...", "jobs": []}`, and
    `payload.get("jobs", [])` turns that into "this employer has no openings".

    `check` already refuses a board that drops to zero rows from a non-zero snapshot,
    which catches this on a busy board. It cannot catch it on a quiet one: 13 of the
    watched boards legitimately sit at zero student postings, and for those an error
    wearing an empty board's clothes is indistinguishable from an ordinary morning.
    So the shape is checked directly rather than inferred from the row count.

    Borrowed from zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships,
    whose `models.clean_listing` makes the same argument: return None, never [], because
    reading an error as an empty board closes every role the employer has.
    """


def _listing(payload, key: str) -> list[dict]:
    """The list at `payload[key]`, or raise rather than return a misleading empty.

    A missing key counts as malformed: every ATS here includes the collection key even
    when it is empty, so its absence means the shape changed under us.
    """
    if not isinstance(payload, dict):
        raise MalformedPayload(f"expected a JSON object, got {type(payload).__name__}")
    error = payload.get("error") or payload.get("errors")
    if error:
        raise MalformedPayload(f"payload carries an error: {str(error)[:200]!r}")
    items = payload.get(key)
    if items is None:
        raise MalformedPayload(f"no {key!r} key in the payload")
    if not isinstance(items, list):
        raise MalformedPayload(f"{key!r} is a {type(items).__name__}, not a list")
    if any(not isinstance(item, dict) for item in items):
        raise MalformedPayload(f"{key!r} contains a non-object member")
    return items


def _listing_root(payload) -> list[dict]:
    """Same contract for an API whose whole response is the list. Lever does this."""
    if not isinstance(payload, list):
        raise MalformedPayload(f"expected a JSON array, got {type(payload).__name__}")
    if any(not isinstance(item, dict) for item in payload):
        raise MalformedPayload("array contains a non-object member")
    return payload


def _page_items(payload, key: str) -> list[dict]:
    """One page of a paginated feed.

    Looser than `_listing` in exactly one way: a missing collection key is read as
    "past the last page" rather than as malformed, because the pagination loops stop on
    an empty batch. An explicit error key is still a failure.
    """
    if not isinstance(payload, dict):
        raise MalformedPayload(f"expected a JSON object, got {type(payload).__name__}")
    error = payload.get("error") or payload.get("errors")
    if error:
        raise MalformedPayload(f"payload carries an error: {str(error)[:200]!r}")
    items = payload.get(key) or []
    if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
        raise MalformedPayload(f"{key!r} is not a list of objects")
    return items


@dataclass(frozen=True)
class Posting:
    title: str
    location: str
    department: str
    employment_type: str
    url: str
    text: str  # description; goes to the classifier, never into the snapshot


def is_student_posting(posting: Posting) -> bool:
    if NOT_A_STUDENT_ROLE.search(posting.title):
        return False
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
    for job in _listing(payload, "jobs"):
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
    for job in _listing_root(payload):
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
    for job in _listing(payload, "jobs"):
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


def fetch_workday(client: httpx.Client, base: str) -> tuple[list[Posting], int]:
    """List a Workday board, then fetch descriptions for the survivors only.

    Workday is why "the firm has no public API" was wrong for so many employers. A
    plain fetch of a Workday posting returns zero characters of text because the page
    renders entirely in JavaScript, so it looked unreachable. The CXS endpoint behind
    it returns clean JSON: measured, 84 of the 96 Workday tenant/site pairs already
    linked from our own snapshots answer it.
    """
    listed: list[dict] = []
    total = None
    for page in range(WORKDAY_MAX_PAGES):
        offset = page * WORKDAY_PAGE
        if total is not None and offset >= total:
            break
        response = client.post(
            f"{base}/jobs",
            json={"appliedFacets": {}, "limit": WORKDAY_PAGE, "offset": offset, "searchText": ""},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        if total is None:
            total = payload.get("total") or 0
        batch = _page_items(payload, "jobPostings")
        if not batch:
            break
        listed += batch

    # A board too large to page through is worse than an absent one: it would look
    # healthy while seeing only the first slice. Workday's searchText does not narrow
    # usefully (NVIDIA: 2,000 jobs, 1,004 "matches" for "intern"), so the honest move
    # is to refuse the board rather than half-read it.
    if total and len(listed) < total:
        raise RuntimeError(
            f"board has {total} postings but only {len(listed)} could be paged "
            f"(Workday caps pages at {WORKDAY_PAGE}); this source would be "
            "silently partial, so it is reported as failing instead"
        )

    # Filter on the title first; only then pay for a description. The location screen
    # runs here too, before the per-job request, because a global employer's board is
    # mostly foreign: RBC's is 37 student titles of which 34 are Canadian, and paying
    # for 37 descriptions to discard 34 is what pushed it past WORKDAY_MAX_DETAILS.
    # Only an explicit foreign country drops a row -- "2 Locations" and anything
    # unrecognised is kept and screened again by `is_us` after the detail fetch.
    matched = [
        job for job in listed
        if STUDENT_TITLE_WORKDAY.search(job.get("title") or "")
        and not NOT_A_STUDENT_ROLE.search(job.get("title") or "")
        and not NON_US_LOCATION.search(job.get("locationsText") or "")
    ]
    interesting = matched[:WORKDAY_MAX_DETAILS]
    if len(matched) > WORKDAY_MAX_DETAILS:
        raise RuntimeError(
            f"{len(matched)} postings passed the title filter but only "
            f"{WORKDAY_MAX_DETAILS} descriptions are fetched per run; raise "
            "WORKDAY_MAX_DETAILS rather than letting this board go partial"
        )

    out = []
    for job in interesting:
        path = job.get("externalPath") or ""
        text = ""
        employment_type = ""
        if path:
            try:
                detail = client.get(f"{base}{path}")
                if detail.status_code < 300:
                    info = detail.json().get("jobPostingInfo") or {}
                    text = _clean(info.get("jobDescription") or "")
                    employment_type = info.get("timeType") or ""
            except Exception:
                pass  # a missing description is not a failed board
        out.append(
            Posting(
                title=(job.get("title") or "").strip(),
                location=(job.get("locationsText") or "").strip(),
                department="(no department)",
                employment_type=employment_type,
                url=f"{base.split('/wday/')[0]}{path}",
                text=text,
            )
        )
    # Reported, not swallowed. This screen runs inside the fetcher so the shared one in
    # check() sees nothing to remove, and for a while that meant a 152-posting board
    # showed "0 suppressed" in HEALTH -- precisely the silent filter this project has
    # been bitten by twice.
    return out, len(listed) - len(matched)


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


def fetch_phenom(client: httpx.Client, endpoint: str) -> tuple[list[Posting], int]:
    """Read a Phenom-hosted career site's JSON API.

    Phenom is why "this firm blocks automation" needed re-testing. Susquehanna's careers
    page is the canonical JavaScript shell in this codebase -- 221 characters of text out
    of 402KB of HTML, the very measurement MIN_TEXT_HTML_RATIO was calibrated against --
    so the firm sat at method=manual as unreachable. The JSON behind it answers our
    honest User-Agent with 263 postings, descriptions and recruiting categories included.
    Nothing was blocked; only the rendering was.

    Unlike Workday there is no second request per posting: the description and the
    qualifications both arrive in the list payload.
    """
    listed: list[dict] = []
    total = None
    for page in range(1, PHENOM_MAX_PAGES + 1):
        response = client.get(
            endpoint, params={"page": page, "limit": PHENOM_PAGE},
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        if total is None:
            total = payload.get("totalCount") or 0
        batch = _page_items(payload, "jobs")
        if not batch:
            break
        listed += batch
        if len(listed) >= total:
            break

    # Same refusal as Workday: half a board that looks healthy is worse than a board
    # that reports itself broken.
    if total and len(listed) < total:
        raise RuntimeError(
            f"site reports {total} postings but only {len(listed)} could be paged; "
            "this source would be silently partial, so it is reported as failing"
        )

    out = []
    for job in listed:
        data = job.get("data") or {}
        category = _phenom_field(data.get("category"))
        if not PHENOM_STUDENT_CATEGORY.search(category):
            continue
        if NOT_A_STUDENT_ROLE.search(_phenom_field(data.get("title"))):
            continue
        city = _phenom_field(data.get("city"))
        country = _phenom_field(data.get("country"))
        out.append(
            Posting(
                title=_phenom_field(data.get("title")),
                # City plus country, because the US screen keys off the country name and
                # "Bala Cynwyd (Philadelphia Area)" matches no city list anywhere.
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
    return out, len(listed) - len(out)


def fetch_eightfold(client: httpx.Client, endpoint: str) -> tuple[list[Posting], int]:
    """Read an Eightfold-hosted board.

    Millennium is why this exists, and it was the largest single hole the 2026-09-12
    source audit found: `millennium-students` watched a careers page whose note said
    "Millennium is on no public ATS", while `campusjobs.mlp.com` answers our honest
    User-Agent with 59 campus postings -- "2027 Quantitative Researcher Intern,
    Austin", "2027 Applied AI Engineer Intern, New York" and so on.

    Unlike Phenom this fetcher applies no screen of its own. It does not need one: the
    board is campus-only by construction, and measured on all 59 postings every one of
    the 17 US titles already matches the shared STUDENT_TITLE screen with none caught
    by the NOT_A_STUDENT_ROLE veto. Adding a second private filter here would be the
    duplication that already caused one bug in this module.

    Descriptions are absent from the list payload -- `job_description` is present and
    empty -- so postings come back with no text and `sources.postings` fetches the
    body for any row that actually changes. That is the cheaper shape anyway: 59
    detail fetches a day to read two of them is what WORKDAY_MAX_DETAILS exists to
    prevent.
    """
    listed: list[dict] = []
    total = None
    for page in range(EIGHTFOLD_MAX_PAGES):
        response = client.get(
            endpoint,
            params={"start": page * EIGHTFOLD_PAGE, "num": EIGHTFOLD_PAGE},
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        if total is None:
            total = payload.get("count") or 0
        batch = _page_items(payload, "positions")
        if not batch:
            break
        listed += batch
        if len(listed) >= total:
            break

    # The same refusal as Workday and Phenom: half a board that looks healthy is worse
    # than a board that reports itself broken.
    if total and len(listed) < total:
        raise RuntimeError(
            f"board reports {total} postings but only {len(listed)} could be paged; "
            "this source would be silently partial, so it is reported as failing"
        )

    out = []
    for job in listed:
        out.append(
            Posting(
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
    return out, 0


PARSERS = {GREENHOUSE: parse_greenhouse, LEVER: parse_lever, ASHBY: parse_ashby}
# Workday needs POST + pagination + a second request per survivor, so it does not fit
# the parse-one-payload shape the other three share.
FETCHERS = {WORKDAY: fetch_workday, PHENOM: fetch_phenom, EIGHTFOLD: fetch_eightfold}


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


def check(source: dict[str, str], client: httpx.Client) -> models.SourceResult:
    """Check one ATS board. Never raises: a failure is a result, not an exception."""
    source_id = source["source_id"]
    method = (source.get("method") or "").strip()
    slug = (source.get("url") or "").strip()
    parser = PARSERS.get(method)
    fetcher = FETCHERS.get(method)
    if (parser is None and fetcher is None) or not slug:
        return models.SourceResult(
            source_id=source_id,
            ok=False,
            error=f"no parser for method={method!r} or empty slug",
        )

    prefiltered_out = 0
    try:
        if fetcher is not None:
            parsed, prefiltered_out = fetcher(client, slug.rstrip("/"))
        else:
            response = client.get(ENDPOINTS[method].format(slug=slug))
            response.raise_for_status()
            parsed = parser(response.json())
    except Exception as exc:
        return models.SourceResult(
            source_id=source_id, ok=False, error=f"{type(exc).__name__}: {exc}"
        )

    # Workday screens on the title and Phenom on the recruiting category, both inside
    # their fetcher, so re-running the shared title/type screen here would drop rows
    # those methods deliberately kept -- "Discovery Program: Quantitative Trading"
    # contains no "intern" at all.
    students = (
        parsed if method in (WORKDAY, PHENOM)
        else [p for p in parsed if is_student_posting(p)]
    )
    kept = [p for p in students if is_us(p)]
    suppressed_not_student = len(parsed) - len(students) + prefiltered_out
    suppressed_not_us = len(students) - len(kept)

    previous_text = store.read_snapshot(source_id, ext="tsv")
    previous = snapshot.parse_snapshot(previous_text) if previous_text else None

    # Spec 10.1. A slug that still resolves but returns nothing is the Tier 2 version of
    # a README restructure: HTTP 200, no exception, and "no open roles" indistinguishable
    # from "we have gone blind". Self-calibrating against yesterday rather than a
    # configured floor, so there is no constant to rot.
    if previous is not None and previous.rows and not parsed and not prefiltered_out:
        return models.SourceResult(
            source_id=source_id,
            ok=False,
            error=f"board returned 0 postings, down from {len(previous.rows)} tracked rows",
        )

    current = to_snapshot(kept)
    text = snapshot.render_snapshot(current)
    extra = {
        "rows": len(current.rows),
        "postings": len(parsed) + prefiltered_out,
        "suppressed_not_student": suppressed_not_student,
        "suppressed_not_us": suppressed_not_us,
    }

    if previous is None:
        return models.SourceResult(
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
            models.Change(
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

    return models.SourceResult(
        source_id=source_id,
        ok=True,
        changes=changes,
        snapshot_text=text,
        content_length=len(text),
        extra=extra,
    )
