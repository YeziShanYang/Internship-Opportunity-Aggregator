---
name: add-opportunity
description: Extract every opportunity from an article or page and add it to the opportunity tracker's watchlists. Use whenever the user supplies a URL, link, or article about internships, programs, competitions, fellowships, scholarships, REUs, hackathons, or quant/tech recruiting - including a bare pasted link with no instructions. Opens the link, pulls out each individual opportunity, probes whether each one can be monitored automatically, and files it into programs.csv plus the right watchlist (sources.csv, Manual Watch, or Priority).
---

# Adding opportunities from an article

The user pastes a link. Open it, find **every** opportunity in it, and file each one so it
lands on a watchlist — not just in the master list.

Repo root: `opportunity-tracker/`. Run all commands from there, with `.venv/bin/python`.

## 1. Open the link

Fetch it with WebFetch. A bare URL with no instructions still means "do this" — do not
ask what to do with it.

If the page is paywalled, JS-only, or returns nothing useful, say so plainly and ask the
user to paste the text. Do not guess at contents you could not read.

## 2. Resolve every link to something that will still exist next year

**A link to one job posting is almost never the right thing to watch.** The requisition
closes when the role is filled, the URL 404s, and next year the same programme reappears
at a different URL. A source pointed at it looks healthy right up to the moment it goes
permanently blind.

So before probing anything, turn the link you were given into a **durable watch target**.
There are only three kinds, in order of preference:

| Kind | When | Where it goes |
|---|---|---|
| **Programme page** | the employer has a page for this specific programme, with the apply button on it | `page_text`, tier 3, `signal: high` |
| **Whole job board** | the employer posts everything through one ATS | the matching ATS method, tier 2, `signal: low`, `program_names` empty |
| **Aggregator** | it is somebody else's curated list, not an employer | `github_readme` if it is a repo README |

Prefer the programme page when one exists: it carries the rolling-deadline language and
the apply button, and it survives the requisition closing. Jane Street is the worked
example — their Greenhouse board has no FTTP listing at all, so the board alone would
never tell you FTTP had opened. Watch both when both exist; they answer different
questions.

### Finding the board behind a posting URL

Read the host. The fingerprint tells you the ATS and the slug is in the path:

| Posting URL contains | Method | `url` column holds |
|---|---|---|
| `job-boards.greenhouse.io/<slug>/...` or `?gh_jid=` | `greenhouse` | the slug, e.g. `janestreet` |
| `jobs.lever.co/<slug>/...` | `lever` | the slug |
| `jobs.ashbyhq.com/<slug>/...` (case-sensitive) | `ashby` | the slug |
| `<tenant>.wdN.myworkdayjobs.com/en-US/<site>/job/...` | `workday` | the full CXS base URL (below) |

**Workday needs care and is worth the trouble** — it is the most common ATS among large
employers and a plain fetch of any Workday page returns *zero* characters of text,
which is why it looks unwatchable. The JSON API behind it works fine. From
`https://capgroup.wd1.myworkdayjobs.com/en-US/capitalgroupcareers/job/Los-Angeles/CAP-Associate---US--2027-_JR7194-1`
take the tenant (`capgroup`), the shard (`wd1`) and the site (`capitalgroupcareers`) and
build:

```
https://capgroup.wd1.myworkdayjobs.com/wday/cxs/capgroup/capitalgroupcareers
```

Confirm it before adding — the shard in the posting URL is not always the one the API
answers on, so try `wd1`, `wd3`, `wd5`, `wd2`:

```bash
curl -s -X POST "https://<tenant>.<shard>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs" \
  -H 'Content-Type: application/json' \
  -d '{"appliedFacets":{},"limit":20,"offset":0,"searchText":""}' | head -c 400
```

A `total` and a `jobPostings` array means it works. Two boards will refuse and should be
left out rather than added half-working: one with more postings than can be paged 20 at a
time (~800 max), and one where nearly every title passes the student filter, which would
mean hundreds of description fetches a day. `job_boards.check` raises on both instead of
reading them partially.

### If the employer has no public board

Then the durable target is their careers or students page as `page_text`. Probe it (§3):
if it yields real text, watch it. If it is a JavaScript shell, it goes to `manual` — do
not point a source at the posting URL as a consolation prize, because that is the failure
this whole section exists to prevent.

### Confirm the placement afterwards

```bash
.venv/bin/python -c "
import csv,re
for r in csv.DictReader(open('data/sources.csv')):
    if re.search(r'/job/|jobid|requisition|_JR\\d|\\?q=JR', r['url'], re.I):
        print('POSTING-SPECIFIC, will die:', r['source_id'], r['url'])
"
```

That should print nothing.

## 3. Extract every opportunity

Read the whole article, not just the headings. A "10 internships for freshmen" listicle
has ten opportunities; a single program page has one. Aggregator pages linking out to
programs mean each *linked program* is the opportunity, not the aggregator.

