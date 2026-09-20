"""Weekly approved price-list report, straight from the PriceFx REST API.

Replaces this by-hand routine:
    export the price list grid -> delete rows that are not approved -> delete
    rows older than the week -> open each price list -> Summary -> Calculate ->
    sort SKU impact -> write down the total and the vendors over +/-100k

Three of those steps do not need doing at all. PriceFx will filter server-side,
so the script asks only for approved lists submitted inside the week and nothing
else ever arrives. The Summary/Calculate step is one call - pricelistmanager.
summarize - which returns SKU Impact already totalled per vendor.

Nothing is written back to PriceFx. Every call here reads.

    python pricefx_weekly.py                 last full Sun-Sat week
    python pricefx_weekly.py --week 2026-09-14    the week starting that Sunday
    python pricefx_weekly.py --check         prove the login and filters work,
                                             write nothing
"""
import argparse
import base64
import configparser
import os
import sys
from datetime import date, datetime, time, timedelta

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "pricefx_config.ini")

# Vendor Name lives in attribute19 - confirmed from the Summary screen's own
# request, where Group By = Vendor Name sends productGroupBy=attribute19.
VENDOR_FIELD = "attribute19"

# The nine things the Summary screen totals. Only SKU Impact is used below, but
# asking for the same set keeps the call identical to the one the UI makes.
PROJECTIONS = [
    ("AVG", "Optimized Margin 1 %"),
    ("SUM", "Page Views 12 Months"),
    ("SUM", "Page Views 3 Months"),
    ("SUM", "ATP"),
    ("AVG", "Total Margin % R12"),
    ("AVG", "Sales Regular Customer %"),
    ("AVG", "Sales E-commerce %"),
    ("AVG", "AVG Discretionary Discount %"),
    ("SUM", "SKU Impact"),
]


def load_config():
    if not os.path.exists(CONFIG):
        sys.exit("No pricefx_config.ini next to this script. Copy the example one "
                 "and fill it in - it is the only place your password lives.")
    cp = configparser.ConfigParser()
    cp.read(CONFIG)
    c = cp["pricefx"]
    return {
        "base_url": c.get("base_url").strip(),
        "partition": c.get("partition").strip(),
        "account": c.get("account").strip(),
        "password": c.get("password"),
        "threshold": float(c.get("vendor_threshold", "100000")),
        "out_dir": c.get("output_folder", HERE).strip(),
    }


def connect(cfg):
    """Basic auth, then swap it for a JWT.

    PriceFx deliberately makes Basic auth slow - about half a second a call - to
    make brute-forcing painful. This script makes one call per price list, so
    doing that on every request would add minutes for no reason. The JWT
    exchange is the documented way round it and is what the docs recommend.
    """
    url = "https://%s/pricefx/%s" % (cfg["base_url"], cfg["partition"])
    raw = "%s/%s:%s" % (cfg["partition"], cfg["account"], cfg["password"])
    s = requests.Session()
    s.headers["Authorization"] = "Basic " + base64.b64encode(raw.encode()).decode("ascii")
    s.headers["Content-Type"] = "application/json"

    r = s.post(url + "/login/extended", timeout=60)
    r.raise_for_status()
    jwt = r.cookies.get("X-PriceFx-jwt")
    if jwt:
        s.headers.pop("Authorization", None)
        s.cookies.set("X-PriceFx-jwt", jwt)
    return s, url


def post(s, url, path, body=None, params=None):
    r = s.post(url + path, json=body or {}, params=params or {}, timeout=180)
    r.raise_for_status()
    return r.json()


def approved_status_values(s, url):
    """Ask PriceFx what its workflow statuses are actually called.

    Hardcoding "APPROVED" is a guess, and a wrong guess here returns an empty
    week that looks exactly like a quiet week. So read the list and match
    anything containing 'approv'.
    """
    try:
        d = post(s, url, "/configurationmanager.get/availableWorkStatus",
                 params={"dataLocale": "en"})
    except Exception:
        return ["APPROVED"]
    found = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and "approv" in o.lower():
            found.append(o)
    walk(d)
    return sorted(set(found)) or ["APPROVED"]


def week_bounds(anchor=None):
    """The Sunday-to-Saturday week that has finished.

    Run on Sunday 21 Sep, this returns Sunday 14 Sep 00:00:00 to Saturday
    20 Sep 23:59:59 - the week that ended last night, not the one starting
    today. Seconds included at both ends: 12:01am would leave a minute at
    midnight belonging to no week at all.
    """
    if anchor:
        start = anchor
    else:
        today = date.today()
        start = today - timedelta(days=(today.weekday() + 1) % 7 + 7)
    end = start + timedelta(days=6)
    return (datetime.combine(start, time(0, 0, 0)),
            datetime.combine(end, time(23, 59, 59)))


def fetch_price_lists(s, url, start, end, statuses):
    """Approved price lists submitted inside the week - filtered server-side."""
    crit = {
        "_constructor": "AdvancedCriteria", "operator": "and",
        "criteria": [
            {"_constructor": "AdvancedCriteria", "operator": "or",
             "criteria": [{"fieldName": "workflowStatus", "operator": "equals",
                           "value": v, "_constructor": "AdvancedCriteria"}
                          for v in statuses]},
            {"fieldName": "submitDate", "operator": "greaterOrEqual",
             "value": start.strftime("%Y-%m-%dT%H:%M:%S"),
             "_constructor": "AdvancedCriteria"},
            {"fieldName": "submitDate", "operator": "lessOrEqual",
             "value": end.strftime("%Y-%m-%dT%H:%M:%S"),
             "_constructor": "AdvancedCriteria"},
        ],
    }
    out, start_row = [], 0
    while True:
        d = post(s, url, "/fetch/PL", {
            "operationType": "fetch", "textMatchStyle": "exact",
            "startRow": start_row, "endRow": start_row + 200,
            "data": crit, "oldValues": None,
        }, {"dataLocale": "en"})
        rows = (d.get("response") or {}).get("data") or d.get("data") or []
        out.extend(rows)
        if len(rows) < 200:
            break
        start_row += 200
    return out


