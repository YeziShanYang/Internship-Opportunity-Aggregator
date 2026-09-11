# opportunity-tracker

A daily job that watches a fixed list of sources for quant / math / CS opportunities
relevant to one specific person — a Stanford first-year, class of 2030 — and emails a
digest by opening a GitHub Issue.

Runs on GitHub Actions, so it does not care whether your laptop is awake. State lives in
this repo as CSVs, which makes **git the database**: `git log` answers "which page
changed on which morning" forever, for free.

**Status: Phases 1 and 2 shipped.** 120 sources — five GitHub repo trackers, 60 ATS
boards (Greenhouse/Workday/Ashby/Lever/Phenom), 52 watched pages and three `manual`
rows — with Issue delivery, one digest a day, a weekly source-discovery pass, and
tick-to-dismiss.

Sources divide by *intake path*, not by tier: aggregator lists that someone else
curates, and named employers. For a named employer the watcher points either at the
whole jobs board or at a specific programme's own page — never at an individual
posting, since those URLs expire inside the year.

---

## Setup (once)

1. Create a **private** GitHub repo and push this code.
2. Add repo secrets (Settings → Secrets and variables → Actions):
   - `GH_PAT` — a fine-grained PAT, **read-only, public repositories**. Lifts the GitHub
     API limit from 60 requests/hour to 5,000. Optional but recommended.
   - `ANTHROPIC_API_KEY` — for the relevance classifier, running Claude. **Optional.**
   - `AZURE_OPENAI_API_KEY` — the alternative classifier backend: a `gpt-5-mini`
     deployment on a Microsoft Foundry resource, funded by Azure for Students credit.
     **Optional.** Used only when `ANTHROPIC_API_KEY` is absent.
   - Either key is enough, and neither is required. Without both, the job still runs and
     still reports everything; changes simply arrive unclassified rather than being
     dropped. Set `CLASSIFIER_PROVIDER` to `anthropic` or `azure` to force one.
   - `GITHUB_TOKEN` is provided automatically. No SMTP, no mail credentials.
