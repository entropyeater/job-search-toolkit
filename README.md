# job-search-toolkit

Probe public ATS job boards (Greenhouse, Ashby, Lever, SmartRecruiters, Workable, Jobvite) for roles matching **your** criteria, across **your** list of target companies, and write the results straight onto a CSV you can filter/sort in a spreadsheet.

No API keys. No dependencies beyond the Python 3 standard library. Your target role, seniority, geography, and any number of custom filters ("only count this role if it's in the observability org", "only count management roles that ask for under 5 years of experience") live in a JSON config file you edit — nothing about a specific job title or city is hardcoded in the scripts.

## Why this exists

Searching company-by-company for "does this company have an open Staff Product Designer role in the Bay Area" doesn't scale past a handful of companies. Most ATS platforms expose a free, no-auth JSON API for their job boards. This toolkit probes those APIs directly, verifies each posting URL is actually live, and gives you one row per company with the best matching role (plus a count of any others) — so a spreadsheet of 50, 500, or 5,000 companies stays current with a single command.

## Quickstart

1. **Make a CSV of companies.** One row per company, at minimum a name and a website/domain. See `companies.example.csv`.
2. **Copy and edit the config.** `cp config.example.json config.json`, then edit the role/geography/seniority regexes to match what *you're* looking for (see "Customizing your config" below).
3. **Probe:**
   ```bash
   python3 scripts/ats_probe.py --config config.json \
     --in companies.csv --name-col Company_Name --site-col Company_Website \
     --out results.json
   ```
4. **Write the results onto your CSV:**
   ```bash
   python3 scripts/apply_results.py --config config.json \
     --csv companies.csv --results results.json
   ```
   Open `companies.csv` in a spreadsheet — it now has match columns filled in per role family, plus `Last_Checked`, `Source_Method`, `Match_Confidence`, and `Web_Search_Needed`.

Try it immediately with the bundled example:
```bash
python3 scripts/ats_probe.py --config config.example.json --in companies.example.csv --out results.json
python3 scripts/apply_results.py --config config.example.json --csv companies.example.csv --results results.json
```

## Customizing your config

Copy `config.example.json` and edit these sections:

- **`geography`** — a role's location string must match `include_regex`. If it *also* matches `foreign_regex` (a name-a-country list), it's dropped unless it *also* matches `us_override_regex`. Delete the whole `geography` key to accept any location. `priority_regex` just affects which role wins when several qualify (e.g. prefer your home city over generic "Remote").
- **`seniority_rank`** — an ordered list of `{regex, score}` used only to rank multiple matches against each other, highest score first.
- **`role_families`** — the list of role searches you're running. Each entry needs:
  - `key` — a short id, used internally and as the default column-name prefix.
  - `include_regex` / `exclude_regex` — a role's title must match include and must not match exclude.
  - `require_seniority` + `seniority_regex` (optional) — require the title to also carry a seniority word.
  - `theme_gate` (optional) — restrict matches further by topic:
    - `mode: "column"` — only active for a given company when a CSV column you name (`enabled_when_column`) is non-blank for that row. Use this for filtering large, multi-product companies down to one org.
    - `mode: "always"` — always active. A role counts if the title/department matches `keywords_regex`, **or** if the company's own category (read from `native_company_columns`) matches `native_company_regex` — i.e., a generic title still counts at a company whose whole product *is* that space.
  - `years_experience_gate` (optional) — parses the job description for a "N years" figure near a term in `terms_regex` and drops the role if that figure is `>= max_years`. A description that never states a figure is **kept**, not dropped (false negatives are worse than a manual double-check) and recorded as `unspecified`.
  - `columns` — which CSV columns this family's `title` / `location` / `pay` / `url` / `additional_count` / `years_required` / `theme` fields get written to. Any key you omit is simply not written.
  - `primary: true` on exactly one family — that family's result also drives the shared `Match_Confidence` / `Source_Method` / `Web_Search_Needed` columns.

You can define one family (just the role you want) or several (e.g. IC + management + adjacent-role variants) — `config.example.json` shows all three gate types in one working config.

## CSV requirements

Only two columns are truly required: a company name column and a website/domain column (names configurable via `--name-col` / `--site-col`). Everything else — `Applied`, `Applied_Date`, any taxonomy columns your `theme_gate`s read, all the match-result columns — is either optional or gets added automatically by `apply_results.py` the first time it runs.

- **`Applied`** (any of `TRUE`/`YES`/`1`/`Y`, case-insensitive) + **`Applied_Date`** (`YYYY-MM-DD`): if a company was applied to within the last `--skip-applied-days` (default 120), it's skipped entirely — not re-probed, not overwritten. A fresh application shouldn't have its role data silently changed underneath you.
- **`Web_Search_Needed`** is a *sticky* flag: once set to `Yes` (meaning no ATS board could be found), it's never cleared by a later run *unless* a real ATS board is subsequently confirmed present. This is the signal for which companies are worth a manual/web-search follow-up — everyone else already has an authoritative "here's what's open" answer from the API.

## What counts as verified

`Match_Confidence` tells you how much to trust a result:
- `HIGH (ATS-API, URL-verified)` — the posting was found via a live ATS API call **and** its URL returned HTTP 200/403/429 on a follow-up request. Trust it.
- `MED (ATS-API, URL unverified)` — found via the API but the URL check was skipped (`--no-verify`) or failed transiently.
- `HIGH (live-verified absent)` — the company's ATS board responded but had **no** matching role. This is a confirmed negative, not "unchecked."
- Blank `Match_Confidence` + `Web_Search_Needed=Yes` — no ATS board could be located at all (common for companies on Workday, Rippling, Avature, or custom in-house systems). This script can't tell you anything about these companies; you'd need to check manually or write your own web-search pass.

**A word of caution about pairing this with an LLM-driven web search:** if you have an agent do a manual/web fallback pass for the `Web_Search_Needed=Yes` companies, don't trust an LLM's summarized search results as a "found role" — they hallucinate titles and go stale. Only count a role as real if you (or the agent) can fetch the *actual* posting URL and confirm the title matches.

## Coverage and limitations

- Realistically resolves an ATS for somewhere around half to two-thirds of companies — the rest use platforms without a public, no-auth API (Workday, Rippling, Avature, Gem, many custom in-house systems) and come back as `no-ats` / `Web_Search_Needed=Yes`.
- Company-slug guessing is the #1 failure mode for the platforms that *are* supported. `ats_probe.py` tries several slug variants automatically; if it still can't find a company you know is on Greenhouse/Ashby/etc., pass the real slug directly: `--company "Name=known-slug" --platform greenhouse`.
- Salary is only ever populated where the ATS itself exposes it (mainly Ashby, occasionally Greenhouse) — never inferred.
- `Additional_Matches_Count` tells you a company has more than one qualifying role, but only the single best match (by the `seniority_rank` ranking) gets written to the row. The full list is in the JSON output if you need it.

## Files

```
config.example.json     — full worked example: 3 role families, all gate types
companies.example.csv   — minimal example company list
scripts/ats_probe.py    — probes ATS APIs, applies your config's filters
scripts/apply_results.py — writes ats_probe.py's JSON output onto your CSV
```

## License

MIT — see `LICENSE`.
