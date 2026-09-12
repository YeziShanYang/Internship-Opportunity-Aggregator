"""The committed snapshot format is frozen, and this is the gate that says so.

`data/snapshots/*.tsv` is the diff basis for every source and, through `git log`, the
audit trail that answers "which page changed on which morning" going back to the first
run. Rewriting those files is a one-way whole-file diff: it destroys the history that is
the entire reason state lives in this repo, and the next run would report every row as
changed because the diff basis moved under it.

So this runs *before* anything in `sources/snapshot.py` is touched, and it exists to
make sure the refactor cannot be the thing that quietly "fixes" a format drift. Measured
when it was written: `render_snapshot(parse_snapshot(t)) == t` for 74 of 74 committed
`.tsv` files, so exactness is the assertion rather than an approximation.

These read the real `data/snapshots/` tree on purpose -- a gate test over a fixture
would gate the fixture. Nothing here writes.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core import paths
from sources import snapshot

SNAPSHOT_DIR = paths.SNAPSHOTS


def _committed(ext: str) -> list[pathlib.Path]:
    return sorted(SNAPSHOT_DIR.glob(f"*.{ext}"))


class SnapshotRoundTripTests(unittest.TestCase):
    """parse then render must be the identity on every committed .tsv."""

    def test_the_snapshot_directory_is_actually_populated(self):
        """Without this, every assertion below passes vacuously on an empty glob --
        which is precisely how a gate test stops gating without going red."""
        tsv, txt = _committed("tsv"), _committed("txt")
        self.assertGreaterEqual(len(tsv), 74, f"expected the 74 committed TSVs, found {len(tsv)}")
        self.assertGreaterEqual(len(txt), 56, f"expected the 56 committed page texts, found {len(txt)}")

    def test_render_of_parse_is_byte_identical_for_every_committed_tsv(self):
        """The gate. A single mismatch means the refactor changed the stored format,
        and a format change is 74 whole-file diffs plus a run that reports every row on
        every board as new."""
        mismatched = []
        for path in _committed("tsv"):
            text = path.read_text(encoding="utf-8")
            if snapshot.render_snapshot(snapshot.parse_snapshot(text)) != text:
                mismatched.append(path.name)
        self.assertEqual(
            mismatched, [],
            "these snapshots no longer survive a parse/render round-trip. Do not "
            "regenerate them -- the stored bytes are the audit trail. Fix the codec.")

    def test_the_round_trip_is_exact_on_the_awkward_shapes_too(self):
        """The three shapes that would break a naive line codec, asserted directly
        rather than left to chance among the 74: a tab inside the value, a homoglyph
        canary, and a row carrying several URLs."""
        snap = snapshot.Snapshot(
            sections=["Trading", "(no department)"],
            rows=[
                snapshot.Row(section="Trading", key="QT @ NYC",
                             value="Type=Intern\tURL=https://x.test/1"),
                snapshot.Row(section="Trading", key="ꓟachine ꓡearning",
                             value="Type=Intern"),
                snapshot.Row(section="(no department)", key="SWE",
                             value="Application=[ATS](https://a.test/1) "
                                   "[Simplify](https://simplify.jobs/p/abc)"),
            ],
        )
        text = snapshot.render_snapshot(snap)
        self.assertEqual(snapshot.render_snapshot(snapshot.parse_snapshot(text)), text)

    def test_an_empty_section_survives_the_round_trip(self):
        """A firm heading with no rows under it means "this firm has no open roles
        right now", which is different from "this firm is not in the list". Losing it
        would make the firm's first posting read as a brand-new section."""
        snap = snapshot.Snapshot(sections=["Quiet Firm"], rows=[])
        text = snapshot.render_snapshot(snap)
        self.assertEqual(snapshot.parse_snapshot(text).sections, ["Quiet Firm"])
        self.assertEqual(snapshot.render_snapshot(snapshot.parse_snapshot(text)), text)


class PositionalUrlTests(unittest.TestCase):
    """`Row.url` is not lossy, it is *positional*, and that is the real defect.

    The first reading of this was that `parse_snapshot` loses information because it
    recovers `url` with a regex. It does not: both producers set
    `url = _URL.findall(value)[0]` and `value` retains every URL, so the recovery is
    exact by construction, which is why the round-trip above is exact.

    What is actually wrong is that "the first URL in the row" is a position, not a
    role. Measured on the committed snapshots: 631 of 1,376 rows hold two or three
    URLs, and on a Simplify row the first is the *company* page, not the posting --
    which is the entire reason `postings.posting_url` exists to re-derive it with a
    second regex. These tests pin that behaviour so that when the URL roles become
    typed, the change is visible here rather than inferred from a digest that looks
    slightly different.
    """

    def test_url_is_the_first_url_in_the_value_not_the_most_useful_one(self):
        row = snapshot.parse_snapshot(
            "# rows\nSWE\tACME / SWE\tApplication=[ATS](https://boards.greenhouse.io/x) "
            "[Simplify](https://simplify.jobs/p/abc-123)\n"
        ).rows[0]
        self.assertEqual(row.url, "https://boards.greenhouse.io/x")

    def test_every_url_in_the_row_is_still_recoverable_from_the_value(self):
        """Which is what makes typed roles a pure improvement rather than a migration:
        the information was never thrown away, only flattened."""
        value = ("Application=[ATS](https://boards.greenhouse.io/x) "
                 "[Simplify](https://simplify.jobs/p/abc-123)")
        row = snapshot.parse_snapshot(f"# rows\nSWE\tACME / SWE\t{value}\n").rows[0]
        self.assertEqual(
            snapshot._URL.findall(row.value),
            ["https://boards.greenhouse.io/x", "https://simplify.jobs/p/abc-123"])

    def test_the_committed_snapshots_really_do_carry_multiple_urls_per_row(self):
        """The measurement the argument above rests on, kept executable so it cannot
        quietly stop being true."""
        multi = 0
        total = 0
        for path in _committed("tsv"):
            for row in snapshot.parse_snapshot(path.read_text(encoding="utf-8")).rows:
                total += 1
                if len(snapshot._URL.findall(row.value)) > 1:
                    multi += 1
        self.assertGreater(total, 1000, total)
        self.assertGreater(multi, 500, f"only {multi} of {total} rows carry several URLs")


if __name__ == "__main__":
    unittest.main(verbosity=2)
