#!/usr/bin/env python3
"""
ats_probe.py — probe public ATS job-board APIs for roles matching your criteria.

Probes public, no-auth ATS JSON APIs (Greenhouse, Ashby, Lever, SmartRecruiters,
Workable, Jobvite) for roles matching one or more "role families" you define in
a JSON config file, HTTP-verifies each posting URL, and emits one "best match +
additional count" record per company per family — ready to drop into a CSV.

WHAT'S CONFIGURABLE (see config.example.json)
  - geography: which role-location strings count as in-scope
  - seniority ranking: how "best match" is picked when several roles qualify
  - role families: any number of independent title/description filters, each
    with its own include/exclude regex, optional seniority requirement,
    optional "years of experience" gate parsed from the job description, and
    optional theme gate (e.g. "only count this role if it's about topic X").
    Each family writes to its own set of CSV columns.

Nothing about a specific job title, city, or company is hardcoded in this
script — that all lives in the config file, so the same script works for any
job search.

USAGE
  # From a CSV with at least a name column and a website column:
  python3 ats_probe.py --config config.json --in companies.csv \
      --name-col Company_Name --site-col Company_Website --out results.json

  # Ad-hoc, one or more "Name=domain" pairs (domain optional):
  python3 ats_probe.py --config config.json \
      --company "Fivetran=fivetran.com" --company "Teleport=goteleport.com"

  # Force a known slug when auto-location fails:
  python3 ats_probe.py --config config.json --company "Harness=harnessinc" --platform greenhouse

OUTPUT (JSON list, one object per company)
  {
    "company": "Fivetran",
    "source_method": "ashby:fivetran",
    "status": "matches" | "none-found" | "no-ats",
    "ats_present": true,
    "ats_jobs_total": 12,
    "web_search_needed": false,
    "families": {
      "<family-key>": {
        "best_match": {"title": ..., "seniority": ..., "location": ..., "pay": ...,
                       "url": ..., "platform": ..., "slug": ..., "verified": true,
                       "extra": {"years_required": "3+", "theme": "observability"}},
        "additional_matches_count": 1,
        "all_matches": [ ... ],
        "off_theme_filtered": 0,
        "years_filtered": 0
      }, ...
    }
  }

Network notes: no API keys needed. 403/429 are treated as ALIVE (rate-limited,
not dead). HTTP 200 alone does NOT prove the right company resolved — spot
check surprising results (see README "Verification" section).
"""

import argparse
import csv
import datetime
import html as _html
import json
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

TIMEOUT = 9
WORKERS = 24
UA = "Mozilla/5.0 (job-search-toolkit ats_probe)"

# Generic "N years" mention parser used by any family's years_experience_gate.
# Not domain-specific — matches "5 years", "5+ years", "5-7 years", etc.
YEARS_MENTION = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*(\d{1,2})\s*)?\+?\s*years?", re.I)
DEFAULT_YEARS_WINDOW = 60  # chars searched either side of a "N years" mention


# ----------------------------------------------------------------------------
# Config loading
# ----------------------------------------------------------------------------
def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    for i, fam in enumerate(cfg.get("role_families", [])):
        if "key" not in fam or "include_regex" not in fam:
            raise ValueError(f"role_families[{i}] must have 'key' and 'include_regex'")
        if "columns" not in fam:
            raise ValueError(f"role_families[{i}] ('{fam['key']}') must have 'columns'")
    if not cfg.get("role_families"):
        raise ValueError("config must define at least one role family in 'role_families'")
    return cfg


def compiled(pattern):
    return re.compile(pattern, re.I) if pattern else None


# ----------------------------------------------------------------------------
# HTTP helper — returns (status_code, body_text_or_None). 403/429 => alive.
# ----------------------------------------------------------------------------
def http_get(url, want_body=True):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace") if want_body else None
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None


def url_alive(url):
    """200 = alive; 403/429 = alive (rate-limited); else dead. Uses GET, follows redirects."""
    code, _ = http_get(url, want_body=False)
    return code in (200, 403, 429)


