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

import datetime
import os
import pathlib
import re
import sys
import time
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import check
import classify
import discover
import screen
import digest
from core import clock, models, paths
from gather import breaker
from persist import store
from process import redirect, suppress
import build_xlsx
from sources import github_repos, job_boards, page_watch, postings, snapshot

# --- the database must survive the suite ---------------------------------------------
#
# Every path in core.paths is a module attribute that its readers resolve at call
# time, and _IsolatedState works by rebinding those attributes at a temp directory.
# That is a convention, not a mechanism: the day a reader switches to
# `from core.paths import SNAPSHOTS` the rebind stops working, and the suite keeps
# passing while writing into the real data/ tree. A green suite that has quietly
# stopped protecting the database is worse than a red one.
#
# This is not hypothetical. It has already happened twice here: postings.CACHE_DIR was
# import-bound, so tests wrote into the real data/postings_cache/, and _IsolatedState
# never rebound PROGRAMS_CSV/SOURCES_CSV, so three tests read live data.
#
# So the convention gets a backstop that does not depend on the convention. The
# committed trees are fingerprinted before the suite and re-checked after it, which
# turns "a test wrote to the database" from invisible into a hard error.
GUARDED_TREES = (paths.DATA, paths.ROOT / "out")

_MANIFEST: dict[str, tuple[int, int]] = {}


def _fingerprint() -> dict[str, tuple[int, int]]:
    """path -> (size, mtime). A byte-identical rewrite still moves mtime, and a test
    rewriting the database is a failure even when the bytes come out the same."""
    seen: dict[str, tuple[int, int]] = {}
    for tree in GUARDED_TREES:
        if not tree.exists():
            continue
        for path in tree.rglob("*"):
            if path.is_file():
                stat = path.stat()
                seen[str(path)] = (stat.st_size, stat.st_mtime_ns)
    return seen


def setUpModule():
    _MANIFEST.update(_fingerprint())


def tearDownModule():
    after = _fingerprint()
    created = sorted(set(after) - set(_MANIFEST))
    deleted = sorted(set(_MANIFEST) - set(after))
    modified = sorted(p for p in set(after) & set(_MANIFEST) if after[p] != _MANIFEST[p])
    if created or deleted or modified:
        raise AssertionError(
            "the suite wrote to the committed data tree, so state isolation is broken. "
            "Whatever path was touched needs adding to _IsolatedState.STATE_PATHS -- do "
            "not relax this check.\n"
            f"  created:  {created}\n"
            f"  deleted:  {deleted}\n"
            f"  modified: {modified}"
        )


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
    def __init__(self, text="", payload=None, status=200, url=""):
        self.text = text
        self._payload = payload or {}
        self.status_code = status
        # httpx sets `.url` to the FINAL url after following redirects, and page_watch
        # compares it against the configured one. A fake without it would let a
        # redirect bug pass the suite.
        self.url = url

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


