"""Relevance judgment for observed changes (spec section 8).

One model call per change that survives triage. The owner profile is passed verbatim
because the class-year and identity gates are the whole point: most of what these
sources surface is aimed at juniors or at groups this owner is not part of, and reading
that off a posting is exactly what an LLM is good at and a regex is not.

Two providers are supported, and the prompt, the schema and the `Judgment` contract are
identical across both -- only the transport differs:

* **Anthropic** (`ANTHROPIC_API_KEY`), running Claude. Preferred whenever it is
  configured, because the system prompt above was written and tuned against it.
* **Azure OpenAI** (`AZURE_OPENAI_API_KEY`), running a `gpt-5-mini` deployment on a
  Microsoft Foundry resource. This exists because Azure for Students grants $100 of
  credit that covers first-party OpenAI models but grants *zero* deployment quota for
  Claude on Azure -- Claude there is Marketplace-billed, and Marketplace purchases are
  excluded from student credit. gpt-5-mini is a small-tier model, weaker than Sonnet;
  it is accepted here because reading a graduation window or an identity gate off
  posting text is a far easier task than the benchmarks that separate the tiers.

Three deliberate safety properties:

* Without any API key, nothing is dropped. Every change is emitted unclassified into
  WORTH A LOOK. Spec section 8 rule 3 is explicit that a false negative -- FTTP opens
  and the tool calls it noise -- is the failure that costs real money, so the
  degraded mode surfaces more, not less. Adding a second provider must not weaken
  this: an unreachable provider degrades exactly like a missing key.
* The `eligible` column is never rewritten. A proposed change is logged and reported
  (rule 5); hand-verified research is not overwritten by a model.
* A provider failure is never silently swallowed. Every failure path returns a
  `Judgment` carrying `error`, so the digest reports it rather than showing a change
  as benignly unclassified (spec 10.1: a failure must never look like a quiet day).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import state

MODEL = "claude-opus-5"

# The Azure side is pinned to the Foundry deployment created for this project. Both are
# overridable by env so the repo is not welded to one resource, but defaulting them here
# means the workflow needs exactly one new secret (the key) rather than three.
AZURE_DEFAULT_DEPLOYMENT = "gpt-5-mini"
AZURE_DEFAULT_ENDPOINT = "https://opptracker-ai-jshi.openai.azure.com/"
AZURE_DEFAULT_API_VERSION = "2024-12-01-preview"

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

# OpenAI's strict json_schema mode requires every declared property to appear in
# `required`; Anthropic's does not. `eligible_proposal` is genuinely optional -- rule 5
# means it is present only when the model wants to propose a value -- so the strict
# variant lists it and lets the empty string carry "no proposal". Derived rather than
# copied so the two schemas cannot drift apart.
STRICT_RESULT_SCHEMA = {
    **RESULT_SCHEMA,
    "required": list(RESULT_SCHEMA["properties"]),
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


ANTHROPIC = "anthropic"
AZURE = "azure"


def select_provider() -> str | None:
    """Which backend to use, or None for degraded (unclassified) mode.

    Anthropic wins when both are configured: the system prompt was written against
    Claude, and gpt-5-mini is a deliberate cost substitution rather than an equal.
    `CLASSIFIER_PROVIDER` forces one either way, which is what the tests use to
    exercise a specific path without unsetting real credentials.
    """
    forced = (os.environ.get("CLASSIFIER_PROVIDER") or "").strip().lower()
    if forced in (ANTHROPIC, AZURE):
        return forced
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ANTHROPIC
    if os.environ.get("AZURE_OPENAI_API_KEY"):
        return AZURE
    return None


def build_client(provider: str):
    """Return (client, deployment). Raises; the caller degrades on any exception."""
    if provider == ANTHROPIC:
        import anthropic

        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("CLASSIFIER_PROVIDER=anthropic but ANTHROPIC_API_KEY is unset")
        return anthropic.Anthropic(api_key=key), MODEL

    from openai import AzureOpenAI

    key = os.environ.get("AZURE_OPENAI_API_KEY")
    if not key:
        raise RuntimeError("CLASSIFIER_PROVIDER=azure but AZURE_OPENAI_API_KEY is unset")
    client = AzureOpenAI(
        api_key=key,
        azure_endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT", AZURE_DEFAULT_ENDPOINT),
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", AZURE_DEFAULT_API_VERSION),
    )
    return client, os.environ.get("AZURE_OPENAI_DEPLOYMENT", AZURE_DEFAULT_DEPLOYMENT)


def _judgment_from_text(change: state.Change, text: str) -> Judgment:
    """Parse a provider's JSON body into a Judgment. Shared by both backends."""
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


def _classify_anthropic(client, model: str, change: state.Change) -> Judgment:
    try:
        response = client.messages.create(
            model=model,
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
    return _judgment_from_text(change, text)


def _classify_azure(client, deployment: str, change: state.Change) -> Judgment:
    try:
        response = client.chat.completions.create(
            model=deployment,
            # gpt-5-mini is a reasoning model: the ceiling is `max_completion_tokens`
            # (`max_tokens` is rejected) and it must cover the hidden reasoning tokens
            # as well as the JSON, so the same generous 4096 applies here.
            max_completion_tokens=4096,
            reasoning_effort="low",
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "judgment",
                    "strict": True,
                    "schema": STRICT_RESULT_SCHEMA,
                },
            },
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _render_change(change)},
            ],
        )
    except Exception as exc:
        return Judgment(change=change, error=f"{type(exc).__name__}: {exc}")

    choice = response.choices[0] if response.choices else None
    if choice is None:
        return Judgment(change=change, error="Azure returned no choices")
    # Azure's content filter and the token ceiling both produce a usable HTTP 200 with
    # an empty or partial body. Neither is a quiet day (spec 10.1), so both become an
    # error on the Judgment rather than a silently unclassified change.
    if choice.finish_reason == "content_filter":
        return Judgment(change=change, error="Azure content filter declined this change")
    if choice.finish_reason == "length":
        return Judgment(change=change, error="response hit max_completion_tokens before completing")

    return _judgment_from_text(change, choice.message.content or "")


def classify_one(provider: str, client, deployment: str, change: state.Change) -> Judgment:
    """Dispatch one change to whichever backend is configured."""
    if provider == ANTHROPIC:
        return _classify_anthropic(client, deployment, change)
    return _classify_azure(client, deployment, change)


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

    provider = select_provider()
    if provider is None:
        for change in to_classify:
            judgments.append(
                Judgment(
                    change=change,
                    program_name=change.program_name,
                    why="Not classified: no classifier credentials are configured "
                    "(neither ANTHROPIC_API_KEY nor AZURE_OPENAI_API_KEY), so this is "
                    "surfaced unjudged rather than dropped.",
                )
            )
        return judgments

    try:
        client, deployment = build_client(provider)
    except Exception as exc:
        for change in to_classify:
            judgments.append(
                Judgment(change=change, program_name=change.program_name, error=str(exc))
            )
        return judgments

    for change in to_classify:
        judgment = classify_one(provider, client, deployment, change)
        if judgment.eligible_proposal:
            state.append_proposal(
                f"{change.source_id}\t{change.key}\tproposed eligible="
                f"{judgment.eligible_proposal}\t{judgment.why}"
            )
        judgments.append(judgment)
    return judgments