def summarize(s, url, pl_id):
    """The Calculate button. Returns vendor rows with SKU Impact already summed."""
    return post(s, url, "/pricelistmanager.summarize", {
        "data": {"query": {
            "objects": ["%s.PL" % pl_id],
            "productGroupBy": VENDOR_FIELD,
            "count": True,
            "projections": [{"weight": "null", "aggregationMode": mode,
                             "fieldName": field} for mode, field in PROJECTIONS],
        }}
    }, {"dataLocale": "en"})


def find_key(row, *wanted):
    """Match a column by meaning, not by exact spelling.

    The capture showed what the browser SENDS but not what comes back, so the
    reply's key for SKU Impact might be 'SKU Impact', 'skuImpact' or
    'sum_SKU_Impact'. Rather than guess once and be wrong silently, look for any
    key whose letters match.
    """
    def norm(x):
        return "".join(ch for ch in str(x).lower() if ch.isalnum())
    targets = [norm(w) for w in wanted]
    for k in row:
        if norm(k) in targets:
            return k
    for k in row:
        if any(t in norm(k) for t in targets):
            return k
    return None


def rows_from(summary):
    """Pull the vendor rows out of whatever shape summarize returns."""
    if isinstance(summary, list):
        return summary
    for key in ("response", "data", "result", "rows"):
        v = summary.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for k2 in ("data", "rows", "records"):
                if isinstance(v.get(k2), list):
                    return v[k2]
    return []


def vendor_impacts(summary):
    """[(vendor, sku_impact)] from one summarize reply."""
    rows = rows_from(summary)
    if not rows or not isinstance(rows[0], dict):
        return [], None
    sample = rows[0]
    vkey = find_key(sample, VENDOR_FIELD, "vendorName", "vendor name", "vendor")
    ikey = find_key(sample, "SKU Impact", "skuImpact", "sum_SKU_Impact")
    if not ikey:
        return [], sample
    out = []
    for r in rows:
        try:
            val = float(r.get(ikey) or 0)
        except (TypeError, ValueError):
            continue
        out.append((str(r.get(vkey, "(no vendor)")), val))
    return out, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", help="Sunday the week starts, YYYY-MM-DD")
    ap.add_argument("--check", action="store_true",
                    help="prove login and filters work, write nothing")
    a = ap.parse_args()

    cfg = load_config()
    anchor = date.fromisoformat(a.week) if a.week else None
    start, end = week_bounds(anchor)
    print("Week: %s to %s" % (start.strftime("%a %d %b %H:%M:%S"),
                              end.strftime("%a %d %b %H:%M:%S")))

    s, url = connect(cfg)
    print("Signed in to %s as %s" % (cfg["partition"], cfg["account"]))

    statuses = approved_status_values(s, url)
    print("Treating these workflow statuses as approved: %s" % ", ".join(statuses))

    pls = fetch_price_lists(s, url, start, end, statuses)
    print("Approved price lists submitted in the week: %d" % len(pls))

    if a.check:
        for p in pls[:5]:
            print("   %s  %s  submitted %s" % (p.get("id"), p.get("label"),
                                               p.get("submitDate")))
        if pls:
            v, unknown = vendor_impacts(summarize(s, url, pls[0].get("id")))
            if unknown is not None:
                print("\nCould not find the SKU Impact column in the reply.")
                print("These are the columns it returned - tell me which one:")
                print("   " + ", ".join(sorted(unknown)))
            else:
                print("\nSKU Impact read back for %d vendors on price list %s."
                      % (len(v), pls[0].get("id")))
        print("\nCheck only - nothing written.")
        return

    report, unknown_cols = [], None
    for i, p in enumerate(pls, 1):
        pid = p.get("id")
        print("  [%d/%d] price list %s" % (i, len(pls), pid))
        vend, unknown = vendor_impacts(summarize(s, url, pid))
        if unknown is not None and unknown_cols is None:
            unknown_cols = unknown
        big = [(n, v) for n, v in vend if abs(v) >= cfg["threshold"]]
        big.sort(key=lambda x: -abs(x[1]))
        report.append({
            "Price List Number": pid,
            "Price List Name": p.get("label"),
            "Material/Source Count": p.get("numberOfItems"),
            "Missing Resale Price": "",
            "Created By": p.get("createdByName"),
            "Submitted": p.get("submitDate"),
            "Calculated Annual Impact (000s)": round(sum(v for _, v in vend) / 1000.0, 1),
            "Vendors over threshold": "; ".join("%s (%+,.0f)" % (n, v) for n, v in big),
        })

    if unknown_cols is not None:
        print("\nWARNING: SKU Impact could not be read on at least one price list, "
              "so those impacts are zero. Columns returned were:")
        print("   " + ", ".join(sorted(unknown_cols)))

    write_report(report, start, end, cfg["out_dir"])


def write_report(rows, start, end, out_dir):
    import csv
    os.makedirs(out_dir, exist_ok=True)
    name = "approved_price_lists_%s_to_%s.csv" % (start.date(), end.date())
    path = os.path.join(out_dir, name)
    cols = ["Price List Number", "Price List Name", "Material/Source Count",
            "Missing Resale Price", "Created By", "Submitted",
            "Calculated Annual Impact (000s)", "Vendors over threshold"]
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print("\nWrote %d rows to %s" % (len(rows), path))


if __name__ == "__main__":
    main()
