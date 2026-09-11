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
import discover
import digest
import state
from sources import github_repos, job_boards, page_watch, postings, snapshot

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


# --- 14.7 -----------------------------------------------------------------------
class PostingFilterTests(unittest.TestCase):
    """The change that made WORTH A LOOK readable: fetch the posting, then judge it.

    A Simplify row on its own is a title and a location -- measured, 7 of 592 rows
    mention any class-year word, and all 7 are incidental. These lock down that the
    posting text reaches the model, that a failed fetch can never masquerade as a
    relevance decision, and that ruled-out items stay visible.
    """

    SOURCE = {"source_id": "simplify-2027", "signal": "low", "program_names": "Board"}

    def _change(self, key="ACME / Software Engineer Intern", url=None):
        detail = f"Company=[ACME](https://simplify.jobs/c/ACME); Role={key}"
        if url:
            detail += f"; Application=[ ](https://ats.example.com/x) [ ]({url})"
        return state.Change(
            source_id="simplify-2027", kind="added", key=key, detail=detail
        )

    def test_14_7_low_signal_rows_now_reach_the_classifier(self):
        """The old bypass is gone: a low-signal row is classified, not skipped.

        Skipping them is what filled WORTH A LOOK with 20-35 unreadable items a day,
        because an unclassified change defaults to relevant=True.
        """
        change = self._change()
        to_classify, summarise_only = classify.triage(
            [change], {"simplify-2027": self.SOURCE}
        )
        self.assertEqual(to_classify, [change], "low-signal rows must be classified now")
        self.assertEqual(summarise_only, [])

    def test_14_7b_posting_url_prefers_the_posting_over_the_company_page(self):
        """/c/ is a company listing; only /p/ carries this role's requirements."""
        url = "https://simplify.jobs/p/06a6fa65-37c0-461f-be1e-748f50cdf55c"
        self.assertEqual(postings.posting_url(self._change(url=url)), url)
        self.assertIsNone(postings.posting_url(self._change()))

    def test_14_7c_posting_text_reaches_the_model(self):
        rendered = classify._render_change(
            self._change(), ("Requirements: rising junior standing required.", "")
        )
        self.assertIn("POSTING TEXT", rendered)
        self.assertIn("rising junior", rendered)

    def test_14_7d_a_failed_fetch_can_never_look_like_a_judgment(self):
        """A network failure must not read as 'the posting states no requirements'.

        If it did, the model would rule items out on the strength of a timeout.
        """
        rendered = classify._render_change(self._change(), ("", "ConnectError: boom"))
        self.assertIn("UNAVAILABLE", rendered)
        self.assertIn("ConnectError: boom", rendered)
        self.assertRegex(rendered, r"[Dd]o not rule this out")

    def test_14_7e_a_javascript_shell_is_a_failure_not_an_empty_posting(self):
        """Workday returns HTTP 200 with no text; that is an error, not a blank page."""
        self.assertLess(len(postings.extract_text("<html><body></body></html>")),
                        postings.MIN_USEFUL_CHARS)

    def test_14_7f_ruled_out_items_stay_visible_with_their_reasons(self):
        """Filtering is only safe if a wrong call is auditable."""
        judgment = classify.Judgment(
            change=self._change(),
            relevant=False,
            classified=True,
            confidence="high",
            why="The posting requires third or fourth year standing.",
        )
        _, body = digest.render([judgment], [], {})
        self.assertIn("RULED OUT", body)
        self.assertIn("third or fourth year", body)
        self.assertIn("filtered out by the classifier", body)

    def test_14_7g2_eligible_is_not_the_same_as_urgent(self):
        """ACT NOW is only useful while it stays short.

        Classifying every source made every eligible job-board row "confidently
        relevant", which promoted 22 generic internships into ACT NOW and emptied
        WORTH A LOOK. Urgency now means rolling review or a first-year-targeted row.
        """
        def judge(**kw):
            change = state.Change(source_id="simplify-2027", kind="added",
                                  key="ACME / Software Engineer Intern", detail="x", **kw)
            return classify.Judgment(change=change, relevant=True, classified=True,
                                     confidence="high")

        self.assertFalse(judge().urgent, "merely eligible is WORTH A LOOK, not ACT NOW")
        self.assertTrue(judge(rolling=True).urgent, "rolling firms close when full")
        self.assertTrue(judge(is_discovery_candidate=True).urgent,
                        "first-year-targeted rows are the point of the tool")

    def test_14_7g_a_stale_owner_profile_nags_in_the_calendar(self):
        """A stale profile mis-sorts everything while still looking well-formed."""
        saved = classify.PROFILE_LAST_REVIEWED
        try:
            classify.PROFILE_LAST_REVIEWED = "2019-01-01"
            _, body = digest.render([], [], {})
            self.assertIn("interest profile was last reviewed", body)
            classify.PROFILE_LAST_REVIEWED = state.today_iso()
            _, fresh = digest.render([], [], {})
            self.assertNotIn("interest profile was last reviewed", fresh)
        finally:
            classify.PROFILE_LAST_REVIEWED = saved

    def test_14_7h_noisy_boards_never_write_to_programs_csv(self):
        """One aggregate programs.csv row must not be driven by 35 per-row judgments."""
        programs = [{"name": "Board", "status": "OPEN", "last_checked": "",
                     "last_changed": "", "snapshot_hash": "", "source_id": "simplify-2027"}]
        judgment = classify.Judgment(
            change=self._change(), relevant=True, classified=True,
            confidence="high", program_name="Board", new_status="CLOSED",
        )
        check.update_program_state(programs, [self.SOURCE], [], [judgment])
        self.assertEqual(programs[0]["status"], "OPEN",
                         "a low-signal board row must not flip the program's status")


