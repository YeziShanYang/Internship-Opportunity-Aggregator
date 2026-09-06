"""Tier 1: GitHub repo READMEs (spec section 5).

No scraping. These are tables in public repos, so we ask the API for the default branch,
fetch the raw README, and diff at the row level keyed on company + role -- not on raw
text, which would fire on every badge and emoji change.

Each repo gets its own parser config because the five READMEs share almost nothing: NUFT
puts the company in the `##` heading above a two-column table, Cruz-Lopez has seven
different header layouts, Simplify uses HTML `<table>` rather than markdown, and zapply
and LuisaE fold company and role into a single linked `Name` column. A "generic markdown
table differ" would produce noise for four of the five.

The stored snapshot is a canonical one-line-per-row rendering rather than the README
itself. That is what makes `git log` readable: a newly posted role is exactly one added
line, instead of a 700KB blob reflowing.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field

import httpx

import state

GITHUB_API = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"

# Spec section 5: a new row whose title looks like an underclassman program is a
# candidate new program, not just a new posting.
DISCOVERY_PATTERN = re.compile(
    r"first.?year|freshman|sophomore|underclass|insight|discovery|invitational"
    r"|immersion|explore|ignite|launch|winternship|pre.?intern",
    re.IGNORECASE,
)

# Spec section 8 rule 4: these review on a rolling basis and close when full, so any
# change on a row mentioning them is high priority regardless of stated deadline.
ROLLING_PATTERN = re.compile(
    r"jane\s*street|nvidia|d\.?\s*e\.?\s*shaw|deshaw|point\s*72|cubist",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RepoConfig:
    """How to read one repo's tables."""

    table_format: str  # "markdown" | "html"
    entity_columns: tuple[str, ...] = ()  # candidate header names for the company
    role_columns: tuple[str, ...] = ()  # candidate header names for the role
    heading_as_entity: bool = False  # company lives in the `##` heading instead
    ignore_columns: tuple[str, ...] = ()  # columns too noisy to diff on
    # Extra columns folded into the row key. Simplify lists the same company and role
    # once per office, so company+role alone collides ~79 times and would hide roles.
    qualifier_columns: tuple[str, ...] = ()
    section_include: re.Pattern[str] | None = None
    section_exclude: re.Pattern[str] | None = None
    # Spec section 10.1 plausibility floors. Pick whichever quantity is *stable* for
    # the repo: NUFT's row count legitimately drains to near zero out of season (58
    # firm tables, 39 of them empty right now), so flooring its rows would cry wolf
    # every summer. Its section count is the invariant instead.
    min_rows: int = 10
    min_sections: int = 0


REPO_CONFIGS: dict[str, RepoConfig] = {
    "nuft-2027": RepoConfig(
        table_format="markdown",
        role_columns=("Role",),
        heading_as_entity=True,
        section_exclude=re.compile(r"^(contributing|using this repository|contents)", re.I),
        min_rows=0,
        min_sections=40,
    ),
    "underclassmen-cruz": RepoConfig(
        table_format="markdown",
        entity_columns=("Company", "University/Organization", "Organization", "State"),
        role_columns=("Role", "Program", "Opportunity", "Scholarship"),
        qualifier_columns=("Location",),
        min_rows=60,
    ),
    "underclassmen-zapply": RepoConfig(
        table_format="markdown",
        entity_columns=("Name",),
        min_rows=20,
    ),
    "luisae": RepoConfig(
        table_format="markdown",
        entity_columns=("Name",),
        min_rows=60,
    ),
    "simplify-2027": RepoConfig(
        table_format="html",
        entity_columns=("Company",),
        role_columns=("Role",),
        # Spec section 5: low signal overall, so keep only the sections that matter.
        section_include=re.compile(r"software engineering|quantitative finance", re.I),
        # "Age" is a relative timestamp ("1d", "2mo"). Diffing it would report every
        # row as changed every single day.
        ignore_columns=("Age",),
        qualifier_columns=("Location",),
        min_rows=150,
    ),
}


@dataclass
class Table:
    heading: str
    headers: list[str]
    rows: list[dict[str, str]] = field(default_factory=list)


@dataclass
class Row:
    section: str
    key: str
    value: str
    url: str = ""

    @property
    def identity(self) -> str:
        """Diff key. Section-qualified: Cruz-Lopez and Simplify both list the same
        company in more than one table, and an unqualified key would silently merge
        them and hide one of the two."""
        return f"{self.section} :: {self.key}"


