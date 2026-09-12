"""The morning digest: the job that composes every stage.

Load state, check every enabled source, screen and classify what changed, render a
digest, deliver it as a GitHub Issue, then write state back so the commit that follows
is the audit trail.

A job is not a stage. This one reaches into `gather` (the source checks and, on a
Monday, discovery's GitHub search), `process` (the suppression filters), `classify` and
`deliver`, plus `persist` at both ends -- which is exactly why it lives in `jobs/`
rather than being squeezed into one level of the stage graph.

The process exits non-zero only when something is wrong with the *tool*. A source that
404s, gets blocked, or returns nothing is data: it lands in the health block and bumps
that source's failure count. Spec section 10.1 -- silent success is the failure mode
that kills projects like this, so "the job exited 0" must never be the only evidence
that anything happened.
"""
from __future__ import annotations

import argparse
import sys

import classify
import digest
import discover
from core import clock, models, paths
from enrich import bodies as enrich_bodies
from gather import clients, collect
from persist import artifacts, store
from process import build, suppress
import screen


# Re-exported so the checker registry has one name for the rest of the tree. The
# method tables themselves live with the stages that own them: which client a method
# needs is a fetch concern (gather.collect.CLIENT_FOR) and which assessor reads it is a
# parse concern (process.build.ASSESSORS).
CHECKERS = collect.CLIENT_FOR
GITHUB_CLIENT = collect.GITHUB_CLIENT
WEB_CLIENT = collect.WEB_CLIENT
UNWATCHED = collect.UNWATCHED


def run_sources(
    sources: list[dict[str, str]], only: str | None
) -> list[models.SourceResult]:
    """Gather, then process. Two stages, one call, for the callers that want both."""
    return build.build(sources, collect.collect(sources, only))


def update_source_state(
    sources: list[dict[str, str]], results: list[models.SourceResult]
) -> None:
    by_id = {source["source_id"]: source for source in sources}
    now = clock.iso()
    for result in results:
        source = by_id.get(result.source_id)
        if source is None:
            continue
        if result.quarantined:
            # The breaker skipped the fetch, so nothing was learned. Leaving every
            # counter alone is what stops a quarantine from extending itself: if this
            # incremented, a source would back off further for not being looked at.
            continue
        source["last_attempt"] = now
        if result.ok:
            source["last_success"] = now
            source["consecutive_failures"] = "0"
        else:
            previous = int(source.get("consecutive_failures") or 0)
            source["consecutive_failures"] = str(previous + 1)


def update_program_state(
    programs: list[dict[str, str]],
    sources: list[dict[str, str]],
    results: list[models.SourceResult],
    judgments: list[classify.Judgment],
) -> None:
    """Update the tracking columns only.

    `eligible`, `notes` and the rest of the hand-researched columns are never touched
    here (spec section 8 rule 5). A status change proposed by the classifier is applied
    to the tracking `status` column, which the tool owns, and only when the model was
    reasonably sure.
    """
    now = clock.iso()
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
                program["snapshot_hash"] = clock.sha256_text(result.snapshot_text)
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
        if program is None or judgment.new_status not in paths.PROGRAM_STATUSES:
            continue
        if judgment.new_status != "unknown" and program["status"] != judgment.new_status:
            store.append_proposal(
                f"{judgment.change.source_id}\t{judgment.program_name}\tstatus "
                f"{program['status']} -> {judgment.new_status}\t{judgment.why}"
            )
            program["status"] = judgment.new_status
            program["last_changed"] = now


def _enrich(
    changes: list[models.Change], args: argparse.Namespace
) -> dict[str, enrich_bodies.PostingBody]:
    """Run the enrich stage and record what it managed."""
    if args.no_classify:
        # Nothing downstream will read a posting body, so fetching ~35 of them would be
        # pure cost. Still writes the (empty) artifact, so a reader can tell the stage
        # ran and declined rather than never having run.
        bodies: dict[str, enrich_bodies.PostingBody] = {}
    else:
        bodies = enrich_bodies.collect(changes)
    artifacts.write(artifacts.ENRICHED, "enriched", bodies)
    return bodies