# --- 14.8 -----------------------------------------------------------------------
@unittest.skipUnless(
    os.environ.get("AZURE_OPENAI_API_KEY"),
    "needs AZURE_OPENAI_API_KEY (live model calls)",
)
class PostingJudgementTests(unittest.TestCase):
    """Does the small model actually make the right call on real posting text?

    gpt-5-mini is weaker than Sonnet, and rules 6 and 7 ask it to *rule things out* --
    the expensive direction to get wrong. The posting text is seeded into the fetch
    cache so these are deterministic about the input while still making a real model
    call about the judgment.
    """

    SOURCE = {"source_id": "simplify-2027", "signal": "low", "program_names": "Board"}
    URL = "https://simplify.jobs/p/00000000-0000-0000-0000-0000000000%02d"

    def setUp(self):
        self._saved = os.environ.get("CLASSIFIER_PROVIDER")
        os.environ["CLASSIFIER_PROVIDER"] = "azure"
        postings.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._written = []

    def tearDown(self):
        for path in self._written:
            path.unlink(missing_ok=True)
        if self._saved is None:
            os.environ.pop("CLASSIFIER_PROVIDER", None)
        else:
            os.environ["CLASSIFIER_PROVIDER"] = self._saved

    def _judge(self, n, role, posting_text):
        url = self.URL % n
        path = postings._cache_path(url)
        path.write_text(posting_text)
        self._written.append(path)
        change = state.Change(
            source_id="simplify-2027", kind="added",
            key=f"ACME / {role}",
            detail=f"Company=ACME; Role={role}; Application=[ ]({url})",
        )
        judgments = classify.classify([change], {"simplify-2027": self.SOURCE})
        self.assertEqual(len(judgments), 1)
        return judgments[0]

    def test_14_8_a_class_year_gate_rules_the_posting_out(self):
        """Rule 6, the filter that does the most work: ~a third of postings gate."""
        j = self._judge(1, "Software Engineer Intern", (
            "Requirements: Currently be in the third or fourth year of a Bachelor's "
            "degree in Computer Science. Expected graduation between December 2027 "
            "and August 2028. Strong programming skills required."))
        self.assertTrue(j.classified, j.error)
        self.assertFalse(j.relevant, j.why)
        self.assertRegex(j.why.lower(), r"third|fourth|year|graduat|2027|2028")

    def test_14_8_b_no_gate_means_relevant_not_ruled_out(self):
        """Absence of a gate must never be read as grounds to rule out (rule 6)."""
        j = self._judge(2, "Software Engineer Intern", (
            "Requirements: Currently enrolled in a Bachelor's or Master's degree "
            "program in Engineering, Computer Science, or a related field in the "
            "United States. Reliable transportation to the worksite."))
        self.assertTrue(j.classified, j.error)
        self.assertTrue(j.relevant, f"a first-year qualifies here: {j.why}")

    def test_14_8_c_a_plainly_unrelated_role_is_ruled_out(self):
        """Rule 7 -- but only for things that are plainly not the owner's field."""
        j = self._judge(3, "IT Document Automation Developer Intern", (
            "Requirements: Currently enrolled in a Bachelor's program. You will "
            "maintain document templates, scanning workflows and records retention "
            "schedules for the corporate records team."))
        self.assertTrue(j.classified, j.error)
        self.assertFalse(j.relevant, j.why)

    def test_14_8_d_a_quant_role_survives_both_new_rules(self):
        """The false negative that would actually cost money."""
        j = self._judge(4, "Quantitative Trading Intern", (
            "Requirements: Currently enrolled in a Bachelor's degree program in "
            "Mathematics, Statistics, Computer Science or a related quantitative "
            "field. All undergraduate years are encouraged to apply."))
        self.assertTrue(j.classified, j.error)
        self.assertTrue(j.relevant, f"must not rule out a quant role: {j.why}")


