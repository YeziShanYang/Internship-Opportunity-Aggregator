"""How much the tracker sees that the largest public list does not. Pure; no I/O.

The question this answers is the one the project is for: is watching 195 sources worth
more than bookmarking Simplify? It counts deduplicated, open, US internship postings in
the target fields, on both sides, and reports the tracker's total, Simplify's total,
and how many the tracker has that appear nowhere on Simplify.

**Simplify is measured from its whole list, never from our snapshot of it.** The
snapshot reads only the README sections in `parse_readme.REPO_CONFIGS`, so comparing
against it would compare the tracker to a part of Simplify it chose -- the first
version of this measurement did exactly that and reported 2.3x where the honest answer
was 1.35x. The reference is Simplify's own `listings.json`: every active, visible
posting, whatever term it is filed under.

**Every matching error runs against the tracker.** Two rows are one posting when they
share a posting link (`core.text.posting_identities`), *or* when they share a company
key and most of their title words. The second rule over-merges -- two different
"Software Engineer Intern" roles at one firm collapse into one -- and that is the
point: over-merging can only lower the tracker's count and its lead, so the number this
prints is a floor rather than an estimate.
"""
from __future__ import annotations

import collections
import re
from dataclasses import dataclass

from core.text import posting_identities
from process.parse_ats import NON_US_LOCATION, US_LOCATION

REFERENCE = "SIMPLIFY"

# A row has to read as student work. ATS snapshots are already filtered to student roles;
# the aggregators are not, and zshah carries a full-time tail.
INTERN = re.compile(
    r"intern|co-?op|summer 20\d\d|undergrad|student|freshman|sophomore|fellowship|"
    r"insight|discovery", re.I)
# The target fields: quant first, then CS, software, data and ML. Hardware, product
# and civil-engineering co-ops are out, so a count inflated by them cannot pass for
# reach. `[quant]` is the tag carried by Simplify's own Quant category.
FIELD = re.compile(
    r"software|\bswe\b|developer|quant|\btrad(er|ing)\b|\bdata\b|machine learning|\bml\b|"
    r"\bai\b|computer|algorithm|math|research|\[quant\]", re.I)
QUANT = re.compile(
    r"quant|\btrad(er|ing)\b|fpga|low.latency|derivativ|\boptions\b|portfolio|systematic|"
    r"\[quant\]", re.I)
# A posting he cannot apply to is not reach, on either side of the comparison.
GRADUATE_ONLY = re.compile(r"\bph\.?d\b|\bmba\b|master'?s\b|\bms\b(?! office)|graduate intern|postdoc", re.I)
_STOP = {"intern", "internship", "internships", "summer", "the", "and", "for", "with",
         "2027", "2026", "program", "programme", "co-op", "coop"}
# zshah writes zero-width spaces inside company names ("Formlabs​internships"),
# which stopped its rows matching their own Simplify copies and inflated the lead.
_ZERO_WIDTH = re.compile(r"[​-‍⁠﻿]")
_AGGREGATOR_HEAD = re.compile(r"\[?([^\]/]+?)\]?(?:\([^)]*\))?\s*/\s*(.*)")
# Title overlap, measured against the shorter title, above which two rows at the same
# company are called one posting.
MATCH_OVERLAP = 0.75


@dataclass(frozen=True)
class Posting:
    source: str
    company: str
    title: str
    ids: frozenset[str]


@dataclass(frozen=True)
class Scope:
    label: str
    tracker: int
    reference: int
    tracker_only: int
    # Of tracker_only, the postings no aggregator carries: read off an employer board.
    employer_only: int

    @property
    def lead(self) -> float:
        """Tracker-only postings as a fraction of the reference: +0.35 is "35% more"."""
        return self.tracker_only / self.reference if self.reference else 0.0


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{2,}", text.lower().replace(" [quant]", ""))) - _STOP


def _company_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())[:5]


