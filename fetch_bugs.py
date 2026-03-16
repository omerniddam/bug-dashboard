#!/usr/bin/env python3
"""
Bug Dashboard Generator
=======================
Reads config.json, fetches 6 months of bug data from Jira,
and generates a self-contained interactive HTML dashboard.

Usage:
    python3 fetch_bugs.py

Output:
    bug_dashboard.html  — open this in any browser, no server needed.
    dashboard_cache.json — raw data cache for debugging.
"""

import json, sys, math, re, requests
from datetime import datetime, date, timedelta, timezone
from collections import defaultdict
from pathlib import Path
import base64, urllib.parse

SCRIPT_DIR   = Path(__file__).parent
CONFIG_PATH  = SCRIPT_DIR / "config.json"
OUTPUT_HTML  = SCRIPT_DIR / "bug_dashboard.html"
CACHE_PATH   = SCRIPT_DIR / "dashboard_cache.json"

# ─── CONFIG ──────────────────────────────────────────────────────────────────

def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(f"ERROR: config.json not found at {CONFIG_PATH}")
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    if "PASTE_YOUR" in cfg.get("api_token", ""):
        sys.exit("ERROR: Please open config.json and replace PASTE_YOUR_API_TOKEN_HERE with your real Jira API token.")
    return cfg

# ─── JIRA API ────────────────────────────────────────────────────────────────