class _IsolatedState:
    """Redirect every path in core.paths at a temp dir.

    Without this the new suites write snapshots, applied.tsv and discovered.csv into
    the real data/ directory -- which they did, until this was added. Test runs must
    not touch the database the job commits.

    Every path constant belongs in STATE_PATHS. The three that were missing --
    PROGRAMS_CSV, SOURCES_CSV and postings.CACHE_DIR -- are why three tests read live
    data and the posting cache was written in place; see the module fingerprint guard
    above, which now catches the next omission rather than trusting this list to be
    complete. The one class that deliberately reads the committed files is
    LiveDataInvariantTests, so the rule reads "everything except that".
    """

    #: Rebinding these is only sound because every reader resolves them as an attribute
    #: of `core.paths` at call time. A `from core.paths import SNAPSHOTS` anywhere
    #: downstream would bind a stale copy and silently send the write back into the
    #: database -- which is exactly the hazard that made hardening this file step 0 of
    #: the refactor rather than a later tidy-up.
    STATE_PATHS = (
        "DATA", "SNAPSHOTS", "PROPOSALS_LOG", "LAST_DELIVERED", "APPLIED_TSV",
        "DISCOVERED_CSV", "LAST_DISCOVERY", "PROGRAMS_CSV", "SOURCES_CSV",
        "OUT_XLSX", "OUT_TRACKED_XLSX", "RUN_DIR",
    )

    def setUp(self):
        super().setUp()
        self.isolate_state()

    def isolate_state(self) -> pathlib.Path:
        """Point every state path at a fresh temp tree and return its root.

        addCleanup rather than tearDown: if setUp raises part-way through, tearDown is
        never called and the globals would stay aimed at a deleted temp dir for the
        rest of the run. Cleanups are registered before each rebind, so a partial
        setUp still unwinds completely.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        for name in self.STATE_PATHS:
            self.addCleanup(setattr, paths, name, getattr(paths, name))
        self.addCleanup(setattr, postings, "CACHE_DIR", postings.CACHE_DIR)

        paths.DATA = root
        paths.SNAPSHOTS = root / "snapshots"
        paths.PROPOSALS_LOG = root / "proposals.log"
        paths.LAST_DELIVERED = root / "last_delivered.txt"
        paths.APPLIED_TSV = root / "applied.tsv"
        paths.DISCOVERED_CSV = root / "discovered.csv"
        paths.LAST_DISCOVERY = root / "last_discovery.txt"
        paths.PROGRAMS_CSV = root / "programs.csv"
        paths.SOURCES_CSV = root / "sources.csv"
        paths.OUT_XLSX = root / "out" / "programs.xlsx"
        paths.OUT_TRACKED_XLSX = root / "out" / "tracked.xlsx"
        postings.CACHE_DIR = root / "postings_cache"
        paths.RUN_DIR = root / ".run"
        paths.SNAPSHOTS.mkdir()
        return root


class AcceptanceTests(_IsolatedState, unittest.TestCase):
    def setUp(self):
        super().setUp()
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

    def _baseline(self, readme=BASE_README):
        result = github_repos.check(self.source, FakeClient(readme))
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.baseline)
        store.write_snapshot("nuft-2027", result.snapshot_text, ext="tsv")
        return result

    # --- 14.2 -------------------------------------------------------------------
    def test_14_2_edited_snapshot_surfaces_in_act_now(self):
        """Hand-edit a stored snapshot to simulate a page change -> ACT NOW."""
        self._baseline()
        # Simulate the previous run not having seen the Jane Street row.
        path = store.snapshot_path("nuft-2027", "tsv")
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
        result = models.SourceResult(source_id="nuft-2027", ok=False, error="HTTP 404")
        source = dict(self.source, consecutive_failures="4", last_success="2026-09-01")
        title, body = digest.render([], [result], {"nuft-2027": source})
        self.assertIn("ACT NOW", body)
        self.assertIn("SOURCE BLIND", body)
        self.assertIn("4 failures running", body)
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
        change = models.Change(
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
        change = models.Change(
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
        self.assertIn("Worth a look", body)
        self.assertIn("unverified", body)

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
        change = models.Change(
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
        healthy = models.SourceResult(source_id="nuft-2027", ok=True)

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
        failing = models.SourceResult(source_id="nuft-2027", ok=False, error="HTTP 500")
        self.assertEqual(digest.should_send([], [failing]), (True, False))
        title, _ = digest.render([], [failing], {})
        self.assertNotIn("(no changes)", title)

        # So is a real change.
        change = models.Change(
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
        healthy = models.SourceResult(source_id="nuft-2027", ok=True)
        failing = models.SourceResult(source_id="nuft-2027", ok=False, error="HTTP 500")
        change = models.Change(
            source_id="nuft-2027", kind="added", key="Jane Street / FTTP", detail="x"
        )
        judgment = classify.Judgment(change=change, relevant=True, classified=True)

        # Tick 1: nothing delivered yet, so the digest goes out.
        self.assertEqual(store.read_last_delivered(), "", "no marker before the first run")
        self.assertEqual(digest.should_send([], [healthy], False), (True, True))

        # Tick 2, an hour later, after tick 1 recorded a delivery: silence, whatever
        # this run happened to find.
        store.write_last_delivered()
        self.assertEqual(store.read_last_delivered(), clock.today_iso())
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
        store.write_last_delivered("2020-01-01")
        self.assertNotEqual(store.read_last_delivered(), clock.today_iso())
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
                digest.delivered_issue_exists(clock.today_iso()),
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
                **{column: "" for column in paths.PROGRAM_COLUMNS},
                "name": "Jane Street FTTP",
                "eligible": "YES",
                "status": "dormant",
            }
        ]
        sources = [dict(self.source, program_names="Jane Street FTTP")]
        change = models.Change(
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
        result = models.SourceResult(
            source_id="nuft-2027", ok=True, changes=[change], snapshot_text="x"
        )
        check.update_program_state(programs, sources, [result], [judgment])
        self.assertEqual(programs[0]["eligible"], "YES", "eligible must be untouched")
        self.assertEqual(programs[0]["status"], "open", "status is ours to maintain")
        self.assertTrue(paths.PROPOSALS_LOG.exists(), "the status change must be logged")

    def test_muted_rows_are_silenced(self):
        self.assertIn("muted", paths.PROGRAM_COLUMNS)


class NoiseRegressionTests(_IsolatedState, unittest.TestCase):
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
class PostingFilterTests(_IsolatedState, unittest.TestCase):
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
        return models.Change(
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
            change = models.Change(source_id="simplify-2027", kind="added",
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
            classify.PROFILE_LAST_REVIEWED = clock.today_iso()
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
class PostingJudgementTests(_IsolatedState, unittest.TestCase):
    """Does the small model actually make the right call on real posting text?

    gpt-5-mini is weaker than Sonnet, and rules 6 and 7 ask it to *rule things out* --
    the expensive direction to get wrong. The posting text is seeded into the fetch
    cache so these are deterministic about the input while still making a real model
    call about the judgment.

    Seeded into a temp cache, not the real one. These seeds used to land in
    data/postings_cache/ and be unlinked again on the way out, which is only invisible
    because the class skips without a key -- run it with one and it edits the database.
    """

    SOURCE = {"source_id": "simplify-2027", "signal": "low", "program_names": "Board"}
    URL = "https://simplify.jobs/p/00000000-0000-0000-0000-0000000000%02d"

    def setUp(self):
        super().setUp()
        self._saved = os.environ.get("CLASSIFIER_PROVIDER")
        os.environ["CLASSIFIER_PROVIDER"] = "azure"
        postings.CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("CLASSIFIER_PROVIDER", None)
        else:
            os.environ["CLASSIFIER_PROVIDER"] = self._saved

    def _judge(self, n, role, posting_text):
        url = self.URL % n
        path = postings._cache_path(url)
        path.write_text(posting_text)
        change = models.Change(
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


# --- 14.9: Phase 2 ---------------------------------------------------------------
class FakeJSONClient:
    """Returns a fixed JSON payload for any URL. Stands in for an ATS."""

    def __init__(self, payload, status=200, text=""):
        self.payload, self.status, self.text = payload, status, text

    def get(self, url, *args, **kwargs):
        return FakeResponse(text=self.text or "", payload=self.payload, status=self.status)


class FakeHTMLClient:
    def __init__(self, html, status=200, final_url=None):
        self.html, self.status = html, status
        # None = no redirect: the response reports the url that was asked for.
        self.final_url = final_url

    def get(self, url, *args, **kwargs):
        return FakeResponse(
            text=self.html, status=self.status, url=self.final_url or url
        )


def _gh(title, location="New York, United States", employment_type=None, content="<p>x</p>"):
    job = {"title": title, "location": {"name": location},
           "departments": [{"name": "Engineering"}],
           "absolute_url": f"https://example.invalid/{title}", "content": content}
    if employment_type:
        job["metadata"] = [{"name": "Employment Type", "value": employment_type}]
    return job


class Phase2ConfigTests(_IsolatedState, unittest.TestCase):
    """The registry, and the silent-skip bug it replaced."""

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
        store.write_snapshot("empties", first.snapshot_text, ext="tsv")
        second = job_boards.check(src, FakeJSONClient({"jobs": []}))
        self.assertFalse(second.ok)
        self.assertIn("0 postings", second.error)

    def test_14_9i_posting_text_rides_along_so_no_second_fetch_is_needed(self):
        """The description arrives in the same response, so rule 6 works on Tier 2."""
        src = {"source_id": "t", "method": "greenhouse", "url": "s", "program_names": ""}
        first = job_boards.check(src, FakeJSONClient({"jobs": [_gh("SWE Intern")]}))
        store.write_snapshot("t", first.snapshot_text, ext="tsv")
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

    def test_14_9l_a_phenom_discovery_programme_survives_with_no_intern_in_its_title(self):
        """The Jane Street trap in a second ATS. Susquehanna's Discovery Programs are
        titled "Discovery Program: Quantitative Trading" -- no "intern" anywhere -- and
        the student-ness lives in a `category` field that arrives as a *list*."""
        payload = {"totalCount": 3, "jobs": [
            {"data": {"title": "Discovery Program: Quantitative Trading",
                      "category": ["Student Discovery Program"], "city": "New York",
                      "country": "United States", "apply_url": "https://x.invalid/1",
                      "description": "<p>graduate in the spring of 2029</p>",
                      "qualifications": ""}},
            {"data": {"title": "Quantitative Trader Internship: Summer 2027",
                      "category": ["Interns + Co-ops"], "city": "Chicago",
                      "country": "United States", "apply_url": "https://x.invalid/2",
                      "description": "<p>x</p>", "qualifications": ""}},
            {"data": {"title": "C++ Developer | Experienced Hire",
                      "category": ["Experienced Professionals"], "city": "New York",
                      "country": "United States", "apply_url": "https://x.invalid/3",
                      "description": "<p>x</p>", "qualifications": ""}},
        ]}
        r = job_boards.check(
            {"source_id": "sig", "method": "phenom",
             "url": "https://careers.example.invalid/api/jobs", "program_names": ""},
            FakeJSONClient(payload),
        )
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.extra["rows"], 2, "both student rows must survive")
        self.assertIn("Discovery Program: Quantitative Trading", r.snapshot_text)
        self.assertNotIn("Experienced Hire", r.snapshot_text)

    def test_14_9m_a_fetcher_side_filter_is_reported_not_swallowed(self):
        """Workday and Phenom screen inside their fetcher, so the shared screen in
        check() has nothing left to remove. That once made a 263-posting board report
        "0 suppressed" in HEALTH -- a silent filter, which this project treats as a bug
        in its own right."""
        payload = {"totalCount": 2, "jobs": [
            {"data": {"title": "SWE Intern", "category": ["Interns + Co-ops"],
                      "city": "New York", "country": "United States",
                      "apply_url": "https://x.invalid/1", "description": "<p>x</p>",
                      "qualifications": ""}},
            {"data": {"title": "Head of Compliance", "category": ["Experienced Professionals"],
                      "city": "New York", "country": "United States",
                      "apply_url": "https://x.invalid/2", "description": "<p>x</p>",
                      "qualifications": ""}},
        ]}
        r = job_boards.check(
            {"source_id": "sig2", "method": "phenom",
             "url": "https://careers.example.invalid/api/jobs", "program_names": ""},
            FakeJSONClient(payload),
        )
        self.assertEqual(r.extra["postings"], 2, "the pre-filter total must be reported")
        self.assertEqual(r.extra["suppressed_not_student"], 1)
        # And as a named report, not only as a total. This regressed once while Tier 2
        # was being split into gather/process: the category screen moved into the
        # parser, the count was still being taken from the fetcher, and a 2-posting
        # board reported 1 posting. The count has to come from the board's own total.
        phenom = [f for f in r.filters if f.filter_id == "ats-phenom-category"]
        self.assertEqual(len(phenom), 1, r.filters)
        self.assertEqual((phenom[0].considered, phenom[0].removed), (2, 1))

    def test_14_9n_a_phenom_field_may_be_a_list_a_dict_or_a_string(self):
        """Field types are per-field and undocumented; the first version of the fetcher
        called .strip() on a list and failed the entire board."""
        self.assertEqual(job_boards._phenom_field(["Interns + Co-ops"]), "Interns + Co-ops")
        self.assertEqual(job_boards._phenom_field({"name": "New Graduates"}), "New Graduates")
        self.assertEqual(job_boards._phenom_field("  Intern  "), "Intern")
        self.assertEqual(job_boards._phenom_field(None), "")

    def test_14_9o_aqr_style_summer_analyst_titles_are_student_roles(self):
        """Measured gap, 2026-09-11: "intern" alone found 140 US student rows across the
        ATS boards and missed 60. AQR was invisible entirely -- all 54 of its postings
        have an empty employment_type and its whole 2027 programme is titled "2027
        Engineering Summer Analyst". Third variant of the Jane Street trap."""
        for title in ("2027 Engineering Summer Analyst", "2027 Research Summer Analyst",
                      "Campus Full Time 2027 - Quantitative Trader",
                      "Graduate Trader Program Chicago 2027",
                      "Software Engineer - University Hire 2027",
                      "Quantitative Trader/Researcher - 2027",
                      "2027 Cubist Quant Academy - Developers"):
            with self.subTest(title=title):
                self.assertTrue(
                    job_boards.is_student_posting(
                        job_boards.Posting(title, "New York", "x", "", "u", "")),
                    title)

    def test_14_9p_a_campus_recruiter_is_not_a_student_role(self):
        """Jobs *about* early-career hiring match the widened screen and are full-time
        staff roles. Jane Street posts three of them."""
        for title in ("Campus Recruiter, Technology",
                      "Campus Relations & Events Associate",
                      "Campus Recruiting Coordinator",
                      "Quantitative Campus Recruiter"):
            with self.subTest(title=title):
                self.assertFalse(
                    job_boards.is_student_posting(
                        job_boards.Posting(title, "New York", "x", "", "u", "")),
                    title)


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
        store.write_snapshot("shrink", r.snapshot_text, ext="txt")
        # Above the absolute floor but well under 40% of the baseline, so the ratio
        # rule is what fires rather than the character minimum.
        small = "<html><body>" + "<p>Registration for the 2027 contest is open.</p>" * 12 + "</body></html>"
        second = page_watch.check(self._src(source_id="shrink"), FakeHTMLClient(small))
        self.assertFalse(second.ok)
        self.assertIn("shrank", second.error)

    def test_14_9n_one_change_per_page_and_discovery_reads_added_text_only(self):
        r = page_watch.check(self._src(source_id="one"), FakeHTMLClient(self.PAGE))
        store.write_snapshot("one", r.snapshot_text, ext="txt")
        grown = self.PAGE.replace("</body>", "<p>New freshman track announced.</p></body>")
        second = page_watch.check(self._src(source_id="one"), FakeHTMLClient(grown))
        self.assertEqual(len(second.changes), 1, "a page must never emit one change per line")
        self.assertTrue(second.changes[0].is_discovery_candidate)

        # A programme going away must not read as a find. Baseline the page *with* a
        # freshman line, then remove it: the only "Freshman" text is in the removed
        # side, so the change must not be a discovery candidate.
        had = self.PAGE.replace("</body>", "<p>Freshman track is open.</p></body>")
        base = page_watch.check(self._src(source_id="gone"), FakeHTMLClient(had))
        store.write_snapshot("gone", base.snapshot_text, ext="txt")
        third = page_watch.check(self._src(source_id="gone"), FakeHTMLClient(self.PAGE))
        self.assertEqual(len(third.changes), 1)
        self.assertFalse(third.changes[0].is_discovery_candidate,
                         "a closure must not be flagged as a discovery")

    def test_14_9q_a_one_line_json_feed_diffs_per_record(self):
        """Four watched sources are vendor JSON feeds served as a single line. A line
        differ on one line can only ever say "the whole feed changed"."""
        feed = '[{"title":"Quant Intern","id":1},{"title":"SWE Intern","id":2}]'
        lines = page_watch.normalise(feed)
        self.assertEqual(len(lines), 2, lines)

    def test_14_9r_no_digest_line_can_exceed_the_cap(self):
        """Wolverine's feed is 147,741 characters on one line. Emitted verbatim it would
        push a single line past GitHub's 65,536-character issue-body limit and fail
        delivery -- a one-byte upstream edit costing the whole digest."""
        monster = "x" * 200_000
        changes = page_watch.diff_pages(
            "s", [], [monster], {"program_names": "P", "url": "u"})
        self.assertTrue(changes)
        longest = max(len(line) for line in changes[0].detail.splitlines())
        self.assertLessEqual(longest, page_watch.MAX_DIFF_LINE_CHARS + 40, longest)
        self.assertIn("chars]", changes[0].detail, "truncation must be visible, not silent")


