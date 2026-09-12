"""Stripping HTML to readable text, and how much of it the classifier is given.

Pure, and in `core` rather than next to the fetcher because `process` needs it: both
the page normaliser and every ATS description parser reduce markup to text, and a stage
may only import at or below its own level. It used to live in `sources/postings.py`,
which meant the parsing layer imported the fetching layer to borrow a regex.
"""
from __future__ import annotations

import re

# What the classifier is given. Postings run to ~34K characters and the tail is
# boilerplate (benefits, EEO statements, "about us"), while the requirements block sits
# near the top. Measured: trimming this is worth 8% of the bill, because almost all of
# the cost is reasoning tokens rather than prompt size.
MAX_TEXT_CHARS = 12_000

_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
_ENTITIES = {"&#x27;": "'", "&amp;": "&", "&quot;": '"', "&lt;": "<", "&gt;": ">",
             "&nbsp;": " "}


def extract_text(html: str) -> str:
    """Strip a page to readable text. Crude on purpose -- the model tolerates noise."""
    text = _SCRIPT_OR_STYLE.sub(" ", html)
    text = _TAG.sub(" ", text)
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    return _WHITESPACE.sub(" ", text).strip()
