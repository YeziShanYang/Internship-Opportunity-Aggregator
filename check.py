"""Deprecated entry point. Use `run.py all` instead.

The daily pipeline lives in `jobs/daily.py` now, composed from the named stages. This
file survives because `.github/workflows/daily.yml` on the default branch is what
actually runs every morning, and GitHub reads that from `main` rather than from a
working tree -- so deleting this before the workflow change is merged would silently
stop the digest. It prints a notice and delegates.
"""
from __future__ import annotations

import sys

from jobs import daily

# Re-exported: `discover` and the test suite both read the checker registry, and
# `tests/test_acceptance.py` drives `run_sources` and the two state updaters directly.
from jobs.daily import (  # noqa: F401
    CHECKERS,
    GITHUB_CLIENT,
    UNWATCHED,
    WEB_CLIENT,
    run_sources,
    update_program_state,
    update_source_state,
)


def main(argv: list[str] | None = None) -> int:
    print(
        "note: check.py is deprecated; `run.py all` is the entry point. Delegating.",
        file=sys.stderr,
    )
    import run

    return run.main(["all", *(argv if argv is not None else sys.argv[1:])])


if __name__ == "__main__":
    sys.exit(main())