# ----------------------------------------------------------------------------
# Slug generation — the #1 failure mode is the wrong slug; try many variants.
# ----------------------------------------------------------------------------
def norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _ordered_unique(seq):
    seen, out = set(), []
    for s in seq:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def slug_variants(name, domain=None):
    """Return slug candidates in PRIORITY order — canonical forms first, then
    decorated variants. Order matters: a wrong-but-existing board for a decorated
    variant must never be probed before the company's real canonical slug."""
    canonical = []
    n = norm(name)
    if n:
        canonical.append(n)
    hy = re.sub(r"[^a-z0-9]+", "-", name.lower().strip()).strip("-")
    canonical.append(hy)
    domain_base = None
    if domain:
        d = re.sub(r"^https?://", "", domain.lower()).split("/")[0]
        d = re.sub(r"^www\.", "", d)
        domain_base = d.split(".")[0]
        canonical.append(domain_base)
        canonical.append(norm(domain_base or ""))
    canonical = _ordered_unique(canonical)

    stripped = []
    for b in canonical:
        for suf in ("inc", "hq", "io", "ai", "labs", "data", "app", "co"):
            if b.endswith(suf) and len(b) > len(suf) + 2:
                stripped.append(b[: -len(suf)])

    # Decorated variants (lowest priority — most likely to collide with others).
    # Keep these reasonably distinctive: ultra-generic suffixes like `tech`/`app`
    # collide with unrelated namesakes on platforms that 200 for any slug
    # (SmartRecruiters/Workable), producing false ATS-present hits.
    decorated = []
    for b in _ordered_unique(canonical + stripped):
        decorated += [f"{b}data", f"{b}inc", f"{b}dev", f"{b}-dev", f"{b}-ai", f"{b}-hq",
                      f"{b}hq", f"{b}db", f"{b}labs"]

    return _ordered_unique(canonical + stripped + decorated)


# ----------------------------------------------------------------------------
# Per-platform probes. Each returns list of dicts: title, location, pay, url.
# Returns None if the board doesn't exist for that slug (so we can try next).
# ----------------------------------------------------------------------------
def probe_greenhouse(slug):
    code, body = http_get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false")
    if code != 200 or not body:
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    jobs = data.get("jobs") or []
    if not jobs and data.get("meta", {}).get("total", 0) == 0:
        return []
    return [
        {
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "pay": "",
            "url": j.get("absolute_url", ""),
        }
        for j in jobs
    ]


def probe_ashby(slug):
    code, body = http_get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    if code != 200 or not body:
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    jobs = data.get("jobs") or []
    return [
        {
            "title": j.get("title", ""),
            "location": j.get("location", "") or j.get("locationName", ""),
            "pay": (j.get("compensation") or {}).get("compensationTierSummary", "") or "",
            "url": j.get("jobUrl", "") or j.get("applyUrl", ""),
            # Ashby exposes department/team and the full description for free —
            # useful for theme gates and years-of-experience gates.
            "department": " ".join(x for x in [j.get("department"), j.get("team")] if x),
            "description": j.get("descriptionPlain", "") or "",
        }
        for j in jobs
    ]


def probe_lever(slug):
    code, body = http_get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if code != 200 or not body:
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    return [
        {
            "title": j.get("text", ""),
            "location": (j.get("categories") or {}).get("location", ""),
            "pay": "",
            "url": j.get("hostedUrl", "") or j.get("applyUrl", ""),
            "department": " ".join(
                x for x in [(j.get("categories") or {}).get("department"),
                            (j.get("categories") or {}).get("team")] if x),
            "description": j.get("descriptionPlain", "") or "",
        }
        for j in data
    ]


def probe_smartrecruiters(slug):
    code, body = http_get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    if code != 200 or not body:
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    content = data.get("content") or []
    # SmartRecruiters returns HTTP 200 + totalFound=0 for ANY slug (even bogus
    # ones), so a 200 is NOT proof the board exists. Treat an empty result as
    # "no board" (None) rather than an empty-but-real board.
    if not content and not data.get("totalFound"):
        return None
    out = []
    for j in content:
        loc = j.get("location") or {}
        loc_str = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
        jid = j.get("id", "")
        out.append(
            {
                "title": j.get("name", ""),
                "location": loc_str + (" (remote)" if loc.get("remote") else ""),
                "pay": "",
                "url": f"https://jobs.smartrecruiters.com/{slug}/{jid}" if jid else "",
            }
        )
    return out


def probe_workable(slug):
    code, body = http_get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    if code != 200 or not body:
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    jobs = data.get("jobs") or []
    # Like SmartRecruiters, Workable's widget endpoint can 200 for non-existent
    # accounts — treat an empty result as "no board" rather than a real empty one.
    if not jobs:
        return None
    out = []
    for j in jobs:
        out.append(
            {
                "title": j.get("title", ""),
                "location": j.get("location", "") or j.get("city", ""),
                "pay": "",
                "url": j.get("url", "") or j.get("application_url", ""),
            }
        )
    return out