For each one, capture: name, its own URL (not the article's), when applications open,
which class years it targets, and any eligibility language quoted from the page.

**Ignore nothing because it looks marginal** — the owner filters, you collect.

## 4. Probe each opportunity's own URL

```bash
.venv/bin/python add_opportunity.py --probe "https://the-opportunity-url"
```

This reports HTTP status, how much visible text a plain fetch yields, whether robots.txt
allows the path, and a recommended tier. Use its `recommendation` verbatim to decide
placement — do not assume a page is watchable:

| Probe says | Placement |
|---|---|
| the employer has an ATS board (§2) | `source` block, **tier 2**, `method: greenhouse` / `lever` / `ashby` / `workday`, `signal: "low"`, `program_names` empty — one board is not one tracked programme, and an empty `program_names` is what stops a 200-job board driving one row's status |
| `render_js=false`, plenty of text | `source` block, tier 3, `render_js: false` |
| `render_js=true` (under ~200 chars) | `source` block with `render_js: true`, **and** a `manual` block with `coverage: "Planned - phase 3"` |
| Under ~500 chars, or a text-to-HTML ratio under 0.0015 | `manual` only — there is nothing to diff |
| `MANUAL - blocked` (401/403) or robots disallows | `manual` only, `coverage: "Never - blocked"`. **Never** work around a block |
| Announced on Instagram/listserv before the site changes | `manual`, `coverage: "Never - announced off-web"` |

Serialise probes with a beat between them. Be a good citizen (spec section 11).

## 5. Set eligibility conservatively

The owner is a Stanford first-year, class of 2030, math/CS, a US citizen based in the US
— and **not** eligible for identity- or hardship-targeted programs.

- Default to **`CHECK`**. The helper script rejects `eligible: "YES"` outright; asserting
  verified eligibility is the owner's call, not yours.
- Use **`NO`** only for a gate the page states explicitly — a junior-year graduation
  window, or a program for women / underrepresented minorities / LGBTQIA+ students /
  students facing barriers to access. Name the gate in `notes`.
- Use **`LATER`** when it is the right program but the wrong year.
- Put the eligibility language you actually read into `notes`, quoted. "Grad window Dec
  2027-Jun 2029" is useful; "seems relevant" is not.

## 6. Check what is already tracked before adding

`data/programs.csv` already holds ~150 hand-researched rows. The script dedupes on exact
name, but **near-miss names slip through** — "MIT Pokerbots" vs "Pokerbots (MIT)" would
create a duplicate.

```bash
grep -i 'keyword' data/programs.csv
```

If a row already exists, **leave it alone.** Its `eligible` and `notes` are
hand-verified research and outrank an article. If the article contradicts it — a better
URL, a changed deadline — record that instead of overwriting:

```bash
.venv/bin/python -c "import state; state.append_proposal('add-opportunity\t<name>\tproposed <field>=<value>\t<why>')"
```

This has already mattered: an article-style addition of MIT Pokerbots would have
overwritten a researched `NO` (the competition server needs a teammate with MIT
certificates) with a hopeful `CHECK`.

## 7. Add them

Build a JSON array and dry-run it first:

```json
[{
  "category": "Competition",
  "name": "Exact Program Name 2027",
  "website": "https://the-program-url",
  "applications_open": "Opens ~Nov; deadline Jan 15",
  "target_years": "1st & 2nd year",
  "eligible": "CHECK",
  "notes": "What it is, quoted eligibility language, and where this came from.",

  "source":   {"tier": 3, "method": "page_text", "render_js": false, "signal": "high",
               "notes": "Probed: 4,669 chars of text over plain HTTP."},
  "manual":   {"coverage": "Never - blocked", "why_manual": "403 to every client.",
               "how_to_check": "Open in your browser.", "when_to_check": "Nov-Jan"},
  "priority": {"band": "A - high value, do not miss", "value": "High",
               "probability": "Medium", "watch_window": "Nov-Jan",
               "will_this_tool_alert_you": "No - site blocks automation",
               "why": "One or two sentences on why it earns the slot."}
}]
```

`category` must be one of: `Quant firm program`, `Competition`, `Tech internship`,
`Research / math`, `Stanford-only`, `Conference / other`, `Local (St. Louis)`,
`Tracker / resource`.

All three of `source`, `manual` and `priority` are optional — include the ones that
apply. Add a `priority` block only for something genuinely high value or high
probability for a first-year; a padded Priority sheet is a useless one.

```bash
.venv/bin/python add_opportunity.py --add /tmp/opps.json --from-article "<article url>" --dry-run
.venv/bin/python add_opportunity.py --add /tmp/opps.json --from-article "<article url>"
.venv/bin/python build_xlsx.py
```

## 8. Commit and report

```bash
git diff --stat && git add -A && git commit -F /tmp/msg.txt && git push
```

Use a message file — commit messages here contain quotes and arrows that break shell
quoting. Then tell the user, briefly:

- How many were added, and how many were already tracked
- Which watchlist each landed on, and **which ones this tool cannot alert them about**
- Anything you marked `NO` and why
- Anything you could not read on the page, stated plainly rather than filled in

Do not run `run.py all` afterwards. New tier-2/3 sources are inert until those phases
exist, and the daily workflow will pick up the new rows on its own.