def _screen(
    changes: list[models.Change],
    bodies: dict[str, enrich_bodies.PostingBody],
) -> dict[str, screen.Verdict]:
    """Run the deterministic screen and record its verdicts."""
    verdicts = screen.apply(changes, bodies)
    artifacts.write(artifacts.SCREENED, "screened", verdicts)
    return verdicts


def _suppress(
    results: list[models.SourceResult], programs: list[dict[str, str]]
) -> tuple[list[models.Change], list[models.FilterReport]]:
    return suppress.suppress(
        [change for result in results for change in result.changes],
        muted=suppress.muted_programmes(programs),
        applied=store.read_applied(),
    )


def _write_change_set(
    results: list[models.SourceResult],
    changes: list[models.Change],
    filters: list[models.FilterReport],
) -> None:
    """Written whether or not this is a dry run. `.run/` is derived scratch rather than
    the database, and a dry run is exactly when someone wants to read it. Written after
    suppression, because a muted change is not a change this run is acting on -- but
    the count of them is, which is what the FilterReports carry."""
    artifacts.write(artifacts.CHANGES, "changes", models.ChangeSet(
        changes=changes,
        metrics=[models.SourceMetrics.of(result) for result in results],
        # Both kinds together: the per-source screens each checker applied, and the two
        # run-wide mute filters. One list, because "did every filter report?" has to be
        # answerable in one place.
        filters=[report for result in results for report in result.filters] + filters,
    ))


def run(args: argparse.Namespace) -> int:
    """The whole morning, over already-parsed arguments from `run.py`."""
    sources = store.read_sources()
    programs = store.read_programs()
    if not sources:
        print("data/sources.csv is empty or missing", file=sys.stderr)
        return 2
    if not programs:
        print("data/programs.csv is empty or missing; run seed_programs.py", file=sys.stderr)
        return 2

    # Cleared before the run, not after: a stage must never read half of this run's
    # artifacts and half of yesterday's. The codec's version check catches a shape
    # change and cannot catch a source that failed today and left last run's body in
    # place, which would read as a fetch that worked.
    artifacts.reset()

    results = run_sources(sources, args.only)
    if not results:
        print("no sources matched; nothing to do", file=sys.stderr)
        return 2

    changes, filters = _suppress(results, programs)
    suppressed_muted = suppress.removed_by(filters, suppress.MUTED)
    suppressed_applied = suppress.removed_by(filters, suppress.APPLIED)
    _write_change_set(results, changes, filters)

    by_id = {source["source_id"]: source for source in sources}

    # The second source-network stage, named rather than hidden. O(changes): bodies are
    # fetched for the rows that moved, not for the ~4,000 postings on the boards.
    bodies = _enrich(changes, args)
    verdicts = _screen(changes, bodies)
    filters.append(screen.report(changes, verdicts))
    judgments = (
        [] if args.no_classify
        else classify.classify(changes, by_id, bodies, verdicts)
    )
    artifacts.write(artifacts.JUDGED, "judged", judgments)

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
    today = clock.today_iso()
    marker_delivered = store.read_last_delivered() == today
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
            with clients.build_github_client(clients.github_token()) as gh, \
                    clients.build_web_client() as web:
                candidates, discovery_notes = discover.run(gh, web, sources)
            if not args.dry_run:
                discover.record(candidates)
                store.write_last_discovery()
            if candidates:
                discovery_lines = discover.lines(candidates)
        except Exception as exc:  # the digest must never be lost because this broke
            discovery_notes = [
                f"the weekly discovery pass failed: {type(exc).__name__}: {exc}"
            ]

    title, body = digest.render(
        judgments, results, by_id, suppressed_applied=suppressed_applied,
        suppressed_muted=suppressed_muted,
        discovery_lines=discovery_lines, status_only=status_only,
        enriched=bodies, filters=filters,
    )
    for note in discovery_notes:
        body += f"\n- ⚠ {note}"

    # The exact bytes that would be posted. This is the artifact worth having most: it
    # is what a later `run.py render` has to reproduce byte-for-byte, and it is the
    # evidence for "the refactor did not change the digest".
    artifacts.write_text(artifacts.DIGEST, body)
    artifacts.write_text(artifacts.DIGEST_TITLE, title + "\n")

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
            store.write_snapshot(
                result.source_id, result.snapshot_text, ext=result.snapshot_ext
            )
    store.write_sources(sources)
    store.write_programs(programs)

    if send:
        print(digest.deliver(title, body))
        store.write_last_delivered()
    else:
        print(
            f"a digest was already delivered on {today}: this run is a schedule retry, "
            "and the cadence is exactly one digest a day (spec section 9)"
        )
    return 0