# Jobvite has no clean slug-based JSON API, but its careersite IS server-rendered
# HTML at jobs.jobvite.com/{slug}/jobs — one <tr> per posting with stable classes.
_JV_ROW = re.compile(
    r'<td class="jv-job-list-name">\s*<a href="(/[^"]+/job/[^"]+)">([^<]+)</a>'
    r'.*?<td class="jv-job-list-location">(.*?)</td>',
    re.S,
)


def probe_jobvite(slug):
    code, body = http_get(f"https://jobs.jobvite.com/{slug}/jobs")
    if code != 200 or not body:
        return None
    rows = _JV_ROW.findall(body)
    if not rows:
        return None  # bogus slug falls back to a JS shell / support page -> no board
    out = []
    for href, title, loc in rows:
        loc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", loc)).strip().strip(",").strip()
        out.append(
            {
                "title": title.strip(),
                "location": loc,
                "pay": "",
                "url": "https://jobs.jobvite.com" + href,
            }
        )
    return out


PLATFORMS = {
    "greenhouse": probe_greenhouse,
    "ashby": probe_ashby,
    "lever": probe_lever,
    "smartrecruiters": probe_smartrecruiters,
    "workable": probe_workable,
    "jobvite": probe_jobvite,
}


# ----------------------------------------------------------------------------
# Geography gate — config-driven, shared across every family.
# ----------------------------------------------------------------------------
class Geography:
    def __init__(self, geo_cfg):
        geo_cfg = geo_cfg or {}
        self.include_rx = compiled(geo_cfg.get("include_regex"))
        self.foreign_rx = compiled(geo_cfg.get("foreign_regex"))
        self.us_override_rx = compiled(geo_cfg.get("us_override_regex"))
        self.priority_rx = compiled(geo_cfg.get("priority_regex"))

    def ok(self, location):
        if not self.include_rx:
            return True  # geography filtering disabled entirely
        if not location:
            return True  # empty location -> eligible, flagged downstream
        if not self.include_rx.search(location):
            return False
        if self.foreign_rx and self.foreign_rx.search(location):
            if not (self.us_override_rx and self.us_override_rx.search(location)):
                return False
        return True

    def priority(self, location):
        if self.priority_rx and location and self.priority_rx.search(location):
            return 2
        if self.include_rx and location and self.include_rx.search(location):
            return 1
        return 0


# ----------------------------------------------------------------------------
# Seniority ranking — config-driven ordered list of (regex, score).
# ----------------------------------------------------------------------------
class SeniorityRank:
    def __init__(self, rank_cfg):
        default = [
            {"regex": r"principal|head|vp|director|chief", "score": 5},
            {"regex": r"staff", "score": 4},
            {"regex": r"lead", "score": 3},
            {"regex": r"senior|sr\.?\b", "score": 2},
        ]
        self.rules = [(compiled(r["regex"]), r["score"], r["regex"]) for r in (rank_cfg or default)]

    def label(self, title):
        for rx, _, pattern in self.rules:
            if rx.search(title):
                return pattern.split("|")[0].title()
        return "Standard"

    def score(self, title):
        for rx, score, _ in self.rules:
            if rx.search(title):
                return score
        return 1


# ----------------------------------------------------------------------------
# Role family — wraps one config['role_families'][i] entry.
# ----------------------------------------------------------------------------
class RoleFamily:
    def __init__(self, cfg, geo):
        self.key = cfg["key"]
        self.label = cfg.get("label", cfg["key"])
        self.columns = cfg["columns"]
        self.primary = bool(cfg.get("primary"))
        self.include_rx = compiled(cfg["include_regex"])
        self.exclude_rx = compiled(cfg.get("exclude_regex"))
        self.require_seniority = bool(cfg.get("require_seniority"))
        self.seniority_rx = compiled(cfg.get("seniority_regex")) if self.require_seniority else None
        self.geo = geo

        tg = cfg.get("theme_gate")
        self.theme_mode = tg.get("mode") if tg else None  # "column" | "always"
        self.theme_keywords_rx = compiled(tg.get("keywords_regex")) if tg else None
        self.theme_column = tg.get("enabled_when_column") if tg else None
        self.native_rx = compiled(tg.get("native_company_regex")) if tg else None
        self.native_columns = (tg.get("native_company_columns") or []) if tg else []

        yg = cfg.get("years_experience_gate")
        self.years_max = yg.get("max_years") if yg else None
        self.years_terms_rx = compiled(yg.get("terms_regex")) if yg else None
        self.years_window = (yg.get("window") if yg else None) or DEFAULT_YEARS_WINDOW

    def title_matches(self, title):
        if not title:
            return False
        if self.exclude_rx and self.exclude_rx.search(title):
            return False
        if not self.include_rx.search(title):
            return False
        if self.require_seniority and not self.seniority_rx.search(title):
            return False
        return True

    def theme_reason(self, title, department, theme_enabled, native):
        if not self.theme_keywords_rx:
            return None, True  # no theme gate configured -> always on-theme
        hay = f"{title} {department or ''}"
        m = self.theme_keywords_rx.search(hay)
        if m:
            return m.group(0), True
        if self.theme_mode == "always" and native:
            return "company-native", True
        if self.theme_mode == "column" and not theme_enabled:
            return None, True  # gate not active for this company -> accept any match
        return None, False


