"""V1 P&L flux (month, budget, year to date) · V2 balance-sheet flux and unreconciled balances ·
V3 investor metrics (cash, burn, runway, MRR), all computed here so the commentary can't invent them."""

from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from ..config import threshold
from ..data import Data
from ..qbo import fy_start
from ..util import ZERO, Period, fmt, pct
from .base import CheckResult, not_run

SECTIONS = [("revenue", "Revenue"), ("cogs", "Cost of goods sold"), ("opex", "Operating expenses"),
            ("other_income", "Other income"), ("other_expense", "Other expenses")]
UP_IS_GOOD = {"revenue", "other_income"}


def section(d: Data, aid: str) -> str:
    t = d.atype(aid)
    return {"Income": "revenue", "Cost of Goods Sold": "cogs", "Expense": "opex", "Other Income": "other_income",
            "Other Expense": "other_expense"}.get(t, "revenue" if d.cls(aid) == "Revenue" else "opex")


def _direction(sec, delta):
    if not delta:
        return ""
    return "favorable" if (delta > 0) == (sec in UP_IS_GOOD) else "unfavorable"


def _drivers(d: Data, aid: str, prev_k: str) -> list[dict]:
    payroll = aid in d.payroll_ids()
    by = defaultdict(lambda: {"current": ZERO, "prior": ZERO, "refs": []})
    for l in d.gl:
        if l["account_id"] == aid:
            k = "(payroll; people not shown)" if payroll else (l["name"] or l["memo"] or "(no payee)")
            by[k]["current"] += d.natural(aid, l["amount"])
            by[k]["refs"].append(f"GL {l['line_id']}")
    for l in d.gl_history:
        if l["account_id"] == aid and l["date"].strftime("%Y-%m") == prev_k:
            k = "(payroll; people not shown)" if payroll else (l["name"] or l["memo"] or "(no payee)")
            by[k]["prior"] += d.natural(aid, l["amount"])
    out = [{"payee": k, "current": v["current"], "prior": v["prior"], "change": v["current"] - v["prior"],
            "refs": v["refs"][:5]} for k, v in by.items()]
    out.sort(key=lambda x: abs(x["change"]), reverse=True)
    return [x for x in out if x["change"] != 0][:5]


