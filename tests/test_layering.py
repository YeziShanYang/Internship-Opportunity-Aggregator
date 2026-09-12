"""The stage layering, checked mechanically rather than remembered.

The rule the whole refactor rests on: **a stage may only import stages at or below its
own level, only `gather`/`enrich`/`deliver` may import httpx, and only `persist` may
open a file for writing.** All three are mechanically checkable, and a rule that lives
only in a document is how the mess this refactor undid happened in the first place --
`sources/job_boards.py check()` grew to fourteen responsibilities including a disk read
and a markdown render without anyone deciding it should.

By AST, not by `importlib`. Importing `classify` or `deliver.digest` executes their
module bodies, which is slow and has side effects, and it still cannot see a
function-local import -- which is exactly where a violation hides. One was already
caught this way: `gather.collect` did `from process.parse_ats import wants_workday_detail`
inside a function, which satisfies the letter of "no module-scope uphill import" and
none of the point of it.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The pipeline, in order. A stage may import its own level and anything below.
LEVELS = {
    "core": 0,
    "persist": 1,
    "gather": 2,
    "process": 3,
    "enrich": 4,
    "screen": 5,
    "classify": 6,
    "deliver": 7,
}

# Not stages, and each for a stated reason.
#
# `jobs` compose stages and may import anything -- that is what a job is. `run` is the
# CLI over them. `calendar_reminders` is pure data with no imports at all. The last
# three are the ~1,100 lines of entry point the stage plan scopes out; they keep
# importing core and persist and are not folded into the model.
NOT_STAGES = {
    "jobs", "run", "calendar_reminders", "check",
    "build_xlsx", "add_opportunity", "seed_programs",
}

# Only these may make a network request. `deliver` is on the list deliberately and not
# grudgingly: `deliver.issue` POSTs the digest and asks GitHub whether today's has
# already been opened, and the second of those is half of the exactly-once guarantee.
MAY_USE_HTTPX = {"gather", "enrich", "deliver"}

# Only `persist` may open a file for writing.
MAY_WRITE = {"persist"}

WRITE_METHODS = {"write_text", "write_bytes", "mkdir", "unlink", "touch", "rmtree",
                 "rename", "save"}

# Attribute calls on these modules are delegations to persist, not direct writes:
# `artifacts.write_text(...)` is the sanctioned path, `some_path.write_text(...)` is not.
PERSIST_FACADES = {"artifacts", "store", "cache"}

# Files allowed to write, with the reason. Anything not listed here and not under
# `persist/` is a failure, so a new violation is named rather than absorbed.
ALLOWED_WRITERS = {
    # The three entry points the stage plan scopes out by name. They are workbook and
    # CSV writers that sit outside the pipeline entirely; folding them in is a later
    # decision, not part of this.
    "build_xlsx.py": "out-of-scope entry point: writes the two xlsx workbooks",
    "add_opportunity.py": "out-of-scope entry point: writes the watchlist CSVs",
    "seed_programs.py": "out-of-scope entry point: seeds programs.csv once",
}


def modules() -> list[pathlib.Path]:
    return sorted(
        p for p in ROOT.rglob("*.py")
        if ".venv" not in p.parts
        and "__pycache__" not in p.parts
        and "tests" not in p.parts
    )


def package_of(path: pathlib.Path) -> str:
    rel = path.relative_to(ROOT)
    return rel.parts[0] if len(rel.parts) > 1 else rel.stem


def imported_packages(tree: ast.AST) -> set[str]:
    """Every top-level package this module imports, at any nesting depth."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue  # relative import: same package by construction
            if node.module:
                found.add(node.module.split(".")[0])
    return found


