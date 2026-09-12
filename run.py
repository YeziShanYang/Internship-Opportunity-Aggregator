"""Stage runner. One verb per pipeline stage, plus `all` for the daily job.

    run.py all --dry-run          the whole pipeline, printing the digest
    run.py all --only jane-street-fttp --dry-run

The point of the verbs is not that anyone runs them one at a time every morning --
`run.py all` does the whole thing in one process, so the Actions workflow keeps its
single step and nothing about the schedule changes. The point is that when a digest
looks wrong, the question "which stage produced this" has an answer you can read off
disk in `.run/`, instead of requiring the prompts to be reconstructed from the state
commits. That reconstruction has been done once, to work out why 2026-09-12 cost 34
cents, and it should never have needed doing.

Stages are being split out one at a time (see REFACTOR-PLAN.md). A verb whose stage is
still bundled inside `check.check`/`classify.classify` says so, and names the step that
separates it, rather than silently doing nothing -- an empty stage that exits 0 is the
same failure shape as a source that goes quiet instead of failing.
"""
from __future__ import annotations

import argparse
import sys

from core import paths

# Stages in pipeline order. `all` is not a stage; it is the composition of them.
STAGES = ("gather", "process", "enrich", "screen", "classify", "render", "deliver")

# Which migration step makes each verb independently runnable. Printed rather than
# kept in a comment, so someone hitting the wall is told where to look.
SPLIT_BY_STEP = {
    "gather": "Step 5", "process": "Step 5", "enrich": "Step 6",
    "screen": "Step 7", "classify": "Step 7", "render": "Step 8",
    "deliver": "Step 9",
}


def _not_yet_split(verb: str, step: str) -> int:
    print(
        f"`{verb}` is not separately runnable yet: it is still bundled inside the "
        f"per-source check.\n"
        f"{step} of REFACTOR-PLAN.md splits it. Use `run.py all` meanwhile -- it runs "
        f"every stage in order and writes the same artifacts to {paths.RUN_DIR.name}/.",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("verb", choices=(*STAGES, "all"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the digest; open no issue and write no state. Stage artifacts are "
             "still written -- they are derived scratch, not the database, and a dry "
             "run is exactly when you want to read them",
    )
    parser.add_argument("--only", help="restrict to a single source_id")
    parser.add_argument(
        "--force-discovery", action="store_true",
        help="run the weekly source-discovery pass regardless of the day",
    )
    parser.add_argument(
        "--no-classify", action="store_true", help="skip the LLM step entirely"
    )
    parser.add_argument(
        "--force-health", action="store_true",
        help="render the quiet-day digest and ignore the once-a-day delivery lock",
    )
    args = parser.parse_args(argv)

    # Imported here rather than at module scope so `run.py --help` does not pay for
    # httpx, openpyxl and the provider SDKs.
    from jobs import daily

    if args.verb == "all":
        return daily.run(args)
    return _not_yet_split(args.verb, SPLIT_BY_STEP[args.verb])


if __name__ == "__main__":
    sys.exit(main())
