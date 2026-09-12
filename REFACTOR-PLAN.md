# Refactor plan: what to take from zshah101's tracker

Written 2026-09-12 after reading
[zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships](https://github.com/zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships)
(8,829 lines across `src/intern_engine/`, 549 test functions, MIT, 741 stars).
Their `ARCHITECTURE.md` is accurate — unusually, the code does what the doc claims.

**This document is a proposal awaiting approval, and should be deleted once it is
either executed or rejected.** It is not a description of how the code works.

## The one-paragraph version

They are building a public product: breadth, freshness, and a dashboard for
thousands of readers. We are building a personal alarm clock: one reader, whose
eligibility is narrow and whose cost of a missed opening is high. Their
architecture is better than ours at almost every *mechanical* concern and
irrelevant to us at several *product* ones. So the move is not to rewrite our
code in their shape — it is to port six specific mechanisms, in dependency
order, and to explicitly decline three of their choices.

## Honest scorecard

### Where they are plainly better

| # | Thing | Them | Us |
|---|---|---|---|
| 1 | **Classification cost** | zero model calls; `requirements.txt` is httpx, requests, supabase | $0.34/day measured, up to $388/yr at our own cap |
| 2 | **Coverage** | 4,803 endpoints / 4,550 employers, grown automatically | 144 sources, grown by hand |
| 3 | **Truncation safety** | every fetch carries `complete`; a partial snapshot may never close a role | `diff_snapshots` reports mass removals from a truncated read |
| 4 | **Malformed vs empty** | `clean_listing` returns None, never `[]`, for junk payloads | no JSON equivalent of our `MIN_TEXT_HTML_RATIO` |
| 5 | **Failure backoff** | circuit breaker, 6h→72h, self-healing, state in git | we escalate at 3 failures and keep hammering forever |
| 6 | **Data provenance** | `DATE_PRECISION` — a real date can never regress to a guess | no notion of field trustworthiness |
| 7 | **Closure evidence** | two consecutive complete misses to close | one diff closes |
| 8 | **HTTP policy** | async, per-host *and* per-provider semaphores, honors `Retry-After` | ThreadPool, no retry, no per-host politeness |
| 9 | **Publish gate** | `verify_accuracy.py` blocks a bad publish on data invariants | tests only; nothing inspects the real data before delivery |
| 10 | **Exactly-once alerts** | outbox drained only after a successful push | we mail independently of whether state committed |
| 11 | **Test depth** | 549 test functions, no network | 75, and `--dry-run` hits the network |

### Where we are better, and should not "modernise" it away

1. **Personalisation is the whole product.** Their `filters.py` answers "is this
   a tech internship in scope". It cannot answer "Jane Street INSIGHT is for
   women, so this person is ineligible", or "graduation window Dec 2027–Aug 2028
   is a junior gate". Our `OWNER_PROFILE` + rules 1–7 are the value; their
   pipeline has no place to put them.
2. **Silence is diagnostic.** We mail every single day, so no digest means
   broken. Their README goes quietly stale if a run dies — `health.json` records
   it, but nothing *tells the reader*. Our spec 10.1 discipline is stronger at
   the level that matters: the human's inbox.
3. **The delivery cap asks GitHub, not a marker.** Because late cron ticks do
   not dispatch in order, a checked-out `last_delivered.txt` can be stale. Their
   outbox is a better exactly-once mechanism, but it does not address
   out-of-order dispatch; ours does. **Port their outbox in addition, not instead.**
4. **We fetch under an honest User-Agent.** They document a `WORKDAY_PROXY`
   secret for "a cheap residential/rotating proxy" to get around
   datacenter-IP blocks. That is the thing we deliberately refuse — Citadel
   stays `manual` precisely because the only route is to lie about the client.
   Do not adopt this. It is the one place their engineering is worse than our
   restraint.
5. **Hand-verified research is protected.** `eligible` and `notes` are never
   rewritten by the tool. They have no concept of a human-owned column.
6. **The owner's file excludes what a watcher already covers.** `covered_by`
   exists so `programs.xlsx` only lists what he must chase himself. They have no
   equivalent, because they have no second surface to be covered by.

## The plan, in dependency order

Each phase is independently shippable and independently revertible. Effort is
my estimate of implementation plus test time.

### Phase 1 — Truncation and malformed-payload safety — **SHIPPED, narrowed**

Shipped as `MalformedPayload` in `sources/job_boards.py`. The truncation half was
**not** built: Workday and Phenom already refuse a board they could only page part
of, and Greenhouse/Lever/Ashby are single-shot full-board APIs with no cap, so the
`complete` flag would have been machinery with nothing to do. The real hole was
`{"error": "...", "jobs": []}` reading as an empty board on the 13 watched boards
that legitimately sit at zero rows.

**Why first:** it is a live correctness bug, not a nicety. Today, a Greenhouse
board that returns `{"error": "rate limited", "jobs": []}` parses as zero rows.
Zero rows is already an alert (good), but a board that returns *page one of
three* parses as a legitimate snapshot, and every row on pages two and three
reads as removed. We have watched 69 ATS boards for two days; this has not
bitten yet, and it will.

- Add `complete: bool` and `incomplete_reason` to the snapshot contract in
  `sources/snapshot.py`, mirroring their `Fetch` / `INCOMPLETE_*` constants.
- Port `clean_listing`'s three disqualifiers into `sources/job_boards.py`: not
  an object, truthy `error`/`errors`, or a non-object list member → treat as
  malformed, never as empty.
- `diff_snapshots` grows a rule: **an incomplete snapshot may not emit
  `removed` changes.** It may still emit `added`.
- Report the complete/incomplete split in HEALTH, per the "every filter reports
  what it removed" rule.

*Effort: ~1 day. Risk: low. Payoff: removes a whole class of false "the
programme closed" reports.*

### Phase 2 — Deterministic pre-screen — **SHIPPED**

Shipped as `screen.py`. Validated against the 2026-09-12 run as a labelled set:
reproduces 28 of the model's 40 rule-outs with zero wrong rule-outs among the 37
it kept, so 36% of calls disappear (the plan guessed 40-60%).

**Why:** this is the cost answer, and it is also a *quality* answer. Their
`sponsorship.py` shows the pattern: phrase-anchored regexes over whole
expressions employers actually write, precision favoured over recall, each with
tests. Measured on our 2026-09-12 run, 81% of the bill was reasoning tokens
spent re-deriving things a regex can settle.

- New `screen.py`: deterministic pre-filters that run *before* `classify.py`.
  Start with the two cheapest and highest-volume judgments, both of which are
  currently done by the model:
  - **Class-year gates.** "rising junior", "rising senior", "penultimate year",
    "third or fourth year", "junior or senior standing", Master's/PhD-only,
    graduation windows in 2027–2028. Per CLAUDE.md about a third of postings
    carry one of these.
  - **Plainly out-of-field roles.** Their `_NON_TECH_EXCLUDE_RE` is the model.
- Adopt their **`VERSION` constant** idea wholesale: `screen.VERSION` is stored
  with each verdict, and bumping it re-screens the backlog. This is the piece
  that makes deterministic rules *improvable* rather than frozen, and we have no
  equivalent today.
- The model then runs only on what survives — genuinely ambiguous personal fit.
  **Rule 3 is preserved: a regex may only rule something out on an explicit
  quoted phrase, never on absence of evidence.** Everything else still reaches
  the digest.

*Effort: ~2–3 days, mostly writing tests against real posting text we already
have cached. Risk: medium — this is the one phase that can lose a real
opportunity, so every rule needs a quoted-phrase requirement and a test.
Payoff: I estimate 40–60% of calls eliminated, on top of the `minimal` effort
change already shipped.*

### Phase 3 — Circuit breaker — **SHIPPED**

Shipped in `state.quarantine_state` + `check.run_sources`. Note the finding that
came out of testing it: the nine sources it would quarantine are all HTTP 403 from
the Actions runner and HTTP 200 from a laptop on the same User-Agent, so they are
datacenter-IP blocked rather than misconfigured. See "The 403 nine" below.

- Port `health.py` almost directly: 3 consecutive failures → quarantine
  6h/12h/24h/48h/72h, one success resets, state in a committed CSV so it is
  auditable in git like everything else.
- Keep our escalation-into-ACT-NOW behaviour; the breaker changes *fetch*
  policy, not *reporting* policy. A quarantined source must still appear in
  HEALTH, or we have built the silent-blindness failure we keep warning about.

*Effort: ~1 day. Risk: low. Payoff: dead sources stop costing time every run;
matters much more once Phase 5 adds hundreds of small-firm boards.*

### Phase 4 — An HTTP layer with retries and per-host politeness

- Port the shape of `net.py`: one place that owns retries, exponential backoff
  with jitter, `Retry-After` honouring, and per-host **and** per-provider
  concurrency caps.
- I would **not** port their async rewrite. Our `ThreadPoolExecutor` is fine at
  144 sources and async would touch every module. Revisit only if Phase 5 pushes
  us past ~1,000 endpoints.

*Effort: ~1 day. Risk: low. Payoff: correctness under rate limits, and we stop
being impolite to Greenhouse, which hosts 45 of our boards on one host.*

### Phase 5 — Automated source discovery, which is the small-firm answer

This is the phase that solves the problem we were mid-way through when this
comparison started. Measured today: filtering our existing aggregators to
St. Louis + the Bay yields 94 rows but only **27 distinct companies**, dominated
by TikTok (40) and ByteDance (15) — the aggregators are themselves biased toward
large employers. Their `discover.py` + `harvester.py` is the fix: mine public
datasets for ATS tokens, probe candidate slugs across ATS vendors, keep the hits.

- Port the harvester pattern, seeded by the **YC OSS company export**
  (`yc-oss.github.io/api/companies/all.json`, 10.4 MB, HTTP 200 under our own
  User-Agent, 6,214 companies with `team_size`, `all_locations`, `isHiring`).
  Filtered to active + Bay Area + 10–200 people gives 576 firms.
- Keep our rule that **discovery proposes and never adds**. Theirs writes
  straight into `companies.json`; ours must keep writing to `discovered.csv`.
- St. Louis needs a different list — YC has only **3** STL companies. That is a
  separate research task, not a code task.

*Effort: ~3–4 days. Risk: medium, mostly cost — hundreds of new boards multiply
Phase 2's savings in the wrong direction unless the weekly cadence and the
deterministic screen land first. **Do not start this before Phase 2.***

### Phase 6 — Outbox, and a pre-delivery data gate

- Port `outbox.py`: the digest queues, the state commit pushes, and only then
  does delivery drain the queue. Keep `delivered_issue_exists` as well — it
  solves out-of-order dispatch, which the outbox does not.
- Port `verify_accuracy.py` as a pre-delivery gate over our own invariants:
  every row in the digest has a source that fetched successfully, no row cites a
  `source_id` that is not in `sources.csv`, `eligible`/`notes` are byte-identical
  to the last commit, thresholds set well below healthy so normal variation
  never blocks a send.

*Effort: ~2 days. Risk: low. Payoff: a failed push can no longer produce a
digest that promises state which was never saved.*

### Explicitly declined

| Their choice | Why not |
|---|---|
| Residential proxy for Workday/Oracle | The honest-User-Agent principle. This is the one place restraint beats their engineering. |
| Public dashboard, Atom feed, JSON API, email list, Supabase, Discord, Telegram | One reader. Every one of these is a surface to maintain for an audience we do not have. |
| Rewrite to `src/` package layout + pyproject | Real but cosmetic at 6,067 lines. Cheap to do later; it buys nothing on its own and touches every import. |
| Async I/O throughout | Only earns its complexity past ~1,000 endpoints. |
| Dropping the LLM entirely | It is the only thing that can read an identity gate or a graduation window against one person's profile. Phase 2 narrows it to where it is actually needed. |

## Suggested order and stopping points

1. **Phase 1** — correctness bug, do regardless.
2. **Phase 2** — the cost answer. Biggest single win; also the riskiest, so it
   gets the most tests.
3. **Phase 3 + 4** — cheap, independent, make Phase 5 safe.
4. **Phase 5** — the small-firm coverage you actually asked for.
5. **Phase 6** — hardening; fine to defer indefinitely.

A reasonable stopping point is after Phase 4: the correctness holes are closed,
the bill is down by roughly three quarters, and nothing about the product has
changed. Phases 5 and 6 are genuine scope increases and worth deciding on
separately.


## Addendum: the 403 nine (found 2026-09-12 while testing Phase 3)

Nine `page_text` sources sit at three consecutive failures with `last_success`
empty: akuna-academy, capstone-careers, crabel-careers, graham-careers,
gts-careers, millennium-students, tower-careers, tudor-careers,
twosigma-campus.

All nine return **HTTP 403 from the GitHub Actions runner and HTTP 200 from a
laptop under the identical `state.USER_AGENT`**. The URLs are correct and the
client is not being refused — the runner's IP range is. That makes this a
different category from Citadel, which refuses our User-Agent and serves a
browser string, and where the only route through is to misrepresent the client.
Here nothing is being misrepresented; the request simply comes from a
datacenter.

Two of the nine are already covered by an ATS source we watch
(`akunacapital-greenhouse`, `towerresearch-greenhouse`), so those two page
watchers are redundant and can be deleted outright. The remaining seven need an
ATS fingerprint hunt per the CLAUDE.md gotcha, which is Phase 5 work.

This is the decision that should be made deliberately rather than by default:
a proxy would recover all nine, and it does not involve lying about the client
the way the Citadel case would. It is still circumventing a block the firm chose
to apply. Worth a conversation before anyone reaches for it.
