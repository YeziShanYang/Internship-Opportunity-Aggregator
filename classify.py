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

import concurrent.futures
import json
import os
from dataclasses import dataclass, field

import state
from sources import postings

MODEL = "claude-opus-5"

# The Azure side is pinned to the Foundry deployment created for this project. Both are
# overridable by env so the repo is not welded to one resource, but defaulting them here
# means the workflow needs exactly one new secret (the key) rather than three.
AZURE_DEFAULT_DEPLOYMENT = "gpt-5-mini"
AZURE_DEFAULT_ENDPOINT = "https://opptracker-ai-jshi.openai.azure.com/"
AZURE_DEFAULT_API_VERSION = "2024-12-01-preview"

# Bounds the daily bill. Any overflow still reaches the digest, just unclassified.
# Raised from 60 when the backend moved to gpt-5-mini and postings started being
# fetched: a classification costs roughly $0.0015 including ~6K tokens of posting text,
# so even the worst day observed (162 changes) is about $0.25.
MAX_CLASSIFICATIONS_PER_RUN = 250

# The work is network-bound, so a modest pool collapses the wall clock without
# troubling the deployment's 500K tokens/minute quota.
MAX_CONCURRENT_CLASSIFICATIONS = 6

# The owner's interests are perishable input, not a constant. A stale profile does not
# fail loudly -- it quietly mis-sorts every item in every digest while the output still
# looks well-formed. Bump this date by hand whenever OWNER_PROFILE changes; digest.py
# reads it and nags in the CALENDAR block once it goes stale.
PROFILE_LAST_REVIEWED = "2026-09-11"
PROFILE_REVIEW_AFTER_DAYS = 183  # ~6 months

