"""Run the deterministic rules over a run's changes, and say what they removed.

The loop and its counter used to live in `classify` as a module-global `ScreenStats`,
which is concretely why stages could not run independently: `run.py render` would have
printed an empty screen line against freshly-initialised globals, and a second
`classify` in the same process would have double-counted. The tally is returned now,
as the same `FilterReport` every other filter in the pipeline emits.

The screen removes changes *before* the model is asked, which makes it the most
invisible filter in the pipeline if it does not report -- and a screen rule that
silently over-matches is exactly how a real opening disappears.
"""
from __future__ import annotations

from core import models
from enrich import bodies as enrich_bodies
from screen import rules

# Carries the rule-set version, so a digest can be read back later and tell which rules
# produced it -- and so the line changes when the rules do.
FILTER_ID = f"screen-v{rules.VERSION}"

MAX_SAMPLES = 5


def apply(
    changes: list[models.Change],
    bodies: dict[str, enrich_bodies.PostingBody] | None = None,
) -> dict[str, rules.Verdict]:
    """{change_id: Verdict} for the changes a quoted phrase settles.

    A change absent from the result is one the screen had no opinion on, which is the
    only correct encoding of "no opinion": an entry saying "not ruled out" would invite
    a reader to treat it as a positive judgment, and there is no such thing as a
    deterministic rule-in.
    """
    table = bodies or {}
    out: dict[str, rules.Verdict] = {}
    for change in changes:
        text, _error = enrich_bodies.for_change(table, change)
        verdict = rules.screen(change.key, text)
        if verdict is not None:
            out[change.change_id] = rules.Verdict(
                why=verdict.why, rule=verdict.rule,
                version=verdict.version, change_id=change.change_id)
    return out


def report(
    changes: list[models.Change], verdicts: dict[str, rules.Verdict]
) -> models.FilterReport:
    """What the screen removed, in the shape every other filter uses."""
    by_rule: dict[str, int] = {}
    for verdict in verdicts.values():
        by_rule[verdict.rule] = by_rule.get(verdict.rule, 0) + 1
    rule_summary = ", ".join(f"{rule} {n}" for rule, n in sorted(by_rule.items()))
    return models.FilterReport(
        stage="screen",
        filter_id=FILTER_ID,
        considered=len(changes),
        removed=len(verdicts),
        reason=(
            f"ruled out on a quoted phrase, with no model call ({rule_summary}). "
            "They are listed in RULED OUT with the phrase that did it"
            if verdicts else
            "no change matched a rule-out phrase; all went to the model"
        ),
        samples=tuple(
            change.key for change in changes
            if change.change_id in verdicts
        )[:MAX_SAMPLES],
    )
