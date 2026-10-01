"""The weekly coverage measurement. A job, not a stage: it fetches (gather), measures
(process), records (persist) and renders one HEALTH line (deliver).

It rides along with the Monday discovery pass, in whichever run actually mails, for the
same reason discovery does: the cap is one issue a day, so it cannot mail on its own.
A failure here is a note in the digest and never costs the digest.
"""
from __future__ import annotations

import httpx

from core import clock, paths
from persist import store
from gather import ats
from process import coverage, parse_readme


def run(web: httpx.Client, todays_snapshots: dict[str, str]) -> list[coverage.Scope]:
    """Measure against Simplify's live list.

    `todays_snapshots` overlays the stored ones, so a source fetched this morning is
    counted as it is now rather than as it was yesterday.
    """
    response = web.get(paths.SIMPLIFY_LISTINGS_URL, timeout=60)
    response.raise_for_status()
    listings = response.json()
    if not isinstance(listings, list) or not listings:
        # A 200 whose shape is wrong is a failure, never "Simplify has no postings".
        raise ValueError("Simplify listings.json was not a non-empty list")
    snapshots = {**store.read_snapshots("tsv"), **todays_snapshots}
    aggregators = set(parse_readme.REPO_CONFIGS)
    bulk = frozenset(s["source_id"] for s in store.read_sources() if s["method"] in ats.BULK)
    return coverage.measure(
        coverage.from_snapshots(snapshots, aggregators, bulk),
        coverage.from_reference(listings),
        aggregators,
    )


def record(scopes: list[coverage.Scope], date: str | None = None) -> None:
    """Append this week's numbers; a re-run on the same date replaces its own rows."""
    date = date or clock.today_iso()
    rows = [row for row in store.read_coverage() if row["date"] != date]
    rows += [
        {"date": date, "scope": s.label, "tracker": str(s.tracker),
         "simplify": str(s.reference), "tracker_only": str(s.tracker_only),
         "employer_only": str(s.employer_only), "lead_pct": f"{s.lead:.0%}"}
        for s in scopes
    ]
    store.write_coverage(rows)


def line(scopes: list[coverage.Scope]) -> str:
    by = {s.label: s for s in scopes}
    fields, quant = by["fields"], by["quant"]
    return (
        f"Coverage vs Simplify's whole list: **{fields.tracker_only} postings in the target "
        f"fields it does not carry (+{fields.lead:.0%})**, {fields.employer_only} of them "
        f"on no aggregator at all; quant +{quant.lead:.0%} ({quant.tracker_only} of "
        f"{quant.tracker}). Floor, not estimate: near-duplicate titles are merged. "
        "History in `data/coverage.csv`."
    )
