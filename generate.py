#!/usr/bin/env python3
"""
SoulSensei Revenue Dashboard — auto-generator
Reads Google Sheets, computes totals, renders index.html from template.html
"""

import json
import os
from collections import defaultdict
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials
from jinja2 import Environment, FileSystemLoader

# ── Sheet IDs ──────────────────────────────────────────────────────────────────
# Both EE Web and EE App live in the same Google Sheets file
EE_SHEET_ID     = "1avlg2CPbTLnYewuAOrOxDJzqDNkYoQD9QAxpCx_QBbA"
EE_WEB_TAB      = "Product Working - Revenue"
EE_APP_TAB      = "EE_Payment_Revenue"

DG_SHEET_ID     = "1y6CGuNwr7RtdHXBImBfYzBG0QnVn4CRWjStf8H9IeVI"
DG_TAB          = "Digital_Payments_Revenue"

DG_DEFAULT_COLORS = ["#5A4A8A","#2D7A6B","#D05A3A","#C47A2B","#B5456A","#7A7570","#4A7A9A","#8B5E3C","#2D6A8A","#6A8A4A"]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]  # read + write

MONTH_KEYS   = ["jan", "feb", "mar", "apr", "may", "jun",
                "jul", "aug", "sep", "oct", "nov", "dec"]
MONTH_LABELS = ["January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"]


def active_months():
    """Returns list of (key, label) from January up to and including current month."""
    n = datetime.now().month  # 1-based
    return list(zip(MONTH_KEYS[:n], MONTH_LABELS[:n]))


# ── Auth ───────────────────────────────────────────────────────────────────────
def get_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        info = json.loads(creds_json)
    else:
        # Local dev: place your service-account key as credentials.json
        with open("credentials.json") as f:
            info = json.load(f)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


# ── Helpers ────────────────────────────────────────────────────────────────────
def parse_month(date_str: str) -> str | None:
    """'16/01/2026, 12:00:00'  →  'jan'"""
    for fmt in ("%d/%m/%Y, %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%b").lower()
        except ValueError:
            continue
    return None