def gather_only(args: argparse.Namespace) -> int:
    """`run.py gather`: fetch every source and stop. Writes `.run/raw/`."""
    sources = store.read_sources()
    if not sources:
        print("data/sources.csv is empty or missing", file=sys.stderr)
        return 2
    artifacts.reset()
    attempts = collect.collect(sources, args.only)
    if not attempts:
        print("no sources matched; nothing to do", file=sys.stderr)
        return 2
    failed = [a for a in attempts if not a.ok and not a.quarantined]
    quarantined = [a for a in attempts if a.quarantined]
    print(
        f"{len(attempts)} sources attempted, {len(attempts) - len(failed) - len(quarantined)} "
        f"fetched, {len(failed)} failed, {len(quarantined)} skipped by the breaker"
    )
    print(f"wrote {artifacts.path(artifacts.RAW_INDEX)}")
    return 0


def process_only(args: argparse.Namespace) -> int:
    """`run.py process`: read `.run/raw/` and write `.run/changes.json`. No network.

    Runnable repeatedly against one gather, which is the property the artifact boundary
    exists to give: a second run must make no requests and produce the same answer.
    """
    sources = store.read_sources()
    programs = store.read_programs()
    if not sources or not programs:
        print("data/sources.csv or data/programs.csv is empty or missing", file=sys.stderr)
        return 2
    results = build.build(sources, build.load_attempts())
    changes, filters = _suppress(results, programs)
    _write_change_set(results, changes, filters)
    print(f"{len(results)} sources processed, {len(changes)} changes")
    print(f"wrote {artifacts.path(artifacts.CHANGES)}")
    return 0


def enrich_only(args: argparse.Namespace) -> int:
    """`run.py enrich`: fetch the posting behind each change in `.run/changes.json`."""
    change_set = artifacts.read(artifacts.CHANGES, "changes", models.ChangeSet)
    bodies = enrich_bodies.collect(change_set.changes)
    artifacts.write(artifacts.ENRICHED, "enriched", bodies)
    line = enrich_bodies.health_line(bodies)
    print(line or f"{len(bodies)} change(s), none needed a posting fetch")
    print(f"wrote {artifacts.path(artifacts.ENRICHED)}")
    return 0


def screen_only(args: argparse.Namespace) -> int:
    """`run.py screen`: rule out what a quoted phrase settles. No network, no model."""
    change_set = artifacts.read(artifacts.CHANGES, "changes", models.ChangeSet)
    bodies = artifacts.read(
        artifacts.ENRICHED, "enriched", dict[str, enrich_bodies.PostingBody])
    verdicts = _screen(change_set.changes, bodies)
    print(digest.filter_lines([screen.report(change_set.changes, verdicts)])[0]
          if verdicts else
          f"0 of {len(change_set.changes)} change(s) matched a rule-out phrase")
    print(f"wrote {artifacts.path(artifacts.SCREENED)}")
    return 0
