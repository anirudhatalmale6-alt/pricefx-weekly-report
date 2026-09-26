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

    python pricefx_weekly.py                    last full Sun-Sat week
    python pricefx_weekly.py --week 2026-09-14  the week starting that Sunday
    python pricefx_weekly.py --month            the month that just finished
    python pricefx_weekly.py --month 2026-08    that calendar month
    python pricefx_weekly.py --check            prove the login and filters
                                                work, write nothing
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

# Printed on the first line of every run. If this is not the version you were
# told to expect, the file you downloaded is not the file that just ran - which
# has happened, and cost an evening of chasing bugs that were already fixed.
VERSION = "v17 - 26 Sep"

# Vendor Name lives in attribute19 - confirmed from the Summary screen's own
# request, where Group By = Vendor Name sends productGroupBy=attribute19.
VENDOR_FIELD = "attribute19"

# Which workflow statuses count as done. NO_APPROVAL_REQUIRED is not an
# oversight on the part of whoever submitted it - it means the list went live
# without needing a sign-off, so it belongs in the week's figures exactly like
# an APPROVED one. Overridable in pricefx_config.ini, because guessing at a
# status spelling is how a price list goes missing without anyone noticing.
DEFAULT_STATUSES = "APPROVED, NO_APPROVAL_REQUIRED"

# Where a price list's status might live in the fetch reply, in the order the
# names should be trusted.
STATUS_FIELDS = ("workflowStatus", "approvalStatus", "status")

# The 1st level hierarchy, exactly as the CategorySummary sheet spells it. The
# analysts name their price lists after these, which is how a price list gets
# attributed - "the way i identify today is by the description name".
CATEGORIES = [
    "Circuit Breakers, Fuses & Protection",
    "Connectors",
    "Electronic Components",
    "Enclosures, Racks & Cabinets",
    "Facilities, Cleaning & Maintenance",
    "Fans & Thermal Management",
    "Industrial Controls",
    "Industrial Data Communications",
    "Lighting & Indication",
    "Mechanical Power Transmission",
    "Motors & Motor Controls",
    "PLCs & HMIs",
    "Pneumatics & Fluid Control",
    "Power Products",
    "Raspberry Pi, Arduino & Development Tools",
    "Relays",
    "Sensors",
    "Switches",
    "Test & Measurement",
    "Tools & Hardware",
    "Uncategorized",
    "Wire & Cable",
]

# Short forms the analysts actually type. Add to this rather than renaming a
# price list.
CATEGORY_ALIASES = {
    "FCM": "Facilities, Cleaning & Maintenance",
    "Uncat": "Uncategorized",
    "PLCs And HMIs": "PLCs & HMIs",
}

CROSS = "Notable Cross Category"

# The Summary screen's Group By calls this "Internet Hierarchy 1" and its values
# are the 22 categories. The field behind that label is not attribute1-40, so
# these spellings are tried as well.
HIERARCHY_NAME_GUESSES = [
    "internetHierarchy1", "InternetHierarchy1", "internet_hierarchy_1",
    "internethierarchy1", "internetHierarchyLevel1", "hierarchy1",
    "productHierarchy1", "level1", "ih1",
]

# PLCI is the product code that says whether a part is stocked. His words:
# "Stocked PLCI is 25 and 45 always / Non-Stocked PLCI is 14, 34, 74, 84".
# Overridable in the ini because he wants to double-confirm the non-stocked set.
DEFAULT_STOCKED_PLCI = "25, 45"
DEFAULT_NONSTOCKED_PLCI = "14, 34, 74, 84"

# Used only when hunting for the PLCI field.
PLCI_PROBE = ["25", "45", "14", "34", "74", "84"]
OVERRIDES = os.path.join(HERE, "category_overrides.csv")

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
        # Optional. Set it and the monthly file lands somewhere of its own;
        # leave it out and monthly and weekly share one folder.
        "month_out_dir": c.get("monthly_output_folder", "").strip() or None,
        # Which product attribute holds the 1st level hierarchy. Run
        # --find-hierarchy once and put the answer here.
        "hierarchy_field": c.get("hierarchy_field", "").strip() or None,
        # Which attribute holds PLCI, and which codes mean what. With this set,
        # stocked / non-stocked is read from the data instead of from the words
        # in the price list name.
        "plci_field": c.get("plci_field", "").strip() or None,
        "stocked_plci": set(
            x.strip() for x in
            c.get("stocked_plci", DEFAULT_STOCKED_PLCI).split(",") if x.strip()),
        "nonstocked_plci": set(
            x.strip() for x in
            c.get("nonstocked_plci", DEFAULT_NONSTOCKED_PLCI).split(",")
            if x.strip()),
        "statuses": [x.strip() for x in
                     c.get("workflow_statuses", DEFAULT_STATUSES).split(",")
                     if x.strip()],
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


