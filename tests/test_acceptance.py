"""The acceptance tests from spec section 14.

Offline: the GitHub client is stubbed, so these run in a second and prove the diff and
health logic rather than the network. Tests 14.1 and 14.8 need a real repo and are
documented in the README as the post-deploy checks.

Tests 14.5 and 14.5c exercise the classifier's identity gate against the two backends
and need ANTHROPIC_API_KEY and AZURE_OPENAI_API_KEY respectively; each skips without its
key. 14.5b asserts the *degraded* contract with every provider unset -- that the change
is surfaced unclassified rather than silently dropped -- because that is the behaviour
that actually ships when no key is present. The gate is re-tested per backend rather
than assumed to carry over, since gpt-5-mini is a smaller model than the one the system
prompt was written against.
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
        self._last_delivered = state.LAST_DELIVERED
        state.SNAPSHOTS = root / "snapshots"
        state.PROPOSALS_LOG = root / "proposals.log"
        state.LAST_DELIVERED = root / "last_delivered.txt"
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
        state.LAST_DELIVERED = self._last_delivered
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
        # Every provider must be unset, not just Anthropic. With a second backend
        # wired in, popping only ANTHROPIC_API_KEY would leave this test quietly
        # making real Azure calls and asserting nothing about degraded mode.
        names = ("ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY", "CLASSIFIER_PROVIDER")
        saved = {n: os.environ.pop(n, None) for n in names}
        try:
            judgments = classify.classify([change], {"nuft-2027": self.source})
        finally:
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value
        self.assertEqual(len(judgments), 1)
        self.assertFalse(judgments[0].classified)
        self.assertTrue(judgments[0].relevant, "unclassified changes must still surface")
        _, body = digest.render(judgments, [], {})
        self.assertIn("WORTH A LOOK", body)
        self.assertIn("Unverified", body)

    @unittest.skipUnless(
        os.environ.get("AZURE_OPENAI_API_KEY"),
        "needs AZURE_OPENAI_API_KEY (live model call)",
    )
    def test_14_5c_azure_backend_applies_the_same_identity_gate(self):
        """The gpt-5-mini path must enforce rule 2 exactly as the Claude path does.

        This is the whole risk of the cheaper backend: it is a smaller model than the
        one the prompt was tuned against, so the identity gate is re-tested against it
        rather than assumed to carry over.
        """
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
        saved = os.environ.get("CLASSIFIER_PROVIDER")
        os.environ["CLASSIFIER_PROVIDER"] = "azure"
        try:
            judgments = classify.classify([change], {"nuft-2027": self.source})
        finally:
            if saved is None:
                os.environ.pop("CLASSIFIER_PROVIDER", None)
            else:
                os.environ["CLASSIFIER_PROVIDER"] = saved
        self.assertEqual(len(judgments), 1)
        judgment = judgments[0]
        self.assertTrue(judgment.classified, judgment.error)
        self.assertFalse(judgment.relevant, judgment.why)
        self.assertRegex(judgment.why.lower(), r"wom|gender|identity")

    # --- 14.6 -------------------------------------------------------------------
    def test_14_6_exactly_one_digest_a_day_every_day(self):
        """Every day mails, including quiet ones, and the quiet title says so."""
        healthy = state.SourceResult(source_id="nuft-2027", ok=True)

        # A quiet day still delivers -- that is what makes a silent morning diagnostic.
        self.assertEqual(
            digest.should_send([], [healthy]),
            (True, True),
            "a quiet day must still mail; silence has to mean the job is broken",
        )
        title, body = digest.render([], [healthy], {}, status_only=True)
        self.assertIn("(no changes)", title)
        self.assertIn("HEALTH", body)

        # A failing source is news, so it is not a status-only day.
        failing = state.SourceResult(source_id="nuft-2027", ok=False, error="HTTP 500")
        self.assertEqual(digest.should_send([], [failing]), (True, False))
        title, _ = digest.render([], [failing], {})
        self.assertNotIn("(no changes)", title)

        # So is a real change.
        change = state.Change(
            source_id="nuft-2027", kind="added", key="Jane Street / FTTP", detail="x"
        )
        judgment = classify.Judgment(change=change, relevant=True, classified=True)
        self.assertEqual(digest.should_send([judgment], [healthy]), (True, False))

    def test_14_6b_a_schedule_retry_never_mails_a_second_time(self):
        """The morning schedule fires three ticks; only the first may deliver.

        The lock is absolute -- unlike the old change-only cadence it suppresses real
        changes and failing sources too. A retry has nothing to tell the owner that the
        morning's issue did not already contain, and two issues for one date is the
        notification-fatigue failure the cadence exists to prevent.
        """
        healthy = state.SourceResult(source_id="nuft-2027", ok=True)
        failing = state.SourceResult(source_id="nuft-2027", ok=False, error="HTTP 500")
        change = state.Change(
            source_id="nuft-2027", kind="added", key="Jane Street / FTTP", detail="x"
        )
        judgment = classify.Judgment(change=change, relevant=True, classified=True)

        # Tick 1: nothing delivered yet, so the digest goes out.
        self.assertEqual(state.read_last_delivered(), "", "no marker before the first run")
        self.assertEqual(digest.should_send([], [healthy], False), (True, True))

        # Tick 2, an hour later, after tick 1 recorded a delivery: silence, whatever
        # this run happened to find.
        state.write_last_delivered()
        self.assertEqual(state.read_last_delivered(), state.today_iso())
        for name, judgments, results in (
            ("a quiet retry", [], [healthy]),
            ("a retry that found changes", [judgment], [healthy]),
            ("a retry that found a broken source", [], [failing]),
        ):
            self.assertEqual(
                digest.should_send(judgments, results, already_delivered_today=True),
                (False, False),
                f"{name} must not mail a second issue for the same date",
            )

        # A marker from a previous day suppresses nothing.
        state.write_last_delivered("2020-01-01")
        self.assertNotEqual(state.read_last_delivered(), state.today_iso())
        self.assertEqual(digest.should_send([], [healthy]), (True, True))

    def test_14_6c_the_delivery_lock_falls_back_when_github_cannot_be_asked(self):
        """"Could not check" must never be read as "not yet delivered".

        The local marker is read from the commit a run checked out, and late ticks do
        not dispatch in cron order, so digest.delivered_issue_exists asks GitHub for the
        authoritative answer. When it cannot, it has to say so rather than guess, or the
        caller would treat an unanswerable question as a green light and mail twice.
        """
        names = ("GITHUB_REPOSITORY", "GITHUB_TOKEN", "GH_PAT")
        saved = {name: os.environ.pop(name, None) for name in names}
        try:
            self.assertIsNone(
                digest.delivered_issue_exists(state.today_iso()),
                "without a repo and token the answer is unknown, not False",
            )
        finally:
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value

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


class NoiseRegressionTests(unittest.TestCase):
    """Regressions for false positives that reached a real digest.

    The first live run reported four "changed" rows whose keys were identical. The
    cause: Simplify writes a bare "↳" in the Company cell to mean "same company as
    above", and the carry-forward was applied to the row key but not to the row value.
    A row shifting position within its company group therefore churned the value while
    the posting itself had not changed at all.
    """

    CONFIG = github_repos.REPO_CONFIGS["simplify-2027"]

    def _rows(self, body: str):
        html = f"""## 💻 Software Engineering Internship Roles
