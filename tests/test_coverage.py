"""The coverage measurement must be a floor: every matching error runs against the tracker."""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from process import coverage  # noqa: E402

AGG = {"simplify-2027", "zshah-2027"}
GH = "https://job-boards.greenhouse.io/acme/jobs/801885{}"


def snapshot(*rows: tuple[str, str]) -> str:
    return "# sections\nX\n# rows\n" + "".join(f"X\t{key}\t{value}\n" for key, value in rows)


def listing(title: str, company: str = "Acme", url: str = "", **kw) -> dict:
    return {"title": title, "company_name": company, "url": url, "active": True,
            "is_visible": True, "terms": ["Summer 2027"], "locations": ["New York, NY"],
            "category": "Software", **kw}


class CoverageTests(unittest.TestCase):
    def measure(self, snapshots: dict[str, str], listings: list[dict]):
        scopes = coverage.measure(coverage.from_snapshots(snapshots, AGG),
                                  coverage.from_reference(listings), AGG)
        return {s.label: s for s in scopes}

    def test_a_shared_posting_link_is_one_posting(self):
        board = snapshot(("Software Engineer Intern @ New York, NY", f"URL={GH.format(1)}"))
        fields = self.measure({"acme-greenhouse": board},
                              [listing("SWE Intern (C++)", url=GH.format(1))])["fields"]
        self.assertEqual((fields.tracker, fields.reference, fields.tracker_only), (1, 1, 0))

    def test_a_posting_simplify_does_not_carry_counts_as_lead(self):
        board = snapshot(("Quantitative Research Intern @ Chicago, IL", f"URL={GH.format(2)}"))
        scopes = self.measure({"acme-greenhouse": board}, [listing("Data Intern")])
        self.assertEqual(scopes["quant"].tracker_only, 1)
        self.assertEqual(scopes["quant"].employer_only, 1)
        self.assertEqual(scopes["fields"].lead, 1.0)

    def test_the_same_title_at_the_same_firm_merges_without_a_link(self):
        # Over-merging is the conservative direction: it can only shrink the lead.
        agg = snapshot(("Acme / Software Engineer Intern @ Austin, TX", "Company=Acme"))
        fields = self.measure({"zshah-2027": agg},
                              [listing("Software Engineer Intern")])["fields"]
        self.assertEqual(fields.tracker_only, 0)

    def test_zero_width_spaces_do_not_split_a_posting(self):
        agg = snapshot(("Form​labs / Software Engineer Intern @ Boston, MA", "x"))
        fields = self.measure({"zshah-2027": agg},
                              [listing("Software Engineer Intern", company="Formlabs")])["fields"]
        self.assertEqual(fields.tracker_only, 0)

    def test_simplify_is_measured_from_its_whole_list(self):
        # Our snapshot of Simplify carries one row; its list carries two. The reference
        # is the list -- comparing against our own partial copy is what produced 2.3x.
        ours = snapshot(("[Acme](x) / Software Engineer Intern @ NYC, NY", "x"))
        fields = self.measure({"simplify-2027": ours},
                              [listing("Software Engineer Intern"),
                               listing("Machine Learning Intern", company="Other")])["fields"]
        self.assertEqual(fields.reference, 2)
        self.assertEqual(fields.tracker_only, 0)

    def test_closed_foreign_and_off_field_postings_are_not_counted(self):
        fields = self.measure({}, [
            listing("Software Engineer Intern", active=False),
            listing("Software Engineer Intern", company="B", locations=["London, UK"]),
            listing("Mechanical Engineering Intern", company="C"),
        ])["fields"]
        self.assertEqual(fields.reference, 0)

    def test_a_posting_simplify_files_under_an_older_term_is_still_carried(self):
        # 167 of 379 "tracker-only" postings on 2026-10-01 were on Simplify by exact
        # link, filed as Winter 2026 or Fall 2026.
        board = snapshot(("Software Engineer Intern @ New York, NY", f"URL={GH.format(4)}"))
        fields = self.measure({"acme-greenhouse": board},
                              [listing("SWE Intern", url=GH.format(4), terms=["Fall 2026"])])["fields"]
        self.assertEqual(fields.tracker_only, 0)

    def test_graduate_only_postings_are_not_counted_on_either_side(self):
        board = snapshot(("Quant Research Intern - PhD @ Chicago, IL", f"URL={GH.format(5)}"))
        fields = self.measure({"acme-greenhouse": board},
                              [listing("MBA Software Intern", company="B")])["fields"]
        self.assertEqual((fields.tracker, fields.reference), (0, 0))

    def test_an_unrecognised_location_is_kept(self):
        board = snapshot(("Software Engineer Intern @ Bay Area Offices", f"URL={GH.format(3)}"))
        self.assertEqual(self.measure({"acme-greenhouse": board}, [])["fields"].tracker, 1)


if __name__ == "__main__":
    unittest.main()