def v1_pl(d: Data, ctx: dict) -> CheckResult:
    r = CheckResult("V1", "P&L: month over month, vs budget, year to date")
    cur_k, prev_k = d.period.key, d.period.prior.key
    if prev_k not in {l["date"].strftime("%Y-%m") for l in d.gl_history}:
        return not_run("V1", r.title, f"no ledger history for {prev_k}")
    m = d.monthly_pl()
    cur, prev = m.get(cur_k, {}), m.get(prev_k, {})
    a_abs, a_pct, a_only = (threshold(d.cfg, k) for k in ("materiality_abs", "materiality_pct", "materiality_abs_only"))

    def material(delta, base):
        p_ = pct(delta, base)
        return abs(delta) >= a_only or (abs(delta) >= a_abs and (p_ is None or abs(p_) >= a_pct))

    budget = ytd_budget = None
    if d.budget is not None:
        months = {b["month"] for b in d.budget}
        if cur_k in months:
            budget = defaultdict(lambda: None)
            for b in d.budget:
                if b["month"] == cur_k:
                    budget[b["account_id"]] = (budget[b["account_id"]] or ZERO) + b["amount"]
            fy = fy_start(d.period, d.cfg["fiscal_year_start_month"])
            need = []
            p = Period(f"{fy.year:04d}-{fy.month:02d}")
            while p.key <= cur_k:
                need.append(p.key)
                p = p.shift(1)
            if all(x in months for x in need):
                ytd_budget = defaultdict(lambda: ZERO)
                for b in d.budget:
                    if b["month"] in need:
                        ytd_budget[b["account_id"]] += b["amount"]
            else:
                r.part("budget, year to date", "not_run", f"the budget doesn't cover every month from {need[0]} to {cur_k}")
        else:
            r.part("vs budget", "not_run", f"the budget file has no amounts for {cur_k}")
    else:
        r.part("vs budget", "not_run", "no budget (optional input)")

    rows = []
    accts = set(cur) | set(prev) | ({a for a in budget} if budget else set()) | {a for a in d.pnl_ytd if d.is_pl(a)}
    for aid in sorted(accts, key=lambda a: (SECTIONS.index(next(s for s in SECTIONS if s[0] == section(d, a))), d.name(a))):
        sec = section(d, aid)
        c, pv = cur.get(aid, ZERO), prev.get(aid, ZERO)
        delta = c - pv
        row = {"account": d.name(aid), "account_id": aid, "section": sec, "current": c, "prior": pv, "change": delta,
               "change_pct": pct(delta, pv), "direction": _direction(sec, delta), "material": material(delta, pv),
               "ytd": d.pnl_ytd.get(aid, ZERO)}
        if budget is not None:
            bud = budget.get(aid)
            row["budget"] = bud
            if bud is None:
                row.update(vs_budget=None, vs_budget_pct=None, budget_direction="not in budget", material_budget=False)
            else:
                vb = c - bud
                row.update(vs_budget=vb, vs_budget_pct=pct(vb, bud), budget_direction=_direction(sec, vb),
                           material_budget=material(vb, bud))
            if ytd_budget is not None:
                row["ytd_budget"] = ytd_budget.get(aid, ZERO)
        if row["material"] or row.get("material_budget"):
            row["drivers"] = _drivers(d, aid, prev_k)
        rows.append(row)

    def tot(field, secs):
        vals = [x.get(field) for x in rows if x["section"] in secs]
        vals = [v for v in vals if v is not None]
        return sum((v for v in vals if v is not None), ZERO)

    totals = {}
    if budget is not None:
        for x in rows:
            x["budgeted_actual"] = x["current"] if x.get("budget") is not None else None
    for fld in ("current", "prior", "ytd") + (("budget", "budgeted_actual") if budget is not None else ()) + (("ytd_budget",) if ytd_budget is not None else ()):
        rev, cogs, opex = tot(fld, {"revenue"}), tot(fld, {"cogs"}), tot(fld, {"opex"})
        oi, oe = tot(fld, {"other_income"}), tot(fld, {"other_expense"})
        totals[fld] = {"revenue": rev, "cogs": cogs, "gross_profit": rev - cogs, "opex": opex,
                       "operating_income": rev - cogs - opex, "other_income": oi, "other_expense": oe,
                       "net_income": rev - cogs - opex + oi - oe,
                       "gross_margin_pct": pct(rev - cogs, rev) if (rev and any(x["section"] == "cogs" for x in rows)) else None}
    lines = {}
    for k in totals["current"]:
        if k == "gross_margin_pct":
            continue
        c, pv = totals["current"][k], totals["prior"][k]
        lines[k] = {"current": c, "prior": pv, "change": c - pv, "change_pct": pct(c - pv, pv), "ytd": totals["ytd"][k]}
        if "budget" in totals:
            lines[k].update(budget=totals["budget"][k], vs_budget=c - totals["budget"][k],
                            not_in_budget=c - totals["budgeted_actual"][k])
        if "ytd_budget" in totals:
            lines[k]["ytd_budget"] = totals["ytd_budget"][k]
    gm = {f: totals[f]["gross_margin_pct"] for f in totals}
    unbudgeted = [{"account": x["account"], "current": x["current"]} for x in rows
                  if budget is not None and x.get("budget") is None and x["current"] != 0]
    bridge = sum(((x["change"] if x["section"] in UP_IS_GOOD else -x["change"]) for x in rows), ZERO)
    ni_change = lines["net_income"]["change"]
    r.data = {"rows": rows, "totals": lines, "gross_margin_pct": gm, "has_budget": budget is not None,
              "not_in_budget": unbudgeted,
              "has_ytd_budget": ytd_budget is not None, "fiscal_year_start": fy_start(d.period, d.cfg["fiscal_year_start_month"]),
              "bridge_check": {"sum_of_changes": bridge, "net_income_change": ni_change, "adds_up": bridge == ni_change},
              "thresholds": {"abs": a_abs, "pct": a_pct, "abs_only": a_only}}
    if bridge != ni_change:
        r.flag("high", "bridge", "The variance bridge doesn't add up to the change in net income", bridge - ni_change,
               "Internal error in the flux table.", ["flux.csv"], "Report this as a bug; don't use the commentary.", pin=True)
    n_mat = sum(1 for x in rows if x["material"] or x.get("material_budget"))
    t = lines
    r.summary = (f"Revenue {fmt(t['revenue']['current'])} ({fmt(t['revenue']['change'], sign=True)} vs {prev_k}), "
                 f"net income {fmt(t['net_income']['current'])}; {n_mat} material line(s)")
    return r