class _IsolatedState:
    """Redirect every state path at a temp dir.

    Without this the new suites write snapshots, applied.tsv and discovered.csv into
    the real data/ directory -- which they did, until this was added. Test runs must
    not touch the database the job commits.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self._saved = {
            name: getattr(state, name)
            for name in ("SNAPSHOTS", "PROPOSALS_LOG", "LAST_DELIVERED", "APPLIED_TSV",
                         "DISCOVERED_CSV", "LAST_DISCOVERY", "DATA")
        }
        state.DATA = root
        state.SNAPSHOTS = root / "snapshots"
        state.PROPOSALS_LOG = root / "proposals.log"
        state.LAST_DELIVERED = root / "last_delivered.txt"
        state.APPLIED_TSV = root / "applied.tsv"
        state.DISCOVERED_CSV = root / "discovered.csv"
        state.LAST_DISCOVERY = root / "last_discovery.txt"
        state.SNAPSHOTS.mkdir()

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(state, name, value)
        self._tmp.cleanup()


# --- 14.9: Phase 2 ---------------------------------------------------------------
class FakeJSONClient:
    """Returns a fixed JSON payload for any URL. Stands in for an ATS."""

    def __init__(self, payload, status=200, text=""):
        self.payload, self.status, self.text = payload, status, text

    def get(self, url, *args, **kwargs):
        return FakeResponse(text=self.text or "", payload=self.payload, status=self.status)


class FakeHTMLClient:
    def __init__(self, html, status=200):
        self.html, self.status = html, status

    def get(self, url, *args, **kwargs):
        return FakeResponse(text=self.html, status=self.status)


def _gh(title, location="New York, United States", employment_type=None, content="<p>x</p>"):
    job = {"title": title, "location": {"name": location},
           "departments": [{"name": "Engineering"}],
           "absolute_url": f"https://example.invalid/{title}", "content": content}
    if employment_type:
        job["metadata"] = [{"name": "Employment Type", "value": employment_type}]
    return job


class Phase2ConfigTests(unittest.TestCase):
    """The registry, and the silent-skip bug it replaced."""

    def test_14_9_every_method_in_sources_csv_has_a_handler(self):
        """A typo'd method must be caught here rather than going unwatched in prod."""
        methods = {(r.get("method") or "").strip() for r in state.read_sources()}
        unknown = methods - set(check.CHECKERS) - {check.UNWATCHED}
        self.assertEqual(unknown, set(), f"sources.csv has unhandled methods: {unknown}")

    def test_14_9b_an_unknown_method_is_a_failure_not_a_silent_skip(self):
        """The bug this replaced: `continue` produced no SourceResult at all, so a
        typo'd row looked exactly like a quiet source (spec 10.1)."""
        results = check.run_sources(
            [{"source_id": "typo", "method": "githb_readme", "url": "x"}], only=None
        )
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertIn("no module handles", results[0].error)

    def test_14_9c_the_github_token_never_reaches_a_job_board(self):
        """Sharing one client would send GH_PAT to every ATS and careers page."""
        with postings.build_client() as web:
            headers = {k.lower() for k in web.headers}
        self.assertNotIn("authorization", headers)