class Phase2DiscoveryTests(_IsolatedState, unittest.TestCase):
    def test_14_9o_discovery_never_writes_sources_csv(self):
        """Discovery proposes and never adds. Watched against a seeded copy rather
        than the real file: a test that asserts "this never writes the database" must
        not be pointed at the database, or the run that disproves it also destroys it."""
        store.write_sources([{"source_id": "seeded", "method": "github_readme",
                              "url": "a/b"}])
        before = paths.SOURCES_CSV.read_bytes()
        discover.record([discover.Candidate("repo", "repo:a/b", "a/b", "u", "e")])
        self.assertEqual(paths.SOURCES_CSV.read_bytes(), before)

    def test_14_9p_a_candidate_already_on_file_is_never_re_proposed(self):
        """Including rejected ones: that status is a permanent tombstone."""
        store.write_discovered([{
            "first_proposed": "2026-01-01", "last_proposed": "2026-01-01", "kind": "repo",
            "key": "repo:seen/repo", "title": "seen/repo", "url": "u",
            "evidence": "e", "status": "rejected",
        }])
        seen = {r["key"] for r in store.read_discovered()}
        self.assertIn("repo:seen/repo", seen)

    def test_14_9q_discovery_only_runs_on_monday_or_after_a_missed_week(self):
        self.assertTrue(discover.due("2026-09-14"))   # a Monday
        self.assertFalse(discover.due("2026-09-16"))  # a Wednesday, ran recently


class Phase2SuppressionTests(_IsolatedState, unittest.TestCase):
    def test_14_9r_a_ticked_item_is_hidden_and_next_cycle_resurfaces(self):
        """Keys hash source_id + row key, so the Summer 2028 repost differs from the
        Summer 2027 one and comes back on its own -- no expiry logic needed."""
        k27 = clock.change_key("b", "SWE Intern, Summer 2027")
        k28 = clock.change_key("b", "SWE Intern, Summer 2028")
        self.assertNotEqual(k27, k28)
        store.write_applied({k27: "2026-09-11"})
        self.assertIn(k27, store.read_applied())
        self.assertNotIn(k28, store.read_applied())

    def test_14_9s2_a_dismissal_is_scoped_to_its_hiring_cycle(self):
        """Next year's repost must return even when the title never says a year.

        Measured on the live boards: 136 of 188 rows carry "Summer 2027" or similar
        and would change key by themselves, but 52 do not -- Jane Street titles every
        student role plainly and puts the season in metadata. Tagging the key with the
        cycle covers both, with no expiry clock that could lapse mid-season.
        """
        plain = "Quantitative Trader @ New York"
        sept = clock.change_key("js", plain, clock.recruiting_cycle("2026-09-11"))
        january = clock.change_key("js", plain, clock.recruiting_cycle("2027-01-15"))
        next_july = clock.change_key("js", plain, clock.recruiting_cycle("2027-07-01"))
        self.assertEqual(sept, january, "a dismissal must hold for the whole season")
        self.assertNotEqual(sept, next_july, "the next season must be a new item")

    def test_14_9s3_the_cycle_rolls_over_mid_year_not_in_january(self):
        """Summer 2027 roles are advertised from about July 2026."""
        self.assertEqual(clock.recruiting_cycle("2026-09-11"), 2027)
        self.assertEqual(clock.recruiting_cycle("2027-06-30"), 2027)
        self.assertEqual(clock.recruiting_cycle("2027-07-01"), 2028)

    def test_14_9t_the_digest_is_one_table_with_act_now_rows_first(self):
        """The owner asked for a table, not two prose sections.

        Urgency became a column rather than a heading, so the ordering guarantee moved
        from the document structure into the row order and needs asserting: every ACT
        NOW row sits above every WORTH A LOOK row.
        """
        rolling = classify.Judgment(
            change=models.Change(
                source_id="js", kind="added", key="SWE Intern @ NYC", detail="x",
                url="https://example.com/a", rolling=True,
            ),
            program_name="Jane Street", relevant=True, classified=True,
            confidence="high", why="First-year eligible.",
        )
        ordinary = classify.Judgment(
            change=models.Change(source_id="b", kind="added", key="Quant Intern", detail="x"),
            program_name="Some Firm", relevant=True, classified=True, confidence="low",
        )
        _, body = digest.render([ordinary, rolling], [], {})

        self.assertIn("## \u25a0 OPPORTUNITIES (2)", body)
        self.assertIn("| Urgency | Company | Position | Notes |", body)
        self.assertNotIn("WORTH A LOOK (", body, "the old section heading is gone")

        rows = [line for line in body.splitlines() if line.startswith("| ")]
        urgencies = [row.split("|")[1].strip() for row in rows[1:]]
        self.assertEqual(
            urgencies, [digest.URGENCY_ACT_NOW, digest.URGENCY_WORTH_A_LOOK],
            "the rolling item is urgent and must be rendered above the ordinary one",
        )
        self.assertIn("[SWE Intern @ NYC](https://example.com/a)", body)

    def test_14_9u_a_pipe_in_a_title_cannot_break_the_table(self):
        """One unescaped pipe silently shifts every later column in that row."""
        judgment = classify.Judgment(
            change=models.Change(
                source_id="b", kind="added", key="SWE | Intern", detail="x",
            ),
            program_name="Some | Firm", relevant=True, classified=True,
            confidence="high", why="Fine | really",
        )
        _, body = digest.render([judgment], [], {})
        row = next(line for line in body.splitlines() if "Intern" in line)
        delimiters = len(re.findall(r"(?<!\\)\|", row))
        self.assertEqual(delimiters, 5, f"row has stray delimiters: {row}")
        self.assertIn(r"SWE \| Intern", row)

    def test_14_9v_a_long_reason_is_truncated_to_a_clause(self):
        """An unbounded `why` wraps the table into the wall of text it replaced."""
        judgment = classify.Judgment(
            change=models.Change(source_id="b", kind="added", key="SWE Intern", detail="x"),
            program_name="Some Firm", relevant=True, classified=True,
            confidence="high", why="word " * 100,
        )
        _, body = digest.render([judgment], [], {})
        row = next(line for line in body.splitlines() if "SWE Intern" in line)
        notes = row.split("|")[4].strip()
        self.assertLessEqual(len(notes), digest.NOTE_CHARS + 1)
        self.assertTrue(notes.endswith("\u2026"), notes)


