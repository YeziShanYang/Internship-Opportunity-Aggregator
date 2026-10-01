"""Bulk board sources: many small employers on one vendor, read as one source."""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core import models  # noqa: E402
from deliver import health  # noqa: E402
from gather import ats  # noqa: E402
from process import parse_ats  # noqa: E402

SOURCE = {"source_id": "smallco-greenhouse", "method": "greenhouse_bulk", "url": "greenhouse.txt",
          "program_names": "Small-company boards (Greenhouse)"}


def board(slug: str, *titles: str, company: str = "") -> dict:
    jobs = [{"id": 8000000 + i, "title": t, "location": {"name": "New York, NY"},
             "absolute_url": f"https://job-boards.greenhouse.io/{slug}/jobs/{8000000 + i}",
             "company_name": company or slug.title()}
            for i, t in enumerate(titles)]
    return {"slug": slug, "payload": {"jobs": jobs}}


def fetched(*pages: dict) -> ats.AtsFetch:
    return ats.AtsFetch(attempt=models.FetchAttempt(source_id="smallco-greenhouse", ok=True),
                        pages=list(pages), listed=len(pages))


def run(previous: str | None, *pages: dict) -> models.SourceResult:
    return parse_ats.assess_bulk(SOURCE, fetched(*pages), previous)


class BulkTests(unittest.TestCase):
    def setUp(self):
        self.base = run(None, board("acme", "Software Engineer Intern"),
                        board("zeta", "Quant Research Intern"))

    def test_first_run_is_a_baseline_with_one_section_per_board(self):
        self.assertTrue(self.base.baseline)
        self.assertIn("acme\t", self.base.snapshot_text)
        self.assertIn("zeta\t", self.base.snapshot_text)

    def test_an_unreadable_board_keeps_its_rows_and_reports_nothing_removed(self):
        result = run(self.base.snapshot_text, board("acme", "Software Engineer Intern"),
                     {"slug": "zeta", "error": "ReadTimeout"})
        self.assertTrue(result.ok)
        self.assertEqual(result.changes, [])
        self.assertIn("Quant Research Intern", result.snapshot_text)
        report = {f.filter_id: f for f in result.filters}[parse_ats.BULK_UNREADABLE]
        self.assertEqual((report.considered, report.removed), (2, 1))
        (line,) = [l for l in health.filter_lines(result.filters) if "unreadable" in l]
        self.assertIn("1 of 2 boards unreadable", line)
        self.assertIn("zeta: ReadTimeout", line)

    def test_most_boards_unreadable_fails_the_source(self):
        result = run(self.base.snapshot_text, {"slug": "acme", "error": "x"},
                     {"slug": "zeta", "error": "y"})
        self.assertFalse(result.ok)

    def test_a_new_posting_names_its_employer_not_the_bulk_source(self):
        result = run(self.base.snapshot_text,
                     board("acme", "Software Engineer Intern", "Data Science Intern",
                           company="Acme Robotics"),
                     board("zeta", "Quant Research Intern"))
        (change,) = result.changes
        self.assertEqual(change.kind, "added")
        self.assertEqual(change.program_name, "Acme Robotics")

    def test_one_board_restructuring_collapses_without_hiding_the_others(self):
        many = [f"Software Engineer Intern {i}" for i in range(parse_ats.MAX_CHANGES_PER_BOARD + 1)]
        result = run(self.base.snapshot_text,
                     board("acme", "Software Engineer Intern", *many),
                     board("zeta", "Quant Research Intern", "Quant Trading Intern"))
        structural = [c for c in result.changes if c.structural]
        ordinary = [c for c in result.changes if not c.structural]
        self.assertEqual(len(structural), 1)
        self.assertIn("rows changed at once", structural[0].key)
        self.assertEqual([c.key.split(" @ ")[0] for c in ordinary], ["Quant Trading Intern"])


if __name__ == "__main__":
    unittest.main()