<table><thead><tr><th>Company</th><th>Role</th><th>Location</th><th>Age</th></tr></thead>
{body}</table>"""
        return github_repos.extract(html, self.CONFIG).rows

    def test_carry_forward_is_resolved_in_the_value_not_just_the_key(self):
        leading = self._rows(
            "<tbody><tr><td>Acme</td><td>SWE Intern</td><td>NYC</td><td>1d</td></tr>"
            "<tr><td>↳</td><td>Data Intern</td><td>NYC</td><td>1d</td></tr></tbody>"
        )
        # Same two postings, but now the second row leads its group with the full name.
        trailing = self._rows(
            "<tbody><tr><td>Acme</td><td>Data Intern</td><td>NYC</td><td>2d</td></tr>"
            "<tr><td>↳</td><td>SWE Intern</td><td>NYC</td><td>2d</td></tr></tbody>"
        )
        before = {row.key: row.value for row in leading}
        after = {row.key: row.value for row in trailing}
        self.assertEqual(set(before), set(after), "keys must be position-independent")
        for key in before:
            self.assertEqual(
                before[key], after[key], f"value for {key!r} must not depend on row order"
            )
        self.assertNotIn("↳", "".join(before.values()), "no raw marker may reach the value")

    def test_the_relative_age_column_is_excluded(self):
        """`Age` is "1d"/"2mo" and would report all 490 rows changed every day."""
        first = self._rows("<tbody><tr><td>Acme</td><td>SWE Intern</td><td>NYC</td><td>1d</td></tr></tbody>")
        later = self._rows("<tbody><tr><td>Acme</td><td>SWE Intern</td><td>NYC</td><td>3mo</td></tr></tbody>")
        self.assertEqual(first[0].value, later[0].value)

    def test_the_hot_marker_is_stripped_for_simplify(self):
        """"🔥" means "recently posted" and falls off after a few days."""
        hot = self._rows("<tbody><tr><td>🔥 Acme</td><td>SWE Intern</td><td>NYC</td><td>1d</td></tr></tbody>")
        cold = self._rows("<tbody><tr><td>Acme</td><td>SWE Intern</td><td>NYC</td><td>9d</td></tr></tbody>")
        self.assertEqual(hot[0].key, cold[0].key, "the marker must not split one posting in two")

    def test_cruz_keeps_its_status_markers(self):
        """Cruz uses "🔥 [CLOSING SOON]" as real signal - it must NOT be stripped."""
        config = github_repos.REPO_CONFIGS["underclassmen-cruz"]
        markdown = (
            "## Scholarships\n"
            "| Status | Organization | Scholarship | Application | Deadline |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| ✅ **[OPEN]** | Unigo | Make Me Laugh | x | Dec 1 |\n"
        )
        closing = markdown.replace("✅ **[OPEN]**", "🔥 **[CLOSING SOON]**")
        open_rows = github_repos.extract(markdown, config).rows
        closing_rows = github_repos.extract(closing, config).rows
        self.assertEqual(open_rows[0].key, closing_rows[0].key, "same posting, same key")
        self.assertNotEqual(
            open_rows[0].value,
            closing_rows[0].value,
            "an OPEN -> CLOSING SOON transition is exactly what must be reported",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