class OwnerWorkbookTests(_IsolatedState, unittest.TestCase):
    """out/programs.xlsx is the owner's file: only what he must chase himself.

    Asked for directly -- it had become a dump of all 187 programmes, which is useless
    for deciding what to go and check. Two classes of row are bloat there: things he
    cannot apply to, and things a watched source already reports in the daily digest.
    """

    SOURCES = {
        "janestreet-fttp": {
            "source_id": "janestreet-fttp", "method": "page_text",
            "url": "https://www.janestreet.com/join-jane-street/programs-and-events/fttp/",
        },
        "citadel-students": {
            "source_id": "citadel-students", "method": "manual",
            "url": "https://www.citadel.com/careers/students/",
        },
        "aqr-greenhouse": {
            "source_id": "aqr-greenhouse", "method": "greenhouse", "url": "aqr",
        },
    }

    def _program(self, **overrides):
        row = {"name": "P", "category": "c", "website": "", "eligible": "YES",
               "notes": "", "source_id": "", "applications_open": "", "target_years": ""}
        row.update(overrides)
        return row

    def test_14_10a_a_shared_domain_is_not_coverage(self):
        """The regression that nearly shipped. Matching on the registrable domain called
        "Jane Street Puzzles (monthly)" covered because janestreet.com is watched -- but
        the watcher points at the FTTP page. Folding the puzzles away would have hidden
        them from the only file that was going to surface them."""
        puzzles = self._program(
            name="Jane Street Puzzles (monthly)",
            website="https://www.janestreet.com/puzzles/current-puzzle/")
        self.assertEqual(build_xlsx.covered_by(puzzles, self.SOURCES), "")

    def test_14_10b_the_exact_watched_page_is_coverage(self):
        fttp = self._program(
            name="Jane Street FTTP",
            website="https://www.janestreet.com/join-jane-street/programs-and-events/fttp/")
        self.assertEqual(build_xlsx.covered_by(fttp, self.SOURCES), "janestreet-fttp")

    def test_14_10c_a_manual_source_is_not_coverage(self):
        """A manual row is precisely a thing the tool cannot watch, so it must leave the
        programme on the hand-check list rather than removing it."""
        discover_citadel = self._program(
            name="Discover Citadel", source_id="citadel-students")
        self.assertEqual(build_xlsx.covered_by(discover_citadel, self.SOURCES), "")

    def test_14_10d_a_dangling_source_id_keeps_the_row_and_warns(self):
        """A renamed source once left programs.csv pointing at a row that no longer
        existed. A dangling reference must never silently delete a programme from the
        owner's list."""
        orphan = self._program(name="Optiver FutureFocus", source_id="gone-away")
        keep, left_out, warnings = build_xlsx.partition([orphan], self.SOURCES)
        self.assertEqual(len(keep), 1, "the row must stay on his list")
        self.assertFalse(left_out)
        self.assertTrue(any("gone-away" in w for w in warnings), warnings)

    def test_14_10e_nothing_can_vanish_without_a_reason(self):
        """Every filter reports what it removed. A programme must appear either on the
        list or on the Left Out sheet, never neither."""
        programs = [
            self._program(name="applyable"),
            self._program(name="ruled out", eligible="NO"),
            self._program(name="dead", eligible="STALE"),
            self._program(name="watched", source_id="aqr-greenhouse"),
        ]
        keep, left_out, _ = build_xlsx.partition(programs, self.SOURCES)
        self.assertEqual(len(keep) + len(left_out), len(programs))
        self.assertEqual([r["name"] for r in keep], ["applyable"])
        reasons = {r["name"]: r["why"] for r in left_out}
        self.assertIn("cannot apply", reasons["ruled out"])
        self.assertIn("cannot apply", reasons["dead"])
        self.assertIn("aqr-greenhouse", reasons["watched"])
        for row in left_out:
            self.assertTrue(row["why"], "every exclusion carries a stated reason")

    def test_14_10f_actionable_rows_sort_first(self):
        programs = [self._program(name="c", eligible="LATER"),
                    self._program(name="a", eligible="YES"),
                    self._program(name="b", eligible="CHECK")]
        keep, _, _ = build_xlsx.partition(programs, self.SOURCES)
        self.assertEqual([r["eligible"] for r in keep], ["YES", "CHECK", "LATER"])


class ClassifierSpendTests(_IsolatedState, unittest.TestCase):
    """The run reports its own bill.

    Before this, answering "why did 2026-09-12 cost 34 cents" meant reconstructing the
    prompts from the snapshot commits and solving backwards from the Azure portal. That
    reconstruction established that 81% of the spend was reasoning tokens -- a fact
    nothing in the digest would have surfaced on its own, which is exactly why the cost
    drifted unnoticed in the first place.
    """

    class _Details:
        def __init__(self, reasoning=0):
            self.reasoning_tokens = reasoning

    class _PromptDetails:
        def __init__(self, cached=0):
            self.cached_tokens = cached

    class _Usage:
        def __init__(self, prompt, completion, reasoning=0, cached=0):
            self.prompt_tokens = prompt
            self.completion_tokens = completion
            self.completion_tokens_details = ClassifierSpendTests._Details(reasoning)
            self.prompt_tokens_details = ClassifierSpendTests._PromptDetails(cached)

    def setUp(self):
        super().setUp()
        classify.reset_usage()

    def tearDown(self):
        classify.reset_usage()

    def _record(self, usage):
        classify._record_usage(
            classify.AZURE, "gpt-5-mini", "minimal", usage,
            input_key="prompt_tokens", output_key="completion_tokens",
            reasoning=getattr(usage.completion_tokens_details, "reasoning_tokens", 0),
            cached=getattr(usage.prompt_tokens_details, "cached_tokens", 0),
        )

    def test_no_calls_means_no_line(self):
        self.assertIsNone(classify.usage_line(), "a run that classified nothing")

    def test_it_reproduces_the_2026_09_12_bill(self):
        """The measured shape of that run: 245,112 in, ~138,167 out, $0.34."""
        self._record(self._Usage(245_112, 138_167, reasoning=132_000))
        self.assertAlmostEqual(classify.USAGE.estimated_usd(), 0.3376, places=3)
        line = classify.usage_line()
        self.assertIn("245,112 in", line)
        self.assertIn("132,000 of it reasoning", line)
        self.assertIn("effort=minimal", line)

    def test_cached_input_bills_at_a_tenth(self):
        self._record(self._Usage(1_000_000, 0, cached=1_000_000))
        self.assertAlmostEqual(classify.USAGE.estimated_usd(), 0.025, places=4)

    def test_an_unpriced_model_says_so_rather_than_guessing(self):
        classify._record_usage(
            classify.ANTHROPIC, "claude-opus-5", "low",
            self._Usage(1000, 500), input_key="prompt_tokens",
            output_key="completion_tokens",
        )
        self.assertIsNone(classify.USAGE.estimated_usd())
        self.assertIn("unpriced", classify.usage_line())

    def test_a_missing_usage_block_is_an_undercount_not_a_free_run(self):
        """Spec 10.1 again: absent data must not read as zero."""
        self._record(self._Usage(1000, 500))
        classify._record_usage(
            classify.AZURE, "gpt-5-mini", "minimal", None,
            input_key="prompt_tokens", output_key="completion_tokens",
        )
        line = classify.usage_line()
        self.assertEqual(classify.USAGE.calls, 2)
        self.assertIn("undercount", line)
        self.assertIn("2 calls", line)

    def test_the_spend_line_reaches_the_digest(self):
        self._record(self._Usage(245_112, 138_167, reasoning=132_000))
        _, body = digest.render([], [], {})
        self.assertIn("classifier: 1 call", body)
        self.assertIn("~$0.33", body)


class MalformedPayloadTests(_IsolatedState, unittest.TestCase):
    """HTTP 200 with a broken body must not read as an empty board.

    `test_14_9h` already covers the busy-board case: a board that drops from N rows to
    zero is refused. That guard infers trouble from the row count, so it cannot fire on
    a board whose previous snapshot was legitimately empty -- and 13 of the watched
    boards sit at zero student postings on an ordinary day. For those, an error wearing
    an empty board's clothes looked exactly like a quiet morning.
    """

    SRC = {"source_id": "quiet", "method": "greenhouse", "url": "s", "program_names": ""}

    def _baseline_of_zero_rows(self):
        """The dangerous starting state: a real, healthy, empty board."""
        first = job_boards.check(self.SRC, FakeJSONClient({"jobs": []}))
        self.assertTrue(first.ok, first.error)
        self.assertEqual(first.extra["rows"], 0)
        store.write_snapshot("quiet", first.snapshot_text, ext="tsv")

    def test_an_error_key_beside_an_empty_list_is_a_failure(self):
        """Greenhouse's actual rate-limit shape."""
        self._baseline_of_zero_rows()
        r = job_boards.check(
            self.SRC, FakeJSONClient({"error": "rate limited", "jobs": []})
        )
        self.assertFalse(r.ok, "an error payload must never read as a quiet board")
        self.assertIn("rate limited", r.error)

    def test_a_genuinely_empty_board_is_still_a_quiet_day(self):
        """The guard must not cost us the true negative it is wrapped around."""
        self._baseline_of_zero_rows()
        r = job_boards.check(self.SRC, FakeJSONClient({"jobs": []}))
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.changes, [])

    def test_a_missing_collection_key_is_a_failure(self):
        self._baseline_of_zero_rows()
        r = job_boards.check(self.SRC, FakeJSONClient({"meta": {"total": 0}}))
        self.assertFalse(r.ok)
        self.assertIn("no 'jobs' key", r.error)

    def test_a_non_list_collection_is_a_failure(self):
        self._baseline_of_zero_rows()
        r = job_boards.check(self.SRC, FakeJSONClient({"jobs": "temporarily unavailable"}))
        self.assertFalse(r.ok)
        self.assertIn("not a list", r.error)

    def test_junk_list_members_are_a_failure(self):
        self._baseline_of_zero_rows()
        r = job_boards.check(self.SRC, FakeJSONClient({"jobs": [1, 2, 3]}))
        self.assertFalse(r.ok)
        self.assertIn("non-object member", r.error)

    def test_lever_gets_the_same_treatment_on_its_bare_array(self):
        src = {"source_id": "lev", "method": "lever", "url": "s", "program_names": ""}
        r = job_boards.check(src, FakeJSONClient({"error": "gone"}))
        self.assertFalse(r.ok)
        self.assertIn("expected a JSON array", r.error)

    def test_an_amazon_style_null_error_is_not_an_error(self):
        """`"error": null` ships on healthy responses; only a truthy value counts."""
        r = job_boards.check(
            self.SRC, FakeJSONClient({"error": None, "jobs": [_gh("SWE Intern")]})
        )
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.extra["rows"], 1)


