"""Tier 1 compatibility shim. The real code is `gather.github_readme` +
`process.parse_readme`.

Kept so `check.CHECKERS`, `discover` and every `FakeClient` test carry on working
untouched while the internals move. The re-exports below are the names other modules
and the tests already reach for.
"""
from __future__ import annotations

import httpx

from core import models
from gather import github_readme
from gather.github_readme import (  # noqa: F401
    GITHUB_API,
    RAW_BASE,
    build_client,
    fetch_readme,
)
from persist import store
from process import parse_readme
from process.parse_readme import (  # noqa: F401
    REPO_CONFIGS,
    RepoConfig,
    Table,
    clean_cell,
    extract,
    parse_html_tables,
    parse_markdown_tables,
)


def check(source: dict[str, str], client: httpx.Client) -> models.SourceResult:
    """Check one Tier 1 repo. Never raises: a failure is a result, not an exception."""
    fetched = github_readme.fetch(source, client)
    previous = store.read_snapshot(source["source_id"], ext="tsv")
    return parse_readme.assess(
        source, REPO_CONFIGS.get(source["source_id"]), fetched, previous
    )
