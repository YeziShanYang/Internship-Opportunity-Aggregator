"""The stage boundary: the runner's verbs, and "every filter reports what it removed".

The second half is the one that earns its place. That rule is older than the
`FilterReport` shape and has been broken twice, both times identically -- a filter was
added, its HEALTH line was not, and the digest stayed plausible while a source went
blind. With one shape for all of them, the rule stops being a habit and becomes an
assertion: a filter that removed rows and emitted no report is a test failure.
"""
from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import digest
import run
from core import models, paths
from persist import artifacts
from process import suppress


def _change(key: str, program_name: str = "") -> models.Change:
    return models.Change(source_id="s", kind="added", key=key, detail="d",
                         program_name=program_name)


class FilterReportTests(unittest.TestCase):
    """Every filter emits a report, including when it removed nothing."""

    def test_a_filter_that_removed_nothing_still_reports(self):
        """The useful case. Reporting zero is what distinguishes "quiet today" from
        "not running", and the difference between those two is the entire subject of
        spec 10.1."""
        kept, reports = suppress.suppress([_change("a")], muted=set(), applied={})
        self.assertEqual(len(kept), 1)
        self.assertEqual({r.filter_id for r in reports}, {suppress.MUTED, suppress.APPLIED})
        self.assertTrue(all(r.removed == 0 for r in reports))

    def test_every_report_accounts_for_exactly_what_it_dropped(self):
        changes = [_change("a", "Muted One"), _change("b", "Live One"), _change("c")]
        kept, reports = suppress.suppress(
            changes, muted={"Muted One"}, applied={})
        self.assertEqual([c.key for c in kept], ["b", "c"])
        self.assertEqual(suppress.removed_by(reports, suppress.MUTED), 1)
        self.assertEqual(sum(r.removed for r in reports), len(changes) - len(kept))

    def test_a_report_carries_samples_so_an_over_matching_filter_can_be_audited(self):
        changes = [_change(f"row-{n}", "Muted One") for n in range(9)]
        _, reports = suppress.suppress(changes, muted={"Muted One"}, applied={})
        muted = next(r for r in reports if r.filter_id == suppress.MUTED)
        self.assertEqual(muted.removed, 9)
        self.assertEqual(len(muted.samples), suppress.MAX_SAMPLES)
        self.assertEqual(muted.samples[0], "row-0")

    def test_a_reason_is_present_on_every_report(self):
        """HEALTH prints it verbatim. A report with no sentence is a count with no
        explanation, which is what the old `before - after` subtractions were."""
        _, reports = suppress.suppress([_change("a")], muted=set(), applied={})
        for report in reports:
            self.assertTrue(report.reason.strip(), report)
            self.assertEqual(report.stage, "process")

    def test_the_applied_filter_keys_on_the_cycle_hash_not_the_row_text(self):
        from core import clock
        change = _change("SWE Intern")
        key = clock.change_key(change.source_id, change.key)
        kept, reports = suppress.suppress([change], muted=set(), applied={key: "2026-01-01"})
        self.assertEqual(kept, [])
        self.assertEqual(suppress.removed_by(reports, suppress.APPLIED), 1)

    def test_a_source_with_no_programme_is_never_muted(self):
        """An empty program_names is a whole job board, not a muted programme. Muting
        one of the 187 programmes must not silence a board that names none of them."""
        kept, _ = suppress.suppress([_change("a")], muted={"Anything"}, applied={})
        self.assertEqual(len(kept), 1)