class DeterministicScreenTests(_IsolatedState, unittest.TestCase):
    """screen.py may only rule OUT, only on a quoted phrase, and never on silence.

    Validated against the 2026-09-12 run: 77 changes the model judged, 40 of which it
    ruled out. The screen reproduces 28 of those 40 with **zero** wrong rule-outs among
    the 37 the model kept. Recall is negotiable -- a miss costs one model call. Precision
    is not -- a wrong rule-out costs a real opportunity, so every phrase below that
    produced a false positive in the first draft is pinned here as a test.
    """

    def assertRuledOut(self, text, rule, title="Software Engineer Intern"):
        v = screen.screen(title, text)
        self.assertIsNotNone(v, f"should have ruled out: {text!r}")
        self.assertEqual(v.rule, rule)
        return v

    def assertDeferred(self, text, title="Software Engineer Intern"):
        v = screen.screen(title, text)
        self.assertIsNone(v, f"must defer to the model, not rule out: {text!r}\n{v}")

    # --- the precision guards: real phrases that the first draft got wrong ---------

    def test_or_later_is_an_open_window_and_includes_a_2030_graduate(self):
        """Qualcomm, 2026-09-12. The first draft ruled this out. It should not."""
        self.assertDeferred("Expected graduation date of November 2027 or later.")

    def test_and_beyond_is_an_open_window(self):
        """Mastercard, 2026-09-12."""
        self.assertDeferred(
            "Currently pursuing a Bachelor's degree in Computer Science, Engineering, "
            "or a related field graduating December 2027 and beyond"
        )

    def test_graduating_after_a_year_is_an_open_window(self):
        """Northrop Grumman, 2026-09-12."""
        self.assertDeferred(
            "Must be pursuing an undergraduate or graduate degree from an accredited "
            "college or university and graduating after August 2027."
        )

    def test_an_internship_season_is_not_a_graduation_year(self):
        """The trap the whole module is built around: a Summer 2027 posting says
        2027 everywhere without saying anything about when you graduate."""
        self.assertDeferred(
            "Software Engineer Intern, Summer 2027. This internship runs June 2027 "
            "through August 2027 and is based in New York."
        )

    def test_a_window_naming_an_acceptable_year_does_not_rule_out(self):
        self.assertDeferred("Graduating between December 2027 and June 2030.")

    def test_a_range_that_includes_sophomores_does_not_rule_out(self):
        self.assertDeferred(
            "Open to rising sophomores, juniors and seniors enrolled full time."
        )

    def test_bachelors_alongside_masters_does_not_rule_out(self):
        self.assertDeferred(
            "Pursuing a Bachelor's, Master's or PhD in Computer Science or related."
        )

    def test_no_posting_text_never_rules_out(self):
        """Absence of evidence is never evidence -- the same instruction the prompt
        gives the model when a posting fetch failed."""
        self.assertDeferred("")
        self.assertDeferred("   \n  ")

    def test_it_can_only_rule_out_never_in(self):
        """There is no affirmative verdict. The only outcomes are a rule-out or None."""
        v = screen.screen("Quantitative Trading Intern", "Open to all undergraduates.")
        self.assertIsNone(v)
        self.assertFalse(hasattr(screen.Verdict("x", "y"), "relevant"))

    # --- the rule-outs, each a real phrase from the validation set -----------------

    def test_an_explicit_excluding_graduation_year(self):
        v = self.assertRuledOut(
            "Expected graduation date: 2027 or 2028.", "graduation-window"
        )
        self.assertIn("2027", v.why, "the reason must quote the evidence")

    def test_a_december_2027_to_summer_2028_window(self):
        self.assertRuledOut(
            "with a graduation date between December 2027 and Summer 2028",
            "graduation-window",
        )

    def test_degree_by_a_date_this_owner_cannot_meet(self):
        self.assertRuledOut(
            "Scheduled to obtain a Bachelor's degree in Computer Science by Summer 2028.",
            "graduation-window",
        )

    def test_junior_level_standing(self):
        self.assertRuledOut(
            "Must be entering junior-level standing by the internship start date.",
            "advanced-standing",
        )

    def test_rising_senior(self):
        self.assertRuledOut("Applicants must be a rising senior.", "advanced-standing")

    def test_graduate_students_only(self):
        self.assertRuledOut(
            "Pursuing a Master's or Ph.D. degree in Computer Science, Data Science, "
            "or a related technical discipline.",
            "graduate-only",
        )

    def test_already_holds_the_degree(self):
        self.assertRuledOut(
            "Must have graduated with a bachelor's degree from an accredited "
            "college or university.",
            "already-graduated",
        )

    # --- title screen (prompt rule 7) ---------------------------------------------

    def test_a_plainly_out_of_field_title(self):
        v = screen.screen("Human Resources Intern", "")
        self.assertIsNotNone(v)
        self.assertEqual(v.rule, "out-of-field-title")

    def test_a_technical_word_rescues_an_out_of_field_title(self):
        """Rule 7's own caveat: when a role is technical at all, keep it."""
        self.assertDeferred("", title="Marketing Data Scientist Intern")
        self.assertDeferred("", title="Recruiting Software Engineer Intern")

    def test_an_ordinary_technical_title_is_untouched(self):
        self.assertDeferred("", title="Quantitative Research Intern")