def _is_us(location: str) -> bool:
    # The same rule as parse_ats.is_us: only a clearly foreign location drops a row.
    return bool(US_LOCATION.search(location)) or not NON_US_LOCATION.search(location)


def from_snapshots(snapshots: dict[str, str], aggregators: set[str]) -> list[Posting]:
    """Postings out of the stored `.tsv` snapshots, one per qualifying row."""
    out = []
    for source_id, text in sorted(snapshots.items()):
        for line in text.splitlines():
            if line.startswith("#") or "\t" not in line:
                continue
            head, _, location = line.split("\t")[1].partition(" @ ")
            if not INTERN.search(head) or GRADUATE_ONLY.search(head) or not _is_us(location):
                continue
            if source_id in aggregators:
                match = _AGGREGATOR_HEAD.match(head)
                company, title = (match.group(1), match.group(2)) if match else ("", head)
            else:
                # An ATS board's rows carry no company; the source id names the firm.
                company, title = source_id.rsplit("-", 1)[0], head
            out.append(Posting(
                source_id, _ZERO_WIDTH.sub("", company).strip(),
                _ZERO_WIDTH.sub("", title).strip(), posting_identities(line)))
    return out


def from_reference(listings: list[dict]) -> list[Posting]:
    """Simplify's whole active list, US postings only, whatever term it is filed under.

    Not just its 2027 terms: Simplify files many live postings under an older or
    off-cycle label ("Summer 2026", "Fall 2026"), and restricting to 2027 counted 103
    of them as postings Simplify lacked when it carried them by exact link.
    """
    out = []
    for item in listings:
        if not (item.get("active") and item.get("is_visible", True)):
            continue
        if not any(_is_us(loc) for loc in (item.get("locations") or [""])):
            continue
        if GRADUATE_ONLY.search(item.get("title", "")):
            continue
        title = item.get("title", "") + (" [quant]" if item.get("category") == "Quant" else "")
        out.append(Posting(REFERENCE, item.get("company_name", ""), title,
                           posting_identities(item.get("url", ""))))
    return out


def _group(postings: list[Posting]) -> list[list[Posting]]:
    parent = list(range(len(postings)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first_with: dict[str, int] = {}
    for i, posting in enumerate(postings):
        for ident in posting.ids:
            if ident in first_with:
                parent[find(i)] = find(first_with[ident])
            else:
                first_with[ident] = i
    by_company: dict[str, list[int]] = collections.defaultdict(list)
    for i, posting in enumerate(postings):
        by_company[_company_key(posting.company)].append(i)
    for members in by_company.values():
        words = [_words(postings[i].title) for i in members]
        for a in range(len(members)):
            for b in range(a + 1, len(members)):
                wa, wb = words[a], words[b]
                if wa and wb and len(wa & wb) / min(len(wa), len(wb)) >= MATCH_OVERLAP:
                    parent[find(members[a])] = find(members[b])
    groups: dict[int, list[Posting]] = collections.defaultdict(list)
    for i, posting in enumerate(postings):
        groups[find(i)].append(posting)
    return list(groups.values())


def measure(tracker: list[Posting], reference: list[Posting],
            aggregators: set[str]) -> list[Scope]:
    """The two scopes HEALTH prints: the target fields, and quant within them."""
    groups = _group(tracker + reference)
    scopes = []
    for label, pattern in (("fields", FIELD), ("quant", QUANT)):
        chosen = [g for g in groups if any(pattern.search(p.title) for p in g)]
        ours = [g for g in chosen if any(p.source != REFERENCE for p in g)]
        theirs = [g for g in chosen if any(p.source == REFERENCE for p in g)]
        only = [g for g in ours if all(p.source != REFERENCE for p in g)]
        employer = [g for g in only if all(p.source not in aggregators for p in g)]
        scopes.append(Scope(label, len(ours), len(theirs), len(only), len(employer)))
    return scopes