class Phase2JobBoardTests(_IsolatedState, unittest.TestCase):
    def test_14_9d_jane_street_survives_despite_no_intern_in_any_title(self):
        """The trap that nearly shipped. Jane Street's 48 student roles are titled
        "Machine Learning Researcher" and the like; the student-ness is in metadata."""
        payload = {"jobs": [
            _gh("Machine Learning Researcher", employment_type="Summer Internship"),
            _gh("Quantitative Trader", employment_type="Summer Internship"),
            _gh("Software Engineer", employment_type="Full-Time: Experienced"),
        ]}
        r = job_boards.check(
            {"source_id": "js", "method": "greenhouse", "url": "janestreet", "program_names": ""},
            FakeJSONClient(payload),
        )
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.extra["rows"], 2, "both student roles must survive")
        self.assertNotIn("intern", r.snapshot_text.lower().replace("internship", ""))

    def test_14_9e_non_us_postings_are_dropped_and_counted(self):
        payload = {"jobs": [
            _gh("Software Engineer Intern", location="New York, United States"),
            _gh("Software Engineer Intern", location="Hong Kong"),
            _gh("Software Engineer Intern", location="London, United Kingdom"),
        ]}
        r = job_boards.check(
            {"source_id": "b", "method": "greenhouse", "url": "s", "program_names": ""},
            FakeJSONClient(payload),
        )
        self.assertEqual(r.extra["rows"], 1)
        self.assertEqual(r.extra["suppressed_not_us"], 2, "suppression must be counted")

    def test_14_9f_a_reworded_description_does_not_churn_the_snapshot(self):
        """Greenhouse bumps updated_at on trivial edits. If the description or its
        hash were in the snapshot, all 230 Jane Street rows would change daily."""
        a = {"jobs": [_gh("SWE Intern", content="<p>original blurb</p>")]}
        b = {"jobs": [_gh("SWE Intern", content="<p>completely rewritten blurb</p>")]}
        src = {"source_id": "b", "method": "greenhouse", "url": "s", "program_names": ""}
        first = job_boards.check(src, FakeJSONClient(a)).snapshot_text
        second = job_boards.check(src, FakeJSONClient(b)).snapshot_text
        self.assertEqual(first, second)

    def test_14_9g_a_homoglyph_title_cannot_forge_a_new_row(self):
        """Boards plant lookalike canaries; unnormalised they become phantom rows."""
        self.assertEqual(
            snapshot.fold("\uA4DFachine \uA4E1earning \uA4E3esearcher"),
            "Machine Learning Researcher",
        )

    def test_14_9h_a_board_that_empties_is_a_failure_not_a_quiet_day(self):
        src = {"source_id": "empties", "method": "greenhouse", "url": "s", "program_names": ""}
        payload = {"jobs": [_gh("SWE Intern")]}
        first = job_boards.check(src, FakeJSONClient(payload))
        self.assertTrue(first.baseline)
        state.write_snapshot("empties", first.snapshot_text, ext="tsv")
        second = job_boards.check(src, FakeJSONClient({"jobs": []}))
        self.assertFalse(second.ok)
        self.assertIn("0 postings", second.error)

    def test_14_9i_posting_text_rides_along_so_no_second_fetch_is_needed(self):
        """The description arrives in the same response, so rule 6 works on Tier 2."""
        src = {"source_id": "t", "method": "greenhouse", "url": "s", "program_names": ""}
        first = job_boards.check(src, FakeJSONClient({"jobs": [_gh("SWE Intern")]}))
        state.write_snapshot("t", first.snapshot_text, ext="tsv")
        second = job_boards.check(
            src,
            FakeJSONClient({"jobs": [
                _gh("SWE Intern"),
                _gh("Data Intern", content="<p>Requirements: rising junior.</p>"),
            ]}),
        )
        added = [c for c in second.changes if "Data Intern" in c.key]
        self.assertTrue(added and "rising junior" in added[0].posting_text)

    def test_14_9j_international_is_not_an_internship(self):
        """Both halves of a lookahead bug that shipped once. "Internal" was excluded
        with (?!al), but "International" is intern + *at*, so it slipped through --
        live, on RBC's board, as "International Equity Fund Analyst"."""
        for title in ("Internal Audit Analyst", "Internal Sales Consultant",
                      "International Equity Fund Analyst", "Internationalisation Lead"):
            with self.subTest(title=title):
                self.assertFalse(job_boards.STUDENT_TITLE.search(title), title)
                self.assertFalse(job_boards.STUDENT_TITLE_WORKDAY.search(title), title)
        for title in ("Software Engineer Intern", "Winternship 2027",
                      "Internship - Trading", "Interns Program"):
            with self.subTest(title=title):
                self.assertTrue(job_boards.STUDENT_TITLE.search(title), title)

    def test_14_9k_a_foreign_workday_row_is_dropped_before_its_detail_fetch(self):
        """RBC Early Talent: 136 of 152 titles pass the title filter and all but ~36
        are Canadian. Paying for a description before screening the location pushed
        the board past WORKDAY_MAX_DETAILS and reported it as failing."""
        self.assertTrue(job_boards.NON_US_LOCATION.search("TORONTO, Ontario, Canada"))
        self.assertTrue(job_boards.NON_US_LOCATION.search("Bengaluru, India"))
        # Unrecognised and multi-location rows must survive to the detail fetch --
        # dropping a real US role to tidy the digest is the expensive error.
        for keep in ("2 Locations", "", "Chicago, Illinois, United States of America",
                     "Springfield"):
            with self.subTest(location=keep):
                self.assertFalse(job_boards.NON_US_LOCATION.search(keep), keep)


