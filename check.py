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
import os
import sys
import time

import classify
import digest
import discover
import state
from sources import github_repos, job_boards, page_watch, postings

# Which module handles each `method` in sources.csv, and which HTTP client it gets.
#
# Two clients, not one, and that is a credential boundary rather than a style choice.
# `github_repos.build_client` puts `Authorization: Bearer <GH_PAT>` on every request it
# makes. While only GitHub was contacted that was harmless; the moment a job board or a
# careers page shares the client, the PAT is sent to boards-api.greenhouse.io,
# api.lever.co, api.ashbyhq.com and every firm's marketing site. The web client carries
# the honest User-Agent and no credentials at all.
GITHUB_CLIENT = "github"
WEB_CLIENT = "web"

CHECKERS: dict[str, tuple] = {
    "github_readme": (github_repos.check, GITHUB_CLIENT),
    "greenhouse": (job_boards.check, WEB_CLIENT),
    "lever": (job_boards.check, WEB_CLIENT),
    "ashby": (job_boards.check, WEB_CLIENT),
    "workday": (job_boards.check, WEB_CLIENT),
    "phenom": (job_boards.check, WEB_CLIENT),
    "page_text": (page_watch.check, WEB_CLIENT),
}

# Defined in state.py so build_xlsx.py can share it. Anything else absent from
# CHECKERS is a broken row, not a source to skip.
UNWATCHED = state.UNWATCHED_METHOD


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
    with github_repos.build_client(token) as github_client, postings.build_client() as web_client:
        clients = {GITHUB_CLIENT: github_client, WEB_CLIENT: web_client}
        for source in sources:
            source_id = source["source_id"]
            if only and source_id != only:
                continue
            method = (source.get("method") or "").strip()
            if method == UNWATCHED:
                continue
            handler = CHECKERS.get(method)
            if handler is None:
                # Spec 10.1. This used to be a bare `continue`, which meant a typo in
                # sources.csv produced no SourceResult at all: no health line, no
                # failure count, and a silently unwatched source indistinguishable
                # from a quiet one. A configuration error has to be visible.
                results.append(
                    state.SourceResult(
                        source_id=source_id,
                        ok=False,
                        error=f"sources.csv sets method={method!r}, which no module handles",
                    )
                )
                continue
            check_fn, client_name = handler
            results.append(check_fn(source, clients[client_name]))
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

    # Aggregate job boards must not drive the status of their own programs.csv row.
    # `judgment.program_name` falls back to `change.program_name`, which for every
    # Simplify row is the single aggregate program "SimplifyJobs Summer 2027
    # Internships". Before postings were classified this path was unreachable for that
    # source; now ~35 per-row judgments a day would each try to set the status of one
    # row that represents the whole board, which is meaningless and would churn
    # proposals.log. Per-program sources are unaffected.
    noisy = {
        source["source_id"]
        for source in sources
        if (source.get("signal") or "high").strip().lower() == "low"
    }

    for judgment in judgments:
        if not (judgment.classified and judgment.relevant):
            continue
        if judgment.confidence == "low":
            continue
        if judgment.change.source_id in noisy:
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
        "--force-discovery",
        action="store_true",
        help="run the weekly source-discovery pass regardless of the day",
    )
    parser.add_argument(
        "--no-classify", action="store_true", help="skip the LLM step entirely"
    )
    parser.add_argument(
        "--force-health",
        action="store_true",
        help="render the quiet-day (status only) digest and ignore the once-a-day lock",
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

    # Anything the owner ticked off in a delivered digest -- applied to, or not
    # interested -- is dropped before classification, which also saves the model call.
    # Newly ticked keys are merged in first so a tick takes effect the next morning.
    applied = state.read_applied()
    newly_ticked = digest.collect_applied()
    if newly_ticked:
        applied.update({k: v for k, v in newly_ticked.items() if k not in applied})
        if not args.dry_run:
            state.write_applied(applied)
    before_applied = len(changes)
    changes = [
        change
        for change in changes
        if state.change_key(change.source_id, change.key) not in applied
    ]
    suppressed_applied = before_applied - len(changes)

    by_id = {source["source_id"]: source for source in sources}
    judgments = (
        [] if args.no_classify else classify.classify(changes, by_id)
    )

    update_source_state(sources, results)
    update_program_state(programs, sources, results, judgments)

    # Exactly one digest a day (spec section 9). The morning schedule fires three ticks
    # so a dropped cron tick is not a missed day, so between one and three runs reach
    # this line every morning and only the first may mail.
    #
    # Ask GitHub first and treat the local marker as the fallback, not the other way
    # round: the marker comes from whatever commit this run checked out, and late ticks
    # do not necessarily dispatch in cron order, so it can be stale. See
    # digest.delivered_issue_exists. Either source saying "delivered" is enough --
    # a stale marker cannot cause a duplicate, only a redundant suppression, and the
    # marker is only ever stale in the direction of a delivery this run did not see.
    today = state.today_iso()
    marker_delivered = state.read_last_delivered() == today
    issue_delivered = digest.delivered_issue_exists(today)
    delivered_today = (
        marker_delivered if issue_delivered is None else (issue_delivered or marker_delivered)
    )

    send, status_only = digest.should_send(
        judgments, results, already_delivered_today=delivered_today
    )
    if args.force_health:
        # A deliberate manual override: render the quiet-day shape on demand and ignore
        # the once-a-day lock.
        send, status_only = True, True

    # Discovery rides along in whichever run actually mails. The gate is "this run is
    # delivering", not "this is the first tick": when a tick is dropped, a later one
    # does the mailing, and the one-issue-per-date cap means discovery cannot open an
    # issue of its own.
    discovery_lines: list[str] = []
    discovery_notes: list[str] = []
    # --force-discovery works under --dry-run too, so the section can be eyeballed
    # before it ever mails; the writes below are what --dry-run actually suppresses.
    if (send and not args.dry_run and discover.due()) or args.force_discovery:
        try:
            token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
            with github_repos.build_client(token) as gh, postings.build_client() as web:
                candidates, discovery_notes = discover.run(gh, web, sources)
            if not args.dry_run:
                discover.record(candidates)
                state.write_last_discovery()
            if candidates:
                discovery_lines = discover.lines(candidates)
        except Exception as exc:  # the digest must never be lost because this broke
            discovery_notes = [
                f"the weekly discovery pass failed: {type(exc).__name__}: {exc}"
            ]

    title, body = digest.render(
        judgments, results, by_id, suppressed_applied=suppressed_applied,
        discovery_lines=discovery_lines, status_only=status_only,
    )
    for note in discovery_notes:
        body += f"\n- ⚠ {note}"

    print(f"{len(results)} sources checked, {len(changes)} changes, {len(judgments)} judged")
    for result in results:
        status = "ok" if result.ok else f"FAIL ({result.error})"
        print(f"  {result.source_id}: {status} rows={result.extra.get('rows', '-')}")

    if args.dry_run:
        print(
            "\n--- would send ---"
            if send
            else f"\n--- would NOT send: already delivered on {today} ---"
        )
        print(f"TITLE: {title}\n")
        print(body)
        print("\n(dry run: no issue opened, no state written)")
        return 0

    for result in results:
        if result.ok and result.snapshot_text is not None:
            state.write_snapshot(
                result.source_id, result.snapshot_text, ext=result.snapshot_ext
            )
    state.write_sources(sources)
    state.write_programs(programs)

    if send:
        print(digest.deliver(title, body))
        state.write_last_delivered()
    else:
        print(
            f"a digest was already delivered on {today}: this run is a schedule retry, "
            "and the cadence is exactly one digest a day (spec section 9)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
