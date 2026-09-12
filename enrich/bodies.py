"""Fetch the posting behind each changed row, so there is something to judge.

A `simplify-2027` row is a title, a company and a location -- nothing a relevance filter
can act on. Measured on the snapshot: 7 of 592 rows mention any class-year word at all,
and all 7 are incidental matches on "Graduate Researcher". That is why the classifier
used to skip that source entirely: not because the rows were unimportant, but because
there was no information in them to be right or wrong about.

The posting page does carry it. Measured on a random sample of 22 live postings, 7
(~32%) state a class-year gate that excludes a first-year -- "rising junior",
"penultimate year", "third year", an expected-graduation window. Fetching turns a row
that could only ever be guessed at into one that can be decided.

Keyed on `change_id`, not on URL, and that is the fix rather than a detail. An
inline-ATS body has no URL to key on at all, and two changed rows can legitimately share
one posting link, so a URL-keyed table could neither hold the first case nor
distinguish the second.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from core import models
from enrich import postings

INLINE = "inline"
FETCHED = "fetched"
ABSENT = "absent"


@dataclass(frozen=True)
class PostingBody:
    """The posting text behind one change, and where it came from.

    `origin` is load-bearing and is why this is not just a string. Prompt rule 6 and
    `screen.screen_posting` both say the same thing: absence of evidence is never
    evidence, so a failed fetch must not read as "this posting states no requirements".
    Collapsing `absent` and a non-empty `error` into one empty string is exactly how a
    network problem turns itself into a relevance decision.
    """

    change_id: str
    text: str = ""
    error: str = ""
    url: str = ""
    origin: str = ABSENT

    @property
    def usable(self) -> bool:
        return bool(self.text)


def _inline(change: models.Change) -> PostingBody:
    """Some sources hand back the body in the same response that lists the job --
    Greenhouse `content=true`, Lever and Ashby `descriptionPlain`, and a page diff
    *is* the text. Those cost nothing and must not be refetched."""
    return PostingBody(
        change_id=change.change_id, text=change.posting_text,
        url=change.posting_url, origin=INLINE)


def needs_fetch(change: models.Change) -> str:
    """The URL to fetch for this change, or "" if there is nothing to fetch.

    Falls back to the regex recovery only for a row read back out of a committed
    snapshot, where the typed `posting_url` was never stored because the TSV format is
    frozen. In practice that is a `removed` row, whose posting has usually gone anyway.
    """
    if change.posting_text:
        return ""
    if change.posting_url:
        return change.posting_url
    return postings.recover_posting_url(change.detail, change.key)


def collect(
    changes: list[models.Change], client: httpx.Client | None = None
) -> dict[str, PostingBody]:
    """{change_id: PostingBody} for every change. O(changes), not O(sources).

    An entry is emitted for *every* change, including the ones with nothing to fetch.
    That is deliberate: `.run/enriched.json` is then a complete account of what this
    stage managed per row, and "this change is missing from the table" stops being
    ambiguous between "nothing to get" and "enrich never ran".
    """
    bodies: dict[str, PostingBody] = {}
    wanted: dict[str, list[models.Change]] = {}

    for change in changes:
        if change.posting_text:
            bodies[change.change_id] = _inline(change)
            continue
        url = needs_fetch(change)
        if url:
            wanted.setdefault(url, []).append(change)
        else:
            bodies[change.change_id] = PostingBody(
                change_id=change.change_id, origin=ABSENT)

    if wanted:
        # One fetch per distinct URL, shared across every change that points at it.
        fetched = postings.fetch_many(list(wanted), client)
        for url, sharing in wanted.items():
            text, error = fetched.get(url, ("", "not fetched"))
            for change in sharing:
                bodies[change.change_id] = PostingBody(
                    change_id=change.change_id, text=text, error=error,
                    url=url, origin=FETCHED)
    return bodies


def for_change(
    bodies: dict[str, PostingBody], change: models.Change
) -> tuple[str, str]:
    """(text, error) for one change, in the shape the prompt renderer wants.

    Falls back to the change's own inline text when the table has no entry for it. That
    text is a property of the Change -- an ATS hands it back in the same response that
    lists the job -- so a caller that has not run `enrich` must not lose it. Losing it
    would turn a screen rule-out into a "no opinion", which is the screen going quiet:
    the item still reaches the digest, but unjudged and with a model call spent on it.

    Returns ("", "") when there is genuinely nothing to say, which the renderer reads as
    "judge on the row alone" rather than as a fetch failure.
    """
    body = bodies.get(change.change_id)
    if body is None:
        return change.posting_text, ""
    return body.text, body.error


def health_line(bodies: dict[str, PostingBody]) -> str | None:
    """What this stage managed, for HEALTH. Every stage says what it did."""
    if not bodies:
        return None
    fetched = [b for b in bodies.values() if b.origin == FETCHED]
    if not fetched:
        return None
    failed = [b for b in fetched if not b.text]
    line = (
        f"postings: {len(fetched) - len(failed)} of {len(fetched)} bodies fetched for "
        f"{len(bodies)} changed row(s)"
    )
    if failed:
        # Named as an undercount, not swallowed: these changes reached the model with a
        # title and no requirements, and the prompt was told to say so.
        line += (
            f" — ⚠ {len(failed)} could not be read, so those were judged on the row "
            "alone and not ruled out on class-year grounds"
        )
    return line + "."
