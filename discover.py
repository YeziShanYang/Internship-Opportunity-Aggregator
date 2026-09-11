"""Weekly pass for sources the watchlist does not have yet.

Every other part of this tool watches a fixed list. That list was assembled by hand
once, and it goes stale the moment a new repo appears or a firm moves onto an ATS.
This looks for what is missing, once a week.

It **proposes and never adds**. Adding a source silently changes what the tool watches
forever, and a bad automatic add poisons the digest in a way that is hard to notice --
the same reasoning that keeps the classifier away from the hand-verified `eligible`
column. Candidates land in `data/discovered.csv` and in the digest; accepting one is a
deliberate act through the `/add-opportunity` skill, which already probes robots.txt
and picks the right watchlist.

Three signals, all free:

1. **GitHub search.** `GH_PAT` is already configured and authenticated search allows 30
   requests a minute, so a handful of queries once a week costs nothing.
2. **ATS fingerprint mining of the snapshots we already store.** Zero network, and the
   highest-yield of the three: `drweng`, `schonfeld`, `dvtrading` and
   `belvederetrading` were all sitting in `data/snapshots/*.tsv` as links, unnoticed,
   while the firms were assumed to have no public board. Each mined slug is verified
   with one live call so a dead one is never proposed.
3. **Slug guessing, but only for firms newly added to a tracked repo.** Rotating
   through every known firm weekly would cost hundreds of requests for a low hit rate;
   the Tier 1 diff already reports when a firm appears for the first time, which is
   exactly when guessing is worth doing.

The digest cap is one issue per date and it is absolute, so this cannot open its own
issue. It rides along in Monday's digest as an extra section.
"""
from __future__ import annotations

import datetime
import re
import time
from dataclasses import dataclass

import httpx

import state
from sources import job_boards

GITHUB_SEARCH = "https://api.github.com/search/repositories"

SEARCH_QUERIES = (
    "underclassmen internships in:name,description",
    "freshman sophomore internship in:name,description",
    "quant internships in:name,description",
    "Summer 2027 Internships in:name",
    "quantitative finance internship list in:name,description",
)

MIN_STARS = 25
PUSHED_WITHIN_DAYS = 120
MAX_SLUG_VERIFICATIONS = 20
BUDGET_SECONDS = 180.0

# Where a board slug hides inside a link we already store.
_FINGERPRINTS = (
    (job_boards.GREENHOUSE, re.compile(r"(?:job-boards|boards)\.greenhouse\.io/([A-Za-z0-9_-]+)")),
    (job_boards.LEVER, re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)")),
    (job_boards.ASHBY, re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)")),
)


@dataclass(frozen=True)
class Candidate:
    kind: str  # "repo" | ats method name
    key: str  # dedupe key, e.g. "repo:owner/name" or "greenhouse:drweng"
    title: str
    url: str
    evidence: str


def due(today: str | None = None) -> bool:
    """Monday, or any day when the last run was over a week ago.

    The fallback matters: a Monday on which all three cron ticks are dropped -- which
    this project has already seen happen -- would otherwise skip a whole week in
    silence, and silence is the one signal this tool is built to make meaningful.
    """
    date = datetime.date.fromisoformat(today or state.today_iso())
    if date.weekday() == 0:
        return True
    last = state.read_last_discovery()
    if not last:
        return False
    try:
        return (date - datetime.date.fromisoformat(last)).days >= 7
    except ValueError:
        return False


def _known(sources: list[dict[str, str]]) -> set[str]:
    known = set()
    for source in sources:
        url = (source.get("url") or "").strip()
        method = (source.get("method") or "").strip()
        if method == "github_readme":
            known.add(f"repo:{url.lower()}")
        elif method in job_boards.PARSERS:
            known.add(f"{method}:{url.lower()}")
    return known


def search_repos(client: httpx.Client, known: set[str], deadline: float) -> list[Candidate]:
    found: dict[str, Candidate] = {}
    cutoff = datetime.date.fromisoformat(state.today_iso()) - datetime.timedelta(
        days=PUSHED_WITHIN_DAYS
    )
    for query in SEARCH_QUERIES:
        if time.monotonic() > deadline:
            break
        try:
            response = client.get(
                GITHUB_SEARCH,
                params={"q": query, "sort": "updated", "per_page": 15},
            )
            response.raise_for_status()
            items = response.json().get("items", [])
        except Exception:
            continue  # one bad query must not lose the others
        for repo in items:
            full = (repo.get("full_name") or "").strip()
            key = f"repo:{full.lower()}"
            if not full or key in known or key in found:
                continue
            if repo.get("archived") or repo.get("fork"):
                continue
            if (repo.get("stargazers_count") or 0) < MIN_STARS:
                continue
            pushed = (repo.get("pushed_at") or "")[:10]
            try:
                if datetime.date.fromisoformat(pushed) < cutoff:
                    continue
            except ValueError:
                continue
            found[key] = Candidate(
                kind="repo",
                key=key,
                title=full,
                url=repo.get("html_url") or "",
                evidence=(
                    f"{repo.get('stargazers_count')} stars, pushed {pushed}; matched "
                    f"\"{query.split(' in:')[0]}\""
                ),
            )
        time.sleep(state.REQUEST_DELAY_SECONDS)
    return list(found.values())