3. Enable Actions and confirm `daily` appears in the Actions tab.
4. Run it once manually: Actions → `daily` → **Run workflow**. It should open an Issue.
5. **Set a recurring monthly calendar reminder: "check opportunity-tracker Actions tab."**
   This one matters — see [Failure modes](#failure-modes) below.

After that it is unattended.

## Running it locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python check.py --dry-run     # fetch everything, print the digest, change nothing
.venv/bin/python check.py --only nuft-2027 --dry-run
.venv/bin/python build_xlsx.py          # regenerate out/programs.xlsx
.venv/bin/python -m unittest discover -s tests -v
```

`--dry-run` opens no Issue and writes no state. Use it freely.

## How it works

```
check.py                 orchestrates: check → classify → render → deliver → save state
sources/snapshot.py      the canonical row/diff shape every source type shares
sources/github_repos.py  repo READMEs, diffed at the row level
sources/job_boards.py    Greenhouse/Workday/Ashby/Lever/Phenom JSON -> canonical rows
sources/page_watch.py    fetch a page, normalise to lines, diff; two floors, no browser
sources/postings.py      fetch the real posting behind a board row, for the classifier
discover.py              the weekly pass that proposes new sources and never adds one
classify.py              one Claude call per change; the class-year and identity gates
digest.py                renders the digest, opens the Issue
calendar_reminders.py    standing reminders for what cannot be automated
build_xlsx.py            CSVs → out/programs.xlsx (yours) + out/tracked.xlsx (the tool's)
seed_programs.py         one-time seed from the original spreadsheet
data/programs.csv        CANONICAL program list — edit this, not the xlsx
data/sources.csv         what to check, and how
data/manual.csv          what this tool CANNOT tell you about → "Manual Watch" sheet
data/priority.csv        ranked shortlist to watch yourself → "Priority" sheet
data/snapshots/*.tsv     one canonical line per listing row, committed
data/proposals.log       append-only: proposed edits the tool refused to make itself
out/programs.xlsx        GENERATED — yours: only what you must check by hand
out/tracked.xlsx         GENERATED — the tool's: all programs, sources, applied, discovered
```

### Why the snapshots are TSV, not the READMEs

The stored snapshot is a sorted, canonical `section → key → value` rendering rather than
the README itself. A newly posted role is then exactly **one added line** in `git diff`,
instead of a 700KB blob reflowing. It also means the diff is keyed on company + role, so
badge churn and emoji edits cannot produce a false positive.

Each repo needs its own parser config, because the five READMEs share almost nothing:
NUFT puts the company in the `##` heading above a two-column table; Cruz-Lopez has seven
different header layouts; Simplify uses HTML `<table>` and a **relative** `Age` column
(`1d`, `2mo`) that is excluded from the diff, because diffing it would report all 490
rows as changed every single day.

Simplify also needs two markers neutralised: a bare `↳` in the Company cell meaning
"same company as above" (resolved into both the key *and* the value, so a row shifting
position within its group does not churn), and `🔥` meaning "recently posted", which
falls off after a few days and would otherwise make a posting look removed and re-added.
Cruz-Lopez uses `🔥` differently — as `Status=🔥 [CLOSING SOON]`, which is real signal —
so the strip is scoped per repo, not global. `tests/test_acceptance.py::NoiseRegressionTests`
locks all four behaviours down.

### Reading the git log

Every run commits, because `last_checked` and `last_success` advance whether or not
anything moved. The message tells you which kind it was:

```
state: heartbeat 2026-09-06 (no upstream changes)   <- the job ran, nothing changed
state: 2026-09-06 upstream changes detected         <- something actually moved
```

So `git log --oneline --grep="upstream changes"` is the list of real change days, and the
heartbeat commits are independent evidence the schedule is still alive.

### Cadence

**Exactly one digest a day, every day** — no more and no less.

This replaced a changes-only cadence on 2026-09-08. That version kept the inbox quieter,
but a morning with no email meant either "nothing changed" or "the job is broken" and you
could not tell which without opening the Actions tab. A fixed daily arrival removes the
ambiguity: **silence now always means broken.** Notification fatigue is handled in the
content instead of by withholding mail — a quiet day is titled `(no changes)` and its body
is just the calendar and health blocks, which you can triage without opening it.

The **no more** half matters just as much. The morning schedule fires three ticks (see
failure mode 3) and each one would otherwise open its own issue; two digests for one date
is the same fatigue failure as an unread daily. So delivery is capped at one issue per
date, and the cap is **absolute** — it suppresses real changes and failing sources too,
because a retry has nothing to tell you that the morning's issue did not already carry.
Nothing is lost either way: the state commit and `data/proposals.log` still record
everything the retry saw.

The cap cannot rest on `data/last_delivered.txt` alone, because a run reads that marker
from the commit it checked out and late ticks do not always dispatch in cron order — so a
tick can see a stale marker and mail a duplicate. The authoritative check is the issue
list itself: the issue *is* the email, so the issues **are** the delivery log, and every
run sees the same one whatever it checked out. The marker is the fallback for when GitHub
cannot be reached.

### What the classifier will not do

It never rewrites the `eligible` column. That column is hand-verified research, and a
model overwriting it destroys work. Proposed changes go to `data/proposals.log` and into
the digest, for you to apply or ignore.

## Adding opportunities from an article

There is a Claude Code skill for this: paste a link to an article and it opens the page,
extracts every opportunity in it, probes each one to see whether it can actually be
monitored, and files it into `programs.csv` plus the right watchlist.

```
.claude/skills/add-opportunity/SKILL.md
```

The same file is copied one level up, in the workspace's `.claude/skills/`, so the skill
is found whether your working directory is this repo or its parent. **If you edit one,
copy it over the other.**

It leans on `add_opportunity.py`, which is also useful directly:

```bash
.venv/bin/python add_opportunity.py --probe "https://some-program"   # can this be watched?
.venv/bin/python add_opportunity.py --add opps.json --dry-run        # validate, write nothing
```

Three guardrails, because an agent drives it: additions are append-only and deduplicated
by name, so hand-verified rows cannot be clobbered; `eligible: "YES"` is **rejected**
(the agent must use `CHECK` and let you confirm); and a `robots.txt` disallow or a 403
routes an opportunity to Manual Watch rather than being worked around.

## Failure modes

1. **Silent success** — a page redesigns, the fetch returns nothing, the job exits 0, and
   you conclude nothing has opened for four months. Mitigated: every source returns a
   result object; "returned zero rows" and "fetch failed" are first-class alerts;
   `consecutive_failures` and `last_success` are tracked per source; **three consecutive
   failures escalates the source into ACT NOW**, not the health footnote. Each source also
   has a plausibility floor — and for NUFT that floor is on *section* count, not row
   count, because its row count legitimately drains to near zero out of season.
2. **GitHub disables scheduled workflows on inactive repos.** This is the classic way
   projects like this die around month six. Whether the bot's own state commits reset that
   clock is not worth relying on. **Mitigation: the monthly calendar reminder in step 5.**
3. **Cron is best-effort — and worse than "delayed".** GitHub does not merely postpone a
   scheduled tick when it is busy, it silently drops it, and there is no run, no log and
   no notification to tell you so. This was observed live: the first two days of the
   schedule produced *zero* scheduled runs while `workflow_dispatch` worked perfectly.
   **Mitigation: the schedule fires three times each morning (07:00 / 08:20 / 09:40 UTC)
   and delivery is capped at one issue per date**, so all three ticks have to be dropped
   to lose a day and a redundant tick costs one 30-second no-op run.

   The ticks GitHub *does* run, it runs badly late. Measured 2026-09-07/08: every tick
   ran 3h27m–4h48m behind its cron, because a private repo on a free personal account
   sits in the lowest scheduler priority tier. A 13:30 UTC cron aimed at 06:30 Pacific
   opened its issue at 10:18 Pacific. No cron value fixes that — the delay is unbounded.
   **Mitigation: schedule with headroom instead of on target.** The job runs at midnight
   Pacific, so even the worst delay seen still lands before 05:00. Arriving early is
   harmless; arriving at lunchtime is not. Nothing assumes an exact run time, and after
   this nothing assumes the ticks arrive in cron order either.
4. **Notification fatigue** — see Cadence.

## Politeness

Requests are serialised with a delay, identify themselves as
`opportunity-tracker/1.0 (+mailto:jasonshi@stanford.edu)`, and are never retried
aggressively. `robots.txt` was checked against every target domain: **no watched path is
disallowed.** Where a site blocks automation, it is marked `manual` in `sources.csv` and
moved to the Manual Watch sheet rather than worked around. The tool never logs in to
anything.

## The two manual sheets

`out/programs.xlsx` has four sheets. Two of them exist because automation cannot cover
everything, and pretending otherwise is how you miss a deadline:

- **Manual Watch** (23 rows) — things this tool provably cannot alert you about, each
  with the reason, how to check, and when. Three groups: sites that return 403 to every
  automated client (all of Citadel, IAS/PCMI, MAA/Putnam), competitions announced on
  Instagram and listservs *before* the website changes (Cornell CTC, UChicago UTC,
  Traders@MIT, Berkeley), and pages with nothing to diff (The Deck Game yields ~28
  characters of text).
- **Priority** (30 rows) — a ranked shortlist to keep an eye on yourself in case this
  repo breaks. Band A is high value with real selection risk (Jane Street FTTP first);
  Band B is Stanford-internal, where your odds are genuinely best (CURIS, SURIM, VPUE
  grants, Directed Reading, Section Leading); Band C is open entry, where showing up is
  the only gate. Every row states **whether this tool will actually alert you** — 11 of
  the 30 are named in the repos already tracked, 13 need Phase 3, and 4 are never
  automatable.

## Verified source facts (probed 2026-09-05)

Worth knowing before extending this, because several came out differently than expected:

- **All of Citadel blocks automation** — `citadel.com`, `citadelsecurities.com` and the
  datathon page return 403 to a bot user-agent *and* a browser user-agent. Discover
  Citadel is freshman-eligible and marked YES, and this tool is structurally blind to it.
  It is the largest gap in the system.
- **~10 of the ~40 Tier 3 pages are JS shells**: AQR university jobs returns *zero*
  characters of text from 62KB of HTML; CURIS 5; SLAC 4; SIG Discovery 520 from a 402KB
  page. These need Playwright.
- **Google Careers ASDI search is server-rendered** (21KB of text) — no browser needed.
  Search for `Associate Software Developer Intern`; it was renamed from STEP.
- **Stale URLs**: Berkeley's `competition.html` is a 404 (site rebuilt in Astro; use
  `/competition/`), D. E. Shaw's `/fellowships` path is gone, DRW 308-redirects to
  `/work-at-drw/listings?filterType=campus&value=Campus`.
- **`LuisaE/opportunities` is on `master`**, and SimplifyJobs is on `dev` — which is why
  the branch is read from the API rather than assumed.
- **Kaggle** listing pages are JS shells and its official API 401s without credentials
  (free: `KAGGLE_USERNAME` / `KAGGLE_KEY`). An unauthenticated internal RPC endpoint does
  return 200, but building a daily job on an undocumented endpoint is not worth it.
- **MIT Pokerbots** is pollable over plain HTTP, but the sheet already marks it `NO`: the
  competition server requires a teammate with MIT certificates.

## Acceptance tests

Spec section 14. `tests/test_acceptance.py` runs offline against a stubbed GitHub client.

| # | What it checks | Status |
|---|---|---|
| 14.1 | `workflow_dispatch` completes and opens an Issue | needs the repo to exist |
| 14.2 | Hand-edited snapshot → reported in ACT NOW | passing |
| 14.3 | A 404 → HEALTH, not a change; `consecutive_failures` increments | passing |
| 14.4 | A new "Freshman Insight Program" row → discovery candidate | passing |
| 14.5 | Women-only program → `relevant: false`, identity gate cited | **needs `ANTHROPIC_API_KEY`** |
| 14.5c | Same identity gate, enforced on the `gpt-5-mini` backend | **needs `AZURE_OPENAI_API_KEY`** |
| 14.6 | A quiet run → an Issue anyway, titled `(no changes)` | passing |
| 14.6b | A same-day retry mails nothing, even when it finds changes or a failure | passing |
| 14.6c | "Cannot ask GitHub" answers `None`, never "not yet delivered" | passing |
| 14.7 | `build_xlsx.py` matches the seed formatting | passing |
| 14.8 | `git log --follow data/programs.csv` is line-level readable | needs the repo to exist |

## What is not built yet

- **Playwright for JS-shell pages.** Ten Tier 3 pages were measured returning real text
  to a plain GET, so no browser ships. SIG's careers page is a shell (402KB of HTML,
  221 characters of text) and is `method=manual` as a result. Adding a browser would
  reach it and a handful like it, at ~3–6 min per run.
- **The rest of the spec's ~45 watch URLs.** Several are stale — Berkeley 404s, D. E.
  Shaw's `/fellowships` path is gone, DRW's URL 308-redirects — so extending the list
  is URL archaeology before it is code.
- **Cross-source dedupe.** Eight of ten Greenhouse boards are firms the NUFT repo also
  lists, so the same opportunity can arrive twice under two names. Left alone on
  purpose: the two are not really duplicates, since Tier 2 carries the full title,
  location and description while NUFT carries a role code, and tick-to-dismiss handles
  any genuine double-sighting.
- **Classifier tuning** — the spec's Phase 4.

Tiers 1 and 2 should work unchanged for years. Tier 3 is the fragile one.
