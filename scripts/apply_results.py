#!/usr/bin/env python3
"""
apply_results.py — map ats_probe.py output onto your company CSV.

Writes the CSV in place (canonical filename never changes; a .bak is made and
removed on success). Rows the probe skipped — recently-applied companies under
the do-not-update rule — are left byte-identical.

Which columns get written is entirely driven by the same config file passed to
ats_probe.py: each role family's `columns` map says where its title/location/
pay/url/etc. land. The family marked `"primary": true` also drives the shared
bookkeeping columns: Last_Checked, Source_Method, Match_Confidence,
Web_Search_Needed.

USAGE
  python3 apply_results.py --config config.json --csv companies.csv \
      --results results.json [--date YYYY-MM-DD]
"""

import argparse
import csv
import datetime
import json
import os
import shutil
import sys

from ats_probe import load_config

ATS_PREFIXES = ("greenhouse:", "ashby:", "lever:", "smartrecruiters:", "workable:", "jobvite:")

# Bookkeeping columns shared by every run, independent of family config.
CORE_COLUMNS = ["Last_Checked", "Source_Method", "Match_Confidence", "Web_Search_Needed"]


def confidence(match, ats_present):
    if match:
        return "HIGH (ATS-API, URL-verified)" if match.get("verified") else "MED (ATS-API, URL unverified)"
    return "HIGH (live-verified absent)" if ats_present else ""


def family_columns(fam_cfg):
    """All output columns this family's `columns` map declares, in stable order."""
    cols = fam_cfg["columns"]
    order = ["title", "location", "pay", "url", "additional_count", "years_required", "theme"]
    return [cols[k] for k in order if cols.get(k)]


def write_family(row, fam_cfg, fam_result):
    cols = fam_cfg["columns"]
    best = fam_result.get("best_match") if fam_result else None
    if best:
        extra = best.get("extra") or {}
        if cols.get("title"):
            row[cols["title"]] = best.get("title", "")
        if cols.get("location"):
            row[cols["location"]] = best.get("location", "")
        if cols.get("pay"):
            row[cols["pay"]] = best.get("pay", "")
        if cols.get("url"):
            row[cols["url"]] = best.get("url", "")
        if cols.get("additional_count"):
            row[cols["additional_count"]] = str(fam_result.get("additional_matches_count", 0))
        if cols.get("years_required"):
            row[cols["years_required"]] = extra.get("years_required", "")
        if cols.get("theme"):
            row[cols["theme"]] = extra.get("theme", "")
        return True
    else:
        for k in ("title", "location", "pay", "url", "years_required", "theme"):
            if cols.get(k):
                row[cols[k]] = ""
        if cols.get("additional_count"):
            row[cols["additional_count"]] = "0"
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="the same config JSON passed to ats_probe.py")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--name-col", default="Company_Name")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    families = cfg["role_families"]
    primary = next((f for f in families if f.get("primary")), families[0])

    with open(args.results, encoding="utf-8") as f:
        results = json.load(f)
    by_name = {}
    for r in results:
        if r.get("status") != "error":
            by_name[r["company"].strip().lower()] = r

    with open(args.csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames)

    all_new_columns = list(CORE_COLUMNS)
    for fam in families:
        for c in family_columns(fam):
            if c not in all_new_columns:
                all_new_columns.append(c)
    for c in all_new_columns:
        if c not in fieldnames:
            fieldnames.append(c)

    stats = {"updated": 0, "untouched": 0, "no_ats": 0, "preserved_no_ats": 0}
    for fam in families:
        stats[fam["key"]] = 0

    for row in rows:
        for c in all_new_columns:
            row.setdefault(c, "")
        res = by_name.get((row.get(args.name_col) or "").strip().lower())
        if not res:
            stats["untouched"] += 1  # skipped by the do-not-update rule
            continue

        fam_results = res.get("families", {})
        primary_res = fam_results.get(primary["key"])
        any_match = any((fam_results.get(f["key"]) or {}).get("best_match") for f in families)

        # No board responded and nothing matched anywhere => no authoritative
        # signal about this company. Never clear existing role data on a no-ats
        # result — it may have come from a verified web-search pass. Only flag
        # it for a web-search follow-up and re-stamp the check date.
        if not res.get("ats_present") and not any_match:
            stats["no_ats"] += 1
            stats["preserved_no_ats"] += 1
            if not (row.get("Web_Search_Needed") or "").strip():
                row["Web_Search_Needed"] = "Yes"
            row["Last_Checked"] = args.date
            if not (row.get("Source_Method") or "").strip():
                row["Source_Method"] = "ATS not auto-located"
            continue

        stats["updated"] += 1
        for fam in families:
            fam_res = fam_results.get(fam["key"])
            matched = write_family(row, fam, fam_res)
            if matched:
                stats[fam["key"]] += 1

        row["Match_Confidence"] = confidence(
            (primary_res or {}).get("best_match"), res.get("ats_present", False))
        row["Last_Checked"] = args.date
        row["Source_Method"] = res.get("source_method", "")

        # Web_Search_Needed is a STICKY company attribute: set once, never unset
        # by a later run, except when a real ATS board is now confirmed present.
        if not (row.get("Web_Search_Needed") or "").strip():
            if not res.get("ats_present"):
                row["Web_Search_Needed"] = "Yes"
        if not res.get("ats_present"):
            stats["no_ats"] += 1
        elif str(res.get("source_method", "")).startswith(ATS_PREFIXES):
            row["Web_Search_Needed"] = ""

    if args.dry_run:
        print(json.dumps(stats, indent=2))
        return

    bak = args.csv + ".bak"
    shutil.copy2(args.csv, bak)
    tmp = args.csv + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})
    os.replace(tmp, args.csv)
    os.remove(bak)
    print(json.dumps(stats, indent=2))
    sys.stderr.write(f"wrote {len(rows)} rows -> {args.csv}\n")


if __name__ == "__main__":
    main()
