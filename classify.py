"""Relevance judgment for observed changes (spec section 8).

One Claude call per change that survives triage. The owner profile is passed verbatim
because the class-year and identity gates are the whole point: most of what these
sources surface is aimed at juniors or at groups this owner is not part of, and reading
that off a posting is exactly what an LLM is good at and a regex is not.

Two deliberate safety properties:

* Without an API key, nothing is dropped. Every change is emitted unclassified into
  WORTH A LOOK. Spec section 8 rule 3 is explicit that a false negative -- FTTP opens
  and the tool calls it noise -- is the failure that costs real money, so the
  degraded mode surfaces more, not less.
* The `eligible` column is never rewritten. A proposed change is logged and reported
  (rule 5); hand-verified research is not overwritten by a model.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import state

MODEL = "claude-opus-5"

# Bounds the daily bill. Any overflow still reaches the digest, just unclassified.
MAX_CLASSIFICATIONS_PER_RUN = 60

# Spec section 8, given to the model verbatim.
OWNER_PROFILE = (
    "First-year undergraduate at Stanford, class of 2030, studying math and/or CS. "
    "Does not meet the eligibility criteria for identity-restricted programmes "
    "-- those reserved for women, transgender or gender-expansive students, "
    "underrepresented racial minorities, or LGBTQIA+ students -- nor for \"barriers "
    "to access and opportunity\" criteria. US citizen, US-based. "
    "Regional St. Louis firms are viable summer options."
)

SYSTEM_PROMPT = f"""You judge whether a change on an opportunity-tracking source matters to one specific person.

The person:
{OWNER_PROFILE}

Rules, in priority order:

1. Class-year gates are the primary filter. A posting requiring graduation between
   Dec 2027 and Aug 2028 is a junior window and is NOT relevant. Graduation-window
   phrasing shifts every cycle, so read the actual dates rather than pattern-matching a
   job title. This person graduates in 2030 and is looking at Summer 2027 and beyond.
2. Identity gates disqualify. Programs for women, underrepresented minorities, LGBTQIA+
   students, or students with barriers to access are relevant: false. Specifically:
   Jane Street INSIGHT and WiSE; Jane Street JSIP, FOCUS and IN FOCUS; Bridgewater
   Rising Fellows; Two Sigma College Mentor Connect; D. E. Shaw Discovery, Latitude and
   Momentum; IMC WiT; Virtu Women's Winternship; SIG "for Women" Discovery Days.
3. Bias toward false positives. Surfacing a borderline item is nearly free; missing a
   real opening is expensive. When you are not sure, set relevant: true and say so in
   `why`, with confidence "low".
4. Rolling deadlines are urgent. Jane Street, NVIDIA, D. E. Shaw and Point72 review on
   a rolling basis and close when full; Jane Street slots have reportedly filled by late
   October. Any change on one of those is high priority regardless of stated deadline.
5. Never assert a new value for the owner's hand-maintained `eligible` column. If the
   change implies one, put it in `eligible_proposal` as a suggestion with reasoning.