def status_of(row):
    """This price list's workflow status, whatever the reply calls the field."""
    for want in STATUS_FIELDS:
        k = find_key(row, want)
        if k and row.get(k) not in (None, ""):
            return str(row[k]).strip()
    return ""


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


def month_bounds(anchor=None):
    """A whole calendar month, the 1st 00:00:00 to the last day 23:59:59.

    The last day is worked out, not assumed to be the 31st - otherwise every
    30-day month would quietly lose its last day and February would lose three.

    With no anchor it does the month that has FINISHED, so running it any time
    in October gives you the whole of September.
    """
    if anchor:
        bits = str(anchor).split("-")
        y, m = int(bits[0]), int(bits[1])
    else:
        today = date.today()
        y, m = (today.year, today.month - 1) if today.month > 1 \
            else (today.year - 1, 12)
    start = date(y, m, 1)
    following = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    end = following - timedelta(days=1)
    return (datetime.combine(start, time(0, 0, 0)),
            datetime.combine(end, time(23, 59, 59)))


def fetch_price_lists(s, url, start, end):
    """Every price list submitted inside the week. Date filtered server-side.

    The status filter used to be part of this query too. It is not any more: a
    server-side status filter can only return what it was asked for, so a list
    with a status I had not thought of - NO_APPROVAL_REQUIRED, as it turned out -
    vanished with nothing to show it had ever existed. Fetching the whole week
    and choosing in Python costs one extra field per row and means anything left
    out can be named and written down.
    """
    crit = {
        "_constructor": "AdvancedCriteria", "operator": "and",
        "criteria": [
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


def summarize(s, url, pl_id, group_field=None):
    """The Calculate button. Returns rows with SKU Impact already summed.

    group_field is what the Summary screen's Group By is set to. attribute19 is
    Vendor Name; the 1st level hierarchy lives in another attribute, which is
    what find_hierarchy_field works out.
    """
    return post(s, url, "/pricelistmanager.summarize", {
        "data": {"query": {
            "objects": ["%s.PL" % pl_id],
            "productGroupBy": group_field or VENDOR_FIELD,
            "count": True,
            "projections": [{"weight": "null", "aggregationMode": mode,
                             "fieldName": field} for mode, field in PROJECTIONS],
        }}
    }, {"dataLocale": "en"})


def _norm(x):
    return "".join(ch for ch in str(x).lower() if ch.isalnum())


# The nine metric labels, normalised. Used to rule columns OUT when hunting for
# the vendor label: whatever carries the vendor name, it is not one of these.
METRIC_NAMES = set(_norm(label) for _, label in PROJECTIONS)


def find_key(row, *wanted):
    """Match a column by meaning, not by exact spelling.

    The capture showed what the browser SENDS but not what comes back, so the
    reply's key for SKU Impact might be 'SKU Impact', 'skuImpact' or
    'sum_SKU_Impact'. Rather than guess once and be wrong silently, look for any
    key whose letters match.
    """
    targets = [_norm(w) for w in wanted]
    for k in row:
        if _norm(k) in targets:
            return k
    for k in row:
        if any(t in _norm(k) for t in targets):
            return k
    return None


def looks_numeric(v):
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    try:
        float(str(v).replace(",", "").replace("$", "").strip())
        return True
    except (TypeError, ValueError):
        return False


def pick_vendor_key(rows):
    """Which key in the summarize rows carries the vendor name.

    By name first. If the reply calls it something I have not seen, fall back on
    the SHAPE of the value: a row is a vendor label plus nine totals, so the
    vendor is the only text in it that is not a number. That holds whatever the
    key is called, which guessing at names does not.

    Scored across ALL the rows, not just the first, because the first row is
    often the grand total and its vendor cell is blank - judging by that one row
    alone would rule the real vendor column out.
    """
    if isinstance(rows, dict):
        rows = [rows]
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return None

    k = find_key(rows[0], VENDOR_FIELD, "vendorName", "vendor name", "vendor",
                 "productAttribute19", "attribute19")
    if k:
        return k

    keys = []
    for r in rows:
        for k2 in r:
            if k2 not in keys:
                keys.append(k2)
    best, best_score = None, 0
    for k2 in keys:
        if _norm(k2) in METRIC_NAMES:
            continue
        score = 0
        for r in rows:
            v = r.get(k2)
            if isinstance(v, str) and v.strip() and not looks_numeric(v):
                score += 1
        if score > best_score:
            best, best_score = k2, score
    return best


# What a grand-total row calls itself, if it calls itself anything.
TOTAL_LABELS = set(["", "total", "grandtotal", "total", "all", "sum", "novendor"])


def drop_grand_total(rows):
    """Remove the summary row the reply carries alongside the vendors.

    The Summary reply returns a grand total as well as one row per vendor. Left
    in, it DOUBLES the annual impact and shows up in the vendor list as a
    phantom vendor whose number is the whole price list, sitting right next to
    the real biggest vendor and looking almost right.

    It is identified by what makes it a total - its value equals the sum of
    every other row - rather than by its label, which is blank here but need not
    stay that way.
    """
    if len(rows) < 2:
        return rows
    total = sum(v for _, v in rows)
    tol = max(1.0, abs(total) * 1e-6)
    candidates = [i for i, (_, v) in enumerate(rows)
                  if abs(v - (total - v)) <= tol]
    if not candidates:
        return rows
    labelled = [i for i in candidates if _norm(rows[i][0]) in TOTAL_LABELS]
    drop = labelled[0] if labelled else candidates[0]
    return [r for i, r in enumerate(rows) if i != drop]


def score_as_hierarchy(rows, field):
    """How many DISTINCT values in these rows are one of the 22 categories.

    This is what makes finding the hierarchy field a measurement rather than a
    guess: the category list is known exactly, so the right field is the one
    whose values ARE those categories. A wrong field scores zero.
    """
    known = set(cat_norm(c) for c in CATEGORIES)
    seen, hits = set(), set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        v = r.get(field)
        if v in (None, ""):
            continue
        n = cat_norm(v)
        seen.add(n)
        if n in known:
            hits.add(n)
    return len(hits), len(seen)


def score_as_plci(rows, field, known):
    """How many distinct values of this field are known PLCI codes."""
    seen, hits = set(), set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        v = r.get(field)
        if v in (None, ""):
            continue
        t = str(v).strip()
        if t.endswith(".0"):
            t = t[:-2]
        seen.add(t)
        if t in known:
            hits.add(t)
    return len(hits), len(seen)


def find_hierarchy_field(s, url, pl_id, candidates=None):
    """Work out which product attribute holds the 1st level hierarchy.

    Tries each candidate as the Summary screen's Group By and keeps the one
    whose returned labels match the known category list. Verifiable, so it can
    be trusted without anyone reading a dropdown to me.
    """
    if candidates is None:
        # VENDOR_FIELD is included deliberately as a positive control: it is
        # known to work, so if even that comes back empty the probe itself is
        # broken and "nothing matched" means nothing at all.
        # VENDOR_FIELD first as the positive control. Then every attribute up
        # to 80 - the first search stopped at 40 and found nothing even though
        # the Summary screen clearly groups by this field, so 1-40 is not where
        # it lives. Then names built from the label itself, in case it is not
        # an attribute at all.
        candidates = [VENDOR_FIELD]
        for i in range(1, 81):
            candidates.append("attribute%d" % i)
        for guess in HIERARCHY_NAME_GUESSES:
            candidates.append(guess)
        seen_c, uniq = set(), []
        for f in candidates:
            if f not in seen_c:
                seen_c.add(f)
                uniq.append(f)
        candidates = uniq
    known_plci = set(PLCI_PROBE)
    cats, plcis, populated, errors = [], [], [], 0
    for f in candidates:
        try:
            rows = rows_from(summarize(s, url, pl_id, f))
        except Exception:
            errors += 1
            continue
        hits, seen = score_as_hierarchy(rows, f)
        if seen:
            cats.append((hits, seen, f))
            sample = []
            for r in rows:
                v = r.get(f) if isinstance(r, dict) else None
                if v not in (None, "") and str(v) not in sample:
                    sample.append(str(v))
                if len(sample) == 3:
                    break
            populated.append((f, seen, sample))
        phits, pseen = score_as_plci(rows, f, known_plci)
        if pseen:
            plcis.append((phits, pseen, f))
    cats.sort(key=lambda x: (-x[0], x[1]))
    plcis.sort(key=lambda x: (-x[0], x[1]))
    return cats, plcis, populated, errors


def classify_plci(rows, stocked, nonstocked):
    """Stocked, Non-Stocked, or unspecified - decided by the PLCI codes present.

    His description: a normal price list is all one or all the other, while a
    Notable Cross Category list "comprise of part #'s that are stocked or
    non-stocked" - both at once. So a mixed list is not an awkward case to
    guess at, it is precisely the thing the cross-category row is for, and
    returning unspecified sends it there.

    Codes we do not recognise are ignored rather than counted as either, so an
    unknown code cannot silently flip a list into the wrong column.
    """
    saw_s = saw_n = False
    for code, amount in rows:
        c = str(code).strip()
        if c.endswith(".0"):
            c = c[:-2]
        if c in stocked:
            saw_s = True
        elif c in nonstocked:
            saw_n = True
    if saw_s and not saw_n:
        return "Stocked"
    if saw_n and not saw_s:
        return "Non-Stocked"
    return None if not (saw_s or saw_n) else "unspecified"


def cat_norm(x):
    """Normalise for matching. '&' becomes 'and' because the sheet writes
    'PLCs & HMIs' where the analyst types 'PLCs And HMIs'."""
    x = str(x).lower().replace("&", " and ")
    return "".join(ch for ch in x if ch.isalnum())


# Normalised category names, for deciding whether a hierarchy label the API
# returns is one of his 22 or something that belongs in the cross row.
KNOWN_CATS = set(cat_norm(c) for c in CATEGORIES)


def load_overrides():
    """price list id or name -> (category, stocked) decided by hand.

    The name rule gets most of them, but it cannot get all of them and it never
    will. A price list whose name says one category is sometimes filed under
    Notable Cross Category instead, because that is a judgement about what the
    work actually covered, not something a name can carry. This file is where
    that judgement lives.

    category_overrides.csv, next to the script:
        price_list,category,stock
        4321,Notable Cross Category,
        Vendor List V2,Notable Cross Category,
        PLCs And HMIs Stock and NonStock,PLCs & HMIs,Stocked
    """
    out = {}
    if not os.path.exists(OVERRIDES):
        return out
    import csv
    with open(OVERRIDES, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            key = cat_norm(row.get("price_list") or "")
            if not key:
                continue
            out[key] = ((row.get("category") or "").strip(),
                        (row.get("stock") or "").strip())
    return out


def category_of(pid, name, overrides):
    """Which 1st level category this price list belongs to, or None for cross.

    Longest match wins, so 'Industrial Data Communications' beats nothing and
    'Connectors' does not swallow a longer name that starts the same way.
    """
    for key in (cat_norm(pid), cat_norm(name)):
        if key in overrides and overrides[key][0]:
            got = overrides[key][0]
            return None if cat_norm(got) == cat_norm(CROSS) else got
    n = cat_norm(name)
    best, blen = None, -1
    for alias, canonical in CATEGORY_ALIASES.items():
        a = cat_norm(alias)
        if n.startswith(a) and len(a) > blen:
            best, blen = canonical, len(a)
    for c in CATEGORIES:
        cn = cat_norm(c)
        # tolerate singular/plural: 'Electronic Component - NonStocked' is
        # 'Electronic Components'
        for cand in set([cn, cn[:-1] if cn.endswith("s") else cn]):
            if cand and n.startswith(cand) and len(cand) > blen:
                best, blen = c, len(cand)
    return best


def stock_of(pid, name, overrides):
    """Stocked, Non-Stocked, or unspecified - read off the name."""
    for key in (cat_norm(pid), cat_norm(name)):
        if key in overrides and overrides[key][1]:
            return overrides[key][1]
    n = cat_norm(name)
    if "nonstock" in n:
        return "Non-Stocked"
    if "stock" in n:
        return "Stocked"
    return "unspecified"


def money_k(v):
    """The sheet's money format: $100k, $(250)k, $(1,000)k, $0k.

    Thousands, a bracketed loss rather than a signed one, and a thousands
    separator inside the brackets.
    """
    s = "{:,.0f}".format(abs(v) / 1000.0)
    return "$(%s)k" % s if v < 0 else "$%sk" % s


def thousands_cell(v):
    """Column G in thousands, accounting style: a loss is bracketed, not signed.

    A positive stays a real number so Excel can still sum and sort the column.
    A negative has to go out as text to carry the brackets - Excel reads
    "(261.7)" back as -261.7, which is the whole point of the notation.
    """
    k = round(abs(v) / 1000.0, 1)
    return "(%s)" % k if v < 0 else k


def thousands(v):
    """206488 -> '+$206k'.  -133187 -> '($133k)'.  Nearest thousand.

    Accounting notation, which is how he asked to read it: a rise carries the
    plus, a fall is bracketed rather than signed.
    """
    k = int(round(abs(v) / 1000.0))
    return "($%dk)" % k if v < 0 else "+$%dk" % k


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


def vendor_impacts(summary, key_hint=None):
    """[(label, sku_impact)] from one summarize reply.

    key_hint names the column to read the label from, for when the reply was
    grouped by something other than vendor.
    """
    rows = rows_from(summary)
    if not rows or not isinstance(rows[0], dict):
        return [], None
    sample = rows[0]
    vkey = key_hint if (key_hint and key_hint in sample) else pick_vendor_key(rows)
    ikey = find_key(sample, "SKU Impact", "skuImpact", "sum_SKU_Impact")
    if not ikey:
        return [], sample
    out = []
    for r in rows:
        try:
            val = float(r.get(ikey) or 0)
        except (TypeError, ValueError):
            continue
        name = r.get(vkey) if vkey else None
        name = str(name).strip() if name not in (None, "") else "(no vendor)"
        out.append((name, val))
    return drop_grand_total(out), None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", help="Sunday the week starts, YYYY-MM-DD")
    ap.add_argument("--month", nargs="?", const="", metavar="YYYY-MM",
                    help="a whole calendar month instead of a week. "
                         "Bare --month does the month that has just finished")
    ap.add_argument("--check", action="store_true",
                    help="prove login and filters work, write nothing")
    ap.add_argument("--find-hierarchy", nargs="?", const="", metavar="PRICELIST",
                    dest="find_hierarchy",
                    help="work out which attribute holds the 1st level "
                         "hierarchy, then stop. Optionally name a price list")
    a = ap.parse_args()
    if a.week and a.month is not None:
        sys.exit("Use --week or --month, not both.")
    if a.month:
        bits = a.month.strip().split("-")
        ok = len(bits) == 2 and bits[0].isdigit() and bits[1].isdigit()
        ok = ok and len(bits[0]) == 4 and 1 <= int(bits[1]) <= 12
        if not ok:
            sys.exit(
                "\n--month takes a year AND a month, like 2026-08.\n"
                "You gave: %s\n\n"
                "    --month 2026-08     August 2026\n"
                "    --month             the month that has just finished\n"
                % a.month)

    if a.find_hierarchy and not a.find_hierarchy.strip().isdigit():
        sys.exit(
            "\n'%s' is not a price list number.\n\n"
            "This is what it looks like when the hyphen in --find-hierarchy is "
            "lost\non the way into the terminal: --find hierarchy gets read as "
            "--find-hierarchy\nwith the word 'hierarchy' as the price list, so "
            "it searches something\nthat does not exist and reports finding "
            "nothing.\n\n"
            "Retype it with the hyphen, and give it a real price list number:\n"
            "    --find-hierarchy 4271\n" % a.find_hierarchy)

    print("pricefx_weekly %s" % VERSION)
    print("Running: %s" % os.path.abspath(__file__))

    cfg = load_config()
    if a.month is not None:
        start, end = month_bounds(a.month or None)
        label = "Month"
        if cfg["month_out_dir"]:
            cfg["out_dir"] = cfg["month_out_dir"]
    else:
        start, end = week_bounds(date.fromisoformat(a.week) if a.week else None)
        label = "Week"
    print("%s: %s to %s" % (label,
                            start.strftime("%a %d %b %Y %H:%M:%S"),
                            end.strftime("%a %d %b %Y %H:%M:%S")))

    s_sess, url = connect(cfg)
    print("Signed in to %s as %s" % (cfg["partition"], cfg["account"]))

    if a.find_hierarchy is not None:
        pid = a.find_hierarchy.strip()
        if not pid:
            some = fetch_price_lists(s_sess, url, start, end)
            if not some:
                sys.exit("No price lists in that window to test against. "
                         "Pass one: --find-hierarchy 4271")
            pid = some[0].get("id")
        print("\nTesting Group By against price list %s" % pid)
        print("Looking for the field whose values are your 22 categories.\n")
        cats, plcis, populated, errors = find_hierarchy_field(s_sess, url, pid)
        lines = []
        control = [p for p in populated if p[0] == VENDOR_FIELD]
        print("Tried %d fields: %d came back with values, %d were rejected."
              % (len(populated) + errors, len(populated), errors))
        if control:
            print("Positive control: %s (Vendor Name) returned %d values, "
                  "so the probe itself works.\n" % (VENDOR_FIELD, control[0][1]))
        else:
            print("Positive control FAILED: even %s returned nothing, so this "
                  "price list has no data to group by. Try another one.\n"
                  % VENDOR_FIELD)

        print("1st LEVEL HIERARCHY - looking for your 22 categories")
        if cats and cats[0][0] >= 2:
            for hits, seen, f in cats[:6]:
                mark = "  <-- this one" if hits == cats[0][0] else ""
                print("  %-16s %2d of %2d values are known categories%s"
                      % (f, hits, seen, mark))
            lines.append("hierarchy_field = %s" % cats[0][2])
        else:
            print("  nothing matched the category list")

        print("\nPLCI - looking for codes %s"
              % ", ".join(PLCI_PROBE))
        if plcis and plcis[0][0] >= 2:
            for hits, seen, f in plcis[:6]:
                mark = "  <-- this one" if hits == plcis[0][0] else ""
                print("  %-16s %2d of %2d values are known PLCI codes%s"
                      % (f, hits, seen, mark))
            lines.append("plci_field = %s" % plcis[0][2])
        else:
            print("  nothing matched the PLCI codes")

        print()
        if lines:
            print("Put these lines in pricefx_config.ini:")
            for l in lines:
                print("    %s" % l)
        else:
            print("Neither field was found on this price list. Try another:")
            print("    --find-hierarchy 4271")
        if populated:
            print("\nEvery field that DID return values, with a few examples.")
            print("If one of these is your 1st level hierarchy or your PLCI,")
            print("tell me its name - you do not need to send me the values.\n")
            for f, seen, sample in populated:
                print("  %-16s %3d values   e.g. %s"
                      % (f, seen, ", ".join(x[:28] for x in sample)))
        print("\nThis price list may simply not contain every category or code.")
        return

    print("Counting these workflow statuses: %s" % ", ".join(cfg["statuses"]))

    everything = fetch_price_lists(s_sess, url, start, end)
    wanted = set(_norm(x) for x in cfg["statuses"])
    pls = [p for p in everything if _norm(status_of(p)) in wanted]
    left_out = [p for p in everything if _norm(status_of(p)) not in wanted]

    print("Price lists submitted in the %s: %d"
          % (label.lower(), len(everything)))
    print("Counted: %d.  Left out: %d." % (len(pls), len(left_out)))
    if left_out:
        seen = sorted(set(status_of(p) or "(blank)" for p in left_out))
        print("Statuses left out: %s" % ", ".join(seen))

    if a.check:
        for p in pls[:5]:
            print("   %s  %s  submitted %s" % (p.get("id"), p.get("label"),
                                               p.get("submitDate")))
        if pls:
            v, unknown = vendor_impacts(summarize(s_sess, url, pls[0].get("id")))
            if unknown is not None:
                print("\nCould not find the SKU Impact column in the reply.")
                print("These are the columns it returned - tell me which one:")
                print("   " + ", ".join(sorted(unknown)))
            else:
                print("\nSKU Impact read back for %d vendors on price list %s."
                      % (len(v), pls[0].get("id")))
        print("\nCheck only - nothing written.")
        return

    overrides = load_overrides()
    report, unknown_cols, first_rows, entries = [], None, None, []
    for i, p in enumerate(pls, 1):
        pid = p.get("id")
        print("  [%d/%d] price list %s" % (i, len(pls), pid))
        summary = summarize(s_sess, url, pid)
        if first_rows is None:
            got = rows_from(summary)
            if got and isinstance(got[0], dict):
                first_rows = got
        vend, unknown = vendor_impacts(summary)
        if unknown is not None and unknown_cols is None:
            unknown_cols = unknown
        big = [(n, v) for n, v in vend if abs(v) >= cfg["threshold"]]
        big.sort(key=lambda x: -abs(x[1]))
        label = p.get("label")
        total = sum(v for _, v in vend)
        stock = stock_of(pid, label, overrides)
        stock_how = "name"
        if overrides.get(cat_norm(pid), ("", ""))[1] or \
                overrides.get(cat_norm(label), ("", ""))[1]:
            stock_how = "override"
        elif cfg["plci_field"]:
            by_code, _ = vendor_impacts(
                summarize(s_sess, url, pid, cfg["plci_field"]),
                key_hint=cfg["plci_field"])
            from_plci = classify_plci(by_code, cfg["stocked_plci"],
                                      cfg["nonstocked_plci"])
            if from_plci:
                if from_plci != stock and stock != "unspecified":
                    print("      note: name says %s, PLCI says %s - "
                          "going with PLCI" % (stock, from_plci))
                stock, stock_how = from_plci, "PLCI"
        forced = category_of(pid, label, overrides)
        if stock == "unspecified":
            # covers both stocked and non-stocked, or says neither - his
            # definition of Notable Cross Category
            parts = [(None, total)]
        elif cfg["hierarchy_field"]:
            # "go to each price list and check the sum impact by 1st level
            # hierarchy" - an All Stocked list spans several categories, so
            # ask PriceFx for the split instead of filing the lot under one.
            hier, _ = vendor_impacts(
                summarize(s_sess, url, pid, cfg["hierarchy_field"]),
                key_hint=cfg["hierarchy_field"])
            parts = [(c if cat_norm(c) in KNOWN_CATS else None, v)
                     for c, v in hier] or [(forced, total)]
        else:
            parts = [(forced, total)]
        entries.append((pid, label, total, parts, stock, stock_how))
        report.append({
            "Price List Number": pid,
            "Price List Name": p.get("label"),
            "Material/Source Count": p.get("numberOfItems"),
            "Missing Resale Price": "",
            "Created By": p.get("createdByName"),
            "Submitted": p.get("submitDate"),
            "Calculated Annual Impact (000s)": thousands_cell(
                sum(v for _, v in vend)),
            "Vendors over threshold": "; ".join(
                "%s %s" % (n, thousands(v)) for n, v in big),
        })

    if unknown_cols is not None:
        print("\nWARNING: SKU Impact could not be read on at least one price list, "
              "so those impacts are zero. Columns returned were:")
        print("   " + ", ".join(sorted(unknown_cols)))

    write_diagnostic(cfg, start, end, everything, pls, left_out, first_rows)
    write_report(report, start, end, cfg["out_dir"])
    write_category_summary(entries, start, end, cfg["out_dir"])


def build_category_summary(entries):
    """entries: [(pid, name, total_impact)] -> (rows, cross, grand_total).

    rows is [(category, summary string)] in the sheet's own order, so it can be
    pasted straight down column B. cross is the Notable Cross Category line.
    """
    buckets, cross_names, cross_total = {}, [], 0.0
    for pid, name, total, parts, stock, _how in entries:
        crossed = False
        for cat, amount in parts:
            if cat is None:
                cross_total += amount
                crossed = True
                continue
            b = buckets.setdefault(cat, {"Stocked": 0.0, "Non-Stocked": 0.0,
                                         "unspecified": 0.0})
            b[stock] += amount
        if crossed:
            cross_names.append(str(name))

    # EVERY category, in the sheet's own order, including the ones with no
    # activity. His sheet has a fixed row per category, so a short list would
    # paste out of alignment and quietly put figures against the wrong name.
    rows = []
    for c in CATEGORIES:
        b = buckets.get(c, {"Stocked": 0.0, "Non-Stocked": 0.0,
                            "unspecified": 0.0})
        if b["Stocked"] == 0 and b["Non-Stocked"] == 0 and b["unspecified"]:
            # his own convention for a category with no split - "Total - $20k"
            rows.append((c, "Total - %s" % money_k(b["unspecified"])))
            continue
        line = "Stocked %s, Non-Stocked - %s" % (money_k(b["Stocked"]),
                                                 money_k(b["Non-Stocked"]))
        if b["unspecified"]:
            line += ", Other - %s" % money_k(b["unspecified"])
        rows.append((c, line))

    cross = ""
    if cross_names:
        cross = "%s - $%s K" % (",".join(cross_names),
                                "{:,.0f}".format(abs(cross_total) / 1000.0))
        if cross_total < 0:
            cross = "%s - $(%s) K" % (",".join(cross_names),
                                      "{:,.0f}".format(abs(cross_total) / 1000.0))
    grand = sum(e[2] for e in entries)
    return rows, cross, grand


def write_category_summary(entries, start, end, out_dir):
    import csv
    os.makedirs(out_dir, exist_ok=True)
    rows, cross, grand = build_category_summary(entries)

    path = os.path.join(out_dir, "category_summary_%s_to_%s.csv"
                        % (start.date(), end.date()))
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Annual Impact = $%s K" % "{:,.0f}".format(grand / 1000.0), ""])
        w.writerow(["1st Level", "Summary"])
        for c, line in rows:
            w.writerow([c, line])
        if cross:
            w.writerow([])
            w.writerow([CROSS, cross])
    print("\nAnnual Impact = $%s K" % "{:,.0f}".format(grand / 1000.0))
    print("Category summary written to %s" % path)

    # Every price list and where it landed. Without this the attribution is
    # invisible, and a price list in the wrong category is the kind of thing
    # nobody notices in a summary.
    apath = os.path.join(out_dir, "category_assignment_%s_to_%s.csv"
                         % (start.date(), end.date()))
    with open(apath, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Price List Number", "Price List Name", "Impact (000s)",
                    "1st Level", "Stocked / Non-Stocked", "How it was decided"])
        for pid, name, total, parts, stock, how in entries:
            for cat, amount in parts:
                cat_how = ("1st level hierarchy" if len(parts) > 1
                           else ("name" if cat
                                 else "no category in the name"))
                w.writerow([pid, name, round(amount / 1000.0, 1),
                            cat or CROSS, stock,
                            "category by %s, stocked by %s" % (cat_how, how)])
    print("Where each price list landed: %s" % apath)
    return path


def write_diagnostic(cfg, start, end, everything, kept, left_out, sum_rows):
    """One file that answers every question I would otherwise have to ask.

    Deliberately carries NO vendor names and NO money: price list ids, workflow
    statuses, and the NAMES and TYPES of the reply's columns. That is enough to
    diagnose anything seen so far, and nothing in it is commercial, so it can be
    sent on without a second thought.

    It exists because three rounds of screenshots were spent on questions this
    file answers in one run.
    """
    out_dir = cfg["out_dir"]
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "diagnostic.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("pricefx_weekly %s\n" % VERSION)
        fh.write("script: %s\n" % os.path.abspath(__file__))
        fh.write("week:   %s to %s\n" % (start, end))
        fh.write("counting statuses: %s\n\n" % ", ".join(cfg["statuses"]))

        fh.write("PRICE LISTS THE WEEK FETCH RETURNED: %d\n" % len(everything))
        fh.write("  counted %d, left out %d\n\n" % (len(kept), len(left_out)))

        tally = {}
        for p in everything:
            st = status_of(p) or "(no status field found)"
            tally[st] = tally.get(st, 0) + 1
        fh.write("Every status seen this week, and how many had it:\n")
        for st in sorted(tally):
            fh.write("  %-34s %d\n" % (st, tally[st]))

        fh.write("\nEvery price list id returned, with its status and whether\n"
                 "it was counted. If an id you expected is NOT in this list at\n"
                 "all, the week filter never saw it and the problem is the\n"
                 "submitted date, not the status.\n\n")
        keptids = set(id(x) for x in kept)
        for p in sorted(everything, key=lambda r: str(r.get("id"))):
            fh.write("  %-8s %-24s %s\n" % (
                p.get("id"),
                (status_of(p) or "(blank)")[:24],
                "counted" if id(p) in keptids else "LEFT OUT"))

        fh.write("\nFIELDS ON A PRICE LIST ROW (names and types only):\n")
        if everything:
            for k in sorted(everything[0]):
                fh.write("  %-34s %s\n" % (k, type(everything[0][k]).__name__))

        fh.write("\nTHE SUMMARY REPLY for the first price list:\n")
        if not sum_rows:
            fh.write("  no rows came back at all\n")
        else:
            chosen = pick_vendor_key(sum_rows)
            fh.write("  rows returned: %d\n" % len(sum_rows))
            fh.write("  vendor name read from: %s\n" %
                     (chosen or "NOTHING - no column matched"))
            if chosen:
                blank = sum(1 for r in sum_rows
                            if r.get(chosen) in (None, "")
                            or not str(r.get(chosen)).strip())
                fh.write("  rows with that column blank: %d "
                         "(a grand total row shows up here)\n" % blank)
            keys = []
            for r in sum_rows:
                for k in r:
                    if k not in keys:
                        keys.append(k)
            fh.write("\n  columns:\n")
            for k in keys:
                kinds = sorted(set(
                    "number" if looks_numeric(r.get(k)) else type(r.get(k)).__name__
                    for r in sum_rows))
                fh.write("    %-34s %s%s\n" % (
                    k, "/".join(kinds),
                    "   <- used as the vendor name" if k == chosen else ""))
    print("Diagnostic written to %s" % path)


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
