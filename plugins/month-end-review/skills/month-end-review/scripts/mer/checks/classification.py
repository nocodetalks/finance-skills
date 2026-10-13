"""K1 wrong account, suspense, impossible signs · K2 accounts vs their own history · K3 unusual inflows."""

from __future__ import annotations

from collections import Counter, defaultdict

from ..config import SUSPENSE_PATTERN, threshold
from ..data import Data
from ..util import ZERO, fmt, norm
from .base import CheckResult, key, not_run


def k1_accounts(d: Data, ctx: dict) -> CheckResult:
    r = CheckResult("K1", "Wrong account, suspense balances and impossible signs")
    for aid, bal in d.tb.items():
        if bal != 0 and SUSPENSE_PATTERN.search(d.name(aid)):
            lines = [l for l in d.gl if l["account_id"] == aid]
            r.flag("medium", "suspense", f"{d.name(aid)} holds {fmt(bal)}", bal,
                   "Suspense and uncategorized balances hide real income, expenses or transfers."
                   + (f" This month: " + ", ".join(f"{fmt(l['amount'])} on {l['date']} ({l['memo'] or l['name']})" for l in lines[:5])
                      if lines else ""),
                   [f"GL {l['line_id']}" for l in lines[:10]] or [f"TB {d.name(aid)}"],
                   "Recategorize each item to its real account.", txns=[l["txn_id"] for l in lines],
                   keys=[key(l["date"], l["amount"]) for l in lines], accounts=[aid], subject=d.name(aid))
    signs = [("ar", 1, "a credit balance (payments not applied, or customers overpaid)"),
             ("ap", -1, "a debit balance (vendors overpaid, or bills missing)"),
             ("prepaid", 1, "a credit balance (amortized more than was paid)"),
             ("accrued", -1, "a debit balance (reversed more than was accrued)"),
             ("deferred_revenue", -1, "a debit balance (more released than billed)"),
             ("cash", 1, "a negative balance")]
    for role, normal, what in signs:
        for aid in sorted(d.ids_for(role)):
            bal = d.tb.get(aid, ZERO)
            if bal * normal < 0:
                r.flag("medium", "sign", f"{d.name(aid)} has {what}: {fmt(bal)}", bal, "This sign should not happen.",
                       [f"TB {d.name(aid)}"], "Find the entry that flipped it.", accounts=[aid], subject=d.name(aid))
    min_lines = int(threshold(d.cfg, "drift_min_history_lines"))
    usual = defaultdict(Counter)
    for l in d.gl_history:
        if d.cls(l["account_id"]) == "Expense" and l["amount"] > 0 and l["name"]:
            usual[norm(l["name"])][l["account_id"]] += 1
    for l in d.gl:
        if d.cls(l["account_id"]) != "Expense" or l["amount"] <= 0 or not l["name"]:
            continue
        c = usual.get(norm(l["name"]))
        if not c:
            continue
        top, n = c.most_common(1)[0]
        tot = sum(c.values())
        if tot >= min_lines and n / tot >= 0.7 and top != l["account_id"]:
            r.flag("medium", "drift", f"{l['name']} {fmt(l['amount'])} booked to {l['account']}; usually {d.name(top)}",
                   l["amount"], f"{n} of {tot} earlier lines for this payee went to {d.name(top)}.", [f"GL {l['line_id']}"],
                   f"Move it to {d.name(top)} unless this purchase really is different.", txns=[l["txn_id"]],
                   accounts=[l["account_id"], top], subject=f"{l['name']}:{l['date']}")
            r.draft(f"Reclass {l['name']} {l['date']}", [(d.name(top), l["amount"], None), (l["account"], None, l["amount"])])
    r.summary = f"{len(r.exceptions)} account issue(s)"
    return r


def k2_history(d: Data, ctx: dict) -> CheckResult:
    title = "Accounts against their own history"
    months = sorted({l["date"].strftime("%Y-%m") for l in d.gl_history})
    if len(months) < 3:
        return not_run("K2", title, f"only {len(months)} month(s) of history; needs 3")
    r = CheckResult("K2", title)
    m = d.monthly_pl()
    cur = m.get(d.period.key, {})
    floor = threshold(d.cfg, "range_floor")
    slack = threshold(d.cfg, "range_slack_pct") / 100
    for aid in sorted({a for mm in months for a in m.get(mm, {})} | set(cur)):
        hist = [m.get(mm, {}).get(aid, ZERO) for mm in months]
        if sum(1 for h in hist if h != 0) < 3:
            continue
        lo, hi, now = min(hist), max(hist), cur.get(aid, ZERO)
        if (now > hi * (1 + slack) and now - hi >= floor) or (now < lo * (1 - slack) and lo - now >= floor):
            r.flag("low", "range", f"{d.name(aid)} {fmt(now)} is outside its {len(months)}-month range ({fmt(lo)} to {fmt(hi)})",
                   now - (hi if now > hi else lo), "Not wrong by itself; worth a look if nothing explains it.",
                   [f"P&L {d.name(aid)}"], "Check the largest lines this month.", accounts=[aid], subject=d.name(aid))
    r.summary = f"{len(r.exceptions)} account(s) outside their usual range"
    return r


def k3_unusual_inflows(d: Data, ctx: dict) -> CheckResult:
    r = CheckResult("K3", "Large inflows booked as revenue from new payers (possible financing)")
    big = threshold(d.cfg, "unusual_inflow_min")
    payer = lambda l: norm(l["name"] or l["memo"] or "")
    seen = {payer(l) for l in d.gl_history if d.cls(l["account_id"]) == "Revenue" and payer(l)}
    hist_max = {}
    for l in d.gl_history:
        if d.cls(l["account_id"]) == "Revenue" and l["amount"] < 0:
            hist_max[l["account_id"]] = max(hist_max.get(l["account_id"], ZERO), -l["amount"])
    for l in d.gl:
        if l["txn_type"].lower() in ("invoice", "credit memo"):
            continue  # billed to a customer through AR: a sale, not money arriving unexplained
        amt = -l["amount"]
        if d.cls(l["account_id"]) != "Revenue" or amt < big:
            continue
        new_payer = not payer(l) or payer(l) not in seen
        bigger_than_ever = amt > 2 * hist_max.get(l["account_id"], ZERO)
        if new_payer or bigger_than_ever:
            who = l["name"] or (f"\"{l['memo']}\"" if l["memo"] else "an unnamed payer")
            why = "no history with this payer" if new_payer else "more than twice anything this account has received before"
            r.flag("high", "inflow", f"{fmt(amt)} from {who} booked to {l['account']}; {why}", amt,
                   f"{l['txn_type']} on {l['date']}. If this is an investment (SAFE, equity, convertible note) or a "
                   "loan, it belongs on the balance sheet, not in revenue.", [f"GL {l['line_id']}"],
                   "Confirm what it is. Move investments and loans to equity or liabilities.", txns=[l["txn_id"]],
                   keys=[key(l["date"], l["amount"])], accounts=[l["account_id"]], subject=f"{who}:{l['date']}")
    r.summary = f"{len(r.exceptions)} large inflow(s) from new payers"
    return r