def _jira_get_json(auth, url, params):
    """GET request returning parsed JSON, or raises on HTTP error."""
    r = requests.get(url, auth=auth, params=params, timeout=30,
                     headers={"Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def jira_search_page(config, jql, fields, max_results=100, expand=None,
                     start_at=0, next_page_token=None, verbose=True):
    """
    Fetch one page from Jira. Supports both:
      - cursor pagination (nextPageToken) used by GET /rest/api/3/search/jql
      - offset pagination (startAt) used by older endpoints
    Returns (issues_list, next_page_token_or_None).
    next_page_token=None means no more pages.
    """
    base = config["jira_url"].rstrip("/")
    auth = (config["email"], config["api_token"])
    fields_list = [f.strip() for f in fields.split(",")] if isinstance(fields, str) else list(fields)
    fields_str  = ",".join(fields_list)

    # Only the new v3 endpoint works (v2 is 410 Gone).
    # It uses nextPageToken for pagination — ignore the capped `total` field.
    url = base + "/rest/api/3/search/jql"
    exp_tag = "expand" if expand else "no-expand"

    try:
        params = {"jql": jql, "fields": fields_str, "maxResults": max_results}
        if expand:
            params["expand"] = expand
        if next_page_token:
            params["nextPageToken"] = next_page_token
        else:
            params["startAt"] = start_at

        r = requests.get(url, auth=auth, params=params, timeout=60,
                         headers={"Accept": "application/json"})

        if r.status_code in (404, 405, 410):
            raise RuntimeError(f"GET /rest/api/3/search/jql returned HTTP {r.status_code}")

        r.raise_for_status()
        data = r.json()

        if "errorMessages" in data and data["errorMessages"]:
            raise RuntimeError("; ".join(str(m) for m in data["errorMessages"]))

        issues = data.get("issues") or data.get("values") or []
        token  = data.get("nextPageToken")   # None when there are no more pages

        if verbose:
            note = " (no changelog)" if not expand else ""
            total = data.get("total", "?")
            print(f"  -> GET /rest/api/3/search/jql [{exp_tag}]: "
                  f"{len(issues)} issues (total reported: {total}){note}")
        return issues, token

    except Exception as e:
        if verbose:
            print(f"  -> GET /rest/api/3/search/jql [{exp_tag}]: {e} — failed")
        raise


def fetch_all(config, jql, fields, expand=None, limit=3000):
    """
    Paginate through ALL Jira results using nextPageToken (up to `limit`).
    The new /rest/api/3/search/jql endpoint returns a nextPageToken when
    more results exist — we follow it until it disappears or we hit `limit`.
    """
    print(f"  JQL: {jql[:120]}{'...' if len(jql)>120 else ''}")
    issues, seen_keys, token = [], set(), None
    page = 0
    while len(issues) < limit:
        batch, token = jira_search_page(
            config, jql, fields,
            max_results=100, expand=expand,
            start_at=page * 100, next_page_token=token,
            verbose=(page == 0),
        )
        if not batch:
            break
        # Filter out any non-dict items (malformed responses)
        batch = [i for i in batch if isinstance(i, dict)]
        new = [i for i in batch if i.get("key") not in seen_keys]
        seen_keys.update(i.get("key") for i in batch)
        issues.extend(new)
        page += 1
        print(f"  Fetched {len(issues)} issues...", end="\r", flush=True)
        # No nextPageToken = Jira says there are no more pages
        if not token:
            break
        # Safety: full batch of duplicates = we've looped
        if not new:
            break
    print(f"  Done: {len(issues)} issues fetched.           ")
    if len(issues) == 0:
        check_jira_auth(config)
    return issues[:limit]


def check_jira_auth(config):
    """Quick auth check: call /myself and /project to verify credentials & access."""
    base = config["jira_url"].rstrip("/")
    auth = (config["email"], config["api_token"])
    print("\nChecking Jira credentials...")
    try:
        me = _jira_get_json(auth, f"{base}/rest/api/3/myself", {})
        print(f"  Auth OK  — logged in as: {me.get('displayName','?')} ({me.get('emailAddress','?')})")
    except Exception as e:
        print(f"  Auth FAIL — {e}")
        print("  Fix: check email and api_token in config.json.")
        return False
    try:
        projects = _jira_get_json(auth, f"{base}/rest/api/3/project", {"maxResults": 50})
        keys = [p.get("key","?") for p in (projects if isinstance(projects, list) else projects.get("values",[]))]
        proj = config.get("projects", "OXDEV")
        has_access = any(k == proj for k in keys)
        if has_access:
            print(f"  Project  OK — '{proj}' is accessible (all visible: {', '.join(keys[:8])}{'...' if len(keys)>8 else ''})")
        else:
            print(f"  Project  WARN — '{proj}' not found. Visible projects: {', '.join(keys[:8])}")
        return has_access
    except Exception as e:
        print(f"  Project check failed: {e}")
        return False


# ─── DATA PROCESSING ─────────────────────────────────────────────────────────

def normalize_dt_str(s):
    """Normalize Jira date string to ISO 8601 with colon in timezone offset.
    Converts +0300 → +03:00 so both Python 3.10 and JavaScript can parse it."""
    if not s:
        return s
    return re.sub(r'([+-])(\d{2})(\d{2})$', r'\1\2:\3', s.replace("Z", "+00:00"))

def parse_dt(s):
    """Parse Jira ISO datetime string → timezone-aware datetime."""
    if not s:
        return None
    try:
        # Normalize +0300 → +03:00 (Python 3.10 requires the colon)
        ns = normalize_dt_str(s)
        dt = datetime.fromisoformat(ns)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

TEAM_ALIASES = {
    # underscore ↔ space variants + any typos (lower-case keys)
    "experience_edge":  "Experience Edge",
    "experience edge":  "Experience Edge",
    "experince_edge":   "Experience Edge",
    "experince edge":   "Experience Edge",
    "experience_core":  "Experience Core",
    "experience core":  "Experience Core",
    "experince_core":   "Experience Core",
    "experince core":   "Experience Core",
    "security_core":    "Security Core",
    "security core":    "Security Core",
    "scanner_g":        "Scanner G",
    "scanner_s":        "Scanner S",
    "agentic_pentest":  "Agentic Pentest",
    "agentic pentest":  "Agentic Pentest",
    "ai_vibesec":       "AI Vibesec",
    "ai vibesec":       "AI Vibesec",
}

def normalize_team(name: str) -> str:
    """Canonicalise team name: collapse underscore variants, fix typos."""
    if not name or name == "Unknown":
        return name
    return TEAM_ALIASES.get(name.lower(), name)

def extract_team(field_value):
    """Extract a team/label string from various Jira custom field formats:
      - None / null            → "Unknown"
      - "Some String"          → "Some String"  (plain string)
      - ["Label1", "Label2"]   → "Label1"        (list of strings, e.g. cf[10032])
      - {"name": "Team X"}     → "Team X"        (dict with name/value, e.g. cf[10001])
      - [{"name": "Team X"}]   → "Team X"        (list of dicts)
    """
    if field_value is None:
        return "Unknown"
    if isinstance(field_value, str):
        return field_value or "Unknown"
    if isinstance(field_value, list) and field_value:
        first = field_value[0]
        if isinstance(first, str):
            return first or "Unknown"
        if isinstance(first, dict):
            return first.get("value") or first.get("name") or first.get("displayName") or "Unknown"
        return str(first) or "Unknown"
    if isinstance(field_value, dict):
        return (field_value.get("value") or field_value.get("name")
                or field_value.get("displayName") or "Unknown")
    return str(field_value) or "Unknown"

def process_issues(raw_issues, config):
    resolved_set = set(config.get("resolved_statuses", ["Done", "To be reviewed by the customer"]))
    team_field    = config.get("team_field",    "customfield_10032")
    account_field = config.get("account_field", "customfield_10112")
    processed    = []

    skipped = 0
    for issue in raw_issues:
        try:
            if not isinstance(issue, dict):
                skipped += 1
                continue
            f          = issue.get("fields") or {}
            if not isinstance(f, dict):
                skipped += 1
                continue
            histories  = (issue.get("changelog") or {}).get("histories", [])
            if not isinstance(histories, list):
                histories = []
            key        = issue.get("key", "")
            if not key:
                skipped += 1
                continue
            status     = (f.get("status") or {}).get("name", "Unknown")
            assignee   = (f.get("assignee") or {}).get("displayName", "Unassigned")
            priority   = (f.get("priority") or {}).get("name", "None")
            # cf[10032] = Department/Team labels field (list of strings e.g. ['Experience_Core'])
            # cf[10001] = Jira built-in Team field (dict with 'name', e.g. {'name':'Scanner G'})
            # Use cf[10032] first; fall back to cf[10001] if not set
            team_raw    = f.get(team_field) or f.get("customfield_10001")
            team        = normalize_team(extract_team(team_raw))
            # Skip QA bugs entirely — they skew cycle-time charts significantly
            if team.lower() == "qa":
                skipped += 1
                continue
            account     = extract_team(f.get(account_field))
            created_str = normalize_dt_str(f.get("created"))
            res_field_str = normalize_dt_str(f.get("resolutiondate"))

            # Walk changelog for first "In Progress" and last transition to a resolved status
            first_ip   = None
            resolved_cl = None
            for h in sorted(histories, key=lambda x: x.get("created", "") if isinstance(x, dict) else ""):
                if not isinstance(h, dict):
                    continue
                for item in (h.get("items") or []):
                    if not isinstance(item, dict):
                        continue
                    if item.get("field") == "status":
                        to = item.get("toString", "")
                        if to == config.get("in_progress_status", "In Progress") and not first_ip:
                            first_ip = normalize_dt_str(h.get("created"))
                        if to in resolved_set:
                            resolved_cl = normalize_dt_str(h.get("created"))

            best_resolved = resolved_cl or res_field_str

            # Cycle times
            cd  = parse_dt(created_str)
            rd  = parse_dt(best_resolved)
            ipd = parse_dt(first_ip)
            ct_created = round((rd - cd).total_seconds()  / 86400, 2) if cd and rd and rd > cd  else None
            ct_ip      = round((rd - ipd).total_seconds() / 86400, 2) if ipd and rd and rd > ipd else None

            processed.append({
                "key":                   key,
                "summary":               f.get("summary", ""),
                "status":                status,
                "assignee":              assignee,
                "priority":              priority,
                "team":                  team,
                "account":               account,
                "created":               created_str,
                "resolved":              best_resolved,
                "resolved_from_changelog": resolved_cl,
                "resolved_from_field":   res_field_str,
                "first_in_progress":     first_ip,
                "cycle_time_created":    ct_created,
                "cycle_time_in_progress": ct_ip,
                "is_resolved":           status in resolved_set,
                "labels":                f.get("labels") or [],
            })
        except Exception:
            skipped += 1
            continue
    if skipped:
        print(f"  ⚠️  Skipped {skipped} malformed issues.")
    return processed

# ─── BACKLOG TIMELINE ────────────────────────────────────────────────────────

def build_timeline(issues, days=182):
    """
    Build a daily open-bug-count series using a difference array.
    An issue is "open" on day D if: created <= D AND (unresolved OR resolved > D).
    """
    today = date.today()
    start = today - timedelta(days=days)
    delta = defaultdict(int)

    for issue in issues:
        cd = parse_dt(issue["created"])
        rd = parse_dt(issue["resolved"]) if issue.get("resolved") else None
        if not cd:
            continue
        s = max(cd.date(), start)
        e = rd.date() if rd else today + timedelta(days=1)
        if s > today:
            continue
        delta[s] += 1
        if e <= today:
            delta[e] -= 1

    running, timeline = 0, []
    for i in range(days + 1):
        d = start + timedelta(days=i)
        running += delta[d]
        timeline.append({"date": d.isoformat(), "open": max(0, running)})
    return timeline

def build_normalized_timeline(issues, days=182):
    """
    Like build_timeline but also tracks unique customers per day.
    For each day: count open bugs AND unique non-Unknown accounts among them.
    Returns list of {date, open, customers, per_customer}.
    O(days × bugs) — fast enough for ~200 days × 2500 bugs.
    """
    today = date.today()
    start = today - timedelta(days=days)

    # Pre-parse every issue once
    parsed = []
    for issue in issues:
        cd = parse_dt(issue.get("created"))
        rd = parse_dt(issue.get("resolved")) if issue.get("resolved") else None
        account = issue.get("account", "Unknown") or "Unknown"
        if cd:
            parsed.append((cd.date(), rd.date() if rd else None, account))

    result = []
    for i in range(days + 1):
        day = start + timedelta(days=i)
        open_count = 0
        customers  = set()
        for cd, rd, account in parsed:
            if cd <= day and (rd is None or rd > day):
                open_count += 1
                if account != "Unknown":
                    customers.add(account)
        n_cust = len(customers) or 1
        result.append({
            "date":         day.isoformat(),
            "open":         open_count,
            "customers":    len(customers),
            "per_customer": round(open_count / n_cust, 2),
        })
    return result


# ─── HTML GENERATION ─────────────────────────────────────────────────────────

def build_html(bugs, timeline, norm_timeline, config, generated_at):
    bugs_json          = json.dumps(bugs,          ensure_ascii=False)
    timeline_json      = json.dumps(timeline,      ensure_ascii=False)
    norm_timeline_json = json.dumps(norm_timeline, ensure_ascii=False)
    dash_cfg      = {
        "jira_url":         config["jira_url"],
        "email":            config.get("email", ""),
        # api_token intentionally omitted — never embed secrets in HTML
        "projects":         config.get("projects", ""),
        "bug_jql":          config.get("bug_jql", "(issuetype = Bug OR labels in (bug, jira_escalated))"),
        "resolved_statuses": config.get("resolved_statuses", ["Done", "To be reviewed by the customer"]),
        "working_days":     config.get("working_days_per_month", 20),
        "generated_at":     generated_at,
        "chart_jql_overrides": config.get("chart_jql_overrides", {}),
        "github_repo":      config.get("github_repo", ""),
        "github_workflow":  config.get("github_workflow", "refresh-dashboard.yml"),
    }
    cfg_json = json.dumps(dash_cfg, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>Bug Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js" charset="utf-8"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;font-size:14px}}
a{{color:#818cf8}}
/* ── Layout */
#app{{display:flex;flex-direction:column;min-height:100vh}}
header{{background:#1e293b;border-bottom:1px solid #334155;padding:14px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}}
header h1{{font-size:20px;font-weight:700;color:#f1f5f9;flex:1}}
.meta{{font-size:12px;color:#94a3b8}}
.refresh-note{{font-size:11px;color:#64748b;font-style:italic}}
/* ── Month bar */
#month-bar{{background:#1e293b;border-bottom:1px solid #334155;padding:10px 24px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
#month-bar label{{font-size:12px;color:#94a3b8;margin-right:4px}}
.m-btn{{padding:5px 12px;border-radius:6px;border:1px solid #334155;background:transparent;color:#94a3b8;cursor:pointer;font-size:12px;transition:all .15s}}
.m-btn:hover{{border-color:#6366f1;color:#e2e8f0}}
.m-btn.active{{background:#6366f1;border-color:#6366f1;color:#fff;font-weight:600}}
/* ── Working days setting */
.wd-wrap{{margin-left:auto;display:flex;align-items:center;gap:6px;font-size:12px;color:#94a3b8}}
.wd-wrap input{{width:48px;background:#0f172a;border:1px solid #334155;border-radius:4px;color:#e2e8f0;padding:3px 6px;font-size:12px;text-align:center}}
/* ── Summary cards */
#summary-row{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;padding:16px 24px}}
.scard{{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px 16px}}
.scard .val{{font-size:28px;font-weight:700;color:#6366f1}}
.scard .lbl{{font-size:11px;color:#94a3b8;margin-top:4px}}
.scard .sub{{font-size:11px;color:#64748b;margin-top:2px}}
/* ── Charts grid */
#charts-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(540px,1fr));gap:16px;padding:0 24px 24px}}
.chart-card{{background:#1e293b;border:1px solid #334155;border-radius:10px;overflow:visible;transition:box-shadow .2s,opacity .15s}}
.chart-card.dragging{{opacity:.45;box-shadow:0 8px 32px rgba(0,0,0,.5)}}
.chart-card.drop-left {{box-shadow:inset 4px 0 0 #6366f1;border-color:#6366f1}}
.chart-card.drop-right{{box-shadow:inset -4px 0 0 #6366f1;border-color:#6366f1}}
.chart-card.drop-above{{box-shadow:inset 0 4px 0 #f59e0b;border-color:#f59e0b}}
.chart-card.drop-below{{box-shadow:inset 0 -4px 0 #f59e0b;border-color:#f59e0b}}
.drop-hint{{position:fixed;background:#1e293b;color:#e2e8f0;font-size:11px;padding:4px 10px;border-radius:6px;border:1px solid #6366f1;pointer-events:none;z-index:9999;white-space:nowrap;display:none}}
.chart-card.minimized .chart-desc,
.chart-card.minimized .chart-body{{display:none}}
.chart-card.minimized{{border-radius:10px}}
.chart-header{{padding:12px 16px 0;display:flex;align-items:center;gap:6px}}
.drag-handle{{color:#475569;cursor:grab;font-size:16px;padding:0 2px;user-select:none;line-height:1;flex-shrink:0}}
.drag-handle:hover{{color:#94a3b8}}
.drag-handle:active{{cursor:grabbing}}
.chart-title{{font-size:14px;font-weight:600;color:#f1f5f9;flex:1;cursor:default;border-radius:4px;padding:1px 4px;outline:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.chart-title[contenteditable=true]{{cursor:text;background:#0f172a;border:1px solid #6366f1;white-space:normal;overflow:visible}}
.chart-desc{{font-size:11px;color:#64748b;padding:2px 16px 8px;font-style:italic}}
.chart-body{{padding:4px 8px 8px}}
.chart-actions{{display:flex;gap:4px;flex-shrink:0}}
.btn-icon{{background:transparent;border:1px solid #334155;border-radius:6px;color:#94a3b8;cursor:pointer;font-size:11px;padding:3px 8px;transition:all .15s}}
.btn-icon:hover{{border-color:#6366f1;color:#e2e8f0}}
.btn-minimize{{font-size:14px;font-weight:700;padding:1px 7px;line-height:1.2}}
.btn-width{{font-size:12px;padding:2px 6px}}
/* ── Modal */
.modal-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:1000;align-items:center;justify-content:center}}
.modal-overlay.open{{display:flex}}
.modal{{background:#1e293b;border:1px solid #334155;border-radius:12px;width:600px;max-width:95vw;max-height:85vh;display:flex;flex-direction:column}}
.modal-head{{padding:16px 20px;border-bottom:1px solid #334155;display:flex;align-items:center}}
.modal-head h2{{font-size:15px;font-weight:600;flex:1}}
.modal-close{{background:transparent;border:none;color:#94a3b8;font-size:18px;cursor:pointer;padding:0 4px;line-height:1}}
.modal-close:hover{{color:#e2e8f0}}
.modal-body{{padding:16px 20px;overflow-y:auto;flex:1}}
.modal-foot{{padding:12px 20px;border-top:1px solid #334155;display:flex;justify-content:flex-end;gap:8px}}
/* ── Form elements */
.form-row{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:10px}}
.form-group{{display:flex;flex-direction:column;gap:4px}}
.form-group label{{font-size:11px;color:#94a3b8;font-weight:500}}
.form-group select,.form-group input[type=text],.form-group input[type=password],.form-group input[type=url],.form-group textarea{{background:#0f172a;border:1px solid #334155;border-radius:6px;color:#e2e8f0;padding:6px 8px;font-size:13px;width:100%;box-sizing:border-box}}
.form-group select:focus,.form-group input:focus,.form-group textarea:focus{{outline:none;border-color:#6366f1}}
.gjql-status{{display:none;margin-top:10px;padding:10px 12px;border-radius:6px;font-size:12px;border:1px solid;white-space:pre-wrap;word-break:break-word}}
.gjql-status.success{{background:#052e16;border-color:#16a34a;color:#86efac}}
.gjql-status.error{{background:#2d1a1a;border-color:#ef4444;color:#fca5a5}}
textarea.jql-box{{font-family:monospace;font-size:12px;min-height:80px;resize:vertical;line-height:1.5}}
.jql-note{{font-size:11px;color:#64748b;margin-top:6px;font-style:italic}}
.copy-btn{{background:#334155;border:1px solid #475569;color:#e2e8f0;border-radius:6px;padding:4px 10px;font-size:11px;cursor:pointer}}
.copy-btn:hover{{background:#475569}}
/* ── Buttons */
.btn-primary{{background:#6366f1;border:none;color:#fff;border-radius:6px;padding:6px 14px;font-size:13px;cursor:pointer;font-weight:500}}
.btn-primary:hover{{background:#4f46e5}}
.btn-ghost{{background:transparent;border:1px solid #334155;color:#94a3b8;border-radius:6px;padding:6px 14px;font-size:13px;cursor:pointer}}
.btn-ghost:hover{{border-color:#6366f1;color:#e2e8f0}}
/* ── Toggle switch */
.toggle-row{{display:flex;align-items:center;gap:8px;margin-bottom:8px;font-size:12px;color:#94a3b8}}
.toggle{{position:relative;display:inline-block;width:36px;height:20px}}
.toggle input{{opacity:0;width:0;height:0}}
.toggle .slider{{position:absolute;inset:0;background:#334155;border-radius:20px;cursor:pointer;transition:.2s}}
.toggle .slider:before{{content:'';position:absolute;width:14px;height:14px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.2s}}
.toggle input:checked+.slider{{background:#6366f1}}
.toggle input:checked+.slider:before{{transform:translateX(16px)}}
/* ── Divider */
.section-title{{font-size:11px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.5px;padding:0 24px 6px;margin-top:8px}}
/* ── Custom JQL badge */
.custom-badge{{display:inline-block;font-size:10px;background:#7c3aed;color:#fff;border-radius:4px;padding:1px 6px;margin-left:6px;vertical-align:middle;font-weight:600}}
/* ── Plotly overrides */
.js-plotly-plot .plotly .modebar{{background:transparent!important}}
.js-plotly-plot .plotly .modebar-btn path{{fill:#64748b!important}}
/* ── Edit mode: hide layout controls from viewers ──────────────────────────── */
body:not(.edit-mode) .edit-only{{display:none!important}}
.btn-edit-mode{{padding:5px 14px;border-radius:6px;font-size:12px;cursor:pointer;font-weight:600;transition:all .2s;white-space:nowrap}}
.btn-edit-mode.view-mode{{background:transparent;border:1px solid #334155;color:#64748b}}
.btn-edit-mode.view-mode:hover{{border-color:#6366f1;color:#e2e8f0}}
.btn-edit-mode.edit-mode-on{{background:#059669;border:1px solid #059669;color:#fff}}
.btn-edit-mode.edit-mode-on:hover{{background:#047857;border-color:#047857}}
</style>
</head>
<body>
<div id="app">

<!-- HEADER -->
<header>
  <h1>🐛 Bug Dashboard</h1>
  <div>
    <div class="meta">Last refreshed: <span id="gen-time"></span></div>
    <div class="refresh-note">Auto-refreshes daily via scheduled task · run fetch_bugs.py to refresh manually</div>
  </div>
  <button class="btn-edit-mode view-mode" id="edit-mode-btn" onclick="toggleEditMode()" title="Unlock layout editing — drag, resize, rename charts">✏️ Edit Layout</button>
  <button class="btn-icon edit-only" onclick="openGlobalJql()" title="Edit the global JQL filter and trigger a fresh data fetch" style="font-size:12px;padding:5px 12px">⚙️ Global JQL</button>
  <button class="btn-ghost" onclick="saveConfigJson()" title="Download config.json with saved JQL overrides" style="font-size:12px;padding:5px 12px">💾 Save Config</button>
</header>

<!-- MONTH BAR -->
<div id="month-bar">
  <label>Period:</label>
  <div id="month-btns"></div>
  <div class="wd-wrap" style="gap:12px">
    <span>Group by:</span>
    <select id="group-dim-select" onchange="groupDim=this.value;renderAll()" style="background:#0f172a;border:1px solid #334155;border-radius:4px;color:#e2e8f0;padding:3px 8px;font-size:12px">
      <option value="team">Team (cf[10032])</option>
      <option value="account">Account (cf[10112])</option>
    </select>
    <span>Working days/month:</span>
    <input type="number" id="wd-input" min="1" max="31" value="20">
    <button class="btn-icon edit-only" onclick="resetLayout()" title="Reset chart order, sizes and titles to defaults" style="margin-left:8px;font-size:11px">↺ Reset layout</button>
  </div>
</div>

<!-- SUMMARY CARDS -->
<div id="summary-row">
  <div class="scard"><div class="val" id="sc-resolved">—</div><div class="lbl">Bugs Resolved</div><div class="sub">this period</div></div>
  <div class="scard"><div class="val" id="sc-open">—</div><div class="lbl">Open Bugs</div><div class="sub">right now</div></div>
  <div class="scard"><div class="val" id="sc-ct">—</div><div class="lbl">Median Cycle Time</div><div class="sub">days (created → resolved)</div></div>
  <div class="scard"><div class="val" id="sc-ratio">—</div><div class="lbl">Open/Closed Ratio</div><div class="sub">lower is better</div></div>
  <div class="scard"><div class="val" id="sc-teams">—</div><div class="lbl">Active Teams</div><div class="sub">with resolved bugs</div></div>
  <div class="scard"><div class="val" id="sc-people">—</div><div class="lbl">Contributors</div><div class="sub">unique assignees</div></div>
</div>

<div class="section-title">Charts</div>

<!-- Drop position tooltip -->
<div id="drop-hint" class="drop-hint"></div>

<!-- CHARTS GRID -->
<div id="charts-grid">

  <!-- Chart 1 -->
  <div class="chart-card" data-cid="c1" data-width="half" draggable="true">
    <div class="chart-header">
      <span class="drag-handle edit-only" title="Drag to reorder">⠿</span>
      <span class="chart-title" ondblclick="startEditTitle(this)" title="Double-click to rename">1 · Total Bugs Resolved — by Team</span>
      <div class="chart-actions">
        <button class="btn-icon btn-minimize" onclick="toggleMinimize(this)" title="Minimise">−</button>
        <button class="btn-icon btn-width edit-only" onclick="toggleWidth(this)" title="Toggle width">⊞</button>
        <button class="btn-icon edit-only" onclick="openEdit('c1')">✏️ Edit</button>
        <button class="btn-icon edit-only" onclick="openJql('c1')">🔍 JQL</button>
      </div>
    </div>
    <div class="chart-desc">Count of bugs transitioned to a resolved status during the selected month</div>
    <div class="chart-body"><div id="c1" style="height:320px"></div></div>
  </div>

  <!-- Chart 2 -->
  <div class="chart-card" data-cid="c2" data-width="half" draggable="true">
    <div class="chart-header">
      <span class="drag-handle edit-only" title="Drag to reorder">⠿</span>
      <span class="chart-title" ondblclick="startEditTitle(this)" title="Double-click to rename">2 · Bugs per Person — by Team</span>
      <div class="chart-actions">
        <button class="btn-icon btn-minimize" onclick="toggleMinimize(this)" title="Minimise">−</button>
        <button class="btn-icon btn-width edit-only" onclick="toggleWidth(this)" title="Toggle width">⊞</button>
        <button class="btn-icon edit-only" onclick="openEdit('c2')">✏️ Edit</button>
        <button class="btn-icon edit-only" onclick="openJql('c2')">🔍 JQL</button>
      </div>
    </div>
    <div class="chart-desc">Resolved bugs ÷ unique assignees per team (workload distribution)</div>
    <div class="chart-body"><div id="c2" style="height:320px"></div></div>
  </div>

  <!-- Chart 3 -->
  <div class="chart-card" data-cid="c3" data-width="half" draggable="true">
    <div class="chart-header">
      <span class="drag-handle edit-only" title="Drag to reorder">⠿</span>
      <span class="chart-title" ondblclick="startEditTitle(this)" title="Double-click to rename">3 · Avg Cycle Time — by Team</span>
      <div class="chart-actions">
        <button class="btn-icon btn-minimize" onclick="toggleMinimize(this)" title="Minimise">−</button>
        <button class="btn-icon btn-width edit-only" onclick="toggleWidth(this)" title="Toggle width">⊞</button>
        <button class="btn-icon edit-only" onclick="openEdit('c3')">✏️ Edit</button>
        <button class="btn-icon edit-only" onclick="openJql('c3')">🔍 JQL</button>
      </div>
    </div>
    <div class="chart-desc">Median days from ticket created (or first "In Progress") to resolved</div>
    <div class="chart-body"><div id="c3" style="height:320px"></div></div>
  </div>

  <!-- Chart 4 -->
  <div class="chart-card" data-cid="c4" data-width="half" draggable="true">
    <div class="chart-header">
      <span class="drag-handle edit-only" title="Drag to reorder">⠿</span>
      <span class="chart-title" ondblclick="startEditTitle(this)" title="Double-click to rename">4 · Est. Working Days per Bug</span>
      <div class="chart-actions">
        <button class="btn-icon btn-minimize" onclick="toggleMinimize(this)" title="Minimise">−</button>
        <button class="btn-icon btn-width edit-only" onclick="toggleWidth(this)" title="Toggle width">⊞</button>
        <button class="btn-icon edit-only" onclick="openEdit('c4')">✏️ Edit</button>
        <button class="btn-icon edit-only" onclick="openJql('c4')">🔍 JQL</button>
      </div>
    </div>
    <div class="chart-desc">Working days / (bugs resolved ÷ assignees) — how many days each person effectively spends per bug</div>
    <div class="chart-body"><div id="c4" style="height:320px"></div></div>
  </div>

  <!-- Chart 5 -->
  <div class="chart-card" data-cid="c5" data-width="full" draggable="true">
    <div class="chart-header">
      <span class="drag-handle edit-only" title="Drag to reorder">⠿</span>
      <span class="chart-title" ondblclick="startEditTitle(this)" title="Double-click to rename">5 · Open vs Closed — by Team</span>
      <div class="chart-actions">
        <button class="btn-icon btn-minimize" onclick="toggleMinimize(this)" title="Minimise">−</button>
        <button class="btn-icon btn-width edit-only" onclick="toggleWidth(this)" title="Toggle width">⊟</button>
        <button class="btn-icon edit-only" onclick="openEdit('c5')">✏️ Edit</button>
        <button class="btn-icon edit-only" onclick="openJql('c5')">🔍 JQL</button>
      </div>
    </div>
    <div class="chart-desc">← Closed this month &nbsp;|&nbsp; Open now → · Sorted by net balance (teams most behind on the left)</div>
    <div class="chart-body"><div id="c5" style="height:360px"></div></div>
  </div>

  <!-- Chart 6 -->
  <div class="chart-card" data-cid="c6" data-width="full" draggable="true">
    <div class="chart-header">
      <span class="drag-handle edit-only" title="Drag to reorder">⠿</span>
      <span class="chart-title" ondblclick="startEditTitle(this)" title="Double-click to rename">6 · Bug Backlog Trend — Last 6 Months</span>
      <div class="chart-actions">
        <button class="btn-icon btn-minimize" onclick="toggleMinimize(this)" title="Minimise">−</button>
        <button class="btn-icon btn-width edit-only" onclick="toggleWidth(this)" title="Toggle width">⊟</button>
        <button class="btn-icon edit-only" onclick="openEdit('c6')">✏️ Edit</button>
        <button class="btn-icon edit-only" onclick="openJql('c6')">🔍 JQL</button>
      </div>
    </div>
    <div class="chart-desc">Total open bugs over time — growing or shrinking? (daily / weekly / monthly aggregation)</div>
    <div class="chart-body"><div id="c6" style="height:340px"></div></div>
  </div>

  <!-- Chart 9 -->
  <div class="chart-card" data-cid="c9" data-width="full" draggable="true">
    <div class="chart-header">
      <span class="drag-handle edit-only" title="Drag to reorder">⠿</span>
      <span class="chart-title" ondblclick="startEditTitle(this)" title="Double-click to rename">9 · Bug Backlog per Customer — Last 6 Months</span>
      <div class="chart-actions">
        <button class="btn-icon btn-minimize" onclick="toggleMinimize(this)" title="Minimise">−</button>
        <button class="btn-icon btn-width edit-only" onclick="toggleWidth(this)" title="Toggle width">⊟</button>
        <button class="btn-icon edit-only" onclick="openEdit('c9')">✏️ Edit</button>
        <button class="btn-icon edit-only" onclick="openJql('c9')">🔍 JQL</button>
      </div>
    </div>
    <div class="chart-desc">Open bugs ÷ unique customers with open bugs — normalised backlog trend. A flat or falling line means you're keeping up even as the customer base grows.</div>
    <div class="chart-body"><div id="c9" style="height:340px"></div></div>
  </div>

  <!-- Chart 7 -->
  <div class="chart-card" data-cid="c7" data-width="full" draggable="true">
    <div class="chart-header">
      <span class="drag-handle edit-only" title="Drag to reorder">⠿</span>
      <span class="chart-title" ondblclick="startEditTitle(this)" title="Double-click to rename">7 · Cycle Time by Team &amp; Priority</span>
      <div class="chart-actions">
        <button class="btn-icon btn-minimize" onclick="toggleMinimize(this)" title="Minimise">−</button>
        <button class="btn-icon btn-width edit-only" onclick="toggleWidth(this)" title="Toggle width">⊟</button>
        <button class="btn-icon edit-only" onclick="openEdit('c7')">✏️ Edit</button>
        <button class="btn-icon edit-only" onclick="openJql('c7')">🔍 JQL</button>
      </div>
    </div>
    <div class="chart-desc">Median days to resolve — grouped by team, each bar colour = priority. Reveals which teams struggle with which severity levels.</div>
    <div class="chart-body"><div id="c7" style="height:380px"></div></div>
  </div>

</div>
</div><!-- /app -->

<!-- EDIT MODAL -->
<div class="modal-overlay" id="edit-overlay">
  <div class="modal">
    <div class="modal-head">
      <h2 id="edit-title">Edit Chart</h2>
      <button class="modal-close" onclick="closeModals()">×</button>
    </div>
    <div class="modal-body" id="edit-body"></div>
    <div class="modal-foot">
      <button class="btn-ghost" onclick="closeModals()">Cancel</button>
      <button class="btn-primary" onclick="applyEdit()">Apply</button>
    </div>
  </div>
</div>

<!-- JQL MODAL -->
<div class="modal-overlay" id="jql-overlay">
  <div class="modal">
    <div class="modal-head">
      <h2 id="jql-title">Query</h2>
      <button class="modal-close" onclick="closeModals()">×</button>
    </div>
    <div class="modal-body">
      <p style="font-size:12px;color:#94a3b8;margin-bottom:10px">
        The JQL below represents what data is shown. You can edit it freely —
        copy &amp; paste into Jira to run it directly, or adjust the filter and click
        <strong>Apply Filter</strong> to update the chart from pre-fetched data.
      </p>
      <div style="display:flex;justify-content:flex-end;margin-bottom:6px">
        <button class="copy-btn" onclick="copyJql()">📋 Copy</button>
      </div>
      <textarea class="jql-box" id="jql-text" style="width:100%;min-height:100px"></textarea>
      <p class="jql-note" id="jql-note"></p>
      <div id="jql-filter-wrap" style="margin-top:12px;display:none">
        <div class="form-row" id="jql-filter-form"></div>
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn-ghost" onclick="closeModals()">Close</button>
      <button class="btn-ghost" id="jql-reset-btn" onclick="resetChartData(activeJql)" style="display:none;color:#f87171;border-color:#f87171">↩ Reset to Default</button>
      <button class="btn-primary" id="jql-apply-btn" onclick="applyJqlFilter()" style="display:none">Apply &amp; Re-fetch</button>
    </div>
  </div>
</div>

<!-- GLOBAL JQL MODAL -->
<div class="modal-overlay" id="gjql-overlay">
  <div class="modal" style="width:620px">
    <div class="modal-head">
      <h2>⚙️ Global JQL Filter</h2>
      <button class="modal-close" onclick="closeModals()">×</button>
    </div>
    <div class="modal-body">
      <p style="font-size:12px;color:#94a3b8;margin-bottom:14px;line-height:1.6">
        This JQL controls <strong>which bugs are fetched from Jira for all charts</strong>.
        Saving triggers the GitHub Actions workflow to regenerate the dashboard with fresh data.
        <br><em>Reload this page after ~2 minutes to see updated data.</em>
      </p>
      <div class="form-group" style="margin-bottom:12px">
        <label>Bug JQL Filter</label>
        <textarea class="jql-box" id="gjql-text" rows="4" style="width:100%;min-height:90px"></textarea>
        <span class="jql-note">Standard Jira JQL — this becomes the base query that all charts build on top of.</span>
      </div>
      <div class="form-group" style="margin-bottom:12px">
        <label>GitHub Personal Access Token &nbsp;<span style="color:#64748b;font-size:11px;font-weight:400">needs <code style="background:#0f172a;padding:1px 4px;border-radius:3px">workflow</code> scope — stored only in this browser</span></label>
        <input type="password" id="gjql-token" placeholder="ghp_xxxxxxxxxxxxxxxxxxxx" autocomplete="off">
      </div>
      <div class="form-group" style="margin-bottom:4px">
        <label>GitHub Repo &nbsp;<span style="color:#64748b;font-size:11px;font-weight:400">e.g. omerniddam/bug-dashboard</span></label>
        <input type="text" id="gjql-repo" placeholder="owner/repo-name">
      </div>
      <div id="gjql-status" class="gjql-status"></div>
    </div>
    <div class="modal-foot">
      <button class="btn-ghost" onclick="closeModals()">Cancel</button>
      <button class="btn-primary" id="gjql-apply-btn" onclick="applyGlobalJql()">🔄 Trigger Refresh</button>
    </div>
  </div>
</div>

<script>
// ─── EMBEDDED DATA ────────────────────────────────────────────────────────────
const ALL_BUGS      = {bugs_json};
const TIMELINE      = {timeline_json};
const NORM_TIMELINE = {norm_timeline_json};
const DASH_CFG      = {cfg_json};

// ─── CHART CONFIGS (editable) ─────────────────────────────────────────────────
// Stores user-edited JQL text per chart — persists via localStorage
const SAVED_JQLS = {{}};

// Per-chart overridden datasets fetched via custom JQL (null = use default ALL_BUGS)
const CHART_BUGS = {{c1:null, c2:null, c3:null, c4:null, c5:null, c6:null, c7:null, c9:null}};

const CONFIGS = {{
  c1: {{ type:'bar', horizontal:false, sortBy:'value', colorScheme:'indigo' }},
  c2: {{ type:'bar', horizontal:false, sortBy:'value', colorScheme:'teal'   }},
  c3: {{ type:'bar', metric:'created', sortBy:'value', colorScheme:'violet' }},
  c4: {{ type:'bar', groupBy:'priority', colorScheme:'amber' }},
  c5: {{ type:'bar', stacked:false, colorScheme:'traffic' }},
  c6: {{ type:'line', aggregation:'weekly', colorScheme:'indigo' }},
  c7: {{ metric:'created', sortBy:'alpha', colorScheme:'plotly' }},
  c9: {{ type:'line', aggregation:'weekly', colorScheme:'teal' }},
}};

// ─── COLOUR PALETTES ──────────────────────────────────────────────────────────
const PALETTES = {{
  indigo: ['#6366f1','#818cf8','#a5b4fc','#c7d2fe','#4f46e5','#4338ca'],
  teal:   ['#14b8a6','#2dd4bf','#5eead4','#99f6e4','#0d9488','#0f766e'],
  violet: ['#8b5cf6','#a78bfa','#c4b5fd','#ddd6fe','#7c3aed','#6d28d9'],
  amber:  ['#f59e0b','#fbbf24','#fcd34d','#fde68a','#d97706','#b45309'],
  traffic:['#22c55e','#ef4444'],  // open=red, closed=green
  plotly: ['#636efa','#ef553b','#00cc96','#ab63fa','#ffa15a','#19d3f3'],
}};

// ─── TEAM COLOURS (consistent across all charts) ──────────────────────────────
const TEAM_COLORS = {{
  'Scanner G':       '#6366f1',
  'Scanner S':       '#f59e0b',
  'Experience Core': '#14b8a6',
  'Experience Edge': '#ec4899',
  'Security Core':   '#8b5cf6',
  'Research':        '#06b6d4',
  'Runtime':         '#f97316',
  'Agentic Pentest': '#22d3ee',
  'CSPM':            '#84cc16',
  'DevOps':          '#3b82f6',
  'AI Vibesec':      '#a855f7',
  'CFG':             '#e11d48',
  'Unknown':         '#475569',
}};
// Fallback palette for any team not in the map
const _FALLBACK_COLORS = ['#64748b','#9ca3af','#6b7280','#4b5563','#374151'];
function teamColor(team) {{
  if (TEAM_COLORS[team]) return TEAM_COLORS[team];
  // deterministic fallback based on name hash
  let h = 0; for (let i=0; i<team.length; i++) h = (h*31 + team.charCodeAt(i)) & 0xffffffff;
  const extra = ['#e879f9','#fb7185','#34d399','#fbbf24','#60a5fa','#a78bfa','#f472b6','#4ade80'];
  return extra[Math.abs(h) % extra.length];
}}
function teamColors(teams) {{ return teams.map(t => teamColor(t)); }}

// Priority ordering
const PRIORITY_ORDER = ['Critical','Highest','High','Medium','Low','Lowest','None'];

// ─── JIRA LIVE FETCH (direct browser → Jira REST API) ────────────────────────
function extractField(raw) {{
  if (!raw) return 'Unknown';
  if (Array.isArray(raw) && raw.length) return typeof raw[0]==='string' ? raw[0] : (raw[0].value||raw[0].name||raw[0].displayName||'Unknown');
  if (typeof raw === 'object') return raw.value||raw.name||raw.displayName||'Unknown';
  if (typeof raw === 'string') return raw;
  return 'Unknown';
}}

function processRawIssue(issue) {{
  const f = issue.fields || {{}};
  const status = (f.status||{{}}).name || 'Unknown';
  const normDt = s => s ? s.replace(/([+-])(\d{{2}})(\d{{2}})$/, '$1$2:$3').replace('Z','+00:00') : null;
  const created = normDt(f.created || null);
  const resolved = normDt(f.resolutiondate || null);
  let ctCreated = null;
  if (created && resolved) {{
    const diff = (new Date(resolved) - new Date(created)) / 86400000;
    if (diff > 0) ctCreated = Math.round(diff*100)/100;
  }}
  const resolvedSet = new Set(DASH_CFG.resolved_statuses);
  return {{
    key: issue.key, summary: f.summary||'', status,
    assignee: (f.assignee||{{}}).displayName||'Unassigned',
    priority: (f.priority||{{}}).name||'None',
    team:    extractField(f.customfield_10032),
    account: extractField(f.customfield_10112),
    created, resolved,
    resolved_from_changelog: null, first_in_progress: null,
    cycle_time_created: ctCreated, cycle_time_in_progress: null,
    is_resolved: resolvedSet.has(status), labels: f.labels||[]
  }};
}}

// Returns true when the dashboard is being served via the local proxy server
function isLocalServer() {{
  return ['localhost','127.0.0.1'].includes(window.location.hostname);
}}

async function fetchFromJira(jql) {{
  if (!isLocalServer()) {{
    throw new Error(
      'CORS error: direct browser→Jira calls are blocked when opening the file directly.\\n\\n' +
      'Fix: run the dashboard through the built-in proxy server:\\n\\n' +
      '  python3 fetch_bugs.py --serve\\n\\n' +
      'Then open  http://localhost:8080  in your browser.'
    );
  }}

  // Use the local proxy: GET /jira?jql=...&startAt=...
  let all = [], startAt = 0;
  for (let page = 0; page < 200; page++) {{
    const url = `/jira?jql=${{encodeURIComponent(jql)}}&startAt=${{startAt}}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`Proxy error ${{res.status}}: ${{await res.text()}}`);
    const data = await res.json();
    if (data.error) throw new Error(`Jira error: ${{data.error}}`);
    const batch = data.issues || data.values || [];
    all.push(...batch.map(processRawIssue));
    startAt += batch.length;
    // Stop when no more results, or when Jira total is reached and page wasn't full
    if (!batch.length || (startAt >= (data.total||0) && batch.length < 100)) break;
  }}
  return all;
}}

function buildTimelineFromBugs(bugs, days=186) {{
  const today = new Date(); today.setHours(0,0,0,0);
  const start = new Date(today); start.setDate(start.getDate()-days);
  const delta = {{}};
  const addDelta = (d, v) => {{ const k=d.toISOString().split('T')[0]; delta[k]=(delta[k]||0)+v; }};
  bugs.forEach(b => {{
    if (!b.created) return;
    const cd = parseDate(b.created); if (!cd) return; cd.setHours(0,0,0,0);
    const rd = b.resolved ? parseDate(b.resolved) : null; if(rd) rd.setHours(0,0,0,0);
    const s = cd < start ? start : cd;
    const e = rd || new Date(today.getTime()+86400000);
    addDelta(s, 1);
    if (e <= today) addDelta(e, -1);
  }});
  let run=0; const tl=[];
  for (let i=0; i<=days; i++) {{
    const d = new Date(start); d.setDate(d.getDate()+i);
    const k = d.toISOString().split('T')[0];
    run = Math.max(0, run+(delta[k]||0));
    tl.push({{date:k, open:run}});
  }}
  return tl;
}}

// ─── STATE ────────────────────────────────────────────────────────────────────
let selYear, selMonth;
let groupDim = 'team';   // 'team' or 'account'
let activeEdit = null;
let activeJql  = null;

// Custom client-side filters per chart (team/priority/status arrays)
const CUSTOM_FILTERS = {{ c1:{{}}, c2:{{}}, c3:{{}}, c4:{{}}, c5:{{}}, c6:{{}}, c7:{{}}, c9:{{}} }};

// ─── DATE HELPERS ─────────────────────────────────────────────────────────────
function monthStart(y,m){{ return new Date(y,m-1,1); }}
function monthEnd(y,m){{   return new Date(y,m,0,23,59,59); }}
function isoDate(d){{       return d.toISOString().split('T')[0]; }}

// Normalize +0300 → +03:00 before parsing (JS Date rejects +HHMM without colon in strict mode)
function parseDate(s) {{
  if (!s) return null;
  const norm = s.replace(/([+-])(\d{{2}})(\d{{2}})$/, '$1$2:$3').replace('Z', '+00:00');
  const d = new Date(norm);
  return isNaN(d.getTime()) ? null : d;
}}

function resolvedInMonth(bug, y, m){{
  if (!bug.resolved) return false;
  const rd = parseDate(bug.resolved);
  if (!rd) return false;
  return rd >= monthStart(y,m) && rd <= monthEnd(y,m);
}}

function isOpen(bug){{ return !bug.is_resolved; }}

// ─── METRICS COMPUTATION ──────────────────────────────────────────────────────
function getMonthBugs(y, m, bugsOverride=null){{
  return (bugsOverride||ALL_BUGS).filter(b => resolvedInMonth(b, y, m));
}}

function median(arr){{
  if (!arr.length) return null;
  const s = [...arr].sort((a,b)=>a-b);
  const mid = Math.floor(s.length/2);
  return s.length % 2 ? s[mid] : (s[mid-1]+s[mid])/2;
}}

function mean(arr){{
  if (!arr.length) return null;
  return arr.reduce((s,v)=>s+v, 0) / arr.length;
}}

function stdDev(arr){{
  if (arr.length < 2) return 0;
  const m = mean(arr);
  return Math.sqrt(arr.reduce((s,v)=>s+(v-m)**2, 0) / arr.length);
}}

function groupBy(arr, key){{
  const g = {{}};
  arr.forEach(x => {{
    const k = x[key] || 'Unknown';
    (g[k] = g[k]||[]).push(x);
  }});
  return g;
}}

function sortPairs(keys, vals, sortBy){{
  const pairs = keys.map((k,i) => [k, vals[i]]).filter(([,v]) => v !== null && v !== undefined);
  if (sortBy === 'value')   pairs.sort((a,b) => b[1]-a[1]);
  if (sortBy === 'asc')     pairs.sort((a,b) => a[1]-b[1]);
  if (sortBy === 'alpha')   pairs.sort((a,b) => a[0].localeCompare(b[0]));
  return [pairs.map(p=>p[0]), pairs.map(p=>p[1])];
}}

// ─── NO-DATA HELPER ───────────────────────────────────────────────────────────
function emptyLayout(msg) {{
  return darkLayout({{
    annotations:[{{
      text: msg || 'No data for this period',
      xref:'paper', yref:'paper', x:0.5, y:0.5,
      showarrow:false, font:{{size:16, color:'#64748b'}},
      xanchor:'center', yanchor:'middle'
    }}],
    xaxis:{{visible:false}}, yaxis:{{visible:false}}
  }});
}}

// ─── CHART 1: Resolved per team ───────────────────────────────────────────────
function renderC1(){{
  const cfg  = CONFIGS.c1;
  const cf   = CUSTOM_FILTERS.c1;
  let bugs   = getMonthBugs(selYear, selMonth, CHART_BUGS.c1);
  if (cf.teams && cf.teams.length) bugs = bugs.filter(b => cf.teams.includes(b.team));

  if (!bugs.length) {{
    Plotly.react('c1', [], emptyLayout('No bugs resolved this month'), plotlyConfig());
    return;
  }}

  const byTeam = groupBy(bugs, groupDim);
  let keys = Object.keys(byTeam), vals = keys.map(k => byTeam[k].length);
  [keys, vals] = sortPairs(keys, vals, cfg.sortBy);
  const colors = (PALETTES[cfg.colorScheme]||PALETTES.indigo);

  const barColors = groupDim === 'team' ? teamColors(keys) : (PALETTES[cfg.colorScheme]||PALETTES.indigo);
  const maxVal = vals.length ? Math.max(...vals) : 1;
  const trace = cfg.type === 'pie'
    ? [{{ type:'pie', labels:keys, values:vals, marker:{{ colors: barColors }}, textinfo:'label+value', hovertemplate:'%{{label}}: %{{value}}<extra></extra>' }}]
    : [{{ type:'bar', x: cfg.horizontal?vals:keys, y: cfg.horizontal?keys:vals,
         orientation: cfg.horizontal?'h':'v',
         marker:{{ color: barColors }},
         text: vals.map(String), textposition:'outside', cliponaxis: false,
         hovertemplate:'%{{x}}: %{{y}}<extra></extra>' }}];

  Plotly.react('c1', trace, darkLayout({{
    title:'',
    xaxis:{{ title: cfg.horizontal?'Bugs Resolved':(groupDim==='account'?'Account':'Team'),
             color:'#94a3b8', gridcolor:'#1e293b',
             tickangle: cfg.horizontal ? 0 : -35, automargin: true }},
    yaxis:{{ title: cfg.horizontal?(groupDim==='account'?'Account':'Team'):'Bugs Resolved',
             color:'#94a3b8', gridcolor:'#334155', automargin: true,
             range: cfg.horizontal ? undefined : [0, maxVal * 1.22] }},
    showlegend: cfg.type==='pie',
    margin: {{ t:30, b: cfg.horizontal?40:110, l: cfg.horizontal?140:60, r:20 }},
  }}), plotlyConfig());
}}

// ─── CHART 2: Bugs per person ─────────────────────────────────────────────────
function renderC2(){{
  const cfg = CONFIGS.c2;
  const cf  = CUSTOM_FILTERS.c2;
  let bugs  = getMonthBugs(selYear, selMonth, CHART_BUGS.c2);
  if (cf.teams && cf.teams.length) bugs = bugs.filter(b => cf.teams.includes(b.team));
  if (!bugs.length) {{ Plotly.react('c2', [], emptyLayout('No bugs resolved this month'), plotlyConfig()); return; }}

  const byTeam = groupBy(bugs, groupDim);
  const keys=[], vals=[], texts=[];
  Object.entries(byTeam).forEach(([team, tbugs])=>{{
    const assignees = new Set(tbugs.map(b=>b.assignee).filter(a=>a&&a!=='Unassigned'));
    const n = assignees.size || 1;
    keys.push(team); vals.push(+(tbugs.length/n).toFixed(2));
    texts.push(`${{tbugs.length}} bugs / ${{n}} person${{n>1?'s':''}}`);
  }});
  const [sk,sv] = sortPairs(keys, vals, cfg.sortBy);
  const c2Colors = groupDim === 'team' ? teamColors(sk) : (PALETTES[cfg.colorScheme]||PALETTES.teal);
  const c2Max = sv.length ? Math.max(...sv) : 1;

  const trace = cfg.type==='pie'
    ? [{{type:'pie',labels:sk,values:sv,marker:{{colors:c2Colors}},textinfo:'label+value'}}]
    : [{{type:'bar', x:cfg.horizontal?sv:sk, y:cfg.horizontal?sk:sv,
         orientation:cfg.horizontal?'h':'v',
         marker:{{color:c2Colors}},
         text:sv.map(v=>v.toFixed(2)), textposition:'outside', cliponaxis:false,
         hovertemplate:'%{{x}}: %{{y:.2f}} bugs/person<extra></extra>'}}];

  Plotly.react('c2', trace, darkLayout({{
    title:'',
    xaxis:{{ title: cfg.horizontal?'Bugs / Person':'Team', color:'#94a3b8', gridcolor:'#1e293b',
             tickangle: cfg.horizontal ? 0 : -35, automargin: true }},
    yaxis:{{ title: cfg.horizontal?'Team':'Bugs / Person', color:'#94a3b8', gridcolor:'#334155',
             automargin: true, range: cfg.horizontal ? undefined : [0, c2Max * 1.22] }},
    margin: {{ t:30, b: cfg.horizontal?40:110, l: cfg.horizontal?140:60, r:20 }},
  }}), plotlyConfig());
}}

// ─── CHART 3: Cycle time ──────────────────────────────────────────────────────
function renderC3(){{
  const cfg   = CONFIGS.c3;
  const cf    = CUSTOM_FILTERS.c3;
  const field = cfg.metric === 'in_progress' ? 'cycle_time_in_progress' : 'cycle_time_created';

  // Fetch bugs for the selected month only (QA excluded at data-processing level)
  let bugs = getMonthBugs(selYear, selMonth, CHART_BUGS.c3)
               .filter(b => b[field] !== null && b[field] !== undefined);
  if (cf.teams && cf.teams.length) bugs = bugs.filter(b => cf.teams.includes(b.team));
  if (!bugs.length) {{ Plotly.react('c3', [], emptyLayout('No cycle time data for this month'), plotlyConfig()); return; }}

  // Aggregate per group: mean, stddev, median
  const byGroup = groupBy(bugs, groupDim);
  const rows = [];
  Object.entries(byGroup).forEach(([grp, gbugs]) => {{
    const vals = gbugs.map(b => b[field]).filter(v => v != null);
    if (!vals.length) return;
    const m  = +mean(vals).toFixed(1);
    const sd = +stdDev(vals).toFixed(1);
    const md = +median(vals).toFixed(1);
    rows.push({{ grp, m, sd, md, n: vals.length }});
  }});

  // Sort by mean descending
  rows.sort((a,b) => b.m - a.m);

  const grps  = rows.map(r => r.grp);
  const means = rows.map(r => r.m);
  const sds   = rows.map(r => r.sd);
  const meds  = rows.map(r => r.md);
  const ns    = rows.map(r => r.n);

  const barColors = groupDim === 'team' ? teamColors(grps) : (PALETTES[cfg.colorScheme]||PALETTES.violet).map((_,i,a)=>a[i%a.length]);
  const c3Max = means.length ? Math.max(...means.map((m,i) => m + (sds[i]||0))) : 1;

  const metricLabel = cfg.metric==='in_progress' ? 'In Progress → Resolved (days)' : 'Created → Resolved (days)';

  const traces = [
    // Bars = mean ± 1 std dev
    {{
      type: 'bar',
      name: 'Average',
      x: grps,
      y: means,
      marker: {{ color: barColors, opacity: 0.85 }},
      cliponaxis: false,
      error_y: {{
        type: 'data',
        array: sds,
        arrayminus: sds.map((sd,i) => Math.min(sd, means[i])),  // floor at 0
        visible: true,
        color: '#94a3b8',
        thickness: 1.5,
        width: 6,
      }},
      text: means.map((m,i) => `${{m}}d`),
      textposition: 'outside',
      hovertemplate: '<b>%{{x}}</b><br>Avg: %{{y:.1f}} days<br>σ: ' +
                     sds.map(s=>s.toFixed(1)).join('|') +  // overridden per-point below
                     '<extra></extra>',
      customdata: rows.map(r => [r.sd, r.md, r.n]),
      hovertemplate: '<b>%{{x}}</b><br>' +
                     'Average: %{{y:.1f}} days<br>' +
                     'Std Dev: ±%{{customdata[0]:.1f}} days<br>' +
                     'Median: %{{customdata[1]:.1f}} days<br>' +
                     'Count: %{{customdata[2]}} bugs<extra></extra>',
    }},
    // Scatter = median (shown as a diamond marker on each bar)
    {{
      type: 'scatter',
      mode: 'markers',
      name: 'Median',
      x: grps,
      y: meds,
      marker: {{
        symbol: 'diamond',
        size: 10,
        color: '#f0f9ff',
        line: {{ color: '#0ea5e9', width: 2 }},
      }},
      hovertemplate: '<b>%{{x}}</b><br>Median: %{{y:.1f}} days<extra></extra>',
    }},
  ];

  Plotly.react('c3', traces, darkLayout({{
    barmode: 'overlay',
    legend: {{ font:{{ color:'#94a3b8' }}, orientation:'h', y:1.06 }},
    xaxis: {{ title: groupDim==='account'?'Account':'Team', color:'#94a3b8', gridcolor:'#1e293b',
              tickangle: -35, automargin: true }},
    yaxis: {{ title: metricLabel, color:'#94a3b8', gridcolor:'#334155',
              range: [0, c3Max * 1.25] }},
    margin: {{ t:50, b:120, l:60, r:20 }},
    annotations: [{{
      text: 'Error bars = ±1 standard deviation  ◆ = median',
      xref:'paper', yref:'paper', x:0, y:-0.18,
      showarrow:false, font:{{size:11, color:'#64748b'}}, xanchor:'left',
    }}],
  }}), plotlyConfig());
}}

// ─── CHART 4: Est. time per bug ───────────────────────────────────────────────
function renderC4(){{
  const cfg = CONFIGS.c4;
  const cf  = CUSTOM_FILTERS.c4;
  const wd  = parseInt(document.getElementById('wd-input').value)||20;
  let bugs  = getMonthBugs(selYear, selMonth, CHART_BUGS.c4);
  if (cf.teams && cf.teams.length) bugs = bugs.filter(b => cf.teams.includes(b.team));
  if (!bugs.length) {{ Plotly.react('c4', [], emptyLayout('No bugs resolved this month'), plotlyConfig()); return; }}

  const colors = PALETTES[cfg.colorScheme]||PALETTES.amber;
  let traces;

  if (cfg.groupBy === 'priority') {{
    const byPri = groupBy(bugs, 'priority');
    const priKeys = PRIORITY_ORDER.filter(p => byPri[p]);
    const vals = priKeys.map(p => {{
      const pb = byPri[p];
      const assignees = new Set(pb.map(b=>b.assignee).filter(a=>a&&a!=='Unassigned'));
      const n = assignees.size||1;
      const bpp = pb.length/n;
      return bpp > 0 ? +(wd/bpp).toFixed(2) : null;
    }}).filter(v=>v!==null);
    const filteredKeys = priKeys.filter((_,i)=> {{
      const pb=byPri[priKeys[i]];const assignees=new Set(pb.map(b=>b.assignee).filter(a=>a&&a!=='Unassigned'));const n=assignees.size||1;return pb.length/n>0;
    }});
    traces = [{{type:'bar', x:filteredKeys, y:vals,
                marker:{{color:colors}},
                text:vals.map(v=>v+'d'), textposition:'outside',
                hovertemplate:'%{{x}}: %{{y:.2f}} days/bug<extra></extra>'}}];
    Plotly.react('c4', traces, darkLayout({{
      title:'',
      xaxis:{{ title:'Priority', color:'#94a3b8', automargin: true }},
      yaxis:{{ title:'Est. Days per Bug', color:'#94a3b8', gridcolor:'#334155' }},
      margin:{{ t:30, b:60, l:60, r:20 }},
    }}), plotlyConfig());
  }} else {{
    // by team
    const byTeam = groupBy(bugs,groupDim);
    const keys=[], vals=[];
    Object.entries(byTeam).forEach(([team,tbugs])=>{{
      const assignees=new Set(tbugs.map(b=>b.assignee).filter(a=>a&&a!=='Unassigned'));
      const n=assignees.size||1, bpp=tbugs.length/n;
      if (bpp>0){{ keys.push(team); vals.push(+(wd/bpp).toFixed(2)); }}
    }});
    const [sk,sv]=sortPairs(keys,vals,cfg.sortBy||'value');
    const c4Colors = groupDim==='team' ? teamColors(sk) : (PALETTES[cfg.colorScheme]||PALETTES.amber);
    const c4Max = sv.length ? Math.max(...sv) : 1;
    traces = [{{type:'bar', x:sk, y:sv,
                marker:{{color:c4Colors}},
                text:sv.map(v=>v+'d'), textposition:'outside', cliponaxis:false,
                hovertemplate:'%{{x}}: %{{y:.2f}} days/bug<extra></extra>'}}];
    Plotly.react('c4', traces, darkLayout({{
      title:'',
      xaxis:{{ title:'Team', color:'#94a3b8', tickangle:-35, automargin:true }},
      yaxis:{{ title:'Est. Days per Bug', color:'#94a3b8', gridcolor:'#334155',
               range:[0, c4Max * 1.22] }},
      margin:{{ t:30, b:110, l:60, r:20 }},
    }}), plotlyConfig());
  }}
}}

// ─── CHART 5: Open vs Closed ──────────────────────────────────────────────────
function renderC5(){{
  const cfg = CONFIGS.c5;
  const cf  = CUSTOM_FILTERS.c5;
  const pool  = CHART_BUGS.c5 || ALL_BUGS;
  let allBugs = [...pool];
  let monthClosed = pool.filter(b => resolvedInMonth(b, selYear, selMonth));
  if (cf.teams && cf.teams.length) {{
    allBugs     = allBugs.filter(b => cf.teams.includes(b[groupDim]));
    monthClosed = monthClosed.filter(b => cf.teams.includes(b[groupDim]));
  }}

  // "Open" = currently unresolved; "Closed" = resolved in the selected month
  const openByTeam   = groupBy(allBugs.filter(b=>!b.is_resolved), groupDim);
  const closedByTeam = groupBy(monthClosed, groupDim);
  const teams = [...new Set([...Object.keys(openByTeam), ...Object.keys(closedByTeam)])].sort();
  const openVals   = teams.map(t => (openByTeam[t]||[]).length);
  const closedVals = teams.map(t => (closedByTeam[t]||[]).length);

  // ── Diverging horizontal bars: open → right (positive), closed → left (negative)
  // Sort by net = open − closed descending (most behind first, most ahead last)
  const rows5 = teams.map((t,i) => ({{t, o:openVals[i], c:closedVals[i], net:openVals[i]-closedVals[i]}}))
                      .sort((a,b) => b.net - a.net);
  const tSorted = rows5.map(r=>r.t);
  const oSorted = rows5.map(r=>r.o);
  const cSorted = rows5.map(r=>r.c);
  const netSorted= rows5.map(r=>r.net);
  const absMax = Math.max(...oSorted, ...cSorted, 1);

  const traces = [
    {{
      type:'bar', name:'Open (now)', orientation:'h',
      y: tSorted, x: oSorted,
      marker:{{ color:'#ef4444' }},
      text: oSorted.map(String), textposition:'outside', cliponaxis:false,
      hovertemplate:'<b>%{{y}}</b><br>Open now: %{{x}}<extra></extra>',
    }},
    {{
      type:'bar', name:'Closed (this month)', orientation:'h',
      y: tSorted, x: cSorted.map(v => -v),  // negative = goes left
      marker:{{ color:'#22c55e' }},
      text: cSorted.map(String), textposition:'outside', cliponaxis:false,
      hovertemplate:'<b>%{{y}}</b><br>Closed this month: %{{customdata}}<extra></extra>',
      customdata: cSorted,
    }},
    // Net balance annotation bar (invisible, just for hover)
    {{
      type:'scatter', mode:'markers', name:'Net balance',
      y: tSorted, x: netSorted.map(()=>0),
      marker:{{ size:0, color:'transparent' }},
      hovertemplate:'<b>%{{y}}</b><br>Net: %{{customdata[0]>0?"+"+"":""}}%{{customdata[0]}} bugs<extra></extra>',
      customdata: netSorted.map(v=>[v]),
      showlegend:false,
    }},
  ];

  Plotly.react('c5', traces, darkLayout({{
    barmode: 'overlay',
    showlegend: true,
    legend: {{ font:{{ color:'#94a3b8' }}, orientation:'h', y:1.04, x:0 }},
    xaxis: {{ title:'← Closed this month  |  Open now →',
              color:'#94a3b8', gridcolor:'#334155', zeroline:true, zerolinecolor:'#475569', zerolinewidth:2,
              tickvals: [-absMax, -Math.round(absMax/2), 0, Math.round(absMax/2), absMax],
              ticktext: [absMax, Math.round(absMax/2), '0', Math.round(absMax/2), absMax].map(String),
              range: [-(absMax*1.28), absMax*1.28] }},
    yaxis: {{ color:'#94a3b8', automargin:true, tickfont:{{size:12}} }},
    margin: {{ t:40, b:60, l:20, r:60 }},
    shapes: [{{  // zero line emphasis
      type:'line', x0:0, x1:0, y0:-0.5, y1:tSorted.length-0.5,
      yref:'y', xref:'x', line:{{color:'#475569', width:2}},
    }}],
  }}), plotlyConfig());
}}

// ─── CHART 6: Backlog trend ───────────────────────────────────────────────────
function renderC6(){{
  const cfg = CONFIGS.c6;
  let tl = CHART_BUGS.c6 ? buildTimelineFromBugs(CHART_BUGS.c6) : [...TIMELINE];

  const agg = cfg.aggregation;
  let dates, vals;

  if (agg === 'daily') {{
    dates = tl.map(p=>p.date);
    vals  = tl.map(p=>p.open);
  }} else if (agg === 'weekly') {{
    const weeks = {{}};
    tl.forEach(p=>{{
      const d=new Date(p.date), dow=d.getDay();
      const sun=new Date(d); sun.setDate(d.getDate()-dow);
      const key=isoDate(sun);
      if (!weeks[key]||p.open>weeks[key]) weeks[key]=p.open; // last value of week
    }});
    // use last day of each week instead
    const weekMap={{}};
    tl.forEach(p=>{{ const d=new Date(p.date),dow=d.getDay();const sun=new Date(d);sun.setDate(d.getDate()-dow);weekMap[isoDate(sun)]=p.open; }});
    dates=Object.keys(weekMap).sort(); vals=dates.map(k=>weekMap[k]);
  }} else {{
    // monthly
    const months={{}};
    tl.forEach(p=>{{ const key=p.date.slice(0,7); months[key]=p.open; }});
    dates=Object.keys(months).sort(); vals=dates.map(k=>months[k]);
  }}

  const color = (PALETTES[cfg.colorScheme]||PALETTES.indigo)[0];
  const trace = cfg.type==='bar'
    ? [{{type:'bar', x:dates, y:vals, marker:{{color}}, hovertemplate:'%{{x}}: %{{y}} open<extra></extra>'}}]
    : [{{type:'scatter', mode:'lines+markers', x:dates, y:vals,
         line:{{color, width:2}}, marker:{{color, size:4}},
         fill:'tozeroy', fillcolor: (color.startsWith('#') ? color + '1a' : color.replace('rgb(','rgba(').replace(')',',0.1)')),
         hovertemplate:'%{{x}}: %{{y}} open bugs<extra></extra>'}}];

  Plotly.react('c6', trace, darkLayout({{
    title:'',
    xaxis:{{title:'Date', color:'#94a3b8', gridcolor:'#334155'}},
    yaxis:{{title:'Open Bugs', color:'#94a3b8', gridcolor:'#334155'}},
  }}), plotlyConfig());
}}

// ─── CHART 9: Normalised backlog (bugs per customer) ─────────────────────────
function renderC9(){{
  const cfg = CONFIGS.c9;
  const tl  = [...NORM_TIMELINE];   // always uses pre-computed normalised data
  if (!tl.length) {{ Plotly.react('c9', [], emptyLayout('No timeline data'), plotlyConfig()); return; }}

  const agg = cfg.aggregation;
  let dates, perCust, custCounts;

  if (agg === 'daily') {{
    dates      = tl.map(p => p.date);
    perCust    = tl.map(p => p.per_customer);
    custCounts = tl.map(p => p.customers);
  }} else if (agg === 'weekly') {{
    // Use last value of each week (Sun–Sat)
    const weekMap = {{}};
    tl.forEach(p => {{
      const d = new Date(p.date), dow = d.getDay();
      const sun = new Date(d); sun.setDate(d.getDate() - dow);
      const key = isoDate(sun);
      weekMap[key] = p;  // last day of week wins
    }});
    dates      = Object.keys(weekMap).sort();
    perCust    = dates.map(k => weekMap[k].per_customer);
    custCounts = dates.map(k => weekMap[k].customers);
  }} else {{
    const monthMap = {{}};
    tl.forEach(p => {{ const key = p.date.slice(0,7); monthMap[key] = p; }});
    dates      = Object.keys(monthMap).sort();
    perCust    = dates.map(k => monthMap[k].per_customer);
    custCounts = dates.map(k => monthMap[k].customers);
  }}

  const mainColor = (PALETTES[cfg.colorScheme] || PALETTES.teal)[0];
  const custColor = '#64748b';

  const traces = [
    // Main line: bugs per customer
    cfg.type === 'bar'
      ? {{ type:'bar', name:'Bugs / Customer', x:dates, y:perCust,
           marker:{{ color:mainColor }},
           hovertemplate:'%{{x}}<br><b>%{{y:.2f}} bugs/customer</b><extra></extra>' }}
      : {{ type:'scatter', mode:'lines', name:'Bugs / Customer', x:dates, y:perCust,
           line:{{ color:mainColor, width:2.5, shape:'spline', smoothing:0.6 }},
           fill:'tozeroy', fillcolor:mainColor.replace(')',',0.12)').replace('rgb','rgba'),
           hovertemplate:'%{{x}}<br><b>%{{y:.2f}} bugs/customer</b><extra></extra>' }},
    // Secondary: unique customer count (dashed, right axis)
    {{ type:'scatter', mode:'lines', name:'Unique Customers', x:dates, y:custCounts,
       yaxis:'y2',
       line:{{ color:custColor, width:1.5, dash:'dot' }},
       hovertemplate:'%{{x}}<br>%{{y}} customers with open bugs<extra></extra>' }},
  ];

  Plotly.react('c9', traces, darkLayout({{
    showlegend: true,
    legend: {{ font:{{ color:'#94a3b8' }}, orientation:'h', y:1.06 }},
    xaxis: {{ title:'Date', color:'#94a3b8', gridcolor:'#1e293b' }},
    yaxis: {{ title:'Open Bugs per Customer', color:mainColor, gridcolor:'#334155' }},
    yaxis2: {{
      title: 'Unique Customers', overlaying:'y', side:'right',
      color: custColor, gridcolor:'transparent', showgrid:false,
      tickfont:{{ color:custColor }}, titlefont:{{ color:custColor }},
    }},
    margin: {{ t:40, b:60, l:60, r:60 }},
  }}), plotlyConfig());
}}

// ─── CHART 7: Cycle time by Team × Priority ───────────────────────────────────
function renderC7(){{
  const cfg   = CONFIGS.c7;
  const cf    = CUSTOM_FILTERS.c7;
  const field = cfg.metric === 'in_progress' ? 'cycle_time_in_progress' : 'cycle_time_created';
  let bugs    = getMonthBugs(selYear, selMonth, CHART_BUGS.c7)
                  .filter(b => b[field] !== null && b[field] !== undefined);
  if (cf.teams && cf.teams.length) bugs = bugs.filter(b => cf.teams.includes(b.team));
  if (!bugs.length) {{ Plotly.react('c7', [], emptyLayout('No cycle time data for this month'), plotlyConfig()); return; }}

  // Collect teams (sorted alphabetically or by total median)
  const byTeam = groupBy(bugs, groupDim);
  let teams = Object.keys(byTeam).sort();
  if (cfg.sortBy === 'value') {{
    teams = teams.sort((a,b) => {{
      const ma = median(byTeam[a].map(x=>x[field]).filter(v=>v!=null)) || 0;
      const mb = median(byTeam[b].map(x=>x[field]).filter(v=>v!=null)) || 0;
      return mb - ma;
    }});
  }}

  // Priority colours — fixed per priority for easy reading
  const PRI_COLORS = {{
    'Critical': '#ef4444',
    'Highest':  '#f97316',
    'High':     '#f59e0b',
    'Medium':   '#6366f1',
    'Low':      '#14b8a6',
    'Lowest':   '#64748b',
    'None':     '#334155',
  }};

  // One trace per priority
  const traces = PRIORITY_ORDER
    .filter(pri => bugs.some(b => b.priority === pri))
    .map(pri => {{
      const yVals = teams.map(team => {{
        const subset = (byTeam[team]||[]).filter(b => b.priority===pri && b[field]!=null);
        return subset.length ? +median(subset.map(b=>b[field])).toFixed(1) : null;
      }});
      return {{
        type: 'bar',
        name: pri,
        x: teams,
        y: yVals,
        marker: {{ color: PRI_COLORS[pri] || '#6366f1' }},
        text: yVals.map(v => v !== null ? v+'d' : ''),
        textposition: 'outside',
        cliponaxis: false,
        hovertemplate: '<b>%{{x}}</b><br>' + pri + ': %{{y:.1f}} days<extra></extra>',
      }};
    }});

  const c7AllVals = traces.flatMap(t => t.y.filter(v => v !== null));
  const c7Max = c7AllVals.length ? Math.max(...c7AllVals) : 1;
  const metricLabel = cfg.metric==='in_progress' ? 'In Progress → Resolved (days)' : 'Created → Resolved (days)';
  Plotly.react('c7', traces, darkLayout({{
    barmode: 'group',
    showlegend: true,
    legend: {{ font:{{ color:'#94a3b8' }}, orientation:'h', y:1.08 }},
    xaxis: {{ title: groupDim==='account'?'Account':'Team', color:'#94a3b8', gridcolor:'#1e293b',
              tickangle:-35, automargin:true }},
    yaxis: {{ title: metricLabel, color:'#94a3b8', gridcolor:'#334155',
              range:[0, c7Max * 1.22] }},
    margin: {{ t:60, b:130, l:60, r:20 }},
  }}), plotlyConfig());
}}

// ─── SUMMARY CARDS ────────────────────────────────────────────────────────────
function updateSummary(){{
  const resolved = getMonthBugs(selYear, selMonth);
  const open     = ALL_BUGS.filter(b=>!b.is_resolved);
  const cts      = resolved.map(b=>b.cycle_time_created).filter(v=>v!=null);
  const total    = ALL_BUGS.length;
  const closedN  = ALL_BUGS.filter(b=>b.is_resolved).length;

  document.getElementById('sc-resolved').textContent = resolved.length;
  document.getElementById('sc-open').textContent     = open.length;
  document.getElementById('sc-ct').textContent       = cts.length ? median(cts).toFixed(1)+'d' : '—';
  document.getElementById('sc-ratio').textContent    = closedN ? (open.length/closedN).toFixed(2) : '—';
  const teams = new Set(resolved.map(b=>b.team).filter(t=>t&&t!=='Unknown'));
  document.getElementById('sc-teams').textContent = teams.size;
  const people = new Set(resolved.map(b=>b.assignee).filter(a=>a&&a!=='Unassigned'));
  document.getElementById('sc-people').textContent = people.size;
}}

// ─── RENDER ALL ───────────────────────────────────────────────────────────────
function renderAll(){{
  updateSummary();
  renderC1(); renderC2(); renderC3(); renderC4(); renderC5(); renderC6(); renderC9(); renderC7();
}}

// ─── MONTH SELECTOR ───────────────────────────────────────────────────────────
function buildMonthBar(){{
  const now = new Date();
  const wrap = document.getElementById('month-btns');
  wrap.innerHTML = '';

  // Default to current month
  const defaultY = now.getFullYear(), defaultM = now.getMonth()+1;

  for (let i=5; i>=0; i--){{
    const d = new Date(now.getFullYear(), now.getMonth()-i, 1);
    const y = d.getFullYear(), m = d.getMonth()+1;
    const label = d.toLocaleString('en-US',{{month:'short', year:'2-digit'}});
    const btn = document.createElement('button');
    btn.className = 'm-btn' + (y===defaultY && m===defaultM ? ' active' : '');
    btn.textContent = label;
    btn.dataset.y = y; btn.dataset.m = m;
    btn.onclick = function(){{
      document.querySelectorAll('.m-btn').forEach(b=>b.classList.remove('active'));
      this.classList.add('active');
      selYear=+this.dataset.y; selMonth=+this.dataset.m;
      renderAll();
    }};
    wrap.appendChild(btn);
  }}
  selYear=defaultY; selMonth=defaultM;
}}

// ─── PLOTLY HELPERS ───────────────────────────────────────────────────────────
function darkLayout(extra){{
  return Object.assign({{
    paper_bgcolor:'transparent', plot_bgcolor:'transparent',
    font:{{color:'#e2e8f0', size:12}},
    margin:{{t:20,b:50,l:50,r:20}},
    hoverlabel:{{bgcolor:'#1e293b',bordercolor:'#334155',font:{{color:'#e2e8f0'}}}},
  }}, extra);
}}
function plotlyConfig(){{
  return {{responsive:true, displayModeBar:true, modeBarButtonsToRemove:['lasso2d','select2d'], displaylogo:false}};
}}

// ─── EDIT MODAL ───────────────────────────────────────────────────────────────
function openEdit(chartId){{
  activeEdit = chartId;
  const cfg = CONFIGS[chartId];
  const titles = {{c1:'Chart 1 · Resolved by Team', c2:'Chart 2 · Bugs per Person',
    c3:'Chart 3 · Cycle Time', c4:'Chart 4 · Est. Time per Bug',
    c5:'Chart 5 · Open vs Closed', c6:'Chart 6 · Backlog Trend',
    c7:'Chart 7 · Cycle Time by Team × Priority',
    c9:'Chart 9 · Backlog Trend per Customer'}};
  document.getElementById('edit-title').textContent = titles[chartId]||'Edit Chart';

  let html = `<div class="form-row">
    <div class="form-group"><label>Chart Type</label><select id="e-type">
      <option value="bar" ${{cfg.type==='bar'?'selected':''}}>Bar</option>
      <option value="line" ${{cfg.type==='line'?'selected':''}}>Line</option>
      <option value="pie" ${{cfg.type==='pie'?'selected':''}}>Pie</option>
      <option value="box" ${{cfg.type==='box'?'selected':''}}>Box Plot</option>
    </select></div>
    <div class="form-group"><label>Color Scheme</label><select id="e-color">
      <option value="indigo" ${{cfg.colorScheme==='indigo'?'selected':''}}>Indigo</option>
      <option value="teal"   ${{cfg.colorScheme==='teal'?'selected':''}}>Teal</option>
      <option value="violet" ${{cfg.colorScheme==='violet'?'selected':''}}>Violet</option>
      <option value="amber"  ${{cfg.colorScheme==='amber'?'selected':''}}>Amber</option>
      <option value="plotly" ${{cfg.colorScheme==='plotly'?'selected':''}}>Plotly</option>
    </select></div>
  </div>`;

  if (['c1','c2','c4'].includes(chartId)) {{
    html += `<div class="form-row">
      <div class="form-group"><label>Sort By</label><select id="e-sort">
        <option value="value" ${{cfg.sortBy==='value'?'selected':''}}>Value (desc)</option>
        <option value="asc"   ${{cfg.sortBy==='asc'?'selected':''}}>Value (asc)</option>
        <option value="alpha" ${{cfg.sortBy==='alpha'?'selected':''}}>Alphabetical</option>
      </select></div>
      <div class="form-group"><label>Orientation</label><select id="e-orient">
        <option value="vertical"   ${{!cfg.horizontal?'selected':''}}>Vertical</option>
        <option value="horizontal" ${{cfg.horizontal?'selected':''}}>Horizontal</option>
      </select></div>
    </div>`;
  }}

  if (chartId === 'c3' || chartId === 'c7') {{
    html += `<div class="form-row">
      <div class="form-group"><label>Cycle Time Metric</label><select id="e-metric">
        <option value="created"     ${{cfg.metric==='created'?'selected':''}}>Created → Resolved</option>
        <option value="in_progress" ${{cfg.metric==='in_progress'?'selected':''}}>In Progress → Resolved</option>
      </select></div>
    </div>`;
  }}
  if (chartId === 'c7') {{
    html += `<div class="form-row">
      <div class="form-group"><label>Sort Teams By</label><select id="e-sort">
        <option value="alpha" ${{cfg.sortBy==='alpha'?'selected':''}}>Alphabetical</option>
        <option value="value" ${{cfg.sortBy==='value'?'selected':''}}>Median (desc)</option>
      </select></div>
    </div>`;
  }}

  if (chartId === 'c4') {{
    html += `<div class="form-row">
      <div class="form-group"><label>Group By</label><select id="e-groupby">
        <option value="priority" ${{cfg.groupBy==='priority'?'selected':''}}>Priority</option>
        <option value="team"     ${{cfg.groupBy==='team'?'selected':''}}>Team</option>
      </select></div>
    </div>`;
  }}

  if (chartId === 'c5') {{
    html += `<div class="toggle-row"><label class="toggle"><input type="checkbox" id="e-stacked" ${{cfg.stacked?'checked':''}}><span class="slider"></span></label><span>Stacked bars</span></div>`;
  }}

  if (chartId === 'c6' || chartId === 'c9') {{
    html += `<div class="form-row">
      <div class="form-group"><label>Aggregation</label><select id="e-agg">
        <option value="daily"   ${{cfg.aggregation==='daily'?'selected':''}}>Daily</option>
        <option value="weekly"  ${{cfg.aggregation==='weekly'?'selected':''}}>Weekly</option>
        <option value="monthly" ${{cfg.aggregation==='monthly'?'selected':''}}>Monthly</option>
      </select></div>
    </div>`;
  }}

  document.getElementById('edit-body').innerHTML = html;
  document.getElementById('edit-overlay').classList.add('open');
}}

function applyEdit(){{
  const cfg = CONFIGS[activeEdit];
  cfg.type = document.getElementById('e-type')?.value || cfg.type;
  cfg.colorScheme = document.getElementById('e-color')?.value || cfg.colorScheme;
  if (document.getElementById('e-sort'))     cfg.sortBy    = document.getElementById('e-sort').value;
  if (document.getElementById('e-orient'))   cfg.horizontal = document.getElementById('e-orient').value==='horizontal';
  if (document.getElementById('e-metric'))   cfg.metric    = document.getElementById('e-metric').value;
  if (document.getElementById('e-groupby'))  cfg.groupBy = document.getElementById('e-groupby').value;
  if (document.getElementById('e-stacked'))  cfg.stacked = document.getElementById('e-stacked').checked;
  if (document.getElementById('e-agg'))      cfg.aggregation = document.getElementById('e-agg').value;
  closeModals();
  renderAll();
}}

// ─── JQL MODAL ────────────────────────────────────────────────────────────────
function buildJql(chartId){{
  const p = DASH_CFG.projects;
  const rs = DASH_CFG.resolved_statuses.map(s=>`"${{s}}"`).join(', ');
  const bq = DASH_CFG.bug_jql;
  const ms = `"${{selYear}}-${{String(selMonth).padStart(2,'0')}}-01"`;
  const me = `"${{isoDate(new Date(selYear,selMonth,0))}}"`;
  const base = `${{bq}}\nAND project in (${{p}})`;
  const jqls = {{
    c1: `${{base}}\nAND status changed to (${{rs}})\nDURING (${{ms}}, ${{me}})\nORDER BY cf[10032] ASC`,
    c2: `${{base}}\nAND status changed to (${{rs}})\nDURING (${{ms}}, ${{me}})\nORDER BY assignee ASC`,
    c3: `${{base}}\nAND status changed to (${{rs}})\nDURING (${{ms}}, ${{me}})\nORDER BY created ASC`,
    c4: `${{base}}\nAND status changed to (${{rs}})\nDURING (${{ms}}, ${{me}})\nORDER BY priority ASC`,
    c5: `${{base}}\nORDER BY cf[10032] ASC\n-- Open:  AND status NOT IN (${{rs}})\n-- Closed: AND status IN (${{rs}})`,
    c6: `${{base}}\n-- Open on date D: created <= D AND (resolution is EMPTY OR resolved > D)\nORDER BY created ASC`,
    c7: `${{base}}\nAND status changed to (${{rs}})\nDURING (${{ms}}, ${{me}})\nORDER BY cf[10032] ASC, priority ASC`,
  }};
  return jqls[chartId] || '';
}}

function openJql(chartId){{
  activeJql = chartId;
  const titles = {{c1:'Chart 1 JQL', c2:'Chart 2 JQL', c3:'Chart 3 JQL',
    c4:'Chart 4 JQL', c5:'Chart 5 JQL', c6:'Chart 6 JQL',
    c7:'Chart 7 JQL', c9:'Chart 9 JQL'}};
  document.getElementById('jql-title').textContent = titles[chartId];
  // Use saved JQL if user has previously edited it, otherwise build from current state
  document.getElementById('jql-text').value = SAVED_JQLS[chartId] || buildJql(chartId);
  document.getElementById('jql-note').textContent =
    'Edit the JQL and click "Apply & Re-fetch" to pull fresh data from Jira. ' +
    'Your JQL edits are saved automatically and persist when you reopen this page.';

  // Show filter controls for c1-c5 (team multi-select)
  const cf = CUSTOM_FILTERS[chartId];
  const allTeams = [...new Set(ALL_BUGS.map(b=>b.team))].sort();
  const filterWrap = document.getElementById('jql-filter-wrap');
  const filterForm = document.getElementById('jql-filter-form');

  if (['c1','c2','c3','c4','c5','c7'].includes(chartId)) {{
    filterWrap.style.display = 'block';
    filterForm.innerHTML = `<div class="form-group" style="grid-column:1/-1">
      <label>Filter by Team (hold Ctrl/Cmd to multi-select — applies instantly without re-fetch)</label>
      <select id="f-teams" multiple style="min-height:100px">
        ${{allTeams.map(t=>`<option value="${{t}}" ${{(cf.teams||[]).includes(t)?'selected':''}}>${{t}}</option>`).join('')}}
      </select>
    </div>`;
  }} else {{
    filterWrap.style.display = 'none';
  }}

  // Always show Apply button; also show Reset if chart has custom data
  document.getElementById('jql-apply-btn').style.display = '';
  const resetBtn = document.getElementById('jql-reset-btn');
  if (CHART_BUGS[chartId]) {{
    resetBtn.style.display = '';
  }} else {{
    resetBtn.style.display = 'none';
  }}

  // Clear any previous error
  const errBox = document.getElementById('jql-error-box');
  if (errBox) errBox.remove();

  document.getElementById('jql-overlay').classList.add('open');
}}

async function applyJqlFilter(){{
  const chartId = activeJql;
  const jqlText = document.getElementById('jql-text');
  const newJql  = jqlText ? jqlText.value.trim() : '';

  // Save team filter selection (no re-fetch needed)
  const sel = document.getElementById('f-teams');
  if (sel) {{
    const chosen = [...sel.selectedOptions].map(o=>o.value);
    CUSTOM_FILTERS[chartId].teams = chosen;
  }}

  // Save JQL to memory + localStorage
  if (newJql) {{
    SAVED_JQLS[chartId] = newJql;
    try {{ localStorage.setItem('jql_' + chartId, newJql); }} catch(e){{}}
  }}

  // Re-fetch from Jira
  const btn = document.getElementById('jql-apply-btn');
  const origText = btn.textContent;
  btn.textContent = '⏳ Fetching…';
  btn.disabled = true;

  try {{
    const bugs = await fetchFromJira(newJql);
    CHART_BUGS[chartId] = bugs;
    try {{ localStorage.setItem('bugs_' + chartId, JSON.stringify(bugs)); }} catch(e){{}}
    updateChartBadge(chartId, true, bugs.length);
    closeModals();
    renderAll();
  }} catch(err) {{
    btn.textContent = origText;
    btn.disabled = false;
    console.error('Jira fetch error:', err);
    // Show error inline below the textarea
    let errBox = document.getElementById('jql-error-box');
    if (!errBox) {{
      errBox = document.createElement('pre');
      errBox.id = 'jql-error-box';
      errBox.style.cssText = 'margin-top:10px;padding:10px 12px;background:#2d1a1a;border:1px solid #ef4444;border-radius:6px;color:#fca5a5;font-size:11px;white-space:pre-wrap;word-break:break-word';
      document.getElementById('jql-note').after(errBox);
    }}
    errBox.textContent = '❌ ' + err.message;
  }}
}}

function resetChartData(chartId){{
  CHART_BUGS[chartId] = null;
  delete SAVED_JQLS[chartId];
  try {{ localStorage.removeItem('jql_' + chartId); }} catch(e){{}}
  try {{ localStorage.removeItem('bugs_' + chartId); }} catch(e){{}}
  updateChartBadge(chartId, false);
  closeModals();
  renderAll();
}}

function copyJql(){{
  const t = document.getElementById('jql-text').value;
  navigator.clipboard.writeText(t).then(()=>{{
    const btn = document.querySelector('.copy-btn');
    btn.textContent='✅ Copied!'; setTimeout(()=>btn.textContent='📋 Copy', 2000);
  }});
}}

function closeModals(){{
  document.getElementById('edit-overlay').classList.remove('open');
  document.getElementById('jql-overlay').classList.remove('open');
  document.getElementById('gjql-overlay').classList.remove('open');
}}

// ─── CHART BADGES ─────────────────────────────────────────────────────────────
function updateChartBadge(chartId, active, count){{
  // Find the chart card's h3 heading
  const card = document.getElementById(chartId)?.closest('.chart-card');
  if (!card) return;
  const h3 = card.querySelector('.chart-header h3');
  if (!h3) return;
  const existing = h3.querySelector('.custom-badge');
  if (existing) existing.remove();
  if (active) {{
    const badge = document.createElement('span');
    badge.className = 'custom-badge';
    badge.textContent = count !== undefined ? `🔄 Custom (${{count}})` : '🔄 Custom';
    badge.title = 'Custom JQL active — click 🔍 JQL to reset';
    h3.appendChild(badge);
  }}
}}

function restoreAllBadges(){{
  ['c1','c2','c3','c4','c5','c6','c7','c9'].forEach(id=>{{
    if (CHART_BUGS[id]) updateChartBadge(id, true, CHART_BUGS[id].length);
  }});
}}

// ─── CONFIG EXPORT ────────────────────────────────────────────────────────────
function saveConfigJson(){{
  const overrides = {{}};
  ['c1','c2','c3','c4','c5','c6','c7','c9'].forEach(id=>{{
    if (SAVED_JQLS[id]) overrides[id] = SAVED_JQLS[id];
  }});
  const cfg = {{
    jira_url:          DASH_CFG.jira_url,
    email:             DASH_CFG.email,
    api_token:         "PASTE_YOUR_API_TOKEN_HERE",  // token not stored in HTML — fill in manually
    projects:          DASH_CFG.projects,
    working_days_per_month: DASH_CFG.working_days,
    resolved_statuses: DASH_CFG.resolved_statuses,
    in_progress_status: 'In Progress',
    bug_jql:           DASH_CFG.bug_jql,
    team_field:        'customfield_10032',
    months_of_history: 6,
    chart_jql_overrides: overrides,
  }};
  const blob = new Blob([JSON.stringify(cfg, null, 2)], {{type:'application/json'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'config.json';
  a.click();
  URL.revokeObjectURL(a.href);
}}

// ─── INIT ─────────────────────────────────────────────────────────────────────

// Restore JQL overrides + cached bug data from localStorage
(function initFromStorage(){{
  ['c1','c2','c3','c4','c5','c6','c7','c9'].forEach(id=>{{
    try {{
      const jql = localStorage.getItem('jql_' + id);
      if (jql) SAVED_JQLS[id] = jql;
      const bugsRaw = localStorage.getItem('bugs_' + id);
      if (bugsRaw) {{
        const parsed = JSON.parse(bugsRaw);
        // Only restore if non-empty — empty arrays are from failed/stuck fetches
        if (parsed && parsed.length > 0) {{
          CHART_BUGS[id] = parsed;
        }} else {{
          localStorage.removeItem('bugs_' + id);
        }}
      }}
    }} catch(e){{}}
  }});
}})();

// Also load chart_jql_overrides baked into the HTML at generation time
if (DASH_CFG.chart_jql_overrides) {{
  Object.entries(DASH_CFG.chart_jql_overrides).forEach(([id,jql])=>{{
    if (!SAVED_JQLS[id]) SAVED_JQLS[id] = jql;  // don't overwrite localStorage
  }});
}}

document.getElementById('edit-overlay').addEventListener('click', e=>{{ if(e.target===e.currentTarget) closeModals(); }});
document.getElementById('jql-overlay').addEventListener('click', e=>{{ if(e.target===e.currentTarget) closeModals(); }});
document.getElementById('gjql-overlay').addEventListener('click', e=>{{ if(e.target===e.currentTarget) closeModals(); }});
document.getElementById('wd-input').addEventListener('change', renderAll);
document.getElementById('gen-time').textContent = DASH_CFG.generated_at;

// ─── LAYOUT: drag-and-drop, minimize, width toggle, editable titles ───────────

const RENDER_FNS = {{c1:renderC1,c2:renderC2,c3:renderC3,c4:renderC4,c5:renderC5,c6:renderC6,c7:renderC7,c9:renderC9}};

function renderChart(cid) {{ if (RENDER_FNS[cid]) RENDER_FNS[cid](); }}

// ── Minimize / expand ─────────────────────────────────────────────────────────
function toggleMinimize(btn) {{
  const card = btn.closest('.chart-card');
  const mini = card.classList.toggle('minimized');
  btn.textContent = mini ? '+' : '−';
  btn.title = mini ? 'Expand' : 'Minimise';
  saveLayout();
}}

// ── Width toggle (half ↔ full) ─────────────────────────────────────────────────
function setCardWidth(card, w) {{
  card.dataset.width = w;
  card.style.gridColumn = w === 'full' ? '1 / -1' : '';
  const btn = card.querySelector('.btn-width');
  if (btn) {{ btn.textContent = w === 'full' ? '⊟' : '⊞'; btn.title = w === 'full' ? 'Switch to half width' : 'Switch to full width'; }}
}}
function toggleWidth(btn) {{
  const card = btn.closest('.chart-card');
  setCardWidth(card, card.dataset.width === 'full' ? 'half' : 'full');
  saveLayout();
  renderChart(card.dataset.cid);  // re-render so Plotly resizes
}}

// ── Editable title ────────────────────────────────────────────────────────────
function startEditTitle(el) {{
  if (!document.body.classList.contains('edit-mode')) return;
  el.contentEditable = 'true';
  el.focus();
  const range = document.createRange(); range.selectNodeContents(el);
  const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
}}
document.addEventListener('keydown', e => {{
  const el = document.activeElement;
  if (e.key === 'Enter' && el.classList.contains('chart-title')) {{
    e.preventDefault(); el.contentEditable = 'false'; el.blur(); saveLayout();
  }}
  if (e.key === 'Escape' && el.classList.contains('chart-title')) {{
    el.contentEditable = 'false'; el.blur();
  }}
}});
document.addEventListener('focusout', e => {{
  const el = e.target;
  if (el.classList.contains('chart-title')) {{ el.contentEditable = 'false'; saveLayout(); }}
}});

// ── Drag-and-drop reordering (position-aware) ────────────────────────────────
// Left/Right zone  → drop target + dragged card share the row as half-width cards
// Above/Below zone → dragged card becomes a full-width row on its own
let _dragCard = null, _dropTarget = null, _dropZone = null;
const grid = document.getElementById('charts-grid');

function _clearDropIndicators() {{
  grid.querySelectorAll('.chart-card').forEach(c =>
    c.classList.remove('drop-left','drop-right','drop-above','drop-below'));
  const h = document.getElementById('drop-hint');
  if (h) h.style.display = 'none';
}}

function _getDropZone(card, clientX, clientY) {{
  const r = card.getBoundingClientRect();
  const relX = (clientX - r.left) / r.width;
  const relY = (clientY - r.top) / r.height;
  if (relX < 0.30) return 'left';
  if (relX > 0.70) return 'right';
  return relY < 0.5 ? 'above' : 'below';
}}

grid.addEventListener('dragstart', e => {{
  if (!document.body.classList.contains('edit-mode')) {{ e.preventDefault(); return; }}
  _dragCard = e.target.closest('.chart-card');
  if (!_dragCard) return;
  _dragCard.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', _dragCard.dataset.cid);
}});

grid.addEventListener('dragover', e => {{
  e.preventDefault(); e.dataTransfer.dropEffect = 'move';
  const over = e.target.closest('.chart-card');
  if (!over || over === _dragCard) {{ _clearDropIndicators(); return; }}
  _clearDropIndicators();
  _dropTarget = over;
  _dropZone   = _getDropZone(over, e.clientX, e.clientY);
  over.classList.add('drop-' + _dropZone);
  const h = document.getElementById('drop-hint');
  if (h) {{
    h.textContent = (_dropZone==='left'||_dropZone==='right') ? '½  Share row' : '▬  Full row';
    h.style.left    = (e.clientX + 14) + 'px';
    h.style.top     = (e.clientY - 10) + 'px';
    h.style.display = 'block';
  }}
}});

grid.addEventListener('dragleave', e => {{
  if (!grid.contains(e.relatedTarget)) _clearDropIndicators();
}});

grid.addEventListener('drop', e => {{
  e.preventDefault();
  _clearDropIndicators();
  if (!_dragCard || !_dropTarget || !_dropZone) return;

  // 1. Reorder in DOM
  if (_dropZone === 'left' || _dropZone === 'above') {{
    grid.insertBefore(_dragCard, _dropTarget);
  }} else {{
    grid.insertBefore(_dragCard, _dropTarget.nextSibling);
  }}

  // 2. Apply widths
  if (_dropZone === 'left' || _dropZone === 'right') {{
    setCardWidth(_dragCard,  'half');
    setCardWidth(_dropTarget, 'half');
  }} else {{
    setCardWidth(_dragCard, 'full');
  }}

  // 3. Sync width-toggle button icons
  [_dragCard, _dropTarget].forEach(c => {{
    if (!c) return;
    const btn = c.querySelector('.btn-width');
    if (btn) btn.textContent = c.dataset.width === 'full' ? '⊟' : '⊞';
  }});
}});

grid.addEventListener('dragend', () => {{
  if (_dragCard) _dragCard.classList.remove('dragging');
  _clearDropIndicators();
  const wasDrag = _dragCard;
  _dragCard = null; _dropTarget = null; _dropZone = null;
  saveLayout();
  // Re-render charts so Plotly adapts to any size changes
  setTimeout(() => {{
    grid.querySelectorAll('.chart-card').forEach(c => {{
      if (!c.classList.contains('minimized') && RENDER_FNS[c.dataset.cid]) {{
        RENDER_FNS[c.dataset.cid]();
      }}
    }});
  }}, 180);
}});

// ── Persist / restore layout ──────────────────────────────────────────────────
function saveLayout() {{
  const state = [...grid.querySelectorAll('.chart-card')].map(c => ({{
    cid:      c.dataset.cid,
    width:    c.dataset.width,
    minimized:c.classList.contains('minimized'),
    title:    c.querySelector('.chart-title')?.textContent?.trim(),
  }}));
  try {{ localStorage.setItem('db_layout_v2', JSON.stringify(state)); }} catch(e) {{}}
}}

function loadLayout() {{
  let state;
  try {{ state = JSON.parse(localStorage.getItem('db_layout_v2') || 'null'); }} catch(e) {{}}
  if (!state || !state.length) {{
    // Apply defaults: set initial grid-column from data-width attributes
    grid.querySelectorAll('.chart-card').forEach(c => setCardWidth(c, c.dataset.width));
    return;
  }}
  state.forEach(({{cid, width, minimized, title}}) => {{
    const card = grid.querySelector(`[data-cid="${{cid}}"]`);
    if (!card) return;
    grid.appendChild(card);               // reorder to saved position
    setCardWidth(card, width || 'half');
    if (minimized) {{
      card.classList.add('minimized');
      const btn = card.querySelector('.btn-minimize');
      if (btn) {{ btn.textContent = '+'; btn.title = 'Expand'; }}
    }}
    if (title) {{
      const t = card.querySelector('.chart-title');
      if (t) t.textContent = title;
    }}
  }});
}}

// ── Reset layout button ───────────────────────────────────────────────────────
function resetLayout() {{
  try {{ localStorage.removeItem('db_layout_v2'); }} catch(e) {{}}
  location.reload();
}}

// ── Global JQL modal ─────────────────────────────────────────────────────────
// Opens the global JQL editor. Pre-fills fields from localStorage or DASH_CFG defaults.
function openGlobalJql() {{
  // Restore saved values, fall back to what was baked into the HTML at generation time
  const savedJql   = (() => {{ try {{ return localStorage.getItem('global_jql'); }} catch(e) {{ return null; }} }})();
  const savedToken = (() => {{ try {{ return localStorage.getItem('gh_pat');     }} catch(e) {{ return null; }} }})();
  const savedRepo  = (() => {{ try {{ return localStorage.getItem('gh_repo');    }} catch(e) {{ return null; }} }})();

  document.getElementById('gjql-text').value  = savedJql  || DASH_CFG.bug_jql  || '';
  document.getElementById('gjql-token').value = savedToken || '';
  document.getElementById('gjql-repo').value  = savedRepo  || DASH_CFG.github_repo || '';

  // Reset status box
  const st = document.getElementById('gjql-status');
  st.className = 'gjql-status'; st.textContent = ''; st.style.display = 'none';

  const btn = document.getElementById('gjql-apply-btn');
  btn.disabled = false; btn.textContent = '🔄 Trigger Refresh';

  document.getElementById('gjql-overlay').classList.add('open');
}}

// Shows a status message inside the Global JQL modal.
function _gjqlStatus(type, msg) {{
  const el = document.getElementById('gjql-status');
  el.className = 'gjql-status ' + type;
  el.textContent = msg;
  el.style.display = 'block';
}}

// Validates inputs, persists them, then calls the GitHub Actions API to trigger a workflow_dispatch.
async function applyGlobalJql() {{
  const jql   = (document.getElementById('gjql-text').value  || '').trim();
  const token = (document.getElementById('gjql-token').value || '').trim();
  const repo  = (document.getElementById('gjql-repo').value  || '').trim();
  const workflow = DASH_CFG.github_workflow || 'refresh-dashboard.yml';

  // ── Validation ────────────────────────────────────────────────────────────
  if (!jql)   {{ _gjqlStatus('error', '❌ JQL cannot be empty.'); return; }}
  if (!token) {{ _gjqlStatus('error', '❌ GitHub token is required.\\n\\nCreate one at github.com/settings/tokens/new with the "workflow" scope.'); return; }}
  if (!repo || !repo.includes('/')) {{ _gjqlStatus('error', '❌ Enter a valid GitHub repo in the format  owner/repo-name'); return; }}

  // ── Persist to localStorage ───────────────────────────────────────────────
  try {{
    localStorage.setItem('global_jql', jql);
    localStorage.setItem('gh_pat',     token);
    localStorage.setItem('gh_repo',    repo);
  }} catch(e) {{}}

  // ── Trigger GitHub Actions workflow_dispatch ───────────────────────────────
  const btn = document.getElementById('gjql-apply-btn');
  btn.disabled = true; btn.textContent = '⏳ Triggering…';
  _gjqlStatus('', ''); document.getElementById('gjql-status').style.display = 'none';

  try {{
    const apiUrl = `https://api.github.com/repos/${{repo}}/actions/workflows/${{workflow}}/dispatches`;
    const res = await fetch(apiUrl, {{
      method: 'POST',
      headers: {{
        'Authorization': `token ${{token}}`,
        'Accept':        'application/vnd.github.v3+json',
        'Content-Type':  'application/json',
      }},
      body: JSON.stringify({{ ref: 'main', inputs: {{ bug_jql: jql }} }}),
    }});

    if (res.status === 204) {{
      // 204 No Content = workflow successfully queued
      _gjqlStatus('success',
        '✅ Refresh triggered successfully!\\n\\n' +
        'The GitHub Actions workflow is now running. It will fetch fresh Jira data using your updated JQL, ' +
        'regenerate the dashboard, and commit it to the repo.\\n\\n' +
        '⏱  Reload this page in ~2 minutes to see the updated data.'
      );
      btn.textContent = '✓ Triggered';
    }} else {{
      // Parse GitHub's error response for a helpful message
      let detail = '';
      try {{
        const body = await res.json();
        detail = body.message ? `\n\n${{body.message}}` : '';
        if (res.status === 401) detail = '\\n\\nYour token appears to be invalid or expired. Generate a new one at github.com/settings/tokens/new';
        if (res.status === 403) detail = '\\n\\nPermission denied. Make sure your token has the "workflow" scope.';
        if (res.status === 404) detail = '\\n\\nWorkflow or repo not found. Check that the repo ' + repo + ' exists and the workflow file is named ' + workflow;
        if (res.status === 422) detail = '\\n\\nUnprocessable — the main branch may not exist, or the workflow file is missing the workflow_dispatch trigger.';
      }} catch(pe) {{}}
      _gjqlStatus('error', `❌ GitHub API returned HTTP ${{res.status}}${{detail}}`);
      btn.disabled = false; btn.textContent = '🔄 Trigger Refresh';
    }}
  }} catch(netErr) {{
    _gjqlStatus('error', `❌ Network error: ${{netErr.message}}\n\nCheck your internet connection and try again.`);
    btn.disabled = false; btn.textContent = '🔄 Trigger Refresh';
  }}
}}

// ── Edit mode toggle ──────────────────────────────────────────────────────────
// Default: OFF — viewers see a clean, locked dashboard.
// Click "Edit Layout" to unlock drag, resize, rename, JQL controls.
function toggleEditMode() {{
  const isEdit = document.body.classList.toggle('edit-mode');
  const btn = document.getElementById('edit-mode-btn');
  if (btn) {{
    btn.textContent = isEdit ? '✓ Done Editing' : '✏️ Edit Layout';
    btn.className   = 'btn-edit-mode ' + (isEdit ? 'edit-mode-on' : 'view-mode');
    btn.title       = isEdit ? 'Click to exit edit mode and lock the layout' : 'Unlock layout editing — drag, resize, rename charts';
  }}
  try {{ localStorage.setItem('edit_mode', isEdit ? '1' : '0'); }} catch(e) {{}}
}}

// Restore edit mode preference (persisted per-browser; default OFF)
(function initEditMode() {{
  let saved;
  try {{ saved = localStorage.getItem('edit_mode'); }} catch(e) {{}}
  if (saved === '1') {{
    document.body.classList.add('edit-mode');
    const btn = document.getElementById('edit-mode-btn');
    if (btn) {{
      btn.textContent = '✓ Done Editing';
      btn.className   = 'btn-edit-mode edit-mode-on';
      btn.title       = 'Click to exit edit mode and lock the layout';
    }}
  }}
}})();

buildMonthBar();
loadLayout();
renderAll();
restoreAllBadges();
</script>
</body>
</html>"""

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    print("─" * 55)
    print("  Bug Dashboard Generator")
    print("─" * 55)

    cfg = load_config()
    print(f"  Jira: {cfg['jira_url']}")
    print(f"  Project(s): {cfg.get('projects','OXDEV')}")

    days  = cfg.get("months_of_history", 6) * 31
    jql   = (
        f"project in ({cfg.get('projects','OXDEV')}) "
        f"AND {cfg.get('bug_jql','(issuetype = Bug OR labels in (bug, jira_escalated))')} "
        f"AND created >= -{days}d "
        f"AND creator = 557058:3e2ef232-d5c4-4c0e-94b0-977a305ebaae "
        f"ORDER BY created ASC"
    )
    fields = (
        "summary,status,assignee,priority,created,resolutiondate,"
        "labels,issuetype,customfield_10001,customfield_10032,customfield_10112"
    )

    # ── Fetch from Jira (with graceful error handling) ──────────────────────
    print(f"\nFetching bugs created in the last {days} days...")
    raw  = []
    bugs = []
    cached_norm_timeline = None
    try:
        raw  = fetch_all(cfg, jql, fields, expand="changelog")
        print("\nProcessing issues...")
        bugs = process_issues(raw, cfg)
        print(f"  Processed {len(bugs)} issues.")
    except Exception as e:
        print(f"\n  ❌ Fetch error: {e}")
        # Try to load previous cache so we don't wipe the dashboard
        if CACHE_PATH.exists():
            try:
                cached = json.loads(CACHE_PATH.read_text())
                bugs = cached.get("bugs", [])
                cached_norm_timeline = cached.get("norm_timeline")
                print(f"  ↩  Falling back to cached data ({len(bugs)} issues).")
            except Exception:
                pass
        if not bugs:
            print("  ⚠️  No cached data either — dashboard will be empty.")

    print("\nBuilding backlog timelines...")
    timeline = build_timeline(bugs, days=days)
    if cached_norm_timeline and not raw:
        # Reuse cached norm timeline when falling back (avoids slow recompute)
        norm_timeline = cached_norm_timeline
    else:
        print("  Building normalised timeline (bugs per customer)...")
        norm_timeline = build_normalized_timeline(bugs, days=days)

    # Save cache (only if we got fresh data)
    if raw:
        with open(CACHE_PATH, "w") as f:
            json.dump({"bugs": bugs, "timeline": timeline, "norm_timeline": norm_timeline},
                      f, ensure_ascii=False)
        print(f"  Cache saved → {CACHE_PATH.name}")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    print("\nGenerating dashboard HTML...")
    html = build_html(bugs, timeline, norm_timeline, cfg, generated_at)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  Dashboard saved → {OUTPUT_HTML.name}")
    if bugs:
        print(f"\n✅ Done! {len(bugs)} bugs loaded.")
    else:
        print(f"\n⚠️  Done but 0 bugs — check terminal output above for errors.")
    print("─" * 55)

def serve(port=8080):
    """
    Start a local HTTP server that:
      GET /                    → serves bug_dashboard.html
      GET /jira?jql=...        → proxies Jira search API (avoids CORS)
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    cfg = load_config()
    jira_url   = cfg["jira_url"].rstrip("/")
    auth_token = base64.b64encode(
        f"{cfg['email']}:{cfg['api_token']}".encode()
    ).decode()
    fields = (
        "summary,status,assignee,priority,created,resolutiondate,"
        "labels,issuetype,customfield_10001,customfield_10032,customfield_10112"
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # silence default access log

        def send_cors(self):
            self.send_header("Access-Control-Allow-Origin",  "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_cors()
            self.end_headers()

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            qs     = urllib.parse.parse_qs(parsed.query)

            # ── Proxy: /jira?jql=...&startAt=...
            if parsed.path == "/jira":
                jql       = qs.get("jql",     [""])[0]
                start_at  = qs.get("startAt", ["0"])[0]
                req_headers = {
                    "Authorization": f"Basic {auth_token}",
                    "Accept":        "application/json",
                    "Content-Type":  "application/json",
                }
                try:
                    # Try POST /search/jql first (new endpoint)
                    post_body = json.dumps({
                        "jql": jql, "fields": fields.split(","),
                        "maxResults": 100, "startAt": int(start_at),  # 100 = Jira's per-page max
                    }).encode()
                    r = requests.post(
                        f"{jira_url}/rest/api/3/search/jql",
                        headers=req_headers, data=post_body, timeout=60,
                    )
                    # Fall back to GET /search if new endpoint not available
                    if r.status_code in (404, 405, 410):
                        params = urllib.parse.urlencode({
                            "jql": jql, "fields": fields,
                            "maxResults": "100", "startAt": start_at,  # 100 = Jira's per-page max
                        })
                        r = requests.get(
                            f"{jira_url}/rest/api/3/search?{params}",
                            headers=req_headers, timeout=60,
                        )
                    body = r.content
                    self.send_response(r.status_code)
                    self.send_header("Content-Type",   "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_cors()
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as e:
                    err = json.dumps({"error": str(e)}).encode()
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.send_cors()
                    self.end_headers()
                    self.wfile.write(err)
                return

            # ── Serve dashboard HTML at /
            if parsed.path in ("/", "/bug_dashboard.html"):
                html_path = OUTPUT_HTML
                if not html_path.exists():
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"bug_dashboard.html not found - run fetch_bugs.py first")
                    return
                body = html_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type",   "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(404)
            self.end_headers()

    httpd = HTTPServer(("localhost", port), Handler)
    print(f"\n🚀  Dashboard running at  http://localhost:{port}")
    print(f"    Jira proxy at          http://localhost:{port}/jira?jql=...")
    print(f"    Press Ctrl+C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


def check_fields():
    """
    Diagnostic: fetch 5 recent bugs and dump all their customfield_* values.
    Run with:  python3 fetch_bugs.py --check-fields
    This helps identify the correct field IDs for team/account.
    """
    print("─" * 60)
    print("  Field Diagnostic — fetching 5 recent bugs")
    print("─" * 60)
    cfg  = load_config()
    auth = (cfg["email"], cfg["api_token"])
    base = cfg["jira_url"].rstrip("/")
    jql  = (
        f"project in ({cfg.get('projects','OXDEV')}) "
        f"AND {cfg.get('bug_jql','(issuetype = Bug OR labels in (bug, jira_escalated))')} "
        f"ORDER BY created DESC"
    )
    params = {
        "jql": jql,
        "fields": "*all",      # Fetch every field
        "maxResults": 5,
    }
    r = requests.get(
        f"{base}/rest/api/3/search/jql",
        auth=auth, params=params,
        headers={"Accept": "application/json"}, timeout=30
    )
    r.raise_for_status()
    issues = r.json().get("issues", [])
    print(f"\nFetched {len(issues)} issues.\n")
    for iss in issues:
        key    = iss.get("key", "?")
        fields = iss.get("fields", {})
        print(f"{'─'*50}")
        print(f"  Issue: {key}")
        print(f"  Summary: {str(fields.get('summary',''))[:60]}")
        # Print all customfields that are non-null
        print(f"  Custom fields (non-null):")
        for k, v in sorted(fields.items()):
            if k.startswith("customfield_") and v is not None:
                print(f"    {k}: {repr(v)[:120]}")
    print("\n─" * 60)
    print("  Look for the field that contains your team name above.")
    print("  Then update 'team_field' in config.json with that key.")
    print("─" * 60)


if __name__ == "__main__":
    if "--serve" in sys.argv:
        port = 8080
        for arg in sys.argv:
            if arg.startswith("--port="):
                port = int(arg.split("=")[1])
        serve(port)
    elif "--check-fields" in sys.argv:
        check_fields()
    else:
        main()