`why` must be one sentence and must name the specific reason -- the class year, the
identity gate, the deadline -- not a generic statement of interest."""

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "program_name": {"type": "string"},
        "new_status": {"type": "string", "enum": list(state.PROGRAM_STATUSES)},
        "why": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "suggested_action": {"type": "string"},
        "eligible_proposal": {"type": "string"},
    },
    "required": [
        "relevant",
        "program_name",
        "new_status",
        "why",
        "confidence",
        "suggested_action",
    ],
    "additionalProperties": False,
}


@dataclass
class Judgment:
    """A classified change. `classified=False` means it reached the digest unjudged."""

    change: state.Change
    relevant: bool = True
    program_name: str = ""
    new_status: str = "unknown"
    why: str = ""
    confidence: str = "low"
    suggested_action: str = ""
    eligible_proposal: str = ""
    classified: bool = False
    error: str = ""

    @property
    def urgent(self) -> bool:
        """Goes in ACT NOW rather than WORTH A LOOK."""
        if not self.relevant:
            return False
        if self.change.rolling:  # spec section 8 rule 4
            return True
        return self.classified and self.confidence in ("medium", "high")


def triage(changes: list[state.Change], sources: dict[str, dict[str, str]]) -> tuple[list[state.Change], list[state.Change]]:
    """Split changes into (classify, summarise-only).

    Spec section 5 calls Simplify low signal: 1169 untagged roles, most of them junior
    SWE postings. Spending a model call on each new one would cost money to produce
    noise, so low-signal sources are classified only when the row matches the
    underclassman discovery regex or a rolling-review firm. High-signal sources are
    always classified, per rule 3.
    """
    to_classify: list[state.Change] = []
    summarise_only: list[state.Change] = []
    for change in changes:
        source = sources.get(change.source_id, {})
        low_signal = (source.get("signal") or "high").strip().lower() == "low"
        interesting = change.is_discovery_candidate or change.rolling
        if low_signal and not interesting:
            summarise_only.append(change)
        else:
            to_classify.append(change)

    if len(to_classify) > MAX_CLASSIFICATIONS_PER_RUN:
        # Keep the most interesting ones; the rest still get reported, unclassified.
        to_classify.sort(key=lambda c: (not c.rolling, not c.is_discovery_candidate))
        summarise_only.extend(to_classify[MAX_CLASSIFICATIONS_PER_RUN:])
        to_classify = to_classify[:MAX_CLASSIFICATIONS_PER_RUN]
    return to_classify, summarise_only


def _render_change(change: state.Change) -> str:
    lines = [
        f"Source: {change.source_id}",
        f"Change type: {change.kind}",
        f"Row: {change.key}",
    ]
    if change.program_name:
        lines.append(f"Tracked program this source informs: {change.program_name}")
    if change.url:
        lines.append(f"Link: {change.url}")
    if change.rolling:
        lines.append("NOTE: mentions a firm that reviews on a rolling basis.")
    lines.append("")
    lines.append(change.detail[:4000])
    return "\n".join(lines)


def classify_one(client, change: state.Change) -> Judgment:
    try:
        response = client.messages.create(
            model=MODEL,
            # Must cover adaptive thinking AND the JSON response. Billing is per token
            # generated, not per token allowed, so a generous ceiling costs nothing and
            # avoids truncating the response on a long diff.
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": RESULT_SCHEMA},
            },
            messages=[{"role": "user", "content": _render_change(change)}],
        )
    except Exception as exc:
        return Judgment(change=change, error=f"{type(exc).__name__}: {exc}")

    if getattr(response, "stop_reason", None) == "refusal":
        # Surface it rather than drop it (rule 3).
        return Judgment(change=change, error="model declined to classify this change")

    text = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return Judgment(change=change, error="could not parse the model's JSON response")

    return Judgment(
        change=change,
        relevant=bool(payload.get("relevant", True)),
        program_name=payload.get("program_name", "") or change.program_name,
        new_status=payload.get("new_status", "unknown"),
        why=payload.get("why", ""),
        confidence=payload.get("confidence", "low"),
        suggested_action=payload.get("suggested_action", ""),
        eligible_proposal=payload.get("eligible_proposal", ""),
        classified=True,
    )


def classify(changes: list[state.Change], sources: dict[str, dict[str, str]]) -> list[Judgment]:
    """Classify what is worth classifying. Never raises, never drops a change."""
    to_classify, summarise_only = triage(changes, sources)
    judgments = [
        Judgment(
            change=change,
            program_name=change.program_name,
            why="Not classified: low-signal source and the row did not match the "
            "underclassman or rolling-firm filters.",
        )
        for change in summarise_only
    ]

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        for change in to_classify:
            judgments.append(
                Judgment(
                    change=change,
                    program_name=change.program_name,
                    why="Not classified: ANTHROPIC_API_KEY is not set, so this is "
                    "surfaced unjudged rather than dropped.",
                )
            )
        return judgments

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
    except Exception as exc:
        for change in to_classify:
            judgments.append(
                Judgment(change=change, program_name=change.program_name, error=str(exc))
            )
        return judgments

    for change in to_classify:
        judgment = classify_one(client, change)
        if judgment.eligible_proposal:
            state.append_proposal(
                f"{change.source_id}\t{change.key}\tproposed eligible="
                f"{judgment.eligible_proposal}\t{judgment.why}"
            )
        judgments.append(judgment)
    return judgments
