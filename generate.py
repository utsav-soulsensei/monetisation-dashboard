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
EE_WEB_SHEET_ID = "1avlg2CPbTLnYewuAOrOxDJzqDNkYoQD9QAxpCx_QBbA"
# Add EE App and DG sheet IDs here when ready:
# EE_APP_SHEET_ID = ""
# DG_SHEET_ID     = ""

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]  # read + write

MONTH_ORDER = ["jan", "feb", "mar", "apr", "may", "jun",
               "jul", "aug", "sep", "oct", "nov", "dec"]


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
    ws = gc.open_by_key(EE_WEB_SHEET_ID).sheet1
    all_values = ws.get_all_values()  # row i → sheet row i+1 (exact, no skipping)

    if len(all_values) < 2:
        return {}, {}, {}

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

        try:
            amount = int(float(row_vals[amount_idx] or 0))
        except (ValueError, TypeError):
            continue

        status         = row_vals[status_idx].strip().lower() if status_idx is not None else ""
        already_marked = revenue_idx is not None and row_vals[revenue_idx].strip() == "Calculated"

        if status == "captured":
            captured[month] += amount
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

    return dict(captured), dict(fail_amt), dict(fail_cnt)


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    gc = get_client()

    # ── EE Web (from sheet) ────────────────────────────────────────────────────
    print("Reading EE Web sheet…")
    web_cap, web_fail_amt, web_fail_cnt = read_ee_web(gc)

    # ── Config (manual data) ───────────────────────────────────────────────────
    with open("config.json") as f:
        cfg = json.load(f)

    app_cap   = cfg["ee_app_captured"]
    app_fail  = cfg["ee_app_failed"]
    visitors  = cfg["visitors"]
    dg_cfg    = cfg["dg"]
    oo_cfg    = cfg["oo"]

    # ── Build EE monthly rows ──────────────────────────────────────────────────
    months = ["jan", "feb", "mar"]
    ee = {}
    for m in months:
        w = web_cap.get(m, 0)
        a = app_cap.get(m, 0)
        ee[m] = {
            "web_raw": w,
            "app_raw": a,
            "total_raw": w + a,
            "web":   fmt_inr(w),
            "app":   fmt_inr(a),
            "total": fmt_inr(w + a),
        }

    ee_grand_raw   = sum(ee[m]["total_raw"] for m in months)
    ee_grand_total = fmt_inr(ee_grand_raw)

    # ── EE failed ─────────────────────────────────────────────────────────────
    ee_failed = {}
    for m in months:
        wa = web_fail_amt.get(m, 0)
        wc = web_fail_cnt.get(m, 0)
        aa = app_fail.get(m, {}).get("amount", 0)
        ac = app_fail.get(m, {}).get("count", 0)
        ee_failed[m] = {
            "web_amount":  fmt_inr(wa),
            "web_count":   wc,
            "app_amount":  fmt_inr(aa),
            "app_count":   ac,
            "total_amount": fmt_inr(wa + aa),
            "total_raw":    wa + aa,
            "has_app":      ac > 0,
        }

    ee_failed_total_raw = sum(ee_failed[m]["total_raw"] for m in months)
    ee_failed_total     = fmt_inr(ee_failed_total_raw)

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
    dg_total_raw = dg_cfg["feb_total"] + dg_cfg["mar_total"]
    dg = {
        "feb_total": fmt_inr(dg_cfg["feb_total"]),
        "mar_total": fmt_inr(dg_cfg["mar_total"]),
        "total":     fmt_inr(dg_total_raw),
        "sellers": [
            {
                **s,
                "price_fmt":   fmt_inr(s["price"]),
                "revenue_fmt": fmt_inr(s["revenue"]),
                "month_label": s["month"].capitalize(),
            }
            for s in dg_cfg["sellers"]
        ],
        "chart": dg_cfg["chart"],
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
        "failed": [
            {**f, "amount_fmt": fmt_inr(f["amount"])}
            for f in oo_cfg["failed"]
        ],
        "failed_total": fmt_inr(oo_cfg["failed_total"]),
    }

    # ── Render ─────────────────────────────────────────────────────────────────
    env = Environment(loader=FileSystemLoader("."), autoescape=False)
    tpl = env.get_template("template.html")

    now = datetime.now()
    output = tpl.render(
        generated_date=now.strftime("%-d %b %Y"),
        ee=ee,
        ee_grand_total=ee_grand_total,
        ee_failed=ee_failed,
        ee_failed_total=ee_failed_total,
        vis=vis,
        dg=dg,
        oo=oo,
    )

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(output)

    print(f"✓ index.html updated — {now.strftime('%d %b %Y, %H:%M')}")


if __name__ == "__main__":
    main()