class ScreenIntegrationTests(_IsolatedState, unittest.TestCase):
    """The screen and the classifier wired together, with no provider behind them.

    These assert that a screened change reaches a verdict *without* a model call, so
    the moment a screen rule stops matching they would fall through and bill a real
    one -- a test that silently starts spending money on the day it starts being
    wrong. Forcing select_provider to None makes that fall-through fail instead.
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, classify, "select_provider", classify.select_provider)
        classify.select_provider = lambda: None
        classify.reset_screen()

    def test_a_screened_change_is_ruled_out_with_no_model_call(self):
        change = models.Change(
            source_id="b", kind="added", key="SWE Intern", detail="x",
            posting_text="Expected graduation date: 2027 or 2028.",
        )
        judgments = classify.classify([change], {"b": {"source_id": "b", "signal": "high"}})
        self.assertEqual(len(judgments), 1)
        self.assertFalse(judgments[0].relevant)
        self.assertTrue(judgments[0].classified, "a quoted phrase is a real judgment")
        self.assertEqual(judgments[0].screen_rule, "graduation-window")
        self.assertEqual(classify.USAGE.calls, 0, "no model call may have been made")

    def test_the_screen_reports_what_it_removed(self):
        change = models.Change(
            source_id="b", kind="added", key="SWE Intern", detail="x",
            posting_text="Must be a rising senior.",
        )
        classify.classify([change], {"b": {"source_id": "b", "signal": "high"}})
        line = classify.screen_line()
        self.assertIn("1 of 1", line)
        self.assertIn("advanced-standing", line)
        _, body = digest.render([], [], {})
        self.assertIn("advanced-standing", body, "HEALTH must carry it")


class CircuitBreakerTests(_IsolatedState, unittest.TestCase):
    """A source that keeps failing backs off, and says so.

    Measured 2026-09-12: nine sources sat at exactly three consecutive failures with
    an empty `last_success` -- never worked once -- and each was fetched three times
    every morning indefinitely. The breaker stops the refetch. What it must NOT stop is
    the reporting: a quarantined source is the strongest form of "blind, not quiet".
    """

    NOW = datetime.datetime(2026, 9, 12, 12, 0, tzinfo=datetime.timezone.utc)

    def test_the_backoff_schedule(self):
        self.assertEqual(breaker.quarantine_hours(0), 0)
        self.assertEqual(breaker.quarantine_hours(2), 0, "below the threshold, keep trying")
        self.assertEqual(breaker.quarantine_hours(3), 6)
        self.assertEqual(breaker.quarantine_hours(4), 12)
        self.assertEqual(breaker.quarantine_hours(5), 24)
        self.assertEqual(breaker.quarantine_hours(6), 48)
        self.assertEqual(breaker.quarantine_hours(7), 72)
        self.assertEqual(breaker.quarantine_hours(40), 72, "the cap holds; boards come back")

    def test_a_healthy_source_is_never_skipped(self):
        skip, _ = breaker.quarantine_state(
            {"consecutive_failures": "0", "last_attempt": self.NOW.isoformat()}, self.NOW
        )
        self.assertFalse(skip)

    def test_a_source_inside_its_window_is_skipped(self):
        src = {
            "consecutive_failures": "3",
            "last_attempt": (self.NOW - datetime.timedelta(hours=1)).isoformat(),
            "last_success": "2026-09-01T00:00:00+00:00",
        }
        skip, why = breaker.quarantine_state(src, self.NOW)
        self.assertTrue(skip)
        self.assertIn("quarantined for 6h", why)

    def test_an_expired_window_is_retried_with_no_human_involved(self):
        src = {
            "consecutive_failures": "3",
            "last_attempt": (self.NOW - datetime.timedelta(hours=7)).isoformat(),
        }
        skip, _ = breaker.quarantine_state(src, self.NOW)
        self.assertFalse(skip, "6h window, 7h ago: it must be retried")

    def test_a_source_that_never_succeeded_says_to_check_the_url(self):
        """All nine of the real quarantine candidates are in this state, and a URL
        that never worked is a configuration bug rather than an outage."""
        src = {
            "consecutive_failures": "3",
            "last_attempt": self.NOW.isoformat(),
            "last_success": "",
        }
        _, why = breaker.quarantine_state(src, self.NOW)
        self.assertIn("never succeeded", why)
        self.assertIn("check the URL", why)

    def test_no_attempt_on_record_means_try_it(self):
        skip, _ = breaker.quarantine_state({"consecutive_failures": "9"}, self.NOW)
        self.assertFalse(skip, "nothing says a window has started")

    def test_a_corrupt_timestamp_fails_open(self):
        """A breaker that silently stops fetching on bad data is the worst outcome."""
        skip, _ = breaker.quarantine_state(
            {"consecutive_failures": "5", "last_attempt": "not a date"}, self.NOW
        )
        self.assertFalse(skip)

    def test_a_quarantined_run_does_not_inflate_its_own_counter(self):
        """If skipping counted as failing, a source would back off further for not
        having been looked at, and 6h would become 72h without new evidence."""
        sources = [{
            "source_id": "s", "consecutive_failures": "3",
            "last_success": "", "last_attempt": "2026-09-12T00:00:00+00:00",
        }]
        check.update_source_state(
            sources, [models.SourceResult(source_id="s", ok=False, quarantined=True)]
        )
        self.assertEqual(sources[0]["consecutive_failures"], "3", "must not increment")
        self.assertEqual(sources[0]["last_attempt"], "2026-09-12T00:00:00+00:00")

    def test_a_real_attempt_records_last_attempt(self):
        sources = [{"source_id": "s", "consecutive_failures": "0", "last_attempt": ""}]
        check.update_source_state(
            sources, [models.SourceResult(source_id="s", ok=False, error="HTTP 500")]
        )
        self.assertEqual(sources[0]["consecutive_failures"], "1")
        self.assertTrue(sources[0]["last_attempt"], "an attempt must start the window")

    def test_a_quarantined_source_is_still_reported_and_still_escalates(self):
        """The breaker changes fetch policy, never reporting policy (spec 10.1)."""
        result = models.SourceResult(
            source_id="gts-careers", ok=False, quarantined=True,
            error="quarantined for 6h after 3 consecutive failures",
        )
        source = {"source_id": "gts-careers", "consecutive_failures": "3", "last_success": ""}
        title, body = digest.render([], [result], {"gts-careers": source})
        self.assertIn("not fetched at all", body)
        self.assertIn("gts-careers", body)
        self.assertIn("SOURCE BLIND", body, "still escalated into the table")
        self.assertIn("source failing", title)


class AggregatorRowRenderingTests(_IsolatedState, unittest.TestCase):
    """An aggregator row names the real employer; the table must show that employer.

    `program_names` for a Simplify or zshah row is the list's own name, identical on
    all 500+ of its rows, so using it as the Company column makes every row look like
    it came from the same place. The employer is in the row key, as a markdown link.
    """

    def _row(self, key, program="SimplifyJobs Summer 2027", url="https://x.test/1"):
        j = classify.Judgment(
            change=models.Change(source_id="simplify-2027", kind="added", key=key,
                                detail="x", url=url),
            program_name=program, relevant=True, classified=True, confidence="high",
        )
        _, body = digest.render([j], [], {})
        return next(l for l in body.splitlines() if l.startswith("| Worth"))

    def test_the_linked_employer_becomes_the_company_column(self):
        row = self._row("[InfiniteQuant](https://simplify.jobs/c/InfiniteQuant) / "
                        "Quantitative Developer Intern @ NYC")
        self.assertIn("| InfiniteQuant |", row)
        self.assertIn("Quantitative Developer Intern @ NYC", row)
        self.assertNotIn("SimplifyJobs", row, "the aggregator is not the employer")

    def test_a_markdown_link_is_never_left_half_escaped(self):
        """The bug this fixes rendered the URL twice and parenthesised the employer."""
        row = self._row("[Lyft](https://simplify.jobs/c/Lyft) / SWE Intern @ SF")
        self.assertNotIn("(Lyft)", row)
        self.assertEqual(row.count("https://"), 1, row)

    def test_a_slash_in_a_job_title_is_not_read_as_an_employer(self):
        """Job-board keys are "Title @ Location". Splitting every key on " / " would
        turn "Software Engineer / Backend Intern" into a company."""
        row = self._row("Software Engineer / Backend Intern @ NYC", program="Jane Street")
        self.assertIn("| Jane Street |", row)
        self.assertIn("Software Engineer / Backend Intern", row)


class RedirectDetectionTests(_IsolatedState, unittest.TestCase):
    """Where we landed is part of whether the fetch succeeded.

    Both HTTP clients are built with `follow_redirects=True` and, until this, nothing
    in the codebase read `response.url`. So a page that had been retired could 302 to a
    friendly error page, return HTTP 200, and clear both content floors on the error
    page's own prose. Measured 2026-09-12, two sources were doing exactly that:
    `aqr-internship-program` had been reporting success from `aqr.com/404` (4,683 chars
    at ratio 0.0929), and `twosigma-campus` had silently moved from the students page
    to the generic careers page.

    Calibration matters as much as detection. Of the 65 watched pages, 5 end up at a
    different URL and 2 of those differ only cosmetically, so a naive check would have
    been 40% noise.
    """

    PAGE = "<html><body>" + ("<p>Real careers content here for the floors.</p>" * 40) + "</body></html>"

    def _src(self, url, **kw):
        base = {"source_id": "p", "url": url, "render_js": "false",
                "selector": "", "program_names": "Test Page"}
        base.update(kw)
        return base

    def test_the_aqr_case_a_redirect_to_404_is_a_failure(self):
        r = page_watch.check(
            self._src("https://www.aqr.com/About-Us/Our-Internship-Program"),
            FakeHTMLClient(self.PAGE, final_url="https://www.aqr.com/404"),
        )
        self.assertFalse(r.ok, "an error page must not read as success")
        self.assertIn("error page", r.error)
        self.assertIn("aqr.com/404", r.error)

    def test_the_two_sigma_case_a_path_change_warns_but_does_not_fail(self):
        """The fetch worked and the text is real -- calling it a failed fetch would be
        a lie. But the row is no longer watching what its program_names claims."""
        r = page_watch.check(
            self._src("https://www.twosigma.com/careers/students/"),
            FakeHTMLClient(self.PAGE, final_url="https://www.twosigma.com/careers/"),
        )
        self.assertTrue(r.ok, r.error)
        self.assertIn("redirected to a different path", r.extra["redirected"])

    def test_a_warned_redirect_reaches_HEALTH_every_morning(self):
        r = page_watch.check(
            self._src("https://www.twosigma.com/careers/students/", source_id="twosigma-campus"),
            FakeHTMLClient(self.PAGE, final_url="https://www.twosigma.com/careers/"),
        )
        _, body = digest.render([], [r], {"twosigma-campus": {"source_id": "twosigma-campus"}})
        self.assertIn("twosigma-campus", body)
        self.assertIn("no longer watching the page it was configured for", body)

    def test_a_www_only_difference_is_silent(self):
        """osqf.org -> www.osqf.org. Real, and worth nothing."""
        r = page_watch.check(
            self._src("https://osqf.org/"),
            FakeHTMLClient(self.PAGE, final_url="https://www.osqf.org/"),
        )
        self.assertTrue(r.ok, r.error)
        self.assertNotIn("redirected", r.extra)

    def test_a_trailing_slash_difference_is_silent(self):
        """www.tower-research.com/open-positions/ -> tower-research.com/open-positions/"""
        r = page_watch.check(
            self._src("https://www.tower-research.com/open-positions/"),
            FakeHTMLClient(self.PAGE, final_url="https://tower-research.com/open-positions"),
        )
        self.assertTrue(r.ok, r.error)
        self.assertNotIn("redirected", r.extra)

    def test_no_redirect_at_all_is_silent(self):
        r = page_watch.check(
            self._src("https://example.invalid/careers"), FakeHTMLClient(self.PAGE)
        )
        self.assertTrue(r.ok, r.error)
        self.assertNotIn("redirected", r.extra)

    def test_the_verdict_helper_recognises_several_error_page_shapes(self):
        for final in ("https://x.test/404", "https://x.test/not-found",
                      "https://x.test/page-not-found/", "https://x.test/error"):
            sev, _ = redirect.verdict("https://x.test/careers", final)
            self.assertEqual(sev, "fail", final)

    def test_a_query_only_change_does_not_warn(self):
        """optiver-students keeps its ?level=student through the redirect; the path is
        what identifies the page."""
        sev, _ = redirect.verdict(
            "https://optiver.com/careers/?level=student",
            "https://optiver.com/careers?level=student&utm=x",
        )
        self.assertEqual(sev, "")


class MutedProgramTests(_IsolatedState, unittest.TestCase):
    """The muted column has to actually mute, and has to say that it did.

    It did neither until 2026-09-12. `program_names` in sources.csv is "|"-separated
    and the filter compared the whole field against a single programme name, so muting
    had no effect on any source covering more than one programme. And the filter
    reported through nothing at all, which is how it went unnoticed that it was not
    filtering -- the two halves of the same failure.
    """

    def _run(self, program_name, muted_names):
        """Through the real filter, not a copy of it.

        This used to reimplement `_all_muted` inline, which meant it was asserting
        against a second copy of the logic -- so the bug it documents (comparing the
        whole "|"-separated field against one name) could have come back in the shipped
        path while these five tests stayed green.
        """
        programs = [
            {"name": n, "muted": "true", "status": "unknown", "last_checked": "",
             "last_changed": "", "snapshot_hash": ""}
            for n in muted_names
        ]
        changes = [models.Change(source_id="s", kind="added", key="Row",
                                 detail="x", program_name=program_name)]
        kept, reports = suppress.suppress(
            changes, muted=suppress.muted_programmes(programs), applied={})
        removed = suppress.removed_by(reports, suppress.MUTED)
        self.assertEqual(removed, len(changes) - len(kept),
                         "the filter's own report must match what it actually dropped")
        return removed

    def test_a_single_muted_programme_is_muted(self):
        self.assertEqual(self._run("Jane Street FTTP", ["Jane Street FTTP"]), 1)

    def test_a_multi_programme_source_is_muted_when_all_are_muted(self):
        """The case that silently never worked."""
        self.assertEqual(
            self._run("Jane Street FTTP|Jane Street INSIGHT",
                      ["Jane Street FTTP", "Jane Street INSIGHT"]),
            1,
        )

    def test_muting_one_programme_does_not_silence_the_others_on_that_row(self):
        """Muting INSIGHT must not also silence FTTP news arriving on the same row."""
        self.assertEqual(
            self._run("Jane Street FTTP|Jane Street INSIGHT", ["Jane Street INSIGHT"]),
            0,
        )

    def test_a_source_with_no_programme_is_never_muted(self):
        """An empty program_names is a whole job board, not a muted programme."""
        self.assertEqual(self._run("", ["Jane Street FTTP"]), 0)

    def test_the_muted_count_reaches_HEALTH(self):
        _, body = digest.render([], [], {}, suppressed_muted=3)
        self.assertIn("3 item(s) were hidden", body)
        self.assertIn("muted=true", body)


class EightfoldTests(_IsolatedState, unittest.TestCase):
    """Millennium's campus board was the largest hole the 2026-09-12 audit found.

    `millennium-students` watched a careers page whose own note asserted "Millennium is
    on no public ATS", while campusjobs.mlp.com answers our honest User-Agent with 59
    campus postings. 17 of them are US.
    """

    SRC = {"source_id": "mlp", "method": "eightfold",
           "url": "https://campusjobs.mlp.com/api/apply/v2/jobs?domain=mlp.com",
           "program_names": ""}

    def _page(self, positions, count):
        return {"count": count, "positions": positions}

    def _pos(self, name, location="New York, New York, United States of America"):
        return {"name": name, "location": location, "department": "Trading",
                "business_unit": "Equities", "type": "ATS",
                "canonicalPositionUrl": f"https://mlp.eightfold.ai/careers/job/{abs(hash(name)) % 10**12}",
                "job_description": ""}

    class Client:
        """Paginates like Eightfold: ten at a time, `num` ignored."""

        def __init__(self, positions, count=None, page=10):
            self.positions, self.count, self.page = positions, count, page
            self.calls = 0

        def get(self, url, *args, params=None, **kwargs):
            self.calls += 1
            start = (params or {}).get("start", 0)
            batch = self.positions[start:start + self.page]
            return FakeResponse(
                payload={"count": self.count if self.count is not None else len(self.positions),
                         "positions": batch},
                url=url,
            )

    def test_it_pages_until_the_count_is_reached(self):
        positions = [self._pos(f"2027 Quantitative Researcher Intern {i}") for i in range(59)]
        client = self.Client(positions)
        r = job_boards.check(self.SRC, client)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.extra["postings"], 59)
        self.assertEqual(r.extra["rows"], 59)
        self.assertEqual(client.calls, 6, "59 postings at ten a page")

    def test_non_us_postings_are_dropped_and_counted_by_the_shared_screen(self):
        """No private filter inside the fetcher: the campus board needs none, and a
        second screen here is the duplication that already caused one bug."""
        positions = [
            self._pos("2027 Quantitative Researcher Intern, Austin"),
            self._pos("2027 Applied AI Engineer Intern", "London, United Kingdom"),
            self._pos("2027 Sector Specialist Intern", "Dubai, United Arab Emirates"),
        ]
        r = job_boards.check(self.SRC, self.Client(positions))
        self.assertEqual(r.extra["rows"], 1)
        self.assertEqual(r.extra["suppressed_not_us"], 2)

    def test_a_partial_page_through_is_refused_rather_than_half_read(self):
        """Same refusal as Workday and Phenom: half a board that looks healthy is
        worse than a board that reports itself broken."""
        positions = [self._pos(f"2027 Intern {i}") for i in range(10)]
        r = job_boards.check(self.SRC, self.Client(positions, count=59))
        self.assertFalse(r.ok)
        self.assertIn("silently partial", r.error)

    def test_the_url_comes_from_the_canonical_position_url(self):
        positions = [self._pos("2027 Quantitative Developer Intern")]
        r = job_boards.check(self.SRC, self.Client(positions))
        self.assertIn("mlp.eightfold.ai/careers/job/", r.snapshot_text)

    def test_the_type_field_is_not_mistaken_for_an_employment_type(self):
        """Every Eightfold row says type="ATS", which is not an employment type."""
        r = job_boards.check(self.SRC, self.Client([self._pos("2027 Intern")]))
        self.assertNotIn("Type=ATS", r.snapshot_text)

    def test_an_error_payload_is_refused(self):
        class Broken:
            def get(self, url, *a, **k):
                return FakeResponse(payload={"error": "rate limited", "positions": []}, url=url)
        r = job_boards.check(self.SRC, Broken())
        self.assertFalse(r.ok)
        self.assertIn("rate limited", r.error)


class AtsProbeTests(_IsolatedState, unittest.TestCase):
    """The probe that finds the real board behind a watched careers page.

    Validated against the live watchlist: from an empty knowledge base it
    rediscovers 11 of the 11 boards the 2026-09-12 hand audit found. These tests pin
    the shapes that made that possible, and the false positives that made the
    documented technique untrustworthy.
    """

    def setUp(self):
        super().setUp()
        # The probe sleeps REQUEST_DELAY_SECONDS between fetches to be polite to real
        # hosts. Against fakes that is 12 seconds of the suite doing nothing, and a
        # slow suite is a suite that stops being run.
        original = paths.REQUEST_DELAY_SECONDS
        paths.REQUEST_DELAY_SECONDS = 0.0
        self.addCleanup(setattr, paths, "REQUEST_DELAY_SECONDS", original)

    class Pages:
        """Serves a fixed body per URL, and 404s anything else."""

        def __init__(self, bodies):
            self.bodies = bodies
            self.fetched = []

        def get(self, url, *args, **kwargs):
            self.fetched.append(url)
            if url in self.bodies:
                return FakeResponse(text=self.bodies[url], status=200, url=url)
            return FakeResponse(text="", status=404, url=url)

    PAGE = "https://firm.test/careers/"

    def _src(self, url=None, source_id="firm-careers", method="page_text"):
        return [{"source_id": source_id, "method": method, "url": url or self.PAGE,
                 "program_names": "", "signal": "low"}]

    def _mine(self, bodies, known=frozenset(), sources=None):
        client = self.Pages(bodies)
        found, notes = discover.mine_pages(
            sources or self._src(), client, set(known), deadline=time.monotonic() + 60
        )
        return found, notes, client

    # --- the false positives that made the documented technique untrustworthy -----

    def test_the_word_leverage_is_not_a_lever_board(self):
        """Measured: CLAUDE.md's bare-keyword grep produced 10 false positives out of
        24 hits across the 65 watched pages, nine of them on "leverage" alone."""
        found, _, _ = self._mine({self.PAGE: "<p>We leverage leveraged Leverage.</p>"})
        self.assertEqual(found, [])

    def test_an_hr_workday_disclosure_is_not_a_job_board(self):
        """brevanhoward-students matched only on "our HR Workday system"."""
        found, notes, _ = self._mine({self.PAGE: "<p>Data is held in our HR Workday system.</p>"})
        self.assertEqual(found, [])
        self.assertEqual(notes, [], "a prose mention must not raise a vendor note either")

    # --- the shapes that had to work -------------------------------------------

    def test_a_plain_board_link(self):
        found, _, _ = self._mine(
            {self.PAGE: '<a href="https://boards.greenhouse.io/acmecapital">Jobs</a>'}
        )
        self.assertEqual([c.key for c in found], ["greenhouse:acmecapital"])

    def test_a_greenhouse_embed_script(self):
        """Verition's and Eclipse's slugs appear only in a grnhse_app embed."""
        found, _, _ = self._mine(
            {self.PAGE: '<script src="https://x/grnhse_app.js?for=veritiongroupllc"></script>'}
        )
        self.assertEqual([c.key for c in found], ["greenhouse:veritiongroupllc"])

    def test_the_api_host(self):
        """Graham's slug appears nowhere but a boards-api URL, and "boards-api" is
        not "boards"."""
        found, _, _ = self._mine({
            self.PAGE: 'fetch("https://boards-api.greenhouse.io/v1/boards/'
                       'grahamcapitalmanagement/jobs?content=true")'
        })
        self.assertEqual([c.key for c in found], ["greenhouse:grahamcapitalmanagement"])

    def test_it_follows_one_hop_to_an_all_jobs_page(self):
        """The owner's actual description: "click a couple more buttons". Probing only
        the watched page found 8 of 11; this is what recovered the other three."""
        found, _, client = self._mine({
            self.PAGE: '<a href="/all-jobs/">See all jobs</a>',
            "https://firm.test/all-jobs/": '<a href="https://jobs.ashbyhq.com/deepfirm">Apply</a>',
        })
        self.assertEqual([c.key for c in found], ["ashby:deepfirm"])
        self.assertIn("https://firm.test/all-jobs/", client.fetched)

    def test_it_does_not_follow_offsite_links(self):
        """Following off-site links turns a probe of our own watchlist into a crawler."""
        found, _, client = self._mine({
            self.PAGE: '<a href="https://elsewhere.test/open-positions">Jobs</a>',
            "https://elsewhere.test/open-positions": '<a href="https://jobs.lever.co/nope">x</a>',
        })
        self.assertEqual(found, [])
        self.assertNotIn("https://elsewhere.test/open-positions", client.fetched)

    def test_the_hop_budget_is_bounded(self):
        links = "".join(f'<a href="/open-positions-{i}">j</a>' for i in range(20))
        _, _, client = self._mine({self.PAGE: links})
        self.assertLessEqual(len(client.fetched), 1 + discover.MAX_HOPS_PER_PAGE)

    # --- hygiene ----------------------------------------------------------------

    def test_url_furniture_is_never_proposed_as_a_board(self):
        """Without this, every Greenhouse-embedding page proposes a board called
        "embed"."""
        found, _, _ = self._mine(
            {self.PAGE: '<script src="https://boards.greenhouse.io/embed/job_board/js?for=realslug">'}
        )
        self.assertEqual([c.key for c in found], ["greenhouse:realslug"])

    def test_a_board_already_watched_is_not_proposed_again(self):
        found, _, _ = self._mine(
            {self.PAGE: '<a href="https://boards.greenhouse.io/acmecapital">x</a>'},
            known={"greenhouse:acmecapital"},
        )
        self.assertEqual(found, [])

    def test_an_unsupported_vendor_is_reported_not_proposed(self):
        """Finding one still means the firm HAS a real board and the page_text row is
        pointed at the wrong thing -- but we cannot watch it yet, so it goes to a
        human instead of into a proposal."""
        found, notes, _ = self._mine(
            {self.PAGE: '<script src="https://cdn.phenompeople.com/x.js"></script>'}
        )
        self.assertEqual(found, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("phenom fingerprint", notes[0])

    def test_only_page_text_sources_are_probed(self):
        _, _, client = self._mine(
            {self.PAGE: "x"}, sources=self._src(method="greenhouse")
        )
        self.assertEqual(client.fetched, [], "an ATS row is already pointed at its board")

    def test_an_unreadable_page_is_silent(self):
        """page_watch already shouts about a page it cannot read; the probe adding a
        second alarm would be noise."""
        found, notes, _ = self._mine({})
        self.assertEqual(found, [])
        self.assertEqual(notes, [])


class LiveDataInvariantTests(unittest.TestCase):
    """The only class that reads the committed data/ tree, and the reason the isolation
    rule can be stated as "everything except this one".

    These two assert things about the real database rather than about a fixture, which
    is the point of them: a method typo'd in sources.csv goes unwatched in production
    and nothing else would notice, and the owner's workbook is only useful if it builds
    from the 187 rows that actually exist. They read and never write -- the module
    fingerprint guard enforces that, for these as much as for everything else.
    """

    def test_14_9_every_method_in_sources_csv_has_a_handler(self):
        """A typo'd method must be caught here rather than going unwatched in prod."""
        methods = {(r.get("method") or "").strip() for r in store.read_sources()}
        unknown = methods - set(check.CHECKERS) - {check.UNWATCHED}
        self.assertEqual(unknown, set(), f"sources.csv has unhandled methods: {unknown}")

    def test_14_10g_both_workbooks_build_and_the_owner_file_excludes_ruled_out_rows(self):
        from openpyxl import load_workbook
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            saved = (paths.OUT_XLSX, paths.OUT_TRACKED_XLSX)
            paths.OUT_XLSX = root / "programs.xlsx"
            paths.OUT_TRACKED_XLSX = root / "tracked.xlsx"
            try:
                built = build_xlsx.build()
                self.assertEqual(len(built), 2)
                for path in built:
                    self.assertTrue(path.exists(), path)
                owner = load_workbook(paths.OUT_XLSX)
                self.assertEqual(owner.worksheets[0].title, "Check By Hand")
                self.assertIn("Left Out", owner.sheetnames)
                codes = {
                    str(row[0]).strip().upper()
                    for row in owner["Check By Hand"].iter_rows(min_row=2, values_only=True)
                }
                self.assertNotIn("NO", codes)
                self.assertNotIn("STALE", codes)
                tracked = load_workbook(paths.OUT_TRACKED_XLSX)
                self.assertEqual(
                    tracked.sheetnames,
                    ["All Programs", "Sources", "Applied", "Discovered"])
                # the full list still exists somewhere -- it moved, it was not dropped
                self.assertEqual(
                    tracked["All Programs"].max_row - 1, len(store.read_programs()))
            finally:
                paths.OUT_XLSX, paths.OUT_TRACKED_XLSX = saved