OWNER_PROFILE = (
    "First-year undergraduate at Stanford, class of 2030, studying math and/or CS. "
    "Does not meet the eligibility criteria for identity-restricted programmes "
    "-- those reserved for women, transgender or gender-expansive students, "
    "underrepresented racial minorities, or LGBTQIA+ students -- nor for \"barriers "
    "to access and opportunity\" criteria. US citizen, US-based. "
    "Regional St. Louis firms are viable summer options. "
    "Interests, in order: quantitative finance and mathematics first, software "
    "engineering second. General finance roles are adjacent and acceptable."
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

6. Class-year gates stated on the posting are decisive. When the POSTING TEXT below
   states a requirement this person cannot meet -- "rising junior", "rising senior",
   "penultimate year", "third or fourth year", "junior or senior standing", a Master's
   or PhD program, or a graduation window in 2027 or 2028 -- set relevant: false and
   quote the exact phrase in `why`. Measured: about a third of these postings carry
   such a gate, so this is the filter that does the most work.
   Absence of a gate is NOT a reason to rule out. "Currently enrolled in a Bachelor's
   degree program" includes a first-year: that is relevant: true.
7. Rule out roles that are clearly outside this person's field. In scope: software
   engineering, quantitative research, trading, mathematics, data/ML, and
   finance-adjacent work. Out of scope, as examples: public relations, human
   resources, marketing, recruiting, change management, IT document automation, and
   general business operations. Only rule out what is plainly unrelated -- when a role
   is technical at all, or you are unsure, keep it. Erring toward applying is correct;
   this person can and does apply broadly.

If the POSTING TEXT is missing or a fetch error is noted, you are judging on a job
title alone. Say so in `why`, use confidence "low", and do NOT rule the item out on
class-year grounds -- you have not seen the requirements.

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
        """Goes in ACT NOW rather than WORTH A LOOK.

        ACT NOW is only useful while it stays short. This used to be "relevant and
        confidently classified", which worked while only high-signal sources were
        classified at all. Once every source was classified that rule promoted every
        generic-but-eligible job-board row -- a measured run put 22 of them in ACT NOW
        and left WORTH A LOOK empty, burying the two or three items that actually
        needed same-day attention.

        So urgency now means one of two specific things: a firm that reviews on a
        rolling basis and closes when full, or a row that reads as aimed at
        first-years. Merely being eligible for something is WORTH A LOOK, not ACT NOW.
        (Failing sources are escalated into the same block separately, by digest.py.)
        """
        if not self.relevant:
            return False
        if self.change.rolling:  # closes when full; time-critical regardless of stage
            return True
        if not self.change.is_discovery_candidate:
            return False
        return self.classified and self.confidence in ("medium", "high")


def triage(changes: list[state.Change], sources: dict[str, dict[str, str]]) -> tuple[list[state.Change], list[state.Change]]:
    """Split changes into (classify, summarise-only).

    Everything is classified now. This used to bypass low-signal sources -- Simplify's
    ~600 untagged roles -- because an Opus call per row cost real money to produce
    noise. Two things changed. The backend is gpt-5-mini, so a row costs ~$0.0015
    instead of ~$0.025; and `sources.postings` now fetches the actual posting, so there
    is finally something worth judging. A Simplify row on its own is a title and a
    location: measured, 7 of 592 rows mention any class-year word, and all 7 are
    incidental matches on "Graduate Researcher". The bypass was skipping rows that
    could not have been judged anyway.

    Skipping them was also what filled WORTH A LOOK with 20-35 unreadable items a day,
    since an unclassified change defaults to `relevant=True`.

    The `signal` column stays in sources.csv: it no longer gates classification, but it
    still marks which sources are noisy, and the overflow sort below prefers rolling
    and discovery rows when a day exceeds the cap.
    """
    to_classify: list[state.Change] = list(changes)
    summarise_only: list[state.Change] = []

    if len(to_classify) > MAX_CLASSIFICATIONS_PER_RUN:
        # Keep the most interesting ones; the rest still get reported, unclassified.
        to_classify.sort(key=lambda c: (not c.rolling, not c.is_discovery_candidate))
        summarise_only.extend(to_classify[MAX_CLASSIFICATIONS_PER_RUN:])
        to_classify = to_classify[:MAX_CLASSIFICATIONS_PER_RUN]
    return to_classify, summarise_only


def _render_change(change: state.Change, posting: tuple[str, str] | None = None) -> str:
    """Render one change for the model. `posting` is the (text, error) from a fetch.

    The distinction between "no posting text" and "the fetch failed" is load-bearing:
    rule 6 tells the model not to rule anything out on class-year grounds when it has
    not seen the requirements, so the failure has to be stated rather than left as a
    silent absence.
    """
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

    text, error = posting if posting else ("", "")
    if text:
        lines.append("")
        lines.append("--- POSTING TEXT (fetched from the live listing) ---")
        lines.append(text[:postings.MAX_TEXT_CHARS])
    elif error:
        lines.append("")
        lines.append(f"--- POSTING TEXT UNAVAILABLE: {error} ---")
        lines.append("Judge on the row above alone. Do not rule this out on class-year")
        lines.append("grounds; you have not seen the requirements.")
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


def _classify_anthropic(client, model: str, change: state.Change, posting=None) -> Judgment:
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
            messages=[{"role": "user", "content": _render_change(change, posting)}],
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


def _classify_azure(client, deployment: str, change: state.Change, posting=None) -> Judgment:
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
                {"role": "user", "content": _render_change(change, posting)},
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


def classify_one(
    provider: str, client, deployment: str, change: state.Change, posting=None
) -> Judgment:
    """Dispatch one change to whichever backend is configured."""
    if provider == ANTHROPIC:
        return _classify_anthropic(client, deployment, change, posting)
    return _classify_azure(client, deployment, change, posting)


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

    # One batch fetch for the whole run rather than a serial fetch per change: ~35
    # postings resolve in well under a second warm, and the module caps its own total
    # wall clock so a hung host cannot stall the daily job.
    fetched = postings.fetch_for_changes(to_classify)

    def judge(change: state.Change) -> Judgment:
        url = postings.posting_url(change)
        return classify_one(
            provider, client, deployment, change, fetched.get(url) if url else None
        )

    # Concurrent because the loop got long. Classifying every source instead of only
    # the high-signal ones took a run from a handful of calls to ~35 on a normal day
    # and 162 on the worst observed; serially at ~3s each that is minutes of runner
    # time for work that is almost entirely waiting on the network.
    #
    # `pool.map` yields results in input order, which matters: proposals.log is an
    # audit trail and must not reorder run to run. Writes happen below, on one thread,
    # after every judgment is in -- `state.append_proposal` appends to a file and is
    # not safe to call from the pool.
    with concurrent.futures.ThreadPoolExecutor(MAX_CONCURRENT_CLASSIFICATIONS) as pool:
        judged = list(pool.map(judge, to_classify))

    for change, judgment in zip(to_classify, judged):
        if judgment.eligible_proposal:
            state.append_proposal(
                f"{change.source_id}\t{change.key}\tproposed eligible="
                f"{judgment.eligible_proposal}\t{judgment.why}"
            )
        judgments.append(judgment)
    return judgments