def years_required(description, terms_rx, window):
    """Smallest explicit "N years" mention adjacent to a gate-relevant term, or
    None if the description never states one. Windows are clipped at neighboring
    years-mentions so a term can't be stolen from an adjacent clause, e.g.
    "7+ years in Design, 2 years of management" correctly attributes only the 2."""
    if not description or not terms_rx:
        return None
    hits = list(YEARS_MENTION.finditer(description))
    years = []
    for i, m in enumerate(hits):
        try:
            v = int(m.group(1))
        except (TypeError, ValueError):
            continue
        if not 0 < v <= 25:
            continue
        prev_end = hits[i - 1].end() if i else 0
        next_start = hits[i + 1].start() if i + 1 < len(hits) else len(description)
        before = description[max(prev_end, m.start() - window):m.start()]
        after = description[m.end():min(next_start, m.end() + window)]
        if terms_rx.search(before) or terms_rx.search(after):
            years.append(v)
    return min(years) if years else None


# ---- job-description fetch (only called for families with a years gate) ----
_TAG_RX = re.compile(r"<[^>]+>")


def _strip_html(s):
    return re.sub(r"\s+", " ", _TAG_RX.sub(" ", s or "")).strip()


def fetch_description(role):
    """Best-effort plain-text job description for a single posting. Ashby and
    Lever already carry descriptionPlain at probe time. Greenhouse needs a
    per-posting fetch, done on demand only for families with a years gate."""
    if role.get("description"):
        return role["description"]
    platform, slug, url = role.get("platform"), role.get("slug"), role.get("url", "")
    if platform == "greenhouse":
        m = re.search(r"/jobs/(\d+)", url) or re.search(r"gh_jid=(\d+)", url)
        if m:
            code, body = http_get(
                f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{m.group(1)}")
            if code == 200 and body:
                try:
                    d = json.loads(body)
                except Exception:
                    return ""
                return _strip_html(_html.unescape(d.get("content") or ""))
    return ""


def rank_key(role, geo, srank):
    score = srank.score(role["title"])
    geo_score = geo.priority(role.get("location", ""))
    has_pay = 1 if role.get("pay") else 0
    return (score, geo_score, has_pay)


def probe_company(name, geo, srank, families, domain=None, forced_slug=None,
                  only_platform=None, verify=True, theme_columns_on=None,
                  native_flags=None):
    """theme_columns_on: dict of {family_key: bool} — per-company theme-gate
    activation for "column"-mode families (from the CSV row, or forced True for
    ad-hoc runs via --force-theme).
    native_flags: dict of {family_key: bool} — per-company "is this company
    native to the family's theme" flag, for "always"-mode native-company gates.
    """
    theme_columns_on = theme_columns_on or {}
    native_flags = native_flags or {}

    platforms = {only_platform: PLATFORMS[only_platform]} if only_platform else PLATFORMS
    slugs = [forced_slug] if forced_slug else slug_variants(name, domain)
    first_method = None
    best_method = None
    best_board_jobs = 0
    matches_method = None
    matches_board_jobs = 0
    fam_matches = {f.key: [] for f in families}
    fam_off_theme = {f.key: 0 for f in families}
    fam_years_filtered = {f.key: 0 for f in families}

    for slug in slugs:
        slug_done = False
        for pname, fn in platforms.items():
            res = fn(slug)
            if res is None:
                continue
            method = f"{pname}:{slug}"
            if first_method is None:
                first_method = method
            if len(res) > best_board_jobs:
                best_board_jobs = len(res)
                best_method = method

            slug_fam_matches = {f.key: [] for f in families}
            slug_off_theme = {f.key: 0 for f in families}
            slug_years_filtered = {f.key: 0 for f in families}

            for r in res:
                title, loc = r["title"], r["location"]
                dept = r.get("department", "")
                if not geo.ok(loc):
                    continue

                def _tag(role, fam):
                    role["platform"] = pname
                    role["slug"] = slug
                    role["seniority"] = srank.label(role["title"])
                    return role

                for fam in families:
                    if not fam.title_matches(title):
                        continue
                    theme_reason, on_theme = fam.theme_reason(
                        title, dept, theme_columns_on.get(fam.key, False),
                        native_flags.get(fam.key, False))
                    if not on_theme:
                        slug_off_theme[fam.key] += 1
                        continue
                    m = _tag(dict(r), fam)
                    extra = {}
                    if theme_reason:
                        extra["theme"] = theme_reason
                    if fam.years_max is not None:
                        yrs = years_required(fetch_description(m), fam.years_terms_rx, fam.years_window)
                        extra["years_required"] = "unspecified" if yrs is None else f"{yrs}+"
                        if yrs is not None and yrs >= fam.years_max:
                            slug_years_filtered[fam.key] += 1
                            continue
                    m["extra"] = extra
                    slug_fam_matches[fam.key].append(m)

            if any(slug_fam_matches.values()):
                for f in families:
                    fam_matches[f.key] = slug_fam_matches[f.key]
                    fam_off_theme[f.key] += slug_off_theme[f.key]
                    fam_years_filtered[f.key] += slug_years_filtered[f.key]
                matches_method = method
                matches_board_jobs = len(res)
                slug_done = True
            else:
                for f in families:
                    fam_off_theme[f.key] += slug_off_theme[f.key]
                    fam_years_filtered[f.key] += slug_years_filtered[f.key]
            break
        # Only stop scanning slugs once we've found a board WITH matches. An
        # empty board (wrong company / nothing posted) must not hijack the result.
        if slug_done:
            break

    located_method = matches_method or best_method or first_method

    def _dedup_rank(lst):
        seen, out = set(), []
        for m in lst:
            k = (m["title"].strip().lower(), m["location"].strip().lower())
            if k not in seen:
                seen.add(k)
                out.append(m)
        out.sort(key=lambda m: rank_key(m, geo, srank), reverse=True)
        return out

    fam_uniq = {f.key: _dedup_rank(fam_matches[f.key]) for f in families}

    if verify:
        for lst in fam_uniq.values():
            for m in lst:
                m["verified"] = bool(m["url"]) and url_alive(m["url"])

    for lst in fam_uniq.values():
        for m in lst:
            m.pop("description", None)

    any_match = any(fam_uniq.values())
    ats_jobs_total = matches_board_jobs if any_match else best_board_jobs
    ats_present = ats_jobs_total > 0

    if any_match:
        status = "matches"
        source = located_method
    elif ats_present:
        status = "none-found"
        source = located_method
    else:
        status = "no-ats"
        source = "ATS not auto-located"

    families_out = {}
    for f in families:
        uniq = fam_uniq[f.key]
        families_out[f.key] = {
            "best_match": uniq[0] if uniq else None,
            "additional_matches_count": max(0, len(uniq) - 1),
            "all_matches": uniq,
            "off_theme_filtered": fam_off_theme[f.key],
            "years_filtered": fam_years_filtered[f.key],
        }

    return {
        "company": name,
        "source_method": source or "ATS not auto-located",
        "status": status,
        "ats_present": ats_present,
        "ats_jobs_total": ats_jobs_total,
        "web_search_needed": not ats_present,
        "families": families_out,
    }


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _recently_applied(applied_val, applied_date_val, days):
    """True if applied_val is truthy and applied_date_val is within `days` days
    of today. Truthy applied values: TRUE/YES/1/Y (case-insensitive) — matches
    both a Google Sheets checkbox export ("TRUE") and a plain "Yes"."""
    if days <= 0:
        return False
    if (applied_val or "").strip().upper() not in ("TRUE", "YES", "1", "Y"):
        return False
    try:
        applied_date = datetime.datetime.strptime((applied_date_val or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return False
    return applied_date >= datetime.date.today() - datetime.timedelta(days=days)


def main():
    ap = argparse.ArgumentParser(description="Probe public ATS APIs for roles matching your config.")
    ap.add_argument("--config", required=True, help="path to a role-matching config JSON file")
    ap.add_argument("--in", dest="infile", help="input CSV of companies")
    ap.add_argument("--name-col", default="Company_Name")
    ap.add_argument("--site-col", default="Company_Website")
    ap.add_argument("--slug-col", help="optional column holding a known ATS slug")
    ap.add_argument("--applied-col", default="Applied",
                    help="CSV column marking a company as already applied-to. Truthy "
                         "values recognized: TRUE, YES, 1 (case-insensitive)")
    ap.add_argument("--applied-date-col", default="Applied_Date",
                    help="CSV column with the YYYY-MM-DD application date")
    ap.add_argument("--skip-applied-days", type=int, default=120,
                    help="skip (don't probe) companies applied to within this many "
                         "days (0 disables this filter)")
    ap.add_argument("--company", action="append", default=[], help='ad-hoc "Name=domain_or_slug"')
    ap.add_argument("--platform", choices=list(PLATFORMS), help="restrict to one platform")
    ap.add_argument("--force-theme", action="store_true",
                    help="for ad-hoc --company runs (no CSV row to read a theme column "
                         "from): treat all 'column'-mode theme gates as active")
    ap.add_argument("--no-verify", action="store_true", help="skip HTTP URL verification")
    ap.add_argument("--out", help="write JSON results here (default: stdout)")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--progress-every", type=int, default=0,
                    help="print a 'checked N/total' progress line to stderr every "
                         "N completions (0 disables; for unattended/log-tailed runs)")
    ap.add_argument("--progress-label", default="",
                    help="prefix for progress lines, e.g. a batch identifier")
    args = ap.parse_args()

    cfg = load_config(args.config)
    geo = Geography(cfg.get("geography"))
    srank = SeniorityRank(cfg.get("seniority_rank"))
    families = [RoleFamily(f, geo) for f in cfg["role_families"]]

    jobs = []  # (name, domain, forced_slug, theme_columns_on, native_flags)
    skipped_applied = []
    if args.infile:
        with open(args.infile, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = (row.get(args.name_col) or "").strip()
                if not name:
                    continue
                if _recently_applied(row.get(args.applied_col), row.get(args.applied_date_col),
                                      args.skip_applied_days):
                    skipped_applied.append(name)
                    continue
                theme_on, native_on = {}, {}
                for fam in families:
                    if fam.theme_mode == "column" and fam.theme_column:
                        theme_on[fam.key] = bool((row.get(fam.theme_column) or "").strip())
                    if fam.theme_mode == "always" and fam.native_rx and fam.native_columns:
                        tax = " ".join((row.get(c) or "") for c in fam.native_columns)
                        native_on[fam.key] = bool(fam.native_rx.search(tax))
                jobs.append((name, (row.get(args.site_col) or "").strip() or None,
                             (row.get(args.slug_col) or "").strip() if args.slug_col else None,
                             theme_on, native_on))
        if skipped_applied:
            sys.stderr.write(
                f"skipped {len(skipped_applied)} companies applied to within "
                f"{args.skip_applied_days} days (do-not-update rule): "
                f"{', '.join(skipped_applied)}\n"
            )
    for pair in args.company:
        if "=" in pair:
            n, d = pair.split("=", 1)
        else:
            n, d = pair, None
        theme_on = {f.key: True for f in families if f.theme_mode == "column"} if args.force_theme else {}
        jobs.append((n.strip(), (d or "").strip() or None, None, theme_on, {}))

    if not jobs:
        ap.error("no companies given (use --in or --company)")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {
            ex.submit(probe_company, n, geo, srank, families, d, s, args.platform,
                      not args.no_verify, theme_on, native_on): n
            for (n, d, s, theme_on, native_on) in jobs
        }
        total = len(futs)
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({"company": futs[fut], "status": "error", "error": str(e)})
            if args.progress_every and len(results) % args.progress_every == 0:
                sys.stderr.write(
                    f"Progress: {args.progress_label}{len(results)}/{total} companies checked\n"
                )
                sys.stderr.flush()
        if args.progress_every and (not results or len(results) % args.progress_every != 0):
            sys.stderr.write(
                f"Progress: {args.progress_label}{len(results)}/{total} companies checked\n"
            )
            sys.stderr.flush()

    results.sort(key=lambda r: r["company"].lower())
    out = json.dumps(results, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out)
        sys.stderr.write(f"wrote {len(results)} companies -> {args.out}\n")
    else:
        print(out)


if __name__ == "__main__":
    main()
