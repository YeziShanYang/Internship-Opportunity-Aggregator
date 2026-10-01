"""Relative ages on watched pages must not churn the page snapshot daily."""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from process import parse_page  # noqa: E402


class BuiltInAgeTests(unittest.TestCase):
    def test_card_ages_normalise_to_one_placeholder(self):
        monday = parse_page.normalise("<div>Software Engineer Intern</div><div>11 Hours Ago Saved</div>")
        tuesday = parse_page.normalise("<div>Software Engineer Intern</div><div>Yesterday Saved</div>")
        reposted = parse_page.normalise("<div>Software Engineer Intern</div><div>Reposted 2 Days Ago Saved</div>")
        self.assertEqual(monday, tuesday)
        self.assertEqual(monday, reposted)

    def test_prose_mentioning_yesterday_is_untouched(self):
        (line,) = parse_page.normalise("<p>Applications opened yesterday for the 2027 cohort.</p>")
        self.assertIn("yesterday", line)


if __name__ == "__main__":
    unittest.main()