@dataclass
class Snapshot:
    """What we remember about a repo between runs.

    Sections are tracked separately from rows because an empty table under a firm
    heading is meaningful: it means "this firm has no open roles right now", which is
    different from "this firm is not in the list". Without it, a firm posting its first
    role would look like a brand-new section.
    """

    sections: list[str] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)


# --------------------------------------------------------------------------- text

_IMG_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_IMG_TAG = re.compile(r"(?is)<img\b[^>]*>")
_ANCHOR = re.compile(r'(?is)<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>')
_TAG = re.compile(r"(?s)<[^>]+>")
_COMMENT = re.compile(r"(?s)<!--.*?-->")
_TRACKING_PARAM = re.compile(r"[?&](utm_[a-z]+|ref)=[^&\s)]*")


def clean_cell(text: str) -> str:
    """Normalise one cell: drop badge images and tags, keep link targets and emoji.

    Link targets are kept deliberately -- a changed `gh_jid` means a genuinely new
    posting, which is signal. Badge images and `utm_*` parameters are pure churn.
    """
    text = _COMMENT.sub(" ", text)
    text = _IMG_MD.sub(" ", text)
    text = _IMG_TAG.sub(" ", text)
    text = _ANCHOR.sub(lambda m: f"[{m.group(2)}]({m.group(1)})", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = _TRACKING_PARAM.sub("", text)
    text = text.replace(" ", " ").replace("**", "")
    return re.sub(r"\s+", " ", text).strip(" |")


def _split_markdown_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def parse_markdown_tables(text: str) -> list[Table]:
    lines = text.splitlines()
    tables: list[Table] = []
    heading = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _HEADING.match(line)
        if match:
            heading = clean_cell(match.group(2))
            index += 1
            continue
        # A table is a `|` line followed by a `|---|` separator.
        if line.lstrip().startswith("|") and index + 1 < len(lines) and _SEPARATOR.match(lines[index + 1]):
            headers = [clean_cell(cell) for cell in _split_markdown_row(line)]
            table = Table(heading=heading, headers=headers)
            index += 2
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                cells = _split_markdown_row(lines[index])
                if not _SEPARATOR.match(lines[index]):
                    table.rows.append(
                        {
                            header: clean_cell(cell)
                            for header, cell in zip(headers, cells)
                        }
                    )
                index += 1
            tables.append(table)
            continue
        index += 1
    return tables


_TABLE_BLOCK = re.compile(r"(?is)<table\b.*?</table>")
_TH = re.compile(r"(?is)<th\b[^>]*>(.*?)</th>")
_TR = re.compile(r"(?is)<tr\b[^>]*>(.*?)</tr>")
_TD = re.compile(r"(?is)<td\b[^>]*>(.*?)</td>")


def parse_html_tables(text: str) -> list[Table]:
    """Parse `<table>` blocks, attributing each to the nearest preceding markdown heading.

    Simplify's README interleaves markdown headings with HTML tables, and the heading is
    the only thing that says which category a table belongs to.
    """
    headings: list[tuple[int, str]] = [
        (match.start(), clean_cell(match.group(2)))
        for match in re.finditer(r"(?m)^(#{1,6})\s+(.*)$", text)
    ]
    tables: list[Table] = []
    for block in _TABLE_BLOCK.finditer(text):
        heading = ""
        for position, title in headings:
            if position < block.start():
                heading = title
            else:
                break
        body = block.group(0)
        headers = [clean_cell(cell) for cell in _TH.findall(body)]
        table = Table(heading=heading, headers=headers)
        for row_html in _TR.findall(body):
            cells = [clean_cell(cell) for cell in _TD.findall(row_html)]
            if cells:
                table.rows.append(dict(zip(headers, cells)))
        tables.append(table)
    # Simplify emits one `<table>` per section but repeats `<tbody>` per row, so blocks
    # can nest oddly; merging same-heading tables keeps sections whole.
    merged: dict[str, Table] = {}
    for table in tables:
        existing = merged.get(table.heading)
        if existing and existing.headers == table.headers:
            existing.rows.extend(table.rows)
        else:
            merged.setdefault(table.heading, table)
    return list(merged.values())


# --------------------------------------------------------------------------- rows


def _first_present(headers: list[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in headers:
            return candidate
    return None


_URL = re.compile(r"https?://[^\s)\]]+")


# Simplify writes a bare "↳" in the Company cell to mean "same company as the row
# above". Taken literally it produces hundreds of rows keyed on "↳".
_CARRY_FORWARD = ("↳", "->", "⤷")


def extract(text: str, config: RepoConfig) -> Snapshot:
    """Flatten every table in the README into canonical `(section, key, value)` rows."""
    parser = parse_markdown_tables if config.table_format == "markdown" else parse_html_tables
    snapshot = Snapshot()
    seen_keys: dict[tuple[str, str], int] = {}
    for table in parser(text):
        if config.section_include and not config.section_include.search(table.heading):
            continue
        if config.section_exclude and config.section_exclude.search(table.heading):
            continue
        entity_column = _first_present(table.headers, config.entity_columns)
        role_column = _first_present(table.headers, config.role_columns)
        if not config.heading_as_entity and not entity_column:
            continue  # not a listing table (legend, key, contents, ...)
        if table.heading and table.heading not in snapshot.sections:
            snapshot.sections.append(table.heading)
        previous_entity = ""
        for cells in table.rows:
            entity = table.heading if config.heading_as_entity else cells.get(entity_column, "")
            if entity.strip() in _CARRY_FORWARD:
                entity = previous_entity
            elif entity.strip():
                previous_entity = entity
            role = cells.get(role_column, "") if role_column else ""
            entity, role = entity.strip(), role.strip()
            if not entity and not role:
                continue
            key = f"{entity} / {role}" if role else entity
            for qualifier in config.qualifier_columns:
                value_ = cells.get(qualifier, "").strip()
                if value_:
                    key = f"{key} @ {value_}"
            value_parts = [
                f"{header}={cells[header]}"
                for header in table.headers
                if header and header not in config.ignore_columns and cells.get(header)
            ]
            value = "; ".join(value_parts)
            urls = _URL.findall(value)
            key = re.sub(r"\s+", " ", key)
            # Any collision that survives the qualifiers gets a stable ordinal rather
            # than being silently deduplicated away.
            seen_keys[(table.heading, key)] = seen_keys.get((table.heading, key), 0) + 1
            occurrence = seen_keys[(table.heading, key)]
            if occurrence > 1:
                key = f"{key} #{occurrence}"
            snapshot.rows.append(
                Row(
                    section=table.heading,
                    key=key,
                    value=value,
                    url=urls[0] if urls else "",
                )
            )
    return snapshot


SECTIONS_HEADER = "# sections"
ROWS_HEADER = "# rows"


def render_snapshot(snapshot: Snapshot) -> str:
    """Canonical, sorted text. This is what gets committed.

    Sorting matters: it means a newly posted role is exactly one added line in
    `git diff`, rather than a reflowed blob.
    """
    lines = [SECTIONS_HEADER]
    lines += sorted(set(snapshot.sections))
    lines.append(ROWS_HEADER)
    lines += sorted({f"{row.section}\t{row.key}\t{row.value}" for row in snapshot.rows})
    return "\n".join(lines) + "\n"


def parse_snapshot(text: str) -> Snapshot:
    snapshot = Snapshot()
    block = ROWS_HEADER  # tolerate a legacy rows-only snapshot
    for line in text.splitlines():
        if not line.strip():
            continue
        if line in (SECTIONS_HEADER, ROWS_HEADER):
            block = line
            continue
        if block == SECTIONS_HEADER:
            snapshot.sections.append(line)
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        section, key, value = parts[0], parts[1], "\t".join(parts[2:])
        urls = _URL.findall(value)
        snapshot.rows.append(
            Row(section=section, key=key, value=value, url=urls[0] if urls else "")
        )
    return snapshot


def diff_snapshots(source_id: str, old: Snapshot, new: Snapshot) -> list[state.Change]:
    """Row-level diff keyed on company + role (spec section 5)."""
    old_by_key = {row.identity: row for row in old.rows}
    new_by_key = {row.identity: row for row in new.rows}
    changes: list[state.Change] = []

    # A brand-new firm section is worth calling out in its own right (spec section 5).
    for section in sorted(set(new.sections) - set(old.sections)):
        if section:
            changes.append(
                state.Change(
                    source_id=source_id,
                    kind="added",
                    key=f"new section: {section}",
                    detail=f'A section that was not in the previous snapshot: "{section}".',
                    is_discovery_candidate=True,
                    rolling=bool(ROLLING_PATTERN.search(section)),
                )
            )

    for key in sorted(new_by_key.keys() - old_by_key.keys()):
        row = new_by_key[key]
        changes.append(
            state.Change(
                source_id=source_id,
                kind="added",
                key=row.key,
                detail=f"New row: {row.value}",
                url=row.url,
                is_discovery_candidate=bool(DISCOVERY_PATTERN.search(f"{row.key} {row.value}")),
                rolling=bool(ROLLING_PATTERN.search(f"{row.key} {row.value}")),
            )
        )

    for key in sorted(old_by_key.keys() - new_by_key.keys()):
        row = old_by_key[key]
        changes.append(
            state.Change(
                source_id=source_id,
                kind="removed",
                key=row.key,
                detail=f"Row disappeared. It previously read: {row.value}",
                url=row.url,
                rolling=bool(ROLLING_PATTERN.search(f"{key} {row.value}")),
            )
        )

    for key in sorted(old_by_key.keys() & new_by_key.keys()):
        before, after = old_by_key[key].value, new_by_key[key].value
        if before != after:
            changes.append(
                state.Change(
                    source_id=source_id,
                    kind="changed",
                    key=new_by_key[key].key,
                    detail=f"Was: {before}\nNow: {after}",
                    url=new_by_key[key].url,
                    is_discovery_candidate=bool(DISCOVERY_PATTERN.search(f"{key} {after}")),
                    rolling=bool(ROLLING_PATTERN.search(f"{key} {after}")),
                )
            )
    return changes


# --------------------------------------------------------------------------- fetch


def fetch_readme(client: httpx.Client, repo: str) -> tuple[str, str]:
    """Return (readme_text, default_branch).

    The branch comes from the API rather than a guess: LuisaE/opportunities is on
    `master` and SimplifyJobs is on `dev`.
    """
    meta = client.get(f"{GITHUB_API}/repos/{repo}")
    meta.raise_for_status()
    branch = meta.json().get("default_branch") or "main"
    time.sleep(state.REQUEST_DELAY_SECONDS)
    readme = client.get(f"{RAW_BASE}/{repo}/{branch}/README.md")
    readme.raise_for_status()
    return readme.text, branch


def build_client(token: str | None) -> httpx.Client:
    headers = {"User-Agent": state.USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(
        headers=headers, timeout=state.HTTP_TIMEOUT_SECONDS, follow_redirects=True
    )


def check(source: dict[str, str], client: httpx.Client) -> state.SourceResult:
    """Check one Tier 1 repo. Never raises: a failure is a result, not an exception."""
    source_id = source["source_id"]
    config = REPO_CONFIGS.get(source_id)
    if config is None:
        return state.SourceResult(
            source_id=source_id, ok=False, error=f"no parser config for {source_id!r}"
        )

    try:
        text, branch = fetch_readme(client, source["url"])
    except httpx.HTTPStatusError as exc:
        return state.SourceResult(
            source_id=source_id,
            ok=False,
            error=f"HTTP {exc.response.status_code} fetching {exc.request.url}",
        )
    except Exception as exc:  # network, DNS, timeout, decode
        return state.SourceResult(
            source_id=source_id, ok=False, error=f"{type(exc).__name__}: {exc}"
        )

    parsed = extract(text, config)

    # Spec section 10.1: a redesign that yields nothing parseable must be an alert, not
    # a silent "nothing changed today".
    shortfall = ""
    if len(parsed.rows) < config.min_rows:
        shortfall = f"parsed only {len(parsed.rows)} rows, expected at least {config.min_rows}"
    elif len(parsed.sections) < config.min_sections:
        shortfall = (
            f"parsed only {len(parsed.sections)} sections, "
            f"expected at least {config.min_sections}"
        )
    if shortfall:
        return state.SourceResult(
            source_id=source_id,
            ok=False,
            content_length=len(text),
            error=(
                f"{shortfall} (from {len(text)} bytes on branch {branch}). "
                "Likely a README restructure."
            ),
        )

    rendered = render_snapshot(parsed)
    previous = state.read_snapshot(source_id, ext="tsv")
    extra = {"rows": len(parsed.rows), "sections": len(parsed.sections), "branch": branch}
    if previous is None:
        return state.SourceResult(
            source_id=source_id,
            ok=True,
            content_length=len(text),
            snapshot_text=rendered,
            baseline=True,
            extra=extra,
        )

    changes = diff_snapshots(source_id, parse_snapshot(previous), parsed)
    for change in changes:
        change.program_name = source.get("program_names", "")
    return state.SourceResult(
        source_id=source_id,
        ok=True,
        changes=changes,
        content_length=len(text),
        snapshot_text=rendered,
        extra=extra,
    )