RECONCILED_BY = [("cash", "T1 bank statement"), ("card", "T1 card statement"), ("processor_clearing", "T3 Stripe balance"),
                 ("ar", "T2 aging"), ("ap", "T2 aging"), ("undeposited_funds", "T2 reviewed"),
                 ("accrued", "C2 reviewed"), ("prepaid", "C3 reviewed"), ("deferred_revenue", "C3 reviewed"),
                 ("accrued_payroll", "T5 reviewed"), ("sales_tax_payable", "T4 vs Stripe Tax"), ("loans", "C4 interest only")]


def v2_balance_sheet(d: Data, ctx: dict) -> CheckResult:
    r = CheckResult("V2", "Balance sheet: every account's movement, and what nothing reconciled")
    big = threshold(d.cfg, "materiality_abs_only")
    by = {}
    for role, label in RECONCILED_BY:
        for aid in d.ids_for(role):
            by.setdefault(aid, label)
    rows, unreconciled = [], []
    for aid in sorted(set(d.tb) | set(d.tb_prior), key=lambda a: (d.cls(a), d.name(a))):
        if d.is_pl(aid):
            continue
        sign = 1 if d.cls(aid) == "Asset" else -1
        cur, prv = sign * d.tb.get(aid, ZERO), sign * d.tb_prior.get(aid, ZERO)
        if cur == 0 and prv == 0:
            continue
        rec = by.get(aid) or ("year-end close" if d.accounts.get(aid, {}).get("account_subtype") == "RetainedEarnings" else "")
        payroll = aid in d.payroll_ids() or "payroll" in d.name(aid).lower()
        payroll_txns = {l["txn_id"] for l in d.gl if l["account_id"] in d.payroll_ids()}
        lines = sorted((l for l in d.gl if l["account_id"] == aid), key=lambda l: abs(l["amount"]), reverse=True)[:3]
        top = [{"date": l["date"], "payee": "(payroll)" if (payroll or l["txn_id"] in payroll_txns) else (l["name"] or l["memo"]),
                "amount": sign * l["amount"], "ref": f"GL {l['line_id']}"} for l in lines]
        row = {"account": d.name(aid), "account_id": aid, "class": d.cls(aid), "prior": prv, "current": cur,
               "change": cur - prv, "reconciled_by": rec or "nothing", "top_lines": top}
        rows.append(row)
        if not rec and cur != 0:
            unreconciled.append({"account": d.name(aid), "balance": cur})
            if abs(cur - prv) >= big:
                r.flag("medium", "bs-unreconciled-move", f"{d.name(aid)} moved {fmt(cur - prv, sign=True)} and no check reconciles it",
                       cur - prv, "Largest lines: " + ", ".join(f"{fmt(t['amount'])} {t['date']} {t['payee']}" for t in top),
                       [t["ref"] for t in top], "Tie this balance to its source (loan statement, cap table, payroll "
                       "liability report, fixed-asset register).", txns=[l["txn_id"] for l in lines], accounts=[aid],
                       subject=d.name(aid))
    r.data = {"rows": rows, "unreconciled": unreconciled}
    r.summary = f"{len(rows)} balance-sheet accounts; {len(unreconciled)} balance(s) no check reconciles"
    return r


def _round(x, q="0.1"):
    return None if x is None else x.quantize(Decimal(q), rounding=ROUND_HALF_UP)


