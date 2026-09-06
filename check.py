"""Entry point for the daily run.

Load state, check every enabled source, classify what changed, render a digest, deliver
it as a GitHub Issue, then write state back so the commit that follows is the audit
trail. Phase 1 wires Tier 1 (GitHub repo READMEs) only.

The process exits non-zero only when something is wrong with the *tool*. A source that
404s, gets blocked, or returns nothing is data: it lands in the health block and bumps
that source's failure count. Spec section 10.1 -- silent success is the failure mode
that kills projects like this, so "the job exited 0" must never be the only evidence
that anything happened.
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
import time

import classify
import digest
import state
from sources import github_repos

TIER1_METHODS = {"github_readme"}


def run_sources(
    sources: list[dict[str, str]], only: str | None
) -> list[state.SourceResult]:
    results: list[state.SourceResult] = []
    token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print(
            "note: no GH_PAT/GITHUB_TOKEN set; using unauthenticated GitHub API "
            "(60 requests/hour instead of 5,000)",
            file=sys.stderr,
        )
    with github_repos.build_client(token) as client:
        for source in sources:
            source_id = source["source_id"]
            if only and source_id != only:
                continue
            if source["method"] not in TIER1_METHODS:
                continue  # Tiers 2 and 3 are not built yet
            if (source.get("method") or "") == "manual":
                continue
            results.append(github_repos.check(source, client))
            time.sleep(state.REQUEST_DELAY_SECONDS)  # spec section 11: be a good citizen
    return results


def update_source_state(
    sources: list[dict[str, str]], results: list[state.SourceResult]
) -> None:
    by_id = {source["source_id"]: source for source in sources}
    now = state.iso()
    for result in results:
        source = by_id.get(result.source_id)
        if source is None:
            continue
        if result.ok:
            source["last_success"] = now
            source["consecutive_failures"] = "0"
        else:
            previous = int(source.get("consecutive_failures") or 0)
            source["consecutive_failures"] = str(previous + 1)


def update_program_state(
    programs: list[dict[str, str]],
    sources: list[dict[str, str]],
    results: list[state.SourceResult],
    judgments: list[classify.Judgment],
) -> None:
    """Update the tracking columns only.

    `eligible`, `notes` and the rest of the hand-researched columns are never touched
    here (spec section 8 rule 5). A status change proposed by the classifier is applied
    to the tracking `status` column, which the tool owns, and only when the model was
    reasonably sure.
    """
    now = state.iso()
    by_source = {source["source_id"]: source for source in sources}
    by_name = {program["name"]: program for program in programs}

    for result in results:
        source = by_source.get(result.source_id)
        if source is None:
            continue
        names = [n.strip() for n in (source.get("program_names") or "").split("|") if n.strip()]
        for name in names:
            program = by_name.get(name)
            if program is None:
                continue
            program["last_checked"] = now
            if result.ok and result.snapshot_text is not None:
                program["snapshot_hash"] = state.sha256_text(result.snapshot_text)
            if result.changes:
                program["last_changed"] = now

    for judgment in judgments:
        if not (judgment.classified and judgment.relevant):
            continue
        if judgment.confidence == "low":
            continue
        program = by_name.get(judgment.program_name)
        if program is None or judgment.new_status not in state.PROGRAM_STATUSES:
            continue
        if judgment.new_status != "unknown" and program["status"] != judgment.new_status:
            state.append_proposal(
                f"{judgment.change.source_id}\t{judgment.program_name}\tstatus "
                f"{program['status']} -> {judgment.new_status}\t{judgment.why}"
            )
            program["status"] = judgment.new_status
            program["last_changed"] = now


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the digest to stdout; open no issue and write no state",
    )
    parser.add_argument("--only", help="check a single source_id")
    parser.add_argument(
        "--no-classify", action="store_true", help="skip the LLM step entirely"
    )
    parser.add_argument(
        "--force-health",
        action="store_true",
        help="render the weekly health summary regardless of the day",
    )
    args = parser.parse_args(argv)

    sources = state.read_sources()
    programs = state.read_programs()
    if not sources:
        print("data/sources.csv is empty or missing", file=sys.stderr)
        return 2
    if not programs:
        print("data/programs.csv is empty or missing; run seed_programs.py", file=sys.stderr)
        return 2

    results = run_sources(sources, args.only)
    if not results:
        print("no sources matched; nothing to do", file=sys.stderr)
        return 2

    changes = [change for result in results for change in result.changes]
    muted = {
        program["name"]
        for program in programs
        if (program.get("muted") or "").strip().lower() == "true"
    }
    changes = [change for change in changes if change.program_name not in muted]

    by_id = {source["source_id"]: source for source in sources}
    judgments = (
        [] if args.no_classify else classify.classify(changes, by_id)
    )

    update_source_state(sources, results)
    update_program_state(programs, sources, results, judgments)

    is_monday = datetime.date.today().weekday() == 0 or args.force_health
    send, health_only = digest.should_send(judgments, results, is_monday)
    title, body = digest.render(judgments, results, by_id, health_only=health_only)

    print(f"{len(results)} sources checked, {len(changes)} changes, {len(judgments)} judged")
    for result in results:
        status = "ok" if result.ok else f"FAIL ({result.error})"
        print(f"  {result.source_id}: {status} rows={result.extra.get('rows', '-')}")

    if args.dry_run:
        print("\n--- would send ---" if send else "\n--- would NOT send (no changes) ---")
        print(f"TITLE: {title}\n")
        print(body)
        print("\n(dry run: no issue opened, no state written)")
        return 0

    for result in results:
        if result.ok and result.snapshot_text is not None:
            state.write_snapshot(result.source_id, result.snapshot_text, ext="tsv")
    state.write_sources(sources)
    state.write_programs(programs)

    if send:
        print(digest.deliver(title, body))
    else:
        print("no changes and not Monday: no issue opened (spec section 9 cadence)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
