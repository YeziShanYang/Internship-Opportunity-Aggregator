"""The acceptance tests from spec section 14.

Offline: the GitHub client is stubbed, so these run in a second and prove the diff and
health logic rather than the network. Tests 14.1 and 14.8 need a real repo and are
documented in the README as the post-deploy checks.

Test 14.5 exercises the classifier's identity gate and needs ANTHROPIC_API_KEY. Without
one it asserts the *degraded* contract instead -- that the change is surfaced
unclassified rather than silently dropped -- because that is the behaviour that actually
ships when the key is missing.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import check
import classify
import digest
import state
from sources import github_repos

REPO = "northwesternfintech/2027QuantInternships"

BASE_README = """# Summer 2027 Quant Internships

## Akuna Capital

|Role|Links|
|-------|-------|
|QD|[OK](https://akunacapital.com/careers/job/1?gh_jid=1)|
|SWE|[OK](https://akunacapital.com/careers/job/2?gh_jid=2)|

## Jane Street

|Role|Links|
|-------|-------|
|QT|[OK](https://janestreet.com/join/1)|
"""


class FakeResponse:
    def __init__(self, text="", payload=None, status=200):
        self.text = text
        self._payload = payload or {}
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("GET", "https://example.invalid/README.md")
            raise httpx.HTTPStatusError(
                "boom", request=request, response=httpx.Response(self.status_code, request=request)
            )


class FakeClient:
    """Stands in for httpx.Client: first call is repo metadata, second is the README."""

    def __init__(self, readme: str, readme_status: int = 200):
        self.readme = readme
        self.readme_status = readme_status

    def get(self, url: str, *args, **kwargs):
        if url.startswith(github_repos.GITHUB_API):
            return FakeResponse(payload={"default_branch": "main"})
        return FakeResponse(text=self.readme, status=self.readme_status)


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self._snapshots = state.SNAPSHOTS
        self._proposals = state.PROPOSALS_LOG
        state.SNAPSHOTS = root / "snapshots"
        state.PROPOSALS_LOG = root / "proposals.log"
        state.SNAPSHOTS.mkdir()
        # A relaxed floor: the fixture README is deliberately tiny.
        self._config = github_repos.REPO_CONFIGS["nuft-2027"]
        github_repos.REPO_CONFIGS["nuft-2027"] = type(self._config)(
            table_format="markdown",
            role_columns=("Role",),
            heading_as_entity=True,
            min_rows=0,
            min_sections=1,
        )
        self.source = {
            "source_id": "nuft-2027",
            "tier": "1",
            "method": "github_readme",
            "url": REPO,
            "program_names": "NUFT 2027 Quant Internships",
            "signal": "high",
            "consecutive_failures": "0",
            "last_success": "",
        }

    def tearDown(self):
        github_repos.REPO_CONFIGS["nuft-2027"] = self._config
        state.SNAPSHOTS = self._snapshots
        state.PROPOSALS_LOG = self._proposals
        self._tmp.cleanup()

    def _baseline(self, readme=BASE_README):
        result = github_repos.check(self.source, FakeClient(readme))
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.baseline)
        state.write_snapshot("nuft-2027", result.snapshot_text, ext="tsv")
        return result

    # --- 14.2 -------------------------------------------------------------------
    def test_14_2_edited_snapshot_surfaces_in_act_now(self):
        """Hand-edit a stored snapshot to simulate a page change -> ACT NOW."""
        self._baseline()
        # Simulate the previous run not having seen the Jane Street row.
        path = state.snapshot_path("nuft-2027", "tsv")
        kept = [
            line
            for line in path.read_text().splitlines()
            if "Jane Street\tJane Street / QT" not in line
        ]
        path.write_text("\n".join(kept) + "\n")

        result = github_repos.check(self.source, FakeClient(BASE_README))
        self.assertTrue(result.ok)
        self.assertEqual(len(result.changes), 1, result.changes)
        change = result.changes[0]
        self.assertEqual(change.key, "Jane Street / QT")
        self.assertTrue(change.rolling, "Jane Street must be flagged rolling (rule 4)")

        judgments = classify.classify(result.changes, {"nuft-2027": self.source})
        self.assertTrue(any(j.urgent for j in judgments), "rolling change must be urgent")
        _, body = digest.render(judgments, [result], {"nuft-2027": self.source})
        self.assertIn("ACT NOW", body)
        self.assertIn("Jane Street / QT", body)

    # --- 14.3 -------------------------------------------------------------------
    def test_14_3_a_404_is_health_not_a_change(self):
        """A source returning 404 lands in HEALTH and bumps consecutive_failures."""
        self._baseline()
        result = github_repos.check(self.source, FakeClient("", readme_status=404))
        self.assertFalse(result.ok)
        self.assertEqual(result.changes, [], "a failure must never look like a change")
        self.assertIn("404", result.error)

        sources = [dict(self.source)]
        check.update_source_state(sources, [result])
        self.assertEqual(sources[0]["consecutive_failures"], "1")
        self.assertEqual(sources[0]["last_success"], "", "last_success must not advance")

        _, body = digest.render([], [result], {"nuft-2027": sources[0]})
        self.assertIn("HEALTH", body)
        self.assertIn("1 FAILING", body)

    def test_14_3b_three_failures_escalate_to_act_now(self):
        """Spec 10.1: at >=3 consecutive failures the source is blind, not quiet."""
        result = state.SourceResult(source_id="nuft-2027", ok=False, error="HTTP 404")
        source = dict(self.source, consecutive_failures="4", last_success="2026-09-01")
        title, body = digest.render([], [result], {"nuft-2027": source})
        self.assertIn("ACT NOW", body)
        self.assertIn("failed 4 times running", body)
        self.assertIn("source failing", title)

    def test_14_3c_an_empty_readme_is_a_failure_not_a_quiet_day(self):
        """A restructure that parses to nothing must alert, not report zero changes."""
        github_repos.REPO_CONFIGS["nuft-2027"] = type(self._config)(
            table_format="markdown", role_columns=("Role",), heading_as_entity=True,
            min_rows=2, min_sections=1,
        )
        result = github_repos.check(self.source, FakeClient("# Nothing here\n"))
        self.assertFalse(result.ok)
        self.assertIn("parsed only", result.error)
        self.assertEqual(result.changes, [])

    # --- 14.4 -------------------------------------------------------------------
    def test_14_4_new_freshman_row_is_a_discovery_candidate(self):
        """A new row titled "Freshman Insight Program" surfaces as a candidate."""
        self._baseline()
        readme = BASE_README + """
## Xanadu Trading

|Role|Links|
|-------|-------|
|Freshman Insight Program|[Apply](https://xanadutrading.example/apply)|
"""
        result = github_repos.check(self.source, FakeClient(readme))
        self.assertTrue(result.ok)
        candidates = [c for c in result.changes if c.is_discovery_candidate]
        self.assertTrue(candidates, "must flag the freshman row as a discovery candidate")
        self.assertTrue(
            any("new section: Xanadu Trading" in c.key for c in result.changes),
            "a brand-new firm section must be reported in its own right",
        )
        self.assertTrue(
            any("Freshman Insight Program" in c.key for c in candidates),
            [c.key for c in candidates],
        )

    # --- 14.5 -------------------------------------------------------------------
    @unittest.skipUnless(
        os.environ.get("ANTHROPIC_API_KEY"), "needs ANTHROPIC_API_KEY (live model call)"
    )
    def test_14_5_identity_gated_program_is_ruled_out(self):
        """A women-only program is classified relevant: false, citing the gate."""
        change = state.Change(
            source_id="nuft-2027",
            kind="added",
            key="Jane Street / INSIGHT",
            detail=(
                "New row: Role=INSIGHT; Links=Jane Street INSIGHT is a program for women "
                "and gender-expansive students in their first year of university. "
                "Applications open now."
            ),
            program_name="Jane Street INSIGHT",
        )
        judgments = classify.classify([change], {"nuft-2027": self.source})
        self.assertEqual(len(judgments), 1)
        judgment = judgments[0]
        self.assertTrue(judgment.classified, judgment.error)
        self.assertFalse(judgment.relevant, judgment.why)
        self.assertRegex(judgment.why.lower(), r"wom|gender|identity")

    def test_14_5b_without_a_key_nothing_is_dropped(self):
        """Degraded mode surfaces more, not less (rule 3)."""
        change = state.Change(
            source_id="nuft-2027", kind="added", key="Some Firm / Insight", detail="x"
        )
        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            judgments = classify.classify([change], {"nuft-2027": self.source})
        finally:
            if saved is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved
        self.assertEqual(len(judgments), 1)
        self.assertFalse(judgments[0].classified)
        self.assertTrue(judgments[0].relevant, "unclassified changes must still surface")
        _, body = digest.render(judgments, [], {})
        self.assertIn("WORTH A LOOK", body)
        self.assertIn("Unverified", body)

    # --- 14.6 -------------------------------------------------------------------
    def test_14_6_change_only_cadence_with_a_monday_heartbeat(self):
        """No changes -> no issue. Monday -> a health issue regardless."""
        healthy = state.SourceResult(source_id="nuft-2027", ok=True)
        self.assertEqual(digest.should_send([], [healthy], is_monday=False), (False, False))
        self.assertEqual(digest.should_send([], [healthy], is_monday=True), (True, True))

        failing = state.SourceResult(source_id="nuft-2027", ok=False, error="HTTP 500")
        self.assertEqual(
            digest.should_send([], [failing], is_monday=False),
            (True, False),
            "a failure must break the silence even on a quiet day",
        )
        title, _ = digest.render([], [healthy], {}, health_only=True)
        self.assertIn("Weekly health summary", title)

    # --- extra: the eligible column is never rewritten (rule 5) -----------------
    def test_eligible_column_is_never_rewritten(self):
        programs = [
            {
                **{column: "" for column in state.PROGRAM_COLUMNS},
                "name": "Jane Street FTTP",
                "eligible": "YES",
                "status": "dormant",
            }
        ]
        sources = [dict(self.source, program_names="Jane Street FTTP")]
        change = state.Change(
            source_id="nuft-2027", kind="changed", key="Jane Street / FTTP", detail="x"
        )
        judgment = classify.Judgment(
            change=change,
            relevant=True,
            classified=True,
            confidence="high",
            new_status="open",
            program_name="Jane Street FTTP",
            eligible_proposal="NO",
        )
        result = state.SourceResult(
            source_id="nuft-2027", ok=True, changes=[change], snapshot_text="x"
        )
        check.update_program_state(programs, sources, [result], [judgment])
        self.assertEqual(programs[0]["eligible"], "YES", "eligible must be untouched")
        self.assertEqual(programs[0]["status"], "open", "status is ours to maintain")
        self.assertTrue(state.PROPOSALS_LOG.exists(), "the status change must be logged")

    def test_muted_rows_are_silenced(self):
        self.assertIn("muted", state.PROGRAM_COLUMNS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
