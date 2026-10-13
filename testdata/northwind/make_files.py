"""The files a Northwind finance person would download for the September 2026 close.

Bank and card exports in the shapes Mercury and Brex use, a Stripe balance-transactions export, a Gusto payroll
summary, a budget, and statements.md: the balances printed on each statement, which a person types in. All of it
comes from tests/fixture_northwind.py, so it matches what load_sandbox.py put in QuickBooks.
"""

from __future__ import annotations

import csv
import json
import shutil
import tempfile
from decimal import Decimal as D
from pathlib import Path

import fixture_northwind as fx


def _write(p: Path, header: list[str], rows: list[list]) -> None:
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def write_files(out: Path) -> str:
    out.mkdir(parents=True, exist_ok=True)
    x = fx.base_extras()
    tmp = Path(tempfile.mkdtemp())
    try:
        man = json.loads((fx.build(tmp) / "inputs" / "manifest.json").read_text())
    finally:
        shutil.rmtree(tmp)
    st = man["statements"]

    mercury = ["Date (UTC)", "Description", "Amount", "Status", "Source Account"]
    _write(out / "mercury-checking-2026-09.csv", mercury,
           [[f"{dt:%m-%d-%Y}", desc, f"{D(a):.2f}", "Sent", "Mercury Checking ••4821"] for dt, desc, a in x["checking"]])
    _write(out / "mercury-treasury-2026-09.csv", mercury,
           [[f"{dt:%m-%d-%Y}", desc, f"{D(a):.2f}", "Sent", "Mercury Treasury ••7310"] for dt, desc, a in x["treasury"]])
    # Brex exports card charges as positive amounts and payments as negative.
    _write(out / "brex-card-2026-09.csv", ["Posted Date", "Description", "Amount"],
           [[dt.isoformat(), desc, f"{-D(a):.2f}"] for dt, desc, a in x["brex"]])
    _write(out / "stripe-balance-transactions-2026-09.csv",
           ["balance_transaction_id", "created_utc", "reporting_category", "gross", "fee", "net", "currency", "description"],
           [[r["id"], f"{r['date']} 14:00:00", r["category"], f"{r['amount']:.2f}", f"{r['fee']:.2f}", f"{r['net']:.2f}",
             r["currency"], r["description"]] for r in x["stripe"]])
    _write(out / "gusto-payroll-2026-09.csv",
           ["Check date", "Pay period start", "Pay period end", "Gross earnings", "Employer taxes"],
           [[r["check_date"], r["period_start"], r["period_end"], r["gross"], r["employer_taxes"]] for r in x["payroll"]])
    _write(out / "budget-2026.csv", ["Account", "2026-09"], [[a, f"{D(v):.2f}"] for a, v in x["budget"]])

    def bal(aid, k):
        return f"${D(st[aid][k]) + 0:,.2f}".replace("$-", "-$")

    sb = man["stripe_balance"]
    (out / "statements.md").write_text(f"""# Northwind SaaS Inc: statements for September 2026

What each statement and portal shows. Type these balances exactly as written when the review asks for them.

| Account | Statement period | Opening balance (Aug 31) | Closing balance (Sep 30) | File |
|---|---|---|---|---|
| Mercury Checking | Sep 1–30, 2026 | {bal('1', 'opening_balance')} | {bal('1', 'ending_balance')} | mercury-checking-2026-09.csv |
| Mercury Treasury | Sep 1–30, 2026 | {bal('2', 'opening_balance')} | {bal('2', 'ending_balance')} | mercury-treasury-2026-09.csv |
| Brex Card (balance owed) | Sep 1–30, 2026 | {bal('3', 'opening_balance')} | {bal('3', 'ending_balance')} | brex-card-2026-09.csv |
| Stripe balance (Balance report) | Sep 1–30, 2026 (UTC) | ${D(sb['opening']):,.2f} | ${D(sb['closing']):,.2f} | stripe-balance-transactions-2026-09.csv |

- **Uncleared items at Aug 31:** none, on all three accounts. Last month's reconciliations had nothing outstanding.
- **Payroll:** Gusto, two runs, with pay periods. File: gusto-payroll-2026-09.csv.
- **Budget:** one column per month. File: budget-2026.csv.
- **First month with this skill:** yes, so opening balances are needed this once.
""", encoding="utf-8")
    names = sorted(p.name for p in out.iterdir())
    return f"Wrote {len(names)} files to {out}:\n  " + "\n  ".join(names)
