"""The NSF REU watcher: an award feed read like a paged job board."""
from __future__ import annotations

import datetime
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core import models  # noqa: E402
from gather import ats  # noqa: E402
from process import parse_ats  # noqa: E402


def award(title: str, division: str, **kw) -> dict:
    return {"id": kw.pop("id", "2548181"), "title": title, "divAbbr": division,
            "awardeeName": "University of California-Los Angeles",
            "awardeeCity": "Los Angeles", "awardeeStateCode": "CA",
            "expDate": "11/30/2029", "abstractText": "Ten undergraduates a summer.", **kw}


def fetched(*awards: dict) -> ats.AtsFetch:
    return ats.AtsFetch(
        attempt=models.FetchAttempt(source_id="nsf-reu-sites", ok=True, status=200),
        pages=[{"response": {"award": list(awards)}}], listed=len(awards))


SOURCE = {"source_id": "nsf-reu-sites", "method": "nsf_awards", "url": "REU Site"}


class NsfTests(unittest.TestCase):
    def test_only_in_field_reu_sites_are_kept_and_the_rest_are_reported(self):
        result = parse_ats.assess(SOURCE, fetched(
            award("REU Site: Applied and Computational Mathematics", "DMS"),
            award("REU Site: Coastal Ecology", "OCE", id="1"),
            award("Collaborative Research: Graph Algorithms", "CCF", id="2"),
        ), None)
        self.assertTrue(result.ok)
        self.assertEqual(result.extra["rows"], 1)
        report = {f.filter_id: f for f in result.filters}["nsf-not-in-field-reu-site"]
        self.assertEqual(report.removed, 2)

    def test_every_row_carries_text_so_enrich_never_fetches_the_award_page(self):
        # www.nsf.gov/awardsearch/* is disallowed by robots.txt.
        (posting,) = parse_ats.parse_nsf(fetched(award("REU Site: Data Science", "IIS")))
        self.assertTrue(posting.text)
        self.assertIn("awardsearch/showAward?AWD_ID=2548181", posting.url)
        self.assertTrue(parse_ats.is_us(posting))

    def test_the_window_is_the_coming_summer(self):
        self.assertEqual(ats.nsf_window_start(datetime.date(2026, 10, 1)), "08/01/2027")
        self.assertEqual(ats.nsf_window_start(datetime.date(2027, 3, 1)), "08/01/2027")

    def test_unstable_paging_is_retried_until_the_union_reaches_the_total(self):
        # Every award starts on one day, so the slice must page, and each pass serves
        # a different 28 of the 30 -- the shape measured on the live API.
        import random
        everything = [{"id": str(i), "title": f"REU Site: {i}", "divAbbr": "DMS"}
                      for i in range(30)]
        rng = random.Random(7)

        day = datetime.date(2024, 9, 1)

        class Client:
            def get(self, url, params, headers):
                first, last = (datetime.datetime.strptime(params[k], "%m/%d/%Y").date()
                               for k in ("startDateStart", "startDateEnd"))
                if not first <= day <= last:
                    count, batch = 0, []
                elif first != last:
                    count, batch = 30, everything[:25]
                elif params["offset"] == 0:
                    Client.served = rng.sample(everything, 28)
                    count, batch = 30, Client.served[:25]
                else:
                    count, batch = 30, Client.served[25:]

                class R:
                    def raise_for_status(self): pass
                    def json(self): return {"response": {
                        "metadata": {"totalCount": count}, "award": batch}}
                return R()

        result = ats._fetch_nsf(Client(), ats.ENDPOINTS[ats.NSF], "REU Site")
        ids = {a["id"] for p in result.pages for a in p["response"]["award"]}
        self.assertEqual(len(ids), 30)
        self.assertEqual(result.listed, 30)

    def test_a_malformed_response_is_a_failure_not_an_empty_list(self):
        class Client:
            def get(self, *a, **kw):
                class R:
                    def raise_for_status(self): pass
                    def json(self): return {"error": "rate limited"}
                return R()
        with self.assertRaises(ats.MalformedPayload):
            ats._fetch_nsf(Client(), ats.ENDPOINTS[ats.NSF], "REU Site")


if __name__ == "__main__":
    unittest.main()