class Phase2PageWatchTests(_IsolatedState, unittest.TestCase):
    PAGE = "<html><body>" + "<p>Registration for the 2027 contest is open.</p>" * 40 + "</body></html>"

    def _src(self, **kw):
        base = {"source_id": "p", "url": "https://example.invalid/", "render_js": "false",
                "selector": "", "program_names": "Test Page"}
        base.update(kw)
        return base

    def test_14_9j_render_js_true_is_refused_loudly(self):
        r = page_watch.check(self._src(render_js="true"), FakeHTMLClient(self.PAGE))
        self.assertFalse(r.ok)
        self.assertIn("render_js=true", r.error)

    def test_14_9k_a_configured_selector_is_refused_rather_than_ignored(self):
        r = page_watch.check(self._src(selector=".main"), FakeHTMLClient(self.PAGE))
        self.assertFalse(r.ok)
        self.assertIn("selector", r.error)

    def test_14_9l_a_javascript_shell_is_a_failure(self):
        shell = "<html>" + "<script>var x=1;</script>" * 3000 + "<body><p>Menu</p></body></html>"
        r = page_watch.check(self._src(), FakeHTMLClient(shell))
        self.assertFalse(r.ok)

    def test_14_9m_a_page_that_shrinks_is_a_failure(self):
        r = page_watch.check(self._src(source_id="shrink"), FakeHTMLClient(self.PAGE))
        state.write_snapshot("shrink", r.snapshot_text, ext="txt")
        # Above the absolute floor but well under 40% of the baseline, so the ratio
        # rule is what fires rather than the character minimum.
        small = "<html><body>" + "<p>Registration for the 2027 contest is open.</p>" * 12 + "</body></html>"
        second = page_watch.check(self._src(source_id="shrink"), FakeHTMLClient(small))
        self.assertFalse(second.ok)
        self.assertIn("shrank", second.error)

    def test_14_9n_one_change_per_page_and_discovery_reads_added_text_only(self):
        r = page_watch.check(self._src(source_id="one"), FakeHTMLClient(self.PAGE))
        state.write_snapshot("one", r.snapshot_text, ext="txt")
        grown = self.PAGE.replace("</body>", "<p>New freshman track announced.</p></body>")
        second = page_watch.check(self._src(source_id="one"), FakeHTMLClient(grown))
        self.assertEqual(len(second.changes), 1, "a page must never emit one change per line")
        self.assertTrue(second.changes[0].is_discovery_candidate)

        # A programme going away must not read as a find. Baseline the page *with* a
        # freshman line, then remove it: the only "Freshman" text is in the removed
        # side, so the change must not be a discovery candidate.
        had = self.PAGE.replace("</body>", "<p>Freshman track is open.</p></body>")
        base = page_watch.check(self._src(source_id="gone"), FakeHTMLClient(had))
        state.write_snapshot("gone", base.snapshot_text, ext="txt")
        third = page_watch.check(self._src(source_id="gone"), FakeHTMLClient(self.PAGE))
        self.assertEqual(len(third.changes), 1)
        self.assertFalse(third.changes[0].is_discovery_candidate,
                         "a closure must not be flagged as a discovery")


