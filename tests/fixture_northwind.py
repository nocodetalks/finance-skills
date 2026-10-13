"""Northwind SaaS, September 2026: a small synthetic company for the tests.

Every number here is made up. The base company plants the 15 mistakes from the brief.
`build(root, scenario=fn)` lets a test add transactions, statement lines, Stripe rows, bills,
invoices or config changes on top, to plant one more realistic situation.

build() writes a ready-to-run review folder: root/review-config.json and root/2026-09/inputs/...
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal as D
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "month-end-review" / "skills" / "month-end-review" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from mer import config as mconfig  # noqa: E402
from mer.util import write_csv, write_json  # noqa: E402

ACCOUNTS = [
    # id, full_name, type, subtype, classification
    ("1", "Mercury Checking", "Bank", "Checking", "Asset"),
    ("2", "Mercury Treasury", "Bank", "Savings", "Asset"),
    ("3", "Brex Card", "Credit Card", "CreditCard", "Liability"),
    ("4", "Accounts Receivable (A/R)", "Accounts Receivable", "AccountsReceivable", "Asset"),
    ("5", "Prepaid Expenses", "Other Current Asset", "PrepaidExpenses", "Asset"),
    ("6", "Accounts Payable (A/P)", "Accounts Payable", "AccountsPayable", "Liability"),
    ("7", "Accrued Liabilities", "Other Current Liability", "AccruedLiabilities", "Liability"),
    ("8", "Deferred Revenue", "Other Current Liability", "OtherCurrentLiabilities", "Liability"),
    ("9", "Retained Earnings", "Equity", "RetainedEarnings", "Equity"),
    ("10", "Preferred Stock", "Equity", "PreferredStock", "Equity"),
    ("11", "Computer Equipment", "Fixed Asset", "MachineryAndEquipment", "Asset"),
    ("12", "Accumulated Depreciation", "Fixed Asset", "AccumulatedDepreciation", "Asset"),
    ("13", "Stripe Clearing", "Bank", "Checking", "Asset"),
    ("14", "SAFE Notes", "Equity", "OtherEquity", "Equity"),
    ("15", "Accrued Bonus", "Other Current Liability", "AccruedLiabilities", "Liability"),
    ("16", "Accrued Payroll", "Other Current Liability", "AccruedLiabilities", "Liability"),
    ("20", "Subscription Revenue", "Income", "ServiceFeeIncome", "Revenue"),
    ("21", "Interest Income", "Other Income", "InterestEarned", "Revenue"),
    ("22", "Services Revenue", "Income", "ServiceFeeIncome", "Revenue"),
    ("30", "Stripe Fees", "Expense", "BankCharges", "Expense"),
    ("31", "Payroll Expenses:Wages", "Expense", "PayrollExpenses", "Expense"),
    ("32", "Payroll Expenses:Payroll Taxes", "Expense", "PayrollExpenses", "Expense"),
    ("33", "Hosting", "Expense", "OtherMiscellaneousServiceCost", "Expense"),
    ("34", "Software", "Expense", "OfficeGeneralAdministrativeExpenses", "Expense"),
    ("35", "Office Supplies", "Expense", "SuppliesMaterials", "Expense"),
    ("36", "Rent", "Expense", "RentOrLeaseOfBuildings", "Expense"),
    ("37", "Meals", "Expense", "EntertainmentMeals", "Expense"),
    ("38", "Insurance", "Expense", "Insurance", "Expense"),
    ("39", "Contractors", "Expense", "OtherMiscellaneousServiceCost", "Expense"),
    ("40", "Uncategorized Expense", "Expense", "OtherMiscellaneousServiceCost", "Expense"),
    ("41", "Legal & Professional", "Expense", "LegalProfessionalFees", "Expense"),
    ("42", "Depreciation", "Expense", "Depreciation", "Expense"),
    ("43", "Bonus", "Expense", "PayrollExpenses", "Expense"),
]
NAMES = {a[0]: a[1] for a in ACCOUNTS}
CLS = {a[0]: a[4] for a in ACCOUNTS}

PERIOD = "2026-09"
HISTORY = ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"]
ACCT_ID = {"checking": "1", "treasury": "2", "brex": "3"}


class Book:
    def __init__(self):
        self.lines = []
        self.n = 0

    def txn(self, ttype, dt, name, memo, entries, doc=""):
        assert sum(D(str(a)) for _, a in entries) == 0, (ttype, dt, name, entries)
        self.n += 1
        tid = str(1000 + self.n)
        seq = {}
        for aid, amt in entries:
            seq[aid] = seq.get(aid, 0) + 1
            self.lines.append({"line_id": f"{tid}-{aid}-{seq[aid]}", "txn_id": tid, "txn_type": ttype, "date": dt,
                               "account_id": aid, "account": NAMES[aid], "name": name, "memo": memo,
                               "doc_num": doc, "amount": D(str(amt))})
        return tid


def d(ym, day):
    return date(int(ym[:4]), int(ym[5:]), day)


def base_history(b: Book):
    b.txn("Journal Entry", d("2026-03", 1), "", "Opening balances", [("1", 900000), ("2", 2000000), ("10", -2900000)])
    for i, ym in enumerate(HISTORY):
        gross = D(45000 + 1000 * i)
        fee = (gross * D("0.0296")).quantize(D("0.01"))
        b.txn("Deposit", d(ym, 8), "Stripe", "Stripe payout", [("1", gross - fee), ("30", fee), ("20", -gross)])
        for day in (14, 28):
            b.txn("Payroll Check", d(ym, day), "Gusto", "Payroll", [("31", 60000), ("32", 4500), ("1", -64500)])
        b.txn("Bill", d(ym, 3), "Amazon Web Services", "AWS usage", [("33", 5500), ("6", -5500)])
        b.txn("Bill Payment (Check)", d(ym, 10), "Amazon Web Services", "", [("6", 5500), ("1", -5500)])
        b.txn("Bill", d(ym, 4), "Acme Design", "Design retainer", [("39", 2400), ("6", -2400)])
        b.txn("Bill Payment (Check)", d(ym, 12), "Acme Design", "", [("6", 2400), ("1", -2400)])
        b.txn("Check", d(ym, 5), "WeWork", "Rent", [("36", 6000), ("1", -6000)])
        b.txn("Expense", d(ym, 6), "Figma", "Figma seats", [("34", 480), ("3", -480)])
        b.txn("Expense", d(ym, 7), "Notion", "Notion workspace", [("34", 300), ("3", -300)])
        b.txn("Expense", d(ym, 9), "Sweetgreen", "Team lunch", [("37", 300), ("3", -300)])
        b.txn("Credit Card Payment", d(ym, 20), "Brex", "Card payment", [("3", 1080), ("1", -1080)])
        b.txn("Deposit", d(ym, 25), "Mercury", "Treasury interest", [("2", 4000), ("21", -4000)])
        if ym == "2026-03":
            b.txn("Check", d(ym, 2), "Vouch Insurance", "Annual insurance policy", [("5", 12000), ("1", -12000)])
        b.txn("Journal Entry", d(ym, 28), "", "Insurance amortization", [("38", 1000), ("5", -1000)])
        if ym == "2026-04":
            b.txn("Deposit", d(ym, 2), "Beta Corp", "Beta Corp annual plan", [("1", 12000), ("8", -12000)])
        if ym >= "2026-04":
            b.txn("Journal Entry", d(ym, 28), "", "Deferred revenue release Beta Corp", [("8", 1000), ("20", -1000)])
        if ym == "2026-08":
            b.txn("Invoice", d(ym, 15), "Gamma LLC", "Consulting", [("4", 1530), ("22", -1530)])
            b.txn("Journal Entry", d(ym, 31), "Cooley LLP", "Accrue August legal (Cooley)", [("41", 3000), ("7", -3000)])


def base_september(b: Book):
    S = PERIOD
    b.txn("Deposit", d(S, 1), "Acme Corp", "Annual plan Sep 2026 - Aug 2027", [("1", 6000), ("22", -6000)])     # 7
    b.txn("Expense", d(S, 1), "HubSpot", "HubSpot annual plan", [("34", 12000), ("1", -12000)])                  # 6
    b.txn("Bill", d(S, 3), "Acme Design", "Design retainer", [("39", 2400), ("6", -2400)], doc="AD-0903")
    b.txn("Bill", d(S, 6), "Acme Design", "Design retainer", [("39", 2400), ("6", -2400)], doc="AD-0903")       # 1
    b.txn("Check", d(S, 5), "WeWork", "Rent", [("36", 6000), ("1", -6000)])
    b.txn("Expense", d(S, 6), "Figma", "Figma seats", [("35", 480), ("3", -480)])                               # 5
    b.txn("Expense", d(S, 9), "Sweetgreen", "Team lunch", [("37", 300), ("3", -300)])
    b.txn("Expense", d(S, 19), "Nobu", "Dinner", [("37", 420), ("3", -420)])                                    # 15
    b.txn("Deposit", d(S, 7), "Stripe", "Stripe payout", [("1", 12000), ("20", -12000)])                        # 8
    b.txn("Refund Receipt", d(S, 10), "Stripe customer", "Refund", [("20", 600), ("1", -600)])                  # 4
    b.txn("Refund Receipt", d(S, 11), "Stripe customer", "Refund", [("20", 600), ("1", -600)])                  # 4
    b.txn("Bill Payment (Check)", d(S, 12), "Acme Design", "", [("6", 2400), ("1", -2400)])
    b.txn("Journal Entry", d(S, 14), "", "Revenue true-up", [("4", 3000), ("20", -3000)])                       # 11
    b.txn("Payroll Check", d(S, 15), "Gusto", "Payroll", [("31", 60000), ("32", 4500), ("1", -64500)])
    b.txn("Expense", d(S, 20), "", "Transfer to treasury", [("40", 50000), ("1", -50000)])                       # 12b
    b.txn("Invoice", d(S, 20), "Delta Inc", "Implementation", [("4", 38220), ("22", -38220)])
    b.txn("Payment", d(S, 22), "Gamma LLC", "Check deposit", [("1", 1350), ("4", -1350)])                        # 12
    b.txn("Deposit", d(S, 27), "Stripe", "Stripe payout", [("1", 13000), ("20", -13000)])
    b.txn("Credit Card Payment", d(S, 28), "Brex", "Card payment", [("3", 1080), ("1", -1080)])
    b.txn("Bill", d(S, 28), "Cooley LLP", "Legal - contracts", [("41", 2000), ("6", -2000)])                    # 13
    b.txn("Deposit", d(S, 30), "Stripe", "Stripe payout", [("1", 11920), ("20", -11920)])
    b.txn("Deposit", d(S, 25), "Mercury", "Treasury interest", [("2", 4100), ("21", -4100)])
    b.txn("Journal Entry", d(S, 28), "", "Deferred revenue release Beta Corp", [("8", 1000), ("20", -1000)])


def base_extras():
    S = PERIOD
    stripe = []
    for k in range(20):
        stripe.append({"id": f"txn_ch{k:02d}", "date": d(S, 1 + k), "category": "charge", "amount": D("2500.00"),
                       "fee": D("74.00"), "net": D("2426.00"), "currency": "usd", "description": f"Subscription #{k}"})
    stripe.append({"id": "txn_re01", "date": d(S, 9), "category": "refund", "amount": D("-600.00"), "fee": D("0"),
                   "net": D("-600.00"), "currency": "usd", "description": "Refund"})
    for pid, day, amt in (("po_01", 5, "12000.00"), ("po_02", 15, "11000.00"), ("po_03", 25, "13000.00"), ("po_04", 30, "11920.00")):
        stripe.append({"id": pid, "date": d(S, day), "category": "payout", "amount": -D(amt), "fee": D("0"),
                       "net": -D(amt), "currency": "usd", "description": "STRIPE PAYOUT"})
    return {
        "checking": [(d(S, 1), "WIRE IN ACME CORP", 6000), (d(S, 1), "HUBSPOT", -12000), (d(S, 8), "CHECK 1041 WEWORK", -6000),
                     (d(S, 7), "STRIPE TRANSFER", 12000), (d(S, 12), "ACME DESIGN", -2400), (d(S, 15), "GUSTO PAYROLL", -64500),
                     (d(S, 17), "STRIPE TRANSFER", 11000), (d(S, 20), "TRANSFER TO TREASURY", -50000),
                     (d(S, 22), "CHECK DEPOSIT", 1530), (d(S, 27), "STRIPE TRANSFER", 13000), (d(S, 28), "BREX PAYMENT", -1080),
                     (d(S, 30), "GUSTO PAYROLL", -64500)],
        "treasury": [(d(S, 20), "TRANSFER FROM CHECKING", 50000), (d(S, 25), "INTEREST", 4100)],
        "brex": [(d(S, 6), "FIGMA", -480), (d(S, 9), "SWEETGREEN", -300), (d(S, 19), "NOBU", -420), (d(S, 28), "PAYMENT", 1080)],
        "stripe": stripe,
        "stripe_opening": D("2000.00"),
        "bills": [
            {"bill_id": "B1", "line_no": 1, "vendor": "Acme Design", "txn_date": "2026-09-03", "created_date": "2026-09-03",
             "doc_num": "AD-0903", "account": "Contractors", "amount": "2400.00", "description": "Design retainer"},
            {"bill_id": "B2", "line_no": 1, "vendor": "Acme Design", "txn_date": "2026-09-06", "created_date": "2026-09-06",
             "doc_num": "AD-0903", "account": "Contractors", "amount": "2400.00", "description": "Design retainer"},
            {"bill_id": "B3", "line_no": 1, "vendor": "Cooley LLP", "txn_date": "2026-09-28", "created_date": "2026-10-06",
             "doc_num": "C-118", "account": "Legal & Professional", "amount": "2000.00", "description": "Legal - contracts"},
            {"bill_id": "B4", "line_no": 1, "vendor": "Amazon Web Services", "txn_date": "2026-10-03", "created_date": "2026-10-03",
             "doc_num": "AWS-0926", "account": "Hosting", "amount": "8200.00", "description": "AWS usage - September 2026"},
        ],
        "open_bills": [
            {"bill_id": "B2", "vendor": "Acme Design", "txn_date": "2026-09-06", "doc_num": "AD-0903", "amount": "2400.00", "balance": "2400.00"},
            {"bill_id": "B3", "vendor": "Cooley LLP", "txn_date": "2026-09-28", "doc_num": "C-118", "amount": "2000.00", "balance": "2000.00"},
        ],
        "ar_credits": [],
        "payroll": [{"check_date": "2026-09-15", "gross": "60000.00", "employer_taxes": "4500.00",
                     "period_start": "2026-09-01", "period_end": "2026-09-15"},
                    {"check_date": "2026-09-30", "gross": "60000.00", "employer_taxes": "4500.00",
                     "period_start": "2026-09-16", "period_end": "2026-09-30"}],
        "invoices": None,           # (invoices, lines) to simulate the Stripe API route
        "outstanding": {},          # account key -> list of {date, description, amount, side}
        "config": {},               # top-level config overrides
        "roles": {},                # role overrides
        "attest": [],
        "opening_adjust": {},       # account key -> bank opening minus book opening (items uncleared at Aug 31)
        "statement_end": {},        # account key -> statement end date (mid-month cards)
        "budget": [("Subscription Revenue", 52000), ("Services Revenue", 30000), ("Payroll Expenses:Wages", 120000),
                   ("Payroll Expenses:Payroll Taxes", 9000), ("Hosting", 6000), ("Software", 800), ("Rent", 6000),
                   ("Contractors", 2400), ("Meals", 300), ("Insurance", 1000), ("Stripe Fees", 1500)],
    }


def natural(aid, amt):
    return -amt if CLS[aid] == "Revenue" else amt


def build(root: Path, with_budget: bool = True, scenario=None) -> Path:
    b = Book()
    x = base_extras()
    base_history(b)
    if scenario:
        scenario("history", b, x)
    history_lines = list(b.lines)
    start = len(b.lines)
    base_september(b)
    if scenario:
        scenario("september", b, x)
    sep_lines = b.lines[start:]

    month_dir = root / PERIOD
    inp = month_dir / "inputs"
    accounts = [{"account_id": a, "name": n.split(":")[-1], "full_name": n, "account_type": t, "account_subtype": st,
                 "classification": c, "active": "true"} for a, n, t, st, c in ACCOUNTS]
    write_csv(inp / "accounts.csv", accounts, ["account_id", "name", "full_name", "account_type", "account_subtype",
                                                "classification", "active"])

    def tb(lines):
        bal = {}
        for l in lines:
            bal[l["account_id"]] = bal.get(l["account_id"], D(0)) + l["amount"]
        return [{"account_id": a, "account": NAMES[a], "debit": v if v > 0 else D(0), "credit": -v if v < 0 else D(0)}
                for a, v in sorted(bal.items(), key=lambda kv: int(kv[0])) if v != 0]

    gl_fields = ["line_id", "txn_id", "txn_type", "date", "account_id", "account", "name", "memo", "doc_num", "amount"]
    write_csv(inp / "tb_prior.csv", tb(history_lines), ["account_id", "account", "debit", "credit"])
    write_csv(inp / "tb_current.csv", tb(history_lines + sep_lines), ["account_id", "account", "debit", "credit"])
    write_csv(inp / "gl.csv", sep_lines, gl_fields)
    write_csv(inp / "gl_history.csv", [dict(l, line_id=f"h{l['date']:%Y-%m}-{l['line_id']}") for l in history_lines], gl_fields)

    pm = defaultdict(lambda: D(0))
    for l in history_lines + sep_lines:
        if CLS[l["account_id"]] in ("Revenue", "Expense"):
            pm[(l["account_id"], l["date"].strftime("%Y-%m"))] += natural(l["account_id"], l["amount"])
    write_csv(inp / "pnl_monthly.csv", [{"account_id": a, "account": NAMES[a], "month": m, "amount": v}
                                        for (a, m), v in sorted(pm.items()) if v != 0], ["account_id", "account", "month", "amount"])
    ytd = defaultdict(lambda: D(0))
    for (a, m), v in pm.items():
        if m >= "2026-01":
            ytd[a] += v
    write_csv(inp / "pnl_ytd.csv", [{"account_id": a, "account": NAMES[a], "amount": v} for a, v in sorted(ytd.items())],
              ["account_id", "account", "amount"])

    allx = history_lines + sep_lines
    ar_gl = sum(l["amount"] for l in allx if l["account_id"] == "4")
    ap_gl = -sum(l["amount"] for l in allx if l["account_id"] == "6")
    ar_je = sum(l["amount"] for l in sep_lines if l["account_id"] == "4" and l["txn_type"] == "Journal Entry")
    write_csv(inp / "aging.csv", [{"kind": "ar", "as_of": "2026-09-30", "total": ar_gl - ar_je, "over_90": D(0)},
                                  {"kind": "ap", "as_of": "2026-09-30", "total": ap_gl, "over_90": D(0)}],
              ["kind", "as_of", "total", "over_90"])
    write_csv(inp / "bills.csv", x["bills"], ["bill_id", "line_no", "vendor", "txn_date", "created_date", "doc_num",
                                              "account", "amount", "description"])
    write_csv(inp / "open_bills.csv", x["open_bills"], ["bill_id", "vendor", "txn_date", "doc_num", "amount", "balance"])
    write_csv(inp / "ar_credits.csv", x["ar_credits"], ["kind", "id", "customer", "date", "amount"])
    write_csv(inp / "stripe.csv", [dict(r, source="fixture") for r in x["stripe"]],
              ["id", "date", "category", "amount", "fee", "net", "currency", "description", "source"])
    stripe_close = x["stripe_opening"] + sum(r["net"] for r in x["stripe"])

    files = {
        "accounts.csv": {"source": "quickbooks"},
        "tb_current.csv": {"source": "quickbooks", "as_of": "2026-09-30", "report_end": "2026-09-30"},
        "tb_prior.csv": {"source": "quickbooks", "as_of": "2026-08-31", "report_end": "2026-08-31"},
        "gl.csv": {"source": "quickbooks", "start": "2026-09-01", "end": "2026-09-30"},
        "gl_history.csv": {"source": "quickbooks", "months": HISTORY},
        "pnl_monthly.csv": {"source": "quickbooks", "start": "2026-03-01", "end": "2026-09-30"},
        "pnl_ytd.csv": {"source": "quickbooks", "start": "2026-01-01", "end": "2026-09-30"},
        "aging.csv": {"source": "quickbooks", "as_of": "2026-09-30"},
        "bills.csv": {"source": "quickbooks", "start": "2026-09-01", "end": "2026-10-20"},
        "open_bills.csv": {"source": "quickbooks"},
        "ar_credits.csv": {"source": "quickbooks"},
        "stripe.csv": {"source": "fixture", "start": "2026-09-01", "end": "2026-09-30", "timezone": "UTC"},
        "payroll.csv": {"source": "fixture", "start": "2026-09-01", "end": "2026-11-04"},
    }
    if x["invoices"] is not None:
        invs, lines = x["invoices"]
        write_csv(inp / "stripe_invoices.csv", invs, ["invoice_id", "customer", "status", "finalized_date", "paid_date",
                                                      "subtotal_ex_tax", "tax", "total"])
        write_csv(inp / "stripe_invoice_lines.csv", lines, ["invoice_id", "line_id", "amount", "period_start", "period_end",
                                                            "subscription", "interval", "interval_count"])
        files["stripe_invoices.csv"] = {"source": "fixture"}
        files["stripe_invoice_lines.csv"] = {"source": "fixture"}
    if with_budget:
        write_csv(inp / "budget.csv", [{"account_id": "", "account": a, "month": PERIOD, "amount": D(v)} for a, v in x["budget"]],
                  ["account_id", "account", "month", "amount"])
        files["budget.csv"] = {"source": "budget-csv"}
    write_csv(inp / "payroll.csv", x["payroll"], ["check_date", "gross", "employer_taxes", "period_start", "period_end"])

    statements = {}
    for k, aid in ACCT_ID.items():
        kind = "card" if k == "brex" else "cash"
        rows = [{"row": i + 2, "date": dt, "description": desc, "amount": D(str(a))} for i, (dt, desc, a) in enumerate(x[k])]
        write_csv(inp / "bank" / f"{aid}.csv", rows, ["row", "date", "description", "amount"])
        opening = sum(l["amount"] for l in history_lines if l["account_id"] == aid) + D(str(x["opening_adjust"].get(k, 0)))
        s_end = x["statement_end"].get(k, date(2026, 9, 30))
        end = opening + sum(r["amount"] for r in rows if r["date"] <= s_end)
        sign = -1 if kind == "card" else 1
        statements[aid] = {"account": NAMES[aid], "kind": kind, "file": f"bank/{aid}.csv", "source_file": f"{k}-sep.csv",
                           "opening_balance": str(sign * opening), "opening_source": "typed", "statement_start": "2026-08-31",
                           "ending_balance": str(sign * end), "statement_end": s_end.isoformat(),
                           "rows_in_statement": sum(1 for r in rows if r["date"] <= s_end),
                           "rows_after_statement": sum(1 for r in rows if r["date"] > s_end)}
    attest = {f"outstanding:{aid}": {"by": "Fixture", "at": "2026-10-11T00:00:00+00:00"}
              for k, aid in ACCT_ID.items() if k not in x["outstanding"]}
    for a in x["attest"]:
        attest[a] = {"by": "Fixture", "at": "2026-10-11T00:00:00+00:00"}
    man = {"period": PERIOD, "files": files, "statements": statements, "attestations": attest,
           "qbo_pull": {"complete": True, "basis": "Accrual", "fiscal_year_start_month": 1, "history_months": 6},
           "stripe_balance": {"opening": str(x["stripe_opening"]), "closing": str(stripe_close), "source": "typed"}}
    if x["outstanding"]:
        rows = []
        man["outstanding_prior"] = {}
        for k, items in x["outstanding"].items():
            aid = ACCT_ID[k]
            man["outstanding_prior"][aid] = {"source": "fixture", "rows": len(items)}
            rows += [dict(it, account_id=aid, source_ref="fixture") for it in items]
        write_csv(inp / "outstanding_prior.csv", rows, ["account_id", "side", "date", "description", "amount", "source_ref"])
    write_json(inp / "manifest.json", man)

    roles = {"cash": ["Mercury Checking", "Mercury Treasury"], "card": ["Brex Card"],
             "ar": ["Accounts Receivable (A/R)"], "ap": ["Accounts Payable (A/P)"],
             "deferred_revenue": ["Deferred Revenue"], "prepaid": ["Prepaid Expenses"],
             "accrued": ["Accrued Liabilities", "Accrued Bonus"], "stripe_revenue": ["Subscription Revenue"],
             "stripe_fees": ["Stripe Fees"], "refunds": [], "payroll_wages": ["Payroll Expenses:Wages"],
             "payroll_taxes": ["Payroll Expenses:Payroll Taxes"]}
    roles.update(x["roles"])
    cfg = {"schema": 2, "company": "Northwind SaaS (fixture)", "currency": "USD", "ledger": "quickbooks",
           "basis": "Accrual", "fiscal_year_start_month": 1, "accounts_confirmed": False, "uses_stripe": True,
           "stripe_billing_via_ar": False, "uses_stripe_tax": False, "uses_payroll_file": True, "roles": roles,
           "accrual_behavior": {"Accrued Bonus": "rollforward"}, "bank_formats": {}, "payroll_format": {}, "thresholds": {}}
    cfg.update(x["config"])
    write_json(root / "review-config.json", cfg)
    mconfig.confirm(root, "Fixture")
    return month_dir


def d_(s):
    return date.fromisoformat(s)


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "northwind-review")
    print(build(out))