class FilterLineTests(unittest.TestCase):
    """How filter reports read in HEALTH."""

    def _reports(self, n: int) -> list[models.FilterReport]:
        return [
            models.FilterReport(
                stage="process", filter_id="ats-not-a-student-role",
                source_id=f"board-{i}", considered=40, removed=30,
                reason="title gives no sign of a student role")
            for i in range(n)
        ]

    def test_a_filter_firing_on_many_sources_reports_a_count_not_a_list(self):
        """Measured on the live watchlist: the student-role screen legitimately fires
        on 62 boards, and naming all 62 is 700 characters nobody reads to the end of.
        A truncated list is worse than a count -- an arbitrary first four reads as if
        the filter only touched those."""
        line = digest.filter_lines(self._reports(62))[0]
        self.assertIn("across 62 sources", line)
        self.assertNotIn("board-0", line)
        self.assertIn("1860 of 2480 rows removed", line)

    def test_a_filter_firing_on_a_few_sources_names_them(self):
        line = digest.filter_lines(self._reports(2))[0]
        self.assertIn("board-0, board-1", line)

    def test_a_run_wide_filter_needs_no_source_list(self):
        line = digest.filter_lines([models.FilterReport(
            stage="process", filter_id="muted-programme", considered=9, removed=3,
            reason="muted in programs.csv")])[0]
        self.assertIn("3 of 9 rows removed —", line)


class ChangeSetArtifactTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(setattr, paths, "RUN_DIR", paths.RUN_DIR)
        paths.RUN_DIR = pathlib.Path(tmp.name) / ".run"

    def test_the_snapshot_text_never_reaches_the_artifact(self):
        """22KB of TSV per source would make changes.json unreadable, and the snapshot
        already has a home in data/snapshots/ where git tracks it. An artifact that
        cannot be read in one look is not doing its job."""
        result = models.SourceResult(
            source_id="s", ok=True, snapshot_text="# rows\n" + "x" * 20_000)
        metrics = models.SourceMetrics.of(result)
        self.assertNotIn("snapshot_text", [f for f in vars(metrics)])
        artifacts.write(artifacts.CHANGES, "changes",
                        models.ChangeSet(metrics=[metrics]))
        self.assertNotIn("xxxx", artifacts.read_text(artifacts.CHANGES))

    def test_metrics_keep_failure_and_quiet_distinguishable(self):
        """SourceMetrics is a projection, and the one thing it may never flatten is
        the difference between a source that failed and one that had nothing to say."""
        failed = models.SourceMetrics.of(
            models.SourceResult(source_id="a", ok=False, error="HTTP 500"))
        quiet = models.SourceMetrics.of(models.SourceResult(source_id="b", ok=True))
        self.assertNotEqual((failed.ok, failed.error), (quiet.ok, quiet.error))
        self.assertEqual(quiet.change_count, 0)
        self.assertTrue(quiet.ok)

    def test_the_change_set_round_trips_through_the_artifact(self):
        original = models.ChangeSet(
            changes=[_change("a")],
            metrics=[models.SourceMetrics(source_id="s", ok=True, change_count=1)],
            filters=[models.FilterReport("process", "muted-programme", 3, 2,
                                         samples=("x", "y"))],
        )
        artifacts.write(artifacts.CHANGES, "changes", original)
        self.assertEqual(
            artifacts.read(artifacts.CHANGES, "changes", models.ChangeSet), original)


class RunnerTests(unittest.TestCase):
    """The verbs exist, and a verb that cannot run yet says so loudly."""

    def _run(self, argv: list[str]) -> tuple[int, str]:
        """Run the CLI with stderr captured. The message is the behaviour under test,
        and a suite that prints eight copies of it is a suite people stop reading."""
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            code = run.main(argv)
        return code, captured.getvalue()

    def test_every_stage_has_a_verb(self):
        self.assertEqual(
            run.STAGES,
            ("gather", "process", "enrich", "screen", "classify", "render", "deliver"))

    def test_an_unsplit_verb_exits_nonzero_rather_than_doing_nothing(self):
        """A stage that exits 0 having done nothing is the same failure shape as a
        source that goes quiet instead of failing."""
        for verb in run.STAGES:
            with self.subTest(verb):
                code, message = self._run([verb])
                self.assertEqual(code, 2)
                self.assertIn(verb, message)
                self.assertIn("run.py all", message, "say what to do instead")

    def test_every_unsplit_verb_names_the_step_that_splits_it(self):
        self.assertEqual(set(run.SPLIT_BY_STEP), set(run.STAGES))
        for step in run.SPLIT_BY_STEP.values():
            self.assertRegex(step, r"^Step \d$")

    def test_an_unknown_verb_is_rejected_by_the_parser(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            run.main(["gathr"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