class HarnessSelfTests(_IsolatedState, unittest.TestCase):
    """The harness is checked by the harness, because it has lied before.

    Both halves of this file's isolation are conventions a future edit can break
    without any test going red, so each one gets an assertion of its own. The module
    fingerprint guard covers the first (a path that was never rebound). This covers the
    second: a class that mixes in _IsolatedState, then overrides setUp and forgets to
    chain -- which leaves it fully unisolated while still reading as isolated at the
    class statement, where anyone auditing would look.
    """

    def test_every_isolated_class_chains_setUp(self):
        import ast
        source = pathlib.Path(__file__).read_text(encoding="utf-8")
        offenders = []
        for node in ast.parse(source).body:
            if not isinstance(node, ast.ClassDef):
                continue
            if "_IsolatedState" not in [
                    b.id for b in node.bases if isinstance(b, ast.Name)]:
                continue
            setup = next((f for f in node.body if isinstance(f, ast.FunctionDef)
                          and f.name == "setUp"), None)
            if setup is None:
                continue  # inherits the mixin's setUp directly
            chains = any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "setUp" and isinstance(n.func.value, ast.Call)
                and isinstance(n.func.value.func, ast.Name)
                and n.func.value.func.id == "super"
                for n in ast.walk(setup))
            if not chains:
                offenders.append(node.name)
        self.assertEqual(offenders, [], "these override setUp without super().setUp(), "
                                       "so isolate_state() never runs for them")

    def test_every_test_class_is_isolated_or_named_as_live(self):
        """The isolation rule, stated as an assertion: everything except one class."""
        import ast
        source = pathlib.Path(__file__).read_text(encoding="utf-8")
        unisolated = [
            node.name for node in ast.parse(source).body
            if isinstance(node, ast.ClassDef)
            and any(isinstance(b, ast.Attribute) and b.attr == "TestCase"
                    for b in node.bases)
            and "_IsolatedState" not in [
                b.id for b in node.bases if isinstance(b, ast.Name)]
        ]
        self.assertEqual(unisolated, ["LiveDataInvariantTests"])


# Last line of the file on purpose. This sat at line 1170 for a while, above eleven
# test classes, so `python tests/test_acceptance.py` collected 62 of 148 tests and
# reported OK -- the 86 below it were never even imported into the run. Use
# `python -m unittest discover -s tests`; this block is only a convenience.
if __name__ == "__main__":
    unittest.main(verbosity=2)
