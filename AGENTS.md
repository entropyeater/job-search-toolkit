# Agent instructions

Guidance for an AI coding agent (Claude Code or similar) operating this toolkit on someone's behalf — running probes, applying results, and doing the manual web-search fallback for companies the API probe can't reach. A human running the scripts by hand can ignore this file; see `README.md` instead.

## Before running anything

- Confirm the user has a `config.json` (copied and edited from `config.example.json`) and a company CSV. Don't invent role/geography criteria yourself — ask, or read their `config.json` if one already exists.
- Confirm the CSV path and which columns hold company name / website — don't assume `Company_Name`/`Company_Website` if the user's file uses different headers; pass `--name-col`/`--site-col` accordingly.
- The CSV is the single source of truth. Always update it in place via `apply_results.py` — never hand-edit match-result columns, and never create dated copies or alternate versions of the file.

## Do-not-update rule

Before probing, `ats_probe.py` itself skips (via `--applied-col`/`--applied-date-col`/`--skip-applied-days`, on by default at 120 days) any company the user has already applied to recently — a fresh application is still in flight and shouldn't have its role data silently overwritten. This is enforced in code, not just by instruction. Report the skip count (`ats_probe.py` prints it to stderr) in your summary; if you know applied rows exist in the CSV but see a skip count of 0, treat that as a bug to investigate, not a clean run.

## Web-search fallback (only for `Web_Search_Needed=Yes` rows)

`ats_probe.py` only covers ATS platforms with a public, no-auth API. Companies on Workday, Rippling, Avature, Gem, or custom in-house systems come back `status: no-ats` / `Web_Search_Needed=Yes` — that's the *entire* list of companies worth a manual web pass. **Never web-search a company whose ATS board responded** (`status: none-found`) — its openings are already known; a web search there wastes calls and risks a false positive from a stale cached listing.

**Concurrency:** if you're running this on the user's machine, keep any web-search fan-out small (roughly 2 in parallel) so the session stays responsive — this is not a constraint the scripts themselves enforce, so respect it by hand. State the batch size before starting so the user can approve scope.

**Cost:** if you have a choice of model for the search→fetch→extract subagents in a web-search fallback pass, prefer a cheap/fast model — it's retrieval and extraction, not reasoning, and this fan-out is typically the dominant cost of a full run.

## Verification — do this before reporting any role as real

A generic web-search tool that returns an LLM-summarized answer (rather than raw listings) **hallucinates roles** — titles, seniority, even entire postings that don't exist, or ones that were real but have since closed. Never report a web-search-sourced role as confirmed on the strength of the search summary alone.

A role only counts as verified with a **live deep-link to that specific posting** that returns HTTP 200 (or 403/429 — rate-limited, not dead) and whose title matches:
- **ATS-sourced rows:** `ats_probe.py` already does this — it HTTP-verifies every posting URL and sets `verified: true/false` per match. Trust `Match_Confidence = HIGH (ATS-API, URL-verified)`; don't re-verify these yourself.
- **Web-fallback rows:** fetch the *actual* careers/board page (not a search-result summary) and confirm the title yourself before writing anything into the CSV. If the board is JS-rendered and unreachable, that's a real limitation, not something to paper over with a guessed answer.

Resolution rules (false negatives — silently missing a real opening — are worse than a flagged uncertainty):
- Confirmed present on a live source → record it with the URL.
- Confirmed absent on an authoritative live source (the ATS API, or a server-rendered careers page) → note it as checked-and-empty; this is a real, useful negative result, not a gap.
- Can't confirm either way (JS-rendered page, no API access) → don't fabricate an answer. Leave the row's match columns blank, keep `Web_Search_Needed=Yes`, and say so plainly rather than reporting an unconfirmed role as found.

## Never clear role data on an unconfirmed result

`apply_results.py` already enforces this: a `no-ats` result (no board responded at all) never clears existing match data in the CSV, because the existing value may have come from a prior verified web-search pass and "no signal this run" is not the same as "confirmed empty." Only an ATS board that actually responds — even with zero matching roles — can clear a stale match. Don't work around this by hand-editing the CSV to force a clear.

## What to report back to the user

After a run, summarize: companies probed, companies skipped (do-not-update rule, with the count cross-checked against the script's own stderr count), matches found broken out **per role family**, companies still needing a manual web-search follow-up (`Web_Search_Needed=Yes`), and — if you ran the web fallback — how many of those you were actually able to confirm versus how many stayed unconfirmed. Report yield honestly; the web-fallback pass is typically low-yield (most `no-ats` companies are hard to verify by web for the same reason they're hard to verify by API — no machine-readable board), so don't imply exhaustive coverage you don't have.