def v3_metrics(d: Data, ctx: dict) -> CheckResult:
    r = CheckResult("V3", "Investor metrics: cash, burn, runway, revenue, margin, MRR")
    p = d.period
    cash_ids = d.ids_for("cash")
    stm = [s for aid, s in d.statements.items() if aid in cash_ids]
    metrics = []

    def add(mid, label, value, unit, definition, status="run", reason=""):
        metrics.append({"id": mid, "label": label, "value": value, "unit": unit, "definition": definition,
                        "status": status, "reason": reason})

    cash_end = sum((s["month_end_dp"] for s in stm), ZERO)
    cash_start = sum((s["month_end_dp"] - sum((l["amount"] for l in s["lines"] if p.contains(l["date"])), ZERO) for s in stm), ZERO)
    add("cash", f"Cash at {p.end}", cash_end, "usd", "sum of bank balances (statements) in roles.cash")
    fin_ids = {a for a, x in d.accounts.items() if x["classification"] == "Equity" or x["account_type"] == "Long Term Liability"} | d.ids_for("loans")

    def financing(lines, month):
        txns = {l["txn_id"] for l in lines if l["account_id"] in fin_ids and l["date"].strftime("%Y-%m") == month}
        return sum((l["amount"] for l in lines if l["txn_id"] in txns and l["account_id"] in cash_ids), ZERO)

    fin_now = financing(d.gl, p.key)
    chg_now_books = sum((l["amount"] for l in d.gl if l["account_id"] in cash_ids), ZERO)
    burn_now = -(chg_now_books - fin_now)
    add("financing", "Financing received this month", fin_now, "usd", "cash in from equity, SAFEs, notes and loans (books)")
    add("net_burn", "Net burn this month", burn_now, "usd",
        "-(change in the books' cash accounts - financing); positive = cash used. Same basis as the 3-month average")
    burns = []
    for k in (p.shift(-2).key, p.shift(-1).key):
        ls = [l for l in d.gl_history if l["date"].strftime("%Y-%m") == k]
        if not ls:
            continue
        chg = sum((l["amount"] for l in ls if l["account_id"] in cash_ids), ZERO)
        burns.append(-(chg - financing(ls, k)))
    burns.append(burn_now)
    if len(burns) == 3:
        avg = sum(burns, ZERO) / 3
        add("avg_burn_3m", "Average net burn, last 3 months", avg, "usd", "books: change in cash accounts minus financing, averaged")
        if avg > 0:
            add("runway_months", "Runway", _round(cash_end / avg), "months", "cash at month-end / average net burn (3 months)")
        else:
            add("runway_months", "Runway", None, "months", "cash / average burn", "not_run", "not burning cash on a 3-month average")
    else:
        add("avg_burn_3m", "Average net burn, last 3 months", None, "usd", "", "not_run", "fewer than 3 months of ledger history")
        add("runway_months", "Runway", None, "months", "", "not_run", "needs the 3-month average burn")

    v1 = ctx.get("V1")
    if v1 and v1.status != "not_run":
        t = v1.data["totals"]
        add("revenue", "Revenue", t["revenue"]["current"], "usd", "operating revenue (Income accounts; excludes other income)")
        add("revenue_growth_pct", "Revenue growth vs last month", t["revenue"]["change_pct"], "pct", "")
        gm = v1.data["gross_margin_pct"]["current"]
        if gm is not None:
            add("gross_margin_pct", "Gross margin", gm, "pct", "(revenue - cost of goods sold) / revenue")
        else:
            add("gross_margin_pct", "Gross margin", None, "pct", "", "not_run", "no cost-of-goods-sold accounts")
        add("net_income", "Net income", t["net_income"]["current"], "usd", "")
    if d.invoice_lines is not None:
        mrr, missing = ZERO, 0
        per_month = {"month": Decimal(1), "year": Decimal(1) / 12, "week": Decimal(52) / 12, "day": Decimal(365) / 12}
        live = {i["invoice_id"] for i in d.invoices if i["status"] in ("paid", "open")}
        for ln in d.invoice_lines:
            if (ln["subscription"] and ln["invoice_id"] in live and ln["period_start"] and ln["period_end"]
                    and ln["period_start"] <= p.end < ln["period_end"]):
                interval, count = ln.get("interval") or "", ln.get("interval_count") or ""
                if interval not in per_month or not str(count).isdigit() or int(count) < 1:
                    missing += 1
                    continue
                mrr += ln["amount"] * per_month[interval] / int(count)
        if missing:
            add("mrr", "MRR", None, "usd", "", "not_run",
                f"{missing} subscription line(s) have no billing interval in the Stripe data; pull again")
        else:
            add("mrr", "MRR", mrr.quantize(Decimal("0.01")), "usd",
                f"Stripe subscription lines whose service period covers {p.end}, after discounts, divided by their "
                "billing interval (monthly as is, annual / 12)")
    else:
        add("mrr", "MRR", None, "usd", "", "not_run", "needs Stripe invoice data (API pull)")
    r.data = {"metrics": metrics}
    runway = next(x for x in metrics if x["id"] == "runway_months")
    r.summary = f"Cash {fmt(cash_end)}; net burn {fmt(burn_now)}; runway " + (
        f"{runway['value']} months" if runway["value"] is not None else f"NOT RUN ({runway['reason']})")
    return r