class Phase2DiscoveryTests(_IsolatedState, unittest.TestCase):
    def test_14_9o_discovery_never_writes_sources_csv(self):
        before = state.SOURCES_CSV.read_bytes()
        discover.record([discover.Candidate("repo", "repo:a/b", "a/b", "u", "e")])
        self.assertEqual(state.SOURCES_CSV.read_bytes(), before)

    def test_14_9p_a_candidate_already_on_file_is_never_re_proposed(self):
        """Including rejected ones: that status is a permanent tombstone."""
        state.write_discovered([{
            "first_proposed": "2026-01-01", "last_proposed": "2026-01-01", "kind": "repo",
            "key": "repo:seen/repo", "title": "seen/repo", "url": "u",
            "evidence": "e", "status": "rejected",
        }])
        seen = {r["key"] for r in state.read_discovered()}
        self.assertIn("repo:seen/repo", seen)

    def test_14_9q_discovery_only_runs_on_monday_or_after_a_missed_week(self):
        self.assertTrue(discover.due("2026-09-14"))   # a Monday
        self.assertFalse(discover.due("2026-09-16"))  # a Wednesday, ran recently


class Phase2SuppressionTests(_IsolatedState, unittest.TestCase):
    def test_14_9r_a_ticked_item_is_hidden_and_next_cycle_resurfaces(self):
        """Keys hash source_id + row key, so the Summer 2028 repost differs from the
        Summer 2027 one and comes back on its own -- no expiry logic needed."""
        k27 = state.change_key("b", "SWE Intern, Summer 2027")
        k28 = state.change_key("b", "SWE Intern, Summer 2028")
        self.assertNotEqual(k27, k28)
        state.write_applied({k27: "2026-09-11"})
        self.assertIn(k27, state.read_applied())
        self.assertNotIn(k28, state.read_applied())

    def test_14_9s2_a_dismissal_is_scoped_to_its_hiring_cycle(self):
        """Next year's repost must return even when the title never says a year.

        Measured on the live boards: 136 of 188 rows carry "Summer 2027" or similar
        and would change key by themselves, but 52 do not -- Jane Street titles every
        student role plainly and puts the season in metadata. Tagging the key with the
        cycle covers both, with no expiry clock that could lapse mid-season.
        """
        plain = "Quantitative Trader @ New York"
        sept = state.change_key("js", plain, state.recruiting_cycle("2026-09-11"))
        january = state.change_key("js", plain, state.recruiting_cycle("2027-01-15"))
        next_july = state.change_key("js", plain, state.recruiting_cycle("2027-07-01"))
        self.assertEqual(sept, january, "a dismissal must hold for the whole season")
        self.assertNotEqual(sept, next_july, "the next season must be a new item")

    def test_14_9s3_the_cycle_rolls_over_mid_year_not_in_january(self):
        """Summer 2027 roles are advertised from about July 2026."""
        self.assertEqual(state.recruiting_cycle("2026-09-11"), 2027)
        self.assertEqual(state.recruiting_cycle("2027-06-30"), 2027)
        self.assertEqual(state.recruiting_cycle("2027-07-01"), 2028)

    def test_14_9s_the_digest_marks_each_item_with_its_key(self):
        judgment = classify.Judgment(
            change=state.Change(source_id="b", kind="added", key="SWE Intern", detail="x"),
            relevant=True, classified=True, confidence="low",
        )
        _, body = digest.render([judgment], [], {})
        self.assertIn("- [ ]", body)
        self.assertIn(f"<!--k:{state.change_key('b', 'SWE Intern')}-->", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