def parse_month_week(date_str: str):
    """Returns (month_key, week_num) e.g. ('apr', 1) for Apr 1–7."""
    for fmt in ("%d/%m/%Y, %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.strftime("%b").lower(), (dt.day - 1) // 7 + 1
        except ValueError:
            continue
    return None, None


def fmt_inr(n: int) -> str:
    """Format integer in Indian Rupee notation: 123456 → ₹1,23,456"""
    if n == 0:
        return "₹0"
    s = str(abs(int(n)))
    if len(s) <= 3:
        return f"₹{s}"
    last3 = s[-3:]
    s = s[:-3]
    groups = []
    while len(s) > 2:
        groups.append(s[-2:])
        s = s[:-2]
    if s:
        groups.append(s)
    groups.reverse()
    return "₹" + ",".join(groups) + "," + last3


def fmt_num(n: int) -> str:
    """Format with commas: 2442 → 2,442"""
    return f"{n:,}"


# ── Sheet readers ──────────────────────────────────────────────────────────────
def read_ee_web(gc):
    """
    Reads EE Web sheet, computes monthly totals, marks processed rows.
    Uses get_all_values() so row numbers are always exact (no empty-row drift).
    Returns:
        captured  – {month: total_amount}
        fail_amt  – {month: total_failed_amount}
        fail_cnt  – {month: count_of_failed_txns}
    """
    ws = gc.open_by_key(EE_SHEET_ID).worksheet(EE_WEB_TAB)
    all_values = ws.get_all_values()  # row i → sheet row i+1 (exact, no skipping)

    if len(all_values) < 2:
        return {}, {}, {}, {}

    headers = all_values[0]
    print(f"  Headers found: {headers}")
    print(f"  Total rows in sheet (incl. header): {len(all_values)}")

    def col_idx(name):
        return headers.index(name) if name in headers else None

    date_idx    = col_idx("Session Date")
    amount_idx  = col_idx("Amount")
    status_idx  = col_idx("Status")
    revenue_idx = col_idx("Revenue")
    col_letter  = chr(ord("A") + revenue_idx) if revenue_idx is not None else None

    print(f"  Revenue col index (0-based): {revenue_idx}, letter: {col_letter}")

    # Debug: show what row 245 looks like
    if len(all_values) >= 245:
        row245 = all_values[244]  # 0-based, row 245 = index 244
        print(f"  Row 245 raw values: {row245}")

    captured = defaultdict(int)
    fail_amt = defaultdict(int)
    fail_cnt = defaultdict(int)
    weekly   = defaultdict(int)   # (month, week_num) → amount
    rows_to_mark = []

    for i, row_vals in enumerate(all_values[1:], start=2):  # start=2 → real sheet row
        # Pad short rows
        while len(row_vals) < len(headers):
            row_vals.append("")

        if date_idx is None:
            continue
        month = parse_month(row_vals[date_idx])
        if not month:
            continue
        _, week_num = parse_month_week(row_vals[date_idx])

        try:
            amount = int(float(row_vals[amount_idx] or 0))
        except (ValueError, TypeError):
            continue

        status         = row_vals[status_idx].strip().lower() if status_idx is not None else ""
        already_marked = revenue_idx is not None and row_vals[revenue_idx].strip() == "Calculated"

        if status == "captured":
            captured[month] += amount
            if week_num:
                weekly[(month, week_num)] += amount
        elif status == "failed":
            fail_amt[month] += amount
            fail_cnt[month] += 1

        if status in ("captured", "failed") and not already_marked and col_letter:
            rows_to_mark.append(i)

    if rows_to_mark and col_letter:
        updates = [{"range": f"{col_letter}{r}", "values": [["Calculated"]]}
                   for r in rows_to_mark]
        ws.batch_update(updates)
        print(f"  Marked {len(rows_to_mark)} new rows as Calculated")

    return dict(captured), dict(fail_amt), dict(fail_cnt), dict(weekly)


def read_ee_app(gc):
    """
    Reads EE App sheet. Only processes NEW rows (Revenue column empty).
    Adds new amounts to base values from config.json to avoid double-counting.
    Returns:
        new_captured  – {month: new_amount_to_add}
        new_fail_amt  – {month: new_failed_amount_to_add}
        new_fail_cnt  – {month: new_failed_count_to_add}
    """
    ws = gc.open_by_key(EE_SHEET_ID).worksheet(EE_APP_TAB)
    all_values = ws.get_all_values()

    if len(all_values) < 2:
        return {}, {}, {}, {}

    headers = all_values[0]
    print(f"  EE App headers: {headers}")
    print(f"  EE App total rows (incl. header): {len(all_values)}")

    def col_idx(name):
        return headers.index(name) if name in headers else None

    date_idx    = col_idx("Date")
    amount_idx  = col_idx("Amount")
    status_idx  = col_idx("Status")
    revenue_idx = col_idx("Revenue")
    col_letter  = chr(ord("A") + revenue_idx) if revenue_idx is not None else None

    new_captured = defaultdict(int)
    new_fail_amt = defaultdict(int)
    new_fail_cnt = defaultdict(int)
    all_weekly   = defaultdict(int)   # ALL rows (month, week_num) → amount
    rows_to_mark = []

    for sheet_row, row_vals in enumerate(all_values[1:], start=2):
        while len(row_vals) < len(headers):
            row_vals.append("")

        if date_idx is None:
            continue
        month = parse_month(row_vals[date_idx])
        if not month:
            continue
        _, week_num = parse_month_week(row_vals[date_idx])

        try:
            amount = int(float(row_vals[amount_idx] or 0))
        except (ValueError, TypeError):
            continue

        status         = row_vals[status_idx].strip().lower() if status_idx is not None else ""
        already_marked = revenue_idx is not None and row_vals[revenue_idx].strip() == "Calculated"

        # Weekly from ALL rows (regardless of Calculated) for the WoW tab
        if status == "captured" and week_num:
            all_weekly[(month, week_num)] += amount

        # Only count NEW rows (not yet marked) for monthly base
        if not already_marked:
            if status == "captured":
                new_captured[month] += amount
            elif status == "failed":
                new_fail_amt[month] += amount
                new_fail_cnt[month] += 1

            if status in ("captured", "failed") and col_letter:
                rows_to_mark.append(sheet_row)

    if rows_to_mark and col_letter:
        updates = [{"range": f"{col_letter}{r}", "values": [["Calculated"]]}
                   for r in rows_to_mark]
        ws.batch_update(updates)
        print(f"  EE App: marked {len(rows_to_mark)} new rows as Calculated")
    else:
        print("  EE App: no new rows to mark")

    return dict(new_captured), dict(new_fail_amt), dict(new_fail_cnt), dict(all_weekly)


def read_dg(gc):
    """
    Reads Digital Goods sheet.
    - new_by_month: only uncalculated rows → added to config base for totals
    - breakdown: ALL captured rows grouped by (seller, price) → for table + chart
    Marks new rows as Calculated.
    """
    ws = gc.open_by_key(DG_SHEET_ID).worksheet(DG_TAB)
    all_values = ws.get_all_values()

    if len(all_values) < 2:
        return {}, {}, {}

    headers = all_values[0]
    print(f"  DG headers: {headers}")
    print(f"  DG total rows (incl. header): {len(all_values)}")

    def col_idx(name):
        return headers.index(name) if name in headers else None

    seller_idx  = col_idx("Resource Value")
    amount_idx  = col_idx("Amount")
    date_idx    = col_idx("Date")
    status_idx  = col_idx("Status")
    revenue_idx = col_idx("Revenue")
    col_letter  = chr(ord("A") + revenue_idx) if revenue_idx is not None else None

    new_by_month = defaultdict(int)
    breakdown    = {}   # {(seller, price): {txns, revenue, first_month_idx, first_month, monthly}}
    all_weekly   = defaultdict(int)   # ALL rows (month, week_num) → amount
    rows_to_mark = []

    for sheet_row, row_vals in enumerate(all_values[1:], start=2):
        while len(row_vals) < len(headers):
            row_vals.append("")

        seller = row_vals[seller_idx].strip() if seller_idx is not None else ""
        if not seller:
            continue

        month = parse_month(row_vals[date_idx]) if date_idx is not None else None
        if not month:
            continue
        _, week_num = parse_month_week(row_vals[date_idx]) if date_idx is not None else (None, None)

        try:
            amount = int(float(row_vals[amount_idx] or 0))
        except (ValueError, TypeError):
            continue

        status         = row_vals[status_idx].strip().lower() if status_idx is not None else ""
        already_marked = revenue_idx is not None and row_vals[revenue_idx].strip() == "Calculated"

        # Weekly from ALL rows (regardless of Calculated) for the WoW tab
        if status == "captured" and week_num:
            all_weekly[(month, week_num)] += amount

        if status == "captured" and not already_marked:
            key = (seller, amount)
            m_idx = MONTH_KEYS.index(month) if month in MONTH_KEYS else 99

            if key not in breakdown:
                breakdown[key] = {
                    "seller": seller, "price": amount,
                    "txns": 0, "revenue": 0,
                    "first_month_idx": m_idx, "first_month": month,
                    "monthly": defaultdict(int),
                }
            else:
                if m_idx < breakdown[key]["first_month_idx"]:
                    breakdown[key]["first_month_idx"] = m_idx
                    breakdown[key]["first_month"] = month

            breakdown[key]["txns"]    += 1
            breakdown[key]["revenue"] += amount
            breakdown[key]["monthly"][month] += amount
            new_by_month[month] += amount

            if col_letter:
                rows_to_mark.append(sheet_row)

    if rows_to_mark and col_letter:
        updates = [{"range": f"{col_letter}{r}", "values": [["Calculated"]]}
                   for r in rows_to_mark]
        ws.batch_update(updates)
        print(f"  DG: marked {len(rows_to_mark)} new rows as Calculated")
    else:
        print("  DG: no new rows to mark")

    return dict(new_by_month), breakdown, dict(all_weekly)


def write_ee_snapshot(gc, ee_months):
    """
    Appends (or updates) a daily row in the 'Daily EE Snapshot' subsheet.
    Columns: Date | Jan-Web | Jan-App | Feb-Web | Feb-App | ...
    Auto-extends headers when a new month becomes active.
    """
    ws = gc.open_by_key(EE_SHEET_ID).worksheet("Daily EE Snapshot")

    # Build expected headers
    expected_headers = ["Date"]
    for m in ee_months:
        label = m["key"].capitalize()  # "Jan", "Feb", etc.
        expected_headers += [f"{label}-Web", f"{label}-App"]

    # Build today's data row
    today = datetime.now().strftime("%d/%m/%Y")
    data_row = [today] + [val for m in ee_months for val in (m["web_raw"], m["app_raw"])]

    existing = ws.get_all_values()

    # Write or update headers if needed
    if not existing or existing[0] != expected_headers:
        ws.update([expected_headers], range_name="A1")
        existing_data = existing[1:] if existing else []
    else:
        existing_data = existing[1:] if len(existing) > 1 else []

    # Check if today already has a row → update it, else append
    existing_dates = [r[0] for r in existing_data]
    if today in existing_dates:
        row_idx = existing_dates.index(today) + 2  # +1 for header, +1 for 1-based
        ws.update([data_row], range_name=f"A{row_idx}")
        print(f"  EE Snapshot: updated row for {today}")
    else:
        ws.append_row(data_row, value_input_option="RAW")
        print(f"  EE Snapshot: appended row for {today}")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    gc = get_client()

    # ── EE Web (from sheet — full recompute) ───────────────────────────────────
    print("Reading EE Web sheet…")
    web_cap, web_fail_amt, web_fail_cnt, ee_web_weekly = read_ee_web(gc)

    # ── EE App (base from config + new rows from sheet) ────────────────────────
    print("Reading EE App sheet…")
    app_new_cap, app_new_fail_amt, app_new_fail_cnt, ee_app_weekly = read_ee_app(gc)

    # ── Config (manual data) ───────────────────────────────────────────────────
    with open("config.json") as f:
        cfg = json.load(f)

    # EE App: base (already on dashboard) + new rows from sheet
    app_base     = cfg["ee_app_captured"]
    app_cap      = {m: app_base.get(m, 0) + app_new_cap.get(m, 0) for m in set(list(app_base.keys()) + list(app_new_cap.keys()))}

    visitors  = cfg["visitors"]
    oo_cfg    = cfg["oo"]

    # ── Build EE monthly rows (dynamic — auto-extends each new month) ──────────
    months = active_months()  # e.g. [("jan","January"), ..., ("mar","March")]
    current_key = months[-1][0]

    ee_months = []
    for key, label in months:
        w = web_cap.get(key, 0)
        a = app_cap.get(key, 0)
        ee_months.append({
            "key":       key,
            "label":     label + (" (MTD)" if key == current_key else ""),
            "web_raw":   w,
            "app_raw":   a,
            "total_raw": w + a,
            "web":       fmt_inr(w),
            "app":       fmt_inr(a),
            "total":     fmt_inr(w + a),
        })

    ee_grand_raw   = sum(m["total_raw"] for m in ee_months)
    ee_grand_total = fmt_inr(ee_grand_raw)

    # Chart data
    ee_chart_labels = [m["label"] for m in ee_months]
    ee_chart_web    = [m["web_raw"] for m in ee_months]
    ee_chart_app    = [m["app_raw"] for m in ee_months]


    # ── DG (from sheet) ────────────────────────────────────────────────────────
    print("Reading DG sheet…")
    dg_new_by_month, dg_breakdown, dg_weekly = read_dg(gc)

    # ── Daily EE Snapshot ─────────────────────────────────────────────────────
    print("Writing EE snapshot…")
    write_ee_snapshot(gc, ee_months)

    # ── Visitors ───────────────────────────────────────────────────────────────
    feb_v = visitors["feb_web"] + visitors["feb_app"]
    mar_v = visitors["mar_web"] + visitors["mar_app"]
    mar_app_pct = round(visitors["mar_app"] / mar_v * 100) if mar_v else 0

    vis = {
        "feb_total": fmt_num(feb_v),
        "feb_web":   fmt_num(visitors["feb_web"]),
        "feb_app":   fmt_num(visitors["feb_app"]),
        "mar_total": fmt_num(mar_v),
        "mar_web":   fmt_num(visitors["mar_web"]),
        "mar_app":   fmt_num(visitors["mar_app"]),
        "total_uniques": fmt_num(visitors["total_uniques"]),
        "mar_app_share": f"{mar_app_pct}%",
        "chart_web": [visitors["feb_web"], visitors["mar_web"]],
        "chart_app": [visitors["feb_app"], visitors["mar_app"]],
    }

    # ── DG ────────────────────────────────────────────────────────────────────
    dg_base          = cfg["dg_base"]
    dg_sellers_base  = cfg["dg_sellers_base"]
    dg_chart_base    = cfg["dg_chart_base"]
    seller_colors    = cfg.get("dg_seller_colors", {})
    active_keys      = [k for k, _ in months]

    # Monthly totals: base + new delta from sheet
    dg_monthly     = {key: dg_base.get(key, 0) + dg_new_by_month.get(key, 0) for key in active_keys}
    dg_grand_total = sum(dg_monthly.values())

    # Breakdown table: start from config base, merge in new sheet rows
    merged_sellers = {(s["name"], s["price"]): dict(s) for s in dg_sellers_base}
    for (seller, price), v in dg_breakdown.items():
        key = (seller, price)
        if key in merged_sellers:
            merged_sellers[key]["txns"]    += v["txns"]
            merged_sellers[key]["revenue"] += v["revenue"]
        else:
            merged_sellers[key] = {
                "name": seller, "price": price,
                "txns": v["txns"], "revenue": v["revenue"],
                "month": v["first_month"],
            }

    dg_sellers = sorted(
        [
            {
                "name":        v["name"],
                "price_fmt":   fmt_inr(v["price"]),
                "txns":        v["txns"],
                "revenue":     v["revenue"],
                "revenue_fmt": fmt_inr(v["revenue"]),
                "month":       v["month"],
                "month_label": v["month"].capitalize(),
            }
            for v in merged_sellers.values()
        ],
        key=lambda x: -x["revenue"]
    )

    # Chart datasets: config base arrays + new monthly delta per seller group
    # Config base uses first-name keys ("Vidhi", "Swapnil" etc.)
    chart_data = {name: list(arr) for name, arr in dg_chart_base.items()}

    # Extend arrays if new months beyond config's 3 columns
    for name in chart_data:
        while len(chart_data[name]) < len(active_keys):
            chart_data[name].append(0)

    # Add new sheet rows to matching chart group (match by first word)
    for (seller, price), v in dg_breakdown.items():
        group = seller.split()[0]  # e.g. "Swapnil" from "Swapnil (OM)"
        if group not in chart_data:
            chart_data[group] = [0] * len(active_keys)
        for mkey, rev in v["monthly"].items():
            if mkey in active_keys:
                chart_data[group][active_keys.index(mkey)] += rev

    color_idx = 0
    dg_chart_datasets = []
    for name, data in sorted(chart_data.items(), key=lambda x: -sum(x[1])):
        color = seller_colors.get(name, DG_DEFAULT_COLORS[color_idx % len(DG_DEFAULT_COLORS)])
        color_idx += 1
        dg_chart_datasets.append({"label": name, "color": color, "data": data})

    dg = {
        "months":         [{"label": label + (" (MTD)" if key == current_key else ""),
                            "total": fmt_inr(dg_monthly.get(key, 0))} for key, label in months],
        "total":          fmt_inr(dg_grand_total),
        "sellers":        dg_sellers,
        "chart_datasets": dg_chart_datasets,
    }

    # ── OO ────────────────────────────────────────────────────────────────────
    oo_total_raw = oo_cfg["feb_total"] + oo_cfg["mar_total"]
    oo = {
        "feb_total": fmt_inr(oo_cfg["feb_total"]),
        "mar_total": fmt_inr(oo_cfg["mar_total"]),
        "total":     fmt_inr(oo_total_raw),
        "feb_sub":   oo_cfg["feb_sub"],
        "mar_sub":   oo_cfg["mar_sub"],
        "practitioners": [
            {
                **p,
                "price_fmt":   fmt_inr(p["price"]),
                "revenue_fmt": fmt_inr(p["revenue"]),
                "month_label": p["month"].capitalize(),
            }
            for p in oo_cfg["practitioners"]
        ],
        "chart": oo_cfg["chart"],
    }

    # ── Week on Week (April 2026) ──────────────────────────────────────────────
    now = datetime.now()
    APR_WEEKS = [
        (1, "W1", "Apr 1–7"),
        (2, "W2", "Apr 8–14"),
        (3, "W3", "Apr 15–21"),
        (4, "W4", "Apr 22–28"),
    ]
    current_week_num = (now.day - 1) // 7 + 1 if now.month == 4 else None

    wow_weeks = []
    for w_num, w_label, w_range in APR_WEEKS:
        ee_w  = ee_web_weekly.get(("apr", w_num), 0)
        ea_w  = ee_app_weekly.get(("apr", w_num), 0)
        dg_w  = dg_weekly.get(("apr", w_num), 0)
        ee_total = ee_w + ea_w
        is_cur = (w_num == current_week_num)
        wow_weeks.append({
            "label":         w_label,
            "range":         w_range,
            "ee_web":        ee_w,
            "ee_app":        ea_w,
            "ee_total":      ee_total,
            "ee_total_fmt":  fmt_inr(ee_total),
            "ee_web_fmt":    fmt_inr(ee_w),
            "ee_app_fmt":    fmt_inr(ea_w),
            "dg":            dg_w,
            "dg_fmt":        fmt_inr(dg_w),
            "is_current":    is_cur,
        })

    wow = {
        "weeks":       wow_weeks,
        "ee_web_data": [w["ee_web"]  for w in wow_weeks],
        "ee_app_data": [w["ee_app"]  for w in wow_weeks],
        "dg_data":     [w["dg"]      for w in wow_weeks],
        "labels":      [w["label"]   for w in wow_weeks],
    }

    # ── Render ─────────────────────────────────────────────────────────────────
    env = Environment(loader=FileSystemLoader("."), autoescape=False)
    tpl = env.get_template("template.html")

    output = tpl.render(
        generated_date=now.strftime("%-d %b %Y"),
        ee_months=ee_months,
        ee_grand_total=ee_grand_total,
        ee_chart_labels=ee_chart_labels,
        ee_chart_web=ee_chart_web,
        ee_chart_app=ee_chart_app,
        vis=vis,
        dg=dg,
        oo=oo,
        wow=wow,
    )

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(output)

    # ── Persist updated bases back to config.json ──────────────────────────────
    # This ensures the next run starts from the correct accumulated totals,
    # not the original hardcoded values.
    cfg_changed = False

    for m, delta in app_new_cap.items():
        if delta > 0:
            cfg["ee_app_captured"][m] = cfg["ee_app_captured"].get(m, 0) + delta
            cfg_changed = True

    for m, delta in dg_new_by_month.items():
        if delta > 0:
            cfg["dg_base"][m] = cfg["dg_base"].get(m, 0) + delta
            cfg_changed = True

    if dg_breakdown:
        cfg["dg_sellers_base"] = [
            {"name": v["name"], "price": v["price"],
             "txns": v["txns"], "revenue": v["revenue"], "month": v["month"]}
            for v in merged_sellers.values()
        ]
        cfg_changed = True

    if cfg_changed:
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        print("✓ config.json bases updated")

    print(f"✓ index.html updated — {now.strftime('%d %b %Y, %H:%M')}")


if __name__ == "__main__":
    main()