def mine_snapshots(known: set[str]) -> list[Candidate]:
    """Board slugs already sitting in the snapshots we store. No network."""
    found: dict[str, Candidate] = {}
    if not state.SNAPSHOTS.exists():
        return []
    for path in sorted(state.SNAPSHOTS.glob("*")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for method, pattern in _FINGERPRINTS:
            for slug in pattern.findall(text):
                key = f"{method}:{slug.lower()}"
                if key in known or key in found:
                    continue
                found[key] = Candidate(
                    kind=method,
                    key=key,
                    title=slug,
                    url=job_boards.ENDPOINTS[method].format(slug=slug),
                    evidence=f"linked from {path.name}; not in sources.csv",
                )
    return list(found.values())


def verify(candidates: list[Candidate], client: httpx.Client, deadline: float) -> list[Candidate]:
    """Drop mined slugs that do not actually answer, so no dead proposal is made."""
    checked: list[Candidate] = []
    budget = MAX_SLUG_VERIFICATIONS
    for candidate in candidates:
        if candidate.kind == "repo":
            checked.append(candidate)
            continue
        if budget <= 0 or time.monotonic() > deadline:
            break
        budget -= 1
        result = job_boards.check(
            {"source_id": candidate.key, "method": candidate.kind,
             "url": candidate.title, "program_names": ""},
            client,
        )
        if not result.ok:
            continue
        rows = result.extra.get("rows", 0)
        total = result.extra.get("postings", 0)
        checked.append(
            Candidate(
                kind=candidate.kind,
                key=candidate.key,
                title=candidate.title,
                url=candidate.url,
                evidence=f"{candidate.evidence}; API returns {total} postings, {rows} US student rows",
            )
        )
        time.sleep(state.REQUEST_DELAY_SECONDS)
    return checked


def run(
    github: httpx.Client, web: httpx.Client, sources: list[dict[str, str]]
) -> tuple[list[Candidate], list[str]]:
    """Never raises. Returns (candidates, notes-for-the-health-block)."""
    deadline = time.monotonic() + BUDGET_SECONDS
    notes: list[str] = []
    known = _known(sources)
    seen = {row["key"] for row in state.read_discovered()}

    candidates: list[Candidate] = []
    try:
        candidates += search_repos(github, known, deadline)
    except Exception as exc:
        notes.append(f"discovery: the GitHub repo search failed ({type(exc).__name__}).")
    try:
        candidates += verify(mine_snapshots(known), web, deadline)
    except Exception as exc:
        notes.append(f"discovery: ATS fingerprint mining failed ({type(exc).__name__}).")

    # A key already on file is never re-proposed, whatever its status. `rejected` is a
    # permanent tombstone; without this the same candidates arrive every Monday.
    fresh = [c for c in candidates if c.key not in seen]
    return fresh, notes


def record(candidates: list[Candidate]) -> None:
    """Append to data/discovered.csv. Never touches sources.csv."""
    if not candidates:
        return
    rows = state.read_discovered()
    today = state.today_iso()
    for candidate in candidates:
        rows.append(
            {
                "first_proposed": today,
                "last_proposed": today,
                "kind": candidate.kind,
                "key": candidate.key,
                "title": candidate.title,
                "url": candidate.url,
                "evidence": candidate.evidence,
                "status": "proposed",
            }
        )
    state.write_discovered(rows)


def lines(candidates: list[Candidate], limit: int = 10) -> list[str]:
    out = [
        "_Proposals only — nothing has been added. Accept one with the "
        "`/add-opportunity` skill; silence it for good by setting `status=rejected` "
        "on its row in `data/discovered.csv`._",
        "",
    ]
    for candidate in candidates[:limit]:
        out.append(f"- **{candidate.kind}** `{candidate.title}` — {candidate.evidence} → {candidate.url}")
    if len(candidates) > limit:
        out.append(f"- …and {len(candidates) - limit} more in `data/discovered.csv`.")
    return out