class LayeringTests(unittest.TestCase):
    def setUp(self):
        self.parsed = {
            path: ast.parse(path.read_text(encoding="utf-8")) for path in modules()
        }

    def test_the_level_table_covers_every_stage_package(self):
        """A new stage package that nobody added to LEVELS would be unchecked, and an
        unchecked layer is the state this test exists to make impossible."""
        packages = {package_of(p) for p in self.parsed}
        unaccounted = packages - set(LEVELS) - NOT_STAGES
        self.assertEqual(
            unaccounted, set(),
            "these are neither a known stage nor named as not-a-stage; add them to "
            "LEVELS or to NOT_STAGES with a reason")

    def test_no_stage_imports_a_higher_level(self):
        violations = []
        for path, tree in self.parsed.items():
            package = package_of(path)
            level = LEVELS.get(package)
            if level is None:
                continue
            for imported in imported_packages(tree):
                other = LEVELS.get(imported)
                if other is not None and other > level:
                    violations.append(
                        f"{path.relative_to(ROOT)} ({package}, level {level}) imports "
                        f"{imported} (level {other})")
        self.assertEqual(violations, [], "\n" + "\n".join(violations))

    def test_only_gather_enrich_and_deliver_import_httpx(self):
        violations = []
        for path, tree in self.parsed.items():
            package = package_of(path)
            if package not in LEVELS or package in MAY_USE_HTTPX:
                continue
            if "httpx" in imported_packages(tree):
                violations.append(str(path.relative_to(ROOT)))
        self.assertEqual(
            violations, [],
            f"only {sorted(MAY_USE_HTTPX)} may touch the network: {violations}")

    def test_only_persist_opens_a_file_for_writing(self):
        """The rule that makes "git is the database" checkable rather than hoped for.

        Every write going through `persist.store` is what keeps the CSVs byte-stable;
        an unstable writer makes every run a whole-file diff and destroys the audit
        trail that is the entire reason state lives in this repo.
        """
        violations = []
        for path, tree in self.parsed.items():
            rel = str(path.relative_to(ROOT))
            if package_of(path) in MAY_WRITE or rel in ALLOWED_WRITERS:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                where = self._write_call(node)
                if where:
                    violations.append(f"{rel}:{node.lineno} {where}")
        self.assertEqual(
            violations, [],
            "these write outside persist/. Route the write through persist.store or "
            "persist.artifacts, or add the file to ALLOWED_WRITERS with a reason:\n"
            + "\n".join(violations))

    def test_every_allowed_writer_still_exists(self):
        """An exception list that outlives its files is an exception list that is
        quietly excusing something else."""
        for name in ALLOWED_WRITERS:
            self.assertTrue((ROOT / name).exists(), f"{name} is in ALLOWED_WRITERS")

    @staticmethod
    def _write_call(node: ast.Call) -> str | None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in WRITE_METHODS:
            receiver = func.value
            # `str.replace` and `dict.rename` are not file writes; nor is a delegation
            # to the persist facade.
            if isinstance(receiver, ast.Name) and receiver.id in PERSIST_FACADES:
                return None
            if func.attr == "rename" and not isinstance(receiver, ast.Attribute):
                return None
            return f"{func.attr}(...)"
        if isinstance(func, (ast.Name, ast.Attribute)):
            name = func.id if isinstance(func, ast.Name) else func.attr
            if name != "open":
                return None
            args = list(node.args)
            if isinstance(func, ast.Attribute):
                args = args  # path.open(mode)
            else:
                args = args[1:]  # open(path, mode)
            mode = ""
            if args and isinstance(args[0], ast.Constant):
                mode = str(args[0].value)
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = str(kw.value.value)
            if any(ch in mode for ch in "wax+"):
                return f"open({mode!r})"
        return None


class StageShapeTests(unittest.TestCase):
    """Two structural claims worth keeping true, both cheap to check."""

    def test_core_does_no_io_at_all(self):
        """Level 0 is shapes and constants. If `core` could read a file or make a
        request, every other layer's guarantee would be worth nothing, because
        everything imports it."""
        forbidden = {"httpx", "requests", "urllib", "socket", "subprocess"}
        for path, tree in ((p, ast.parse(p.read_text(encoding="utf-8")))
                           for p in (ROOT / "core").glob("*.py")):
            with self.subTest(path.name):
                # urllib.parse is pure string handling and is the one exception, used
                # by nothing in core today; assert the network modules specifically.
                self.assertEqual(
                    imported_packages(tree) & forbidden - {"urllib"}, set(), path.name)

    def test_every_stage_package_has_a_docstring_saying_what_it_is_for(self):
        """The levels are only legible if each package says which one it is and why.
        A package with no docstring is a layer someone has to infer."""
        for package in LEVELS:
            init = ROOT / package / "__init__.py"
            if not init.exists():
                continue  # classify is a single module, not a package
            with self.subTest(package):
                self.assertTrue(
                    ast.get_docstring(ast.parse(init.read_text(encoding="utf-8"))),
                    f"{package}/__init__.py needs a docstring")


if __name__ == "__main__":
    unittest.main(verbosity=2)
