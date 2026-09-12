"""The posting-body cache: the last file write outside this package.

Small enough to have lived in `enrich.postings` as three lines, and worth moving anyway.
It was the only remaining place in the tree that opened a file for writing outside
`persist`, so the layering test could not be turned on with it there -- and a rule
enforced everywhere except one place is a rule with an exception nobody remembers.

The cache is an optimisation and a failure to use it is never an error. A posting that
cannot be cached is still returned; a posting that cannot be read back is refetched.
"""
from __future__ import annotations

import hashlib
import pathlib

from core import paths


def posting_path(url: str) -> pathlib.Path:
    """Resolved through `paths` at call time, so tests redirect it like everything
    else. As an import-bound constant it was one of the two leaks that made hardening
    the test harness step 0 of this refactor."""
    return paths.POSTINGS_CACHE / f"{hashlib.sha1(url.encode()).hexdigest()}.txt"


def read_posting(url: str) -> str | None:
    """The cached body, or None. None means "not cached", never "empty posting"."""
    path = posting_path(url)
    if not path.exists():
        return None
    try:
        return path.read_text()
    except OSError:
        return None  # unreadable is the same as absent: refetch it


def write_posting(url: str, text: str) -> None:
    """Best effort. The cache is an optimisation; failing to write it is not an error."""
    try:
        paths.POSTINGS_CACHE.mkdir(parents=True, exist_ok=True)
        posting_path(url).write_text(text)
    except OSError:
        pass
