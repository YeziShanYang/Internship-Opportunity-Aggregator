"""Deprecated compatibility shim. Import from `core`, `persist` or `gather` instead.

`state.py` used to be four unrelated modules in one file: the paths and column lists
(now `core.paths`), the clock and hashing helpers (`core.clock`), the dataclasses every
stage passes along (`core.models`), all of the file I/O (`persist.store`), the circuit
breaker (`gather.breaker`) and the redirect verdict (`process.redirect`). Only one of
those six did any I/O, and having them share a module is what made "which layer is
allowed to touch the disk" unanswerable -- which is the question the stage refactor
exists to answer.

This forwards by `__getattr__` rather than re-exporting by value, so `state.SNAPSHOTS`
stays in sync with `core.paths.SNAPSHOTS` even after a test redirects it. Re-exporting
would have bound a stale copy and broken test isolation silently.

Nothing in the tree imports this any more. It is kept for one commit so the split is
reviewable on its own, and is deleted in the next one.
"""
from __future__ import annotations

from core import clock as _clock
from core import models as _models
from core import paths as _paths
from gather import breaker as _breaker
from persist import store as _store
from process import redirect as _redirect

_SOURCES = (_paths, _clock, _models, _store, _breaker)

_RENAMED = {"redirect_verdict": _redirect.verdict}


def __getattr__(name: str):
    if name in _RENAMED:
        return _RENAMED[name]
    for module in _SOURCES:
        try:
            return getattr(module, name)
        except AttributeError:
            continue
    raise AttributeError(f"module 'state' has no attribute {name!r}")
