"""Underclassman postings stay in ACT NOW until they leave their board.

Asked for directly on 2026-09-18. Every other ACT NOW test is a *dated* reason that
fires on the morning a row moves; this one is a standing fact about who the posting is
for, so the interesting cases are all about time and about silence: a pin survives the
day nothing happens to it, and a pin is never retired by a source that failed.

The size cases are here too, because the feature is only deliverable at all because of
them. Matching the whole snapshot line put 114 rows in scope against GitHub's
65,536-character issue limit; matching the row's own title puts a handful in scope.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core import models
from deliver import digest, urgency

TODAY = "2026-09-18"
YESTERDAY = "2026-09-17"


def _judgment(key: str, *, relevant=True, class_year="", source_id="s",
              kind="added", change_id="", deadline="") -> models.Judgment:
    change = models.Change(source_id=source_id, kind=kind, key=key, detail="d",
                           change_id=change_id or f"{source_id}:{key}")
    return models.Judgment(change=change, outcome=models.MODEL, relevant=relevant,
                           class_year=class_year, deadline=deadline, confidence="high")


def _pin(change_id="s:Acme / Sophomore Analyst", source_id="s",
         first_pinned=YESTERDAY) -> models.PinnedRow:
    return models.PinnedRow(change_id=change_id, source_id=source_id, company="Acme",
                            position="Sophomore Analyst", first_pinned=first_pinned,
                            last_seen=first_pinned)


class UnderclassmanDetectionTests(unittest.TestCase):
    def test_class_year_from_the_posting_body_promotes(self):
        """The field the classifier reads off the body, which is where it usually is."""
        self.assertTrue(urgency.targets_underclassmen(
            _judgment("Acme / Analyst", class_year="freshmen and sophomores")))

    def test_row_title_promotes(self):
        self.assertTrue(urgency.targets_underclassmen(
            _judgment("Acme / First-Year Insight Program")))

    def test_section_heading_does_not_promote(self):
        """The whole reason the block is deliverable.

        `Row.identity` is section-qualified and three watched repos are underclassman
        trackers end to end, so their headings carry the word on every row. `Change.key`
        is the row's own key and excludes the heading, which is why this is the field
        the rule reads. Measured 2026-09-18: matching the full line caught 114 rows,
        110 of them from those three repos.
        """
        judgment = _judgment("Acme / Backend Engineer Intern")
        judgment.change.detail = "CS underclassmen internships\tAcme\tBackend Engineer"
        self.assertFalse(urgency.targets_underclassmen(judgment))

    def test_a_collapse_notice_is_never_pinned(self):
        """Caught on a forced re-baseline of underclassmen-cruz, 2026-09-18.

        The collapse guard reports "underclassmen-cruz: 78 of 107 rows changed at once"
        as a single `changed` row whose key is the source's own prose. It matched the
        underclassman rule on the word in the source id and pinned a board restructure
        into ACT NOW as an opportunity, where nothing would ever have retired it.
        """
        judgment = _judgment("underclassmen-cruz: 78 of 107 rows changed at once")
        judgment.change.structural = True
        self.assertFalse(urgency.targets_underclassmen(judgment))
        self.assertEqual(
            urgency.refresh_pins([judgment], [], {"s": {judgment.change.change_id}},
                                 {"s"}, TODAY, digest.company_and_position), [])

    def test_a_new_section_row_is_never_pinned(self):
        judgment = _judgment("new section: CS underclassmen internships")
        judgment.change.structural = True
        self.assertFalse(urgency.targets_underclassmen(judgment))

    def test_junior_only_row_is_not_promoted(self):
        self.assertFalse(urgency.targets_underclassmen(
            _judgment("Acme / Junior Summer Analyst", class_year="rising seniors")))

    def test_an_underclassman_row_is_urgent(self):
        self.assertTrue(urgency.is_urgent(_judgment("Acme / Sophomore Analyst")))

    def test_an_irrelevant_underclassman_row_is_not_urgent(self):
        """Relevance still gates everything. A ruled-out row prints once in RULED OUT."""
        self.assertFalse(urgency.is_urgent(
            _judgment("Acme / Sophomore Analyst", relevant=False)))


class PinRefreshTests(unittest.TestCase):
    def test_a_new_underclassman_row_is_pinned(self):
        judgments = [_judgment("Acme / Sophomore Analyst")]
        pins = urgency.refresh_pins(
            judgments, [], {"s": {"s:Acme / Sophomore Analyst"}}, {"s"}, TODAY,
            digest.company_and_position)
        self.assertEqual([p.change_id for p in pins], ["s:Acme / Sophomore Analyst"])
        self.assertEqual(pins[0].first_pinned, TODAY)

    def test_a_ruled_out_row_is_not_pinned(self):
        pins = urgency.refresh_pins(
            [_judgment("Acme / Sophomore Analyst", relevant=False)], [],
            {"s": {"s:Acme / Sophomore Analyst"}}, {"s"}, TODAY,
            digest.company_and_position)
        self.assertEqual(pins, [])

    def test_a_pin_survives_a_day_with_no_changes(self):
        """The feature. Nothing moved, the row is still on the board, it stays."""
        pins = urgency.refresh_pins(
            [], [_pin()], {"s": {"s:Acme / Sophomore Analyst"}}, {"s"}, TODAY,
            digest.company_and_position)
        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0].first_pinned, YESTERDAY)  # not re-dated
        self.assertEqual(pins[0].last_seen, TODAY)

    def test_a_pin_is_dropped_once_the_posting_comes_down(self):
        pins = urgency.refresh_pins(
            [], [_pin()], {"s": set()}, {"s"}, TODAY, digest.company_and_position)
        self.assertEqual(pins, [])

    def test_a_failing_source_never_retires_a_pin(self):
        """"We have stopped looking" must never render as "it closed".

        The same rule the circuit breaker follows when it insists a quarantined source
        still appears in HEALTH. `checked` is empty here, so the empty live set must
        not be read as evidence.
        """
        pins = urgency.refresh_pins(
            [], [_pin()], {"s": set()}, set(), TODAY, digest.company_and_position)
        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0].last_seen, YESTERDAY)  # honest about when we last saw it

    def test_a_removed_row_retires_its_pin(self):
        pins = urgency.refresh_pins(
            [_judgment("Acme / Sophomore Analyst", kind="removed")], [_pin()],
            {"s": set()}, {"s"}, TODAY, digest.company_and_position)
        self.assertEqual(pins, [])

    def test_live_ids_are_yesterdays_rows_plus_todays_diff(self):
        previous = {"s": {"old", "gone"}}
        changes = [
            models.Change(source_id="s", kind="added", key="k", detail="d",
                          change_id="new"),
            models.Change(source_id="s", kind="removed", key="k", detail="d",
                          change_id="gone"),
        ]
        self.assertEqual(
            urgency.live_change_ids(previous, changes, {"s"}), {"s": {"old", "new"}})

    def test_an_unchecked_source_keeps_yesterdays_rows_verbatim(self):
        previous = {"s": {"old"}}
        changes = [models.Change(source_id="s", kind="removed", key="k", detail="d",
                                 change_id="old")]
        self.assertEqual(urgency.live_change_ids(previous, changes, set()), {"s": {"old"}})


class PinnedRenderingTests(unittest.TestCase):
    def _body(self, judgments, pinned):
        _, body = digest.render(judgments, [], {}, pinned=pinned)
        return body

    def test_a_carried_pin_renders_compactly_in_act_now(self):
        body = self._body([], [_pin()])
        row = next(line for line in body.splitlines() if "Acme" in line)
        self.assertIn(digest.URGENCY_ACT_NOW, row)
        self.assertIn(f"pinned {YESTERDAY}", row)
        self.assertNotIn("<br>", row)  # the full bullet cell is what this replaces

    def test_a_pin_is_not_printed_twice_on_the_day_it_moves(self):
        judgment = _judgment("Acme / Sophomore Analyst")
        body = self._body([judgment], [_pin(change_id=judgment.change.change_id)])
        self.assertEqual(sum("Acme" in line for line in body.splitlines()), 1)

    def test_a_carried_pin_is_far_cheaper_than_a_full_row(self):
        """577 bytes a full row is what made the naive version undeliverable."""
        compact = digest._pinned_row(_pin(), TODAY)
        self.assertLess(len(compact), 200)
