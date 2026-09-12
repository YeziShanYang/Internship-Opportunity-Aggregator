# Refactor plan

**Status 2026-09-12.** Two bodies of work are recorded here.

*Shipped and pushed:* the three mechanisms ported from
[zshah101's tracker](https://github.com/zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships)
— malformed-payload refusal, the deterministic pre-screen (`screen.py`, 36% of model
calls removed with zero wrong rule-outs against a labelled run), and the circuit
breaker. Then the whole source audit: 12 boards added, an `eightfold` method reaching
Millennium's 59 campus postings, redirect detection, and the automatic ATS probe.
See "Part 1 outcome" at the end and `git log` from `ddfc990`.

*Shipped:* **the stage refactor, Part 2 below.** All ten steps (0 through 9) landed.
See "Part 2 outcome" at the end for what actually happened, including the four places
the plan turned out to be wrong about the code. Part 2's step list is kept as a record
of the sequence, not as work to do.

Baseline when Part 2 started: **157 sources**, **148 tests**. Now: **158 sources**,
**219 tests** green via `.venv/bin/python -m unittest discover -s tests`.

This document is a proposal plus a record. It is **not** a description of how the code
currently works — `CLAUDE.md` in the parent directory is. Delete sections as they stop
earning their place.

## Why bother: the honest comparison

Their engine is better than ours at almost every *mechanical* concern and irrelevant to
us at several *product* ones, which is why the move was never to rewrite our code in
their shape but to port specific mechanisms and decline others.

## Honest scorecard

### Where they are plainly better

| # | Thing | Them | Us |
|---|---|---|---|
| 1 | **Classification cost** | zero model calls; `requirements.txt` is httpx, requests, supabase | $0.34/day measured, up to $388/yr at our own cap |
| 2 | **Coverage** | 4,803 endpoints / 4,550 employers, grown automatically | 157 sources, grown by hand |
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

### Phases 1-3 — SHIPPED

- **Phase 1**, malformed-payload refusal, narrowed: the truncation half was never
  built because Workday and Phenom already refuse a partially-paged board and the
  single-shot APIs have no cap. The real hole was `{"error": "...", "jobs": []}`
  reading as an empty board on the 13 boards that legitimately sit at zero rows.
- **Phase 2**, `screen.py`: 28 of the model's 40 rule-outs reproduced with **zero**
  wrong rule-outs among the 37 it kept, so 36% of calls disappear.
- **Phase 3**, the circuit breaker: 6h→72h backoff, one success resets, `--only`
  bypasses. Testing it turned up the 403 nine — see the addendum.

### Phase 4 — An HTTP layer with retries and per-host politeness

- Port the shape of `net.py`: one place that owns retries, exponential backoff
  with jitter, `Retry-After` honouring, and per-host **and** per-provider
  concurrency caps.
- I would **not** port their async rewrite. Our `ThreadPoolExecutor` is fine at
  157 sources and async would touch every module. Revisit only if Phase 5 pushes
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

---

# Part 2 — the stage refactor (SHIPPED)

## Why

Responsibilities are smeared across modules. The *import graph is already clean and
acyclic* — the tangle is **inside functions, and in where I/O happens**:
`sources/job_boards.py check()` does fourteen distinct things including a disk read and
markdown rendering; `classify.py` makes network requests in the middle of a model-call
stage; `digest.py` queries the GitHub API from inside rendering. There is nowhere to
put a new step, and no way to inspect what any step produced.

Two decisions already made and not to be relitigated: **materialised stages** (each
writes an artifact the next reads, so every stage is separately runnable), and **flat
stage modules, not a `src/` package** — the benefits of a `src/` layout accrue to
distributed libraries, and this is a single-owner script that runs in one Actions job
and is never pip-installed.

## Target shape

```
run.py                  CLI: gather | process | enrich | screen | classify | render
                             | deliver | all   (replaces check.py's entry point)

core/       (level 0)   Shapes and constants only. No I/O of any kind.
  models.py             Change, SourceResult, Metrics, Judgment, Verdict
  paths.py              paths, USER_AGENT, timeouts, column lists
  clock.py              utcnow/iso/today_iso/sha256_text/change_key
  profile.py            OWNER_PROFILE + PROFILE_LAST_REVIEWED

persist/    (level 1)   ALL storage, READ AND WRITE. Sits below gather, not last.
  store.py              every read_*/write_* (from state.py)
  artifacts.py          .run/*.json stage handoff

gather/     (level 2)   ONLY network I/O for sources. No parsing, no filtering.
  breaker.py            quarantine_state - this is fetch policy, so it lives here
  github_readme.py      fetch a README
  ats.py                fetch an ATS feed (greenhouse/lever/ashby/workday/phenom
                        /eightfold/workable)
  page.py               fetch a page as HTML, recording the FINAL url
  resolve.py            NEW: domain-anchored ATS probe -> proposals

process/    (level 3)   ONLY pure transformation. No network, no model.
  parse_readme.py       (from sources/github_repos.py, parsing half)
  parse_ats.py          (from sources/job_boards.py, parsing + filtering half)
  parse_page.py         (from sources/page_watch.py, normalising half)
  snapshot.py           Row/Snapshot/diff (from sources/snapshot.py)
  suppress.py           the muted + applied.tsv filters, currently loose in main()

enrich/     (level 4)   The SECOND I/O stage, named rather than hidden. Fetches
                        posting bodies for CHANGED rows only — O(changes), not
                        O(sources). Today this hides inside classify.py.

screen/     (level 5)   ONLY deterministic verdicts. (today's screen.py)
classify/   (level 6)   ONLY model calls. (classify.py minus its fetching)
deliver/    (level 7)   Rendering, the HEALTH strings, urgency, and the Issue POST.

jobs/                   Composes stages into a runnable job. Not a stage itself.
  daily.py              the morning digest
  discovery.py          the Monday pass (today's discover.py)
  resolve.py            the ATS re-resolution pass
```

**On the two I/O stages.** A single "gather everything up front" stage is not
achievable, and pretending otherwise would be the dishonest kind of tidy: which
postings to fetch is only known *after* diffing, because we fetch bodies for
changed rows and not for the other ~4,000 postings on the boards. So there are
exactly two source-network stages, `gather` and `enrich`, and the pipeline says
so out loud. (The zshah repo does the same — its `enrich.py` is "O(new roles),
not O(all jobs)".)

**Three corrections to my first draft of this layering, all of which were wrong:**

1. **`persist` cannot be last.** I had it as "ONLY writes, at the end". But
   `process` needs yesterday's snapshot and `classify` needs `applied.tsv`, so
   "a stage may only import earlier stages" would forbid every read. `persist`
   is therefore **level 1 storage, read and write, below gather**.
2. **`deliver` must import `httpx`.** `digest.deliver` POSTs the issue and
   `delivered_issue_exists` GETs the issue list, so "only gather/enrich may
   touch the network" was unsatisfiable as written. The rule becomes: only
   `gather`, `enrich` and `deliver` may import `httpx`.
3. **`discover.py` is not a stage.** It does GitHub search (gather), snapshot
   mining (read), `record()` (persist) and `lines()` (render) — it is a second
   *job* that composes stages, hence `jobs/`. Same for the new resolver.

The rule that makes the rest hold: **a stage may only import stages at or below
its own level, and only `gather`/`enrich`/`deliver` may import `httpx`.** Both
are mechanically checkable, and `tests/test_layering.py` will check them by
walking the AST of every module — not by importing them, because importing
`classify`/`digest` executes module bodies and still cannot see function-local
imports, which is exactly where violations hide. A rule that lives only in a
document is how the source bug happened in the first place.

## The migration sequence

Each step is one commit that leaves the suite green, so a regression is
attributable to a step rather than to "the refactor".

### Step 0 — harden the test harness FIRST (before touching any production file)

This is counter-intuitive and it is the most important step. My instinct was to
start with `state.py` because everything imports it. That is a trap.

`state.read_snapshot` and friends resolve `SNAPSHOTS`/`PROGRAMS_CSV` as **module
globals at call time**, and the `_IsolatedState` test mixin
(`tests/test_acceptance.py:698`) works by rebinding exactly those globals to a
temp directory. The moment that I/O moves into `persist/store.py` and does
`from core.paths import SNAPSHOTS`, the rebind silently stops working — and
~60 tests keep passing **while writing into the real `data/` tree**. A green
suite that has quietly stopped protecting the database is the worst possible
state to migrate from.

This is not hypothetical: it has already happened twice in this codebase.
`postings.CACHE_DIR` is import-bound at `sources/postings.py:46`, so tests
already write into the real `data/postings_cache/`, and `_IsolatedState` never
rebinds `PROGRAMS_CSV`/`SOURCES_CSV`, which is why three tests read live data.

So, one commit, zero production files:

- Move `if __name__ == "__main__": unittest.main()` from line 1161 to the end of
  the file. Expect the count to *rise* above 148 — ~60 tests defined below it
  never run under direct invocation. Any newly-revealed failure is a
  pre-existing bug; fix it in this commit or mark it `expectedFailure` with a
  reason.
- Extend `_IsolatedState` to also rebind `PROGRAMS_CSV`, `SOURCES_CSV`,
  `DISCOVERED_CSV` and `postings.CACHE_DIR`, using `addCleanup` rather than
  `tearDown` so a failure in `setUp` still restores.
- Add `setUpModule`/`tearDownModule` that record a manifest (path → mtime+size)
  of the real `data/` tree and assert it is unchanged afterwards. **This is the
  safety net for every later step**: it turns "silently wrote to the database"
  from invisible into a hard failure.
- Move the three live-data tests into a named `LiveDataInvariantTests` so the
  isolation rule reads as "everything except this class".
- `ScreenIntegrationTests` currently makes a **live model call** if a screen rule
  stops matching. Monkeypatch `classify.select_provider` to `None` in `setUp`.

### Step 1 — unbundle `state.py` (two commits)

`state.py` does four unrelated jobs. Split into `core/paths.py`, `core/models.py`,
`core/clock.py`, `gather/breaker.py` (the quarantine logic is fetch policy) and
`persist/store.py` (all file I/O). Every function in `store.py` must reference
`paths.SNAPSHOTS` rather than importing the name, preserving test rebinding.
`state.py` survives one commit as a re-export shim, then goes.

The cleanest single win in the whole refactor falls out here:
`sources/snapshot.py` imports `state` *solely* to construct `state.Change`, so it
drops its persistence dependency entirely.

### Step 2 — freeze the snapshot format with a gate test

I had this wrong and it is worth correcting, because it changes the fix.
`Row.url` is *not* lossy: both producers set `url = _URL.findall(value)[0]` and
`value` retains every URL, so `parse_snapshot`'s recovery is exact by
construction — measured, `render_snapshot(parse_snapshot(t)) == t` for **74 of
74** committed `.tsv` files.

The real defect is that `url` is **positional**. On 631 of 1,376 rows `value`
holds two or three URLs, and for Simplify rows `urls[0]` is the *company* page
(`simplify.jobs/c/…`) rather than the posting page (`simplify.jobs/p/…`) — which
is exactly what `postings.posting_url` exists to work around. So the fix is
**typed URL roles** (`listing_url` / `posting_url` / `apply_url`), not lossless
serialisation.

Either way the committed TSV must not change: rewriting ~144 snapshots is a
one-way whole-file diff that destroys the `git log` audit trail which is the
entire reason state lives in this repo, and it would make the next run report
every row as changed. So commit the 74-file round-trip as a gate test **before**
touching `snapshot.py`, ensuring the refactor cannot be the thing that quietly
"fixes" a format drift.

### Step 3 — introduce `run.py` and `.run/` (cheap, fully reversible)

All eight verbs exist; `all` initially just calls `check.main(argv)`. No
behaviour change, no test churn — but from here every later step can be verified
by diffing `.run/*.json` before and after, which is stronger evidence than the
unit tests alone.

### Step 4 — decompose the three `check()` functions, via shim

One commit each, easiest first: `page_watch` → `github_repos` → `job_boards`.
For each, extract `gather/<type>.fetch(source, client) -> Fetched` and pure
`process.plan/parse/assess`, then rewrite the old `check()` **in place** as a
five-line shim that calls them. The shim is the point: `check.CHECKERS` and every
`FakeJSONClient`/`FakeHTMLClient` test keep passing untouched while the internals
move. `previous` becomes a parameter, which is what finally lifts
`read_snapshot` out of the gather layer.

Two things to fix *while* moving rather than before: `job_boards.py:643`'s
re-parse of the diff key (`to_snapshot` already knows the posting, so emit row
and text together and join on identity), and the `MAX_CHANGES_PER_BOARD` collapse
at `:648`, which renders markdown inside gather — move the prose to `deliver/`
and keep only the count here.

**`job_boards` is the single riskiest commit in the plan.** The plausibility
check, the two suppression counters and the collapse are three independent
behaviours that each report to HEALTH, and any one going quiet is precisely the
silent-filter failure this project has already been bitten by twice.

### Step 5 — gather-only, and drop the shims

`run.py gather` writes the artifact; `run.py process` reads it. `check.py`
becomes a deprecation shim.

### Steps 6–9 — un-mix `classify` and `deliver`

6. Hoist `postings.fetch_for_changes` out of `classify.classify` into `enrich/`.
7. Move the screen loop and its module-global `SCREEN` counter into `screen/`.
8. Move `screen_line()`/`usage_line()` into `deliver/health.py` as functions over
   data, `Judgment.urgent` into `deliver/urgency.py`, and the owner profile into
   `core/profile.py`. After this `deliver` no longer imports `classify` at all.
9. Route every remaining write through `persist`, and turn on the layering test.

### Where to stop

**Stop after step 5 if time runs out.** Steps 0–5 deliver the whole structural
payoff: `state.py` unbundled, the three `check()` monsters reduced to
gather-plus-process, `snapshot.py` free of its persistence dependency, and a test
harness that can no longer lie. Steps 6–9 are quality; the code is coherent
without them. Never stop mid-step-4 with one source split and two not.

Split the 1,736-line test file **as each stage lands**, not upfront — a move that
size plus a semantic change in one commit is unreviewable.

### Out of scope, and named so

`build_xlsx.py`, `add_opportunity.py`, `seed_programs.py` and
`calendar_reminders.py` are ~1,100 lines of entry point that sit outside the
stage model. They keep importing `core` + `persist` via a compat shim. Folding
them in is a later decision, not part of this.

## Stage artifacts

Written to `.run/` and **gitignored** (it is not in `.gitignore` today), following
the precedent already set by `data/postings_cache/` — derived, re-fetchable data
that would otherwise bury the CSV history `git log` exists to answer. The raw
bodies in particular are ~157 fetched pages and must never be committed.

| Artifact | Written by | Contains |
|---|---|---|
| `.run/raw/{source_id}.body` | `gather` | the undecoded bytes exactly as fetched |
| `.run/raw/index.json` | `gather` | ~157 × `FetchAttempt`: status, size, sha256, final URL, timing (~36 KB) |
| `.run/changes.json` | `process` | `[Change]` + `SourceMetrics` + `[FilterReport]` |
| `.run/enriched.json` | `enrich` | `dict[change_id, PostingBody]` |
| `.run/screened.json` | `screen` | `dict[change_id, Verdict]` — absent means "no opinion" |
| `.run/judged.json` | `classify` | `[Judgment]` + `Usage` |
| `.run/digest.md` | `render` | the exact issue body that would be posted |

**Raw bodies are per-source files, not one `raw.json`.** JSON-escaping a 300 KB
HTML page inflates it 15–30% for nothing; `run.py process --only X` should not
parse 15 MB to read one source; and writing bytes verbatim defers the decode
decision to `process`, where the parser knows the encoding, rather than forcing
a lossy `response.text` at the gather boundary. The other artifacts stay single
files — they are small and their whole value is being readable in one look.

`run.py all` runs every stage in order in one process, so the Actions workflow
keeps its current single step and nothing about the schedule changes.

## The inter-stage contracts

Three ideas carry most of the value here.

**1. Three fields for three jobs, instead of one string doing all three.** Today
`key` is a rendered human string that is *also* the diff identity and is *also*
re-parsed as structured data in four places. It splits into:

- `key` — the rendered string, **byte-identical to today**, because it is TSV
  column 2 and the input to `state.change_key`; changing it re-keys
  `applied.tsv` and invalidates 130 files.
- `change_id` — an opaque stable hash, computed once in `process`, used as the
  join key by every downstream stage.
- `facts: RowFacts` — the structured payload (`entity`, `role`, `location`,
  `ordinal`, and the three typed URL roles).

The key realisation: **nothing needs to be inferred.** `github_repos.extract`
already has the parsed `cells` (`Company`, `Role`, `Location`, `Application`)
and flattens them into `"Company=…; Role=…"`; `job_boards.to_snapshot` already
holds a `Posting` object and flattens it to `"Type=…; URL=…"`. Every re-parse
site is re-deriving data that was in hand two frames up the stack. So this
deletes `postings.posting_url`'s regex, `job_boards.py:643`'s
`key.split(" @ ")[0].split(" #")[0]`, and `digest`'s `_LINKED_ENTITY`/`_unlink`
markdown-link heuristics — including the aggregator-employer bug I fixed by hand
yesterday, which stops being possible.

`RowFacts` also carries `provenance: source | tsv_recovered`, because a
`removed` change only exists in the *old* TSV, so its facts are best-effort
while an `added`/`changed` row's are authoritative. That is the same idea as the
zshah repo's `DATE_PRECISION`, and it lets the renderer degrade honestly instead
of printing a worse guess with equal confidence.

**2. `SourceResult.extra` dies into two typed shapes.** `SourceMetrics` for the
counters, and one `FilterReport` shape that **all six filter sites** use:

```python
@dataclass(frozen=True)
class FilterReport:
    stage: str; filter_id: str
    considered: int = 0; removed: int = 0
    source_id: str = ""      # "" = run-wide
    reason: str = ""         # one human sentence; NEVER parsed
    samples: tuple[str, ...] = ()   # up to 5 removed keys, for audit
```

This is the part that most directly serves the project's own hard rule. Two of
the six filters currently report through **nothing at all** — `check.main`'s
`muted` filter, and `github_repos`' `section_include`/`section_exclude`/
`ignore_columns`. With one shape, *"this filter removed rows and emitted no
`FilterReport`"* becomes a testable assertion instead of an invisible hole.

**3. `posting_text` leaves `Change`, and `Judgment` gets a discriminator.**
`Change` becomes frozen, and the three separate population paths for
`posting_text` collapse into `enrich` writing one table keyed on `change_id`
(not URL — an inline-ATS body has no URL to key on, and two changes can share
one). `Judgment` gains `outcome: model | screened | unclassified | error |
over_budget`, which collapses the five different shapes its five call sites
produce today into one shape with five named constructors; `classified` becomes
a derived property rather than a field set by hand.

**Delete `classify.screen_line()` and `usage_line()`.** They emit markdown from
a stage forbidden to emit markdown, *and* they read module globals — which is
concretely why stages cannot run independently today: `run.py render` would
print an empty HEALTH block against freshly-initialised globals. They become
data on the artifacts (`filters`, `usage`) and `deliver` owns every string.
Likewise `Judgment.urgent` becomes a pure function in `deliver/`, since three of
its inputs live on `Change` and were never properties of the model's answer.

**Serialisation: a hand-rolled ~70-line codec.** Not pydantic — that is 4→6
packages including a compiled wheel, to validate files this same process wrote
200 ms earlier. Not bare `dataclasses.asdict` either: it has no inverse, and
`tuple` round-trips to `list`, which breaks `frozen=True` equality. Write with
`json.dumps(..., indent=2, sort_keys=True, ensure_ascii=False)` so field order
never causes a whole-file diff and the emoji headings and homoglyph canaries stay
legible in one. A `version` mismatch is a hard error: a stale `.run/` from before
a field rename must fail loudly rather than read as a quiet day.

## Verification

- `.venv/bin/python -m unittest discover -s tests` stays green at every step.
  **Not** `python tests/test_acceptance.py` — `if __name__ == "__main__"` sits
  mid-file at line 1161, so direct invocation silently skips the ~60 tests
  defined below it. Fixing that is part of the test-split step.
- `tests/test_layering.py` walks every module's AST and asserts no stage imports
  a higher level, and that only `gather`/`enrich`/`deliver` import `httpx`. AST
  rather than `importlib`, because importing `classify`/`digest` executes their
  module bodies and still cannot see function-local imports — which is where
  violations hide. A third assertion: a write call (`open(..., "w")`,
  `write_text`, `mkdir`) outside `persist/` is a failure, with an explicit
  `ALLOWED_EXCEPTIONS` set so violations are listed rather than hidden.
- **The 74-file snapshot round-trip gate**, committed before `snapshot.py` is
  touched: `render_snapshot(parse_snapshot(t)) == t` for every committed `.tsv`.
- **"Every filter reports what it removed" becomes executable**: a test asserts
  that any `FilterReport` with `removed > 0` was actually emitted onto the
  artifact, closing the two filters that today report through nothing at all.
- A codec gate test: `load(C, json.loads(s)) == obj` and `write(write(x)) ==
  write(x)` for every artifact type.
- A golden-output test: run the full pipeline against the committed snapshots
  and assert the rendered digest is byte-identical before and after each
  refactor step. This is the real safety net for a move this size.
- `.venv/bin/python run.py all --dry-run` must produce the same digest as
  `check.py --dry-run` does today, on the same inputs.
- `run.py gather` then `run.py classify` twice: the second classify must make
  zero network requests, proving the artifact boundary actually holds.
- `data/snapshots/*.tsv` must be byte-identical after a no-change run — the
  format is the audit trail and 130 files plus every past diff depend on it.
- `resolve` must independently rediscover the 13 boards listed above.

---

## Part 1 outcome (recorded 2026-09-12)

All four items shipped. What actually happened, for anyone picking this up cold:

1. **Redirect detection.** `state.redirect_verdict` compares where we asked to go with
   where we landed; an error-page path fails the source, any other path change is a
   standing HEALTH warning, and scheme/`www.`/trailing-slash differences are normalised
   away because 2 of the 5 real redirects were nothing else. `twosigma-campus`
   re-pointed to `/careers/internships/` (167 rows). **AQR turned out not to be
   re-pointable** — `/Careers` and `/careers/internships` redirect to the homepage,
   `/Insights/Careers` and `/About-Us/Careers` 404, `careers.aqr.com` is a JS shell
   with zero text. It stays and fails loudly; its eligibility research is logged in
   `proposals.log` as needing a human check. Posting coverage is intact via
   `aqr-greenhouse`.
2. **12 boards added**, all verified live and all healthy through the real pipeline.
3. **The probe**, `discover.mine_pages`, rediscovers 11 of 11 hand-found boards from an
   empty knowledge base. Three shapes were load-bearing: one hop deeper (8 of 11
   without it), the `grnhse_app ?for=` embed, and the `boards-api` host.
   Domain-anchored, because the old keyword grep scored 10 false positives in 24 hits.
4. **The `muted` column** now actually mutes, and reports that it did.

Plus an `eightfold` method: Millennium's campus board, 59 postings, **17 US rows kept**.

Two things deliberately left open, both recorded in the source notes:
- **GTS** is on iCIMS with no machine-readable endpoint found; stays `page_text`.
- **The 403 nine** (see `REFACTOR-PLAN.md`'s addendum) are still 403 from the Actions
  runner and 200 from a laptop under the same User-Agent. Two of the nine are now
  redundant anyway. The proxy question is still the owner's to decide and was not
  actioned.

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

---

## Part 2 outcome (recorded 2026-09-12)

All ten steps landed, one commit per step except where a step needed two or three. The
target shape is the shape the code has. 219 tests, 6 skipped without API keys.

### What the verification actually showed

- **129 of the 130 committed snapshots are byte-identical** after a full non-dry run of
  all 153 sources through the refactored pipeline, executed against a copy of `data/`
  so the database was never written. The one that differs, `simplify-2027.tsv`, differs
  by three content lines — a new Klaviyo posting and an InfiniteQuant role retitled —
  which is exactly the three changes the run reported. `sources.csv` came back with the
  same 158 rows and the same columns.
- **The artifact boundary holds.** `run.py gather` then `run.py process` twice gives
  byte-identical `changes.json` with no second fetch; process takes 0.69s over 153
  sources against 43MB of stored bodies. `run.py all` then `run.py render` gives a
  byte-identical digest and title with no network and no model call.
- **All 81 ATS sources returned identical row counts** across the Tier 2 split, and the
  aggregate "4154 postings were not student roles and 397 were outside the US" was
  unchanged to the digit.
- **The layering test catches violations.** Verified by injecting four — an uphill
  `process`→`deliver` import, `httpx` in `process`, `httpx` in `core`, and a
  `write_text` buried inside a function in `classify` — and all four were named with
  file and line.

### Four places the plan was wrong about the code

1. **The `74` in step 2 was right, and "130 snapshots" was a different number.**
   `data/snapshots/` holds 130 files, of which 74 are `.tsv` with a parse/render
   round-trip and 56 are `.txt` page text with no codec. The plan's 74-of-74 measurement
   was correct.
2. **Step 0's `__main__` block hid 86 tests, not ~60**, and sat at line 1170 rather than
   1161. It also could not have revealed new failures, because `unittest discover`
   imports the module and always ran all 148 — the block only ever mattered for direct
   invocation.
3. **`14_9o` could not move to `LiveDataInvariantTests`.** The plan says move all three
   live-data tests there, but that test calls `discover.record()`, which writes
   `discovered.csv`; un-isolating it would have pointed that write at the real database.
   A test asserting "this never writes the database" must not be aimed at the database.
   It got a seeded temp copy instead.
4. **"Move the collapse prose to `deliver/`" was half right.** The markdown did not
   belong in the fetcher, and it is in `process` now. But the collapsed item's `detail`
   is the *classifier's* input, not digest markdown, so moving it into `deliver` would
   have relocated the model's context into the renderer. The collapse is a typed
   `BoardCollapse` on the result so HEALTH cannot forget it; the `detail` stayed where
   the model can see it.

### Three bugs the refactor found, all pre-existing

- **Phenom's recruiting-category filter went silent** the moment the screen moved into
  the parser: the count was still being taken from the fetcher, so a 2-posting board
  reported 1 posting. Caught by the suite, inside the very commit the plan warned was
  the riskiest and for exactly the reason it named. The count now comes from the board's
  own reported total, and the live digest says it out loud for the first time:
  `ats-phenom-category: 161 of 261 rows removed (sig-phenom)`.
- **`github_repos`' three section screens reported through nothing at all.** Measured on
  the live board: Simplify's `section_include` excludes 941 of 1,579 rows and
  `ignore_columns` drops 638 column values, and no digest had ever mentioned either. A
  heading rename would have emptied the source in silence, with the row-count floor
  reporting a README restructure that had not happened.
- **The over-budget `why` was stale.** It said "low-signal source and the row did not
  match the underclassman or rolling-firm filters", which stopped being true when
  triage stopped bypassing low-signal sources.

### What the test harness turned out to be hiding (step 0)

`python tests/test_acceptance.py` collected 62 of 148 tests and printed OK. Three tests
read live data because `_IsolatedState` never rebound `PROGRAMS_CSV`/`SOURCES_CSV`.
`PostingJudgementTests` seeded fixtures into the real `data/postings_cache/` — invisible
only because it skips without a key. And `ScreenIntegrationTests` would have billed a
live model call on the day a screen rule stopped matching. The fingerprint guard in
`setUpModule`/`tearDownModule` now fails the run if `data/` or `out/` moves at all, and
it was verified by deliberately leaking a snapshot write.

### Left undone, deliberately

- **`RowFacts` and full typed URL roles.** The concrete payoff the plan named was
  deleting `postings.posting_url`'s regex and `digest`'s `_LINKED_ENTITY`/`_unlink`
  heuristics. The first is done: `Change.posting_url` is recorded by the producer that
  had the parsed cells, and the regex survives only as a documented fallback for a row
  read back out of a frozen-format snapshot. The markdown-link heuristics in the
  renderer are still there — they parse an *aggregator's own* cell text, which no
  producer upstream has in a better form, so typing the field would move the heuristic
  rather than remove it.
- **`gather/resolve.py` and the `resolve` job.** Listed in the target shape; it is
  Phase 5 work, not part of the stage split, and `jobs.discovery.mine_pages` already
  does the domain-anchored probe weekly.
- **Folding `build_xlsx.py`, `add_opportunity.py`, `seed_programs.py` and
  `calendar_reminders.py` into the stage model.** Scoped out by the plan; they are named
  in the layering test's exception list with a reason each, so the exception is visible
  rather than assumed.
