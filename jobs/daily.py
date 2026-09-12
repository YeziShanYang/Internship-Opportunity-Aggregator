"""The morning digest: the job that composes every stage.

Thin for now on purpose. The pipeline still lives in `check.run`, and this exists so
that `run.py` is the entry point from the first commit of the refactor rather than the
last -- which is what lets every later step be verified by diffing `.run/*.json` before
and after, instead of only by the unit suite. Step 5 inverts the dependency: the
composition moves here and `check.py` becomes a deprecation shim.
"""
from __future__ import annotations

import argparse

import check


def run(args: argparse.Namespace) -> int:
    return check.run(args)
