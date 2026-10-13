"""T2 AR/AP aging vs GL plus AR hygiene; T5 payroll vs the payroll provider."""

from __future__ import annotations

from decimal import Decimal

from ..config import threshold
from ..data import Data
from ..util import ZERO, add_days, days_between, fmt, overlap_days
from .base import CheckResult, is_je, key, not_run, pick


def t2_subledgers(d: Data, ctx: dict) -> CheckResult:
    r = CheckResult("T2", "AR and AP agings tie to the ledger; receivables are clean")
    tol = threshold(d.cfg, "tie_tolerance")
    mat = threshold(d.cfg, "materiality_abs")
    for kind, role, label in (("ar", "ar", "Accounts receivable"), ("ap", "ap", "Accounts payable")):
        ids = d.ids_for(role)
        if not ids:
            r.part(label, "not_run", f"roles.{role} is empty")
            continue
        books = sum((d.tb.get(a, ZERO) for a in ids), ZERO)
        books_nat = books if kind == "ar" else -books
        aging = d.aging[kind]["total"]
        diff = books_nat - aging
        status = "tie" if abs(diff) <= tol else "differs"
        r.tie(label, books_nat, aging, f"{kind.upper()} aging at {d.period.end}", status)
        if status == "differs":
            jes = [l for l in d.gl if l["account_id"] in ids and is_je(l)]
            detail = f"Ledger {fmt(books_nat)} vs aging {fmt(aging)}. " + (
                "Journal entries posted straight to the control account this month: "
                + ", ".join(f"{fmt(l['amount'])} on {l['date']} ({l['memo'] or l['name'] or 'no memo'})" for l in jes)
                if jes else "No journal entry hit the control account this month; look at earlier months.")
            r.flag("high", "aging-differs", f"{label}: ledger and aging differ by {fmt(diff)}", diff, detail,
                   [f"GL {l['line_id']}" for l in jes] or [f"TB {label}"],
                   "Replace manual entries on the control account with invoices/bills or payments.",
                   txns=[l["txn_id"] for l in jes], accounts=ids, subject=label, pin=True)
        else:
            # QuickBooks makes a journal entry to A/R or A/P carry a customer or vendor, so it lands in the aging and
            # everything ties. The entry is still an open item no invoice or bill backs. Reclasses that net to zero
            # inside one entry (one customer to another) are fine.
            net: dict = {}
            for l in d.gl:
                if l["account_id"] in ids and is_je(l):
                    net[l["txn_id"]] = net.get(l["txn_id"], ZERO) + l["amount"]
            jes = [l for l in d.gl if l["txn_id"] in net and net[l["txn_id"]] != 0 and l["account_id"] in ids]
            if jes:
                total = sum(net.values(), ZERO) * (1 if kind == "ar" else -1)
                other = sorted({l["account"] for l in d.gl if l["txn_id"] in {j["txn_id"] for j in jes}
                                and l["account_id"] not in ids})
                r.flag("medium" if abs(total) >= mat else "low", "je-to-control",
                       f"{label}: {fmt(total)} of journal entries posted straight to the control account", total,
                       "Journal entries this month: "
                       + ", ".join(f"{fmt(l['amount'] if kind == 'ar' else -l['amount'])} on {l['date']} "
                                   f"({l['memo'] or l['name'] or 'no memo'})" for l in jes)
                       + (f", against {', '.join(other)}" if other else "")
                       + ". The aging ties because QuickBooks puts these in it, but no invoice or bill backs them.",
                       [f"GL {l['line_id']}" for l in jes],
                       "Replace each with an invoice or bill (or a payment), or book it to the right account.",
                       txns=list(net), accounts=ids, subject=f"{label} journal entries")
    if d.ids_for("ar"):
        credits = [c for c in d.ar_credits if c["date"] <= d.period.end]
        tot = sum((c["amount"] for c in credits), ZERO)
        if credits and tot >= mat:
            r.flag("medium", "unapplied", f"{fmt(tot)} of customer payments and credits is unapplied ({len(credits)} item(s))",
                   tot, "Unapplied payments and open credit memos (dated by month-end, from the last 12 months) make "
                   "the aging and collections wrong. Their open balance is as of the pull date.", [f"{c['kind']} {c['id']}" for c in credits[:10]],
                   "Apply each payment or credit to its invoice.", accounts=d.ids_for("ar"), subject="unapplied")
        over = d.aging["ar"]["over_90"]
        if over >= mat:
            r.flag("medium", "ar-over-90", f"{fmt(over)} of receivables is more than 90 days past due", over,
                   f"Out of {fmt(d.aging['ar']['total'])} total AR at {d.period.end}.", ["AR aging"],
                   "Chase it, and decide whether any of it needs a bad-debt allowance.", accounts=d.ids_for("ar"),
                   subject="ar-over-90")
    for aid in d.ids_for("undeposited_funds"):
        bal = d.tb.get(aid, ZERO)
        prior = d.tb_prior.get(aid, ZERO)
        if bal != 0:
            stale = prior > 0
            r.flag("medium" if stale else "low", "undeposited",
                   f"{d.name(aid)} holds {fmt(bal)} at month-end" + (f" (it held {fmt(prior)} last month too)" if stale else ""),
                   bal, ("Customer payments recorded but not matched to a bank deposit for over a month. "
                         if stale else "Payments received late in the month and not yet deposited. ") +
                   "Each should match a deposit on the bank statement.",
                   [f"TB {d.name(aid)}"], "Match each receipt to its bank deposit; anything that never arrived is a mistake.",
                   accounts=[aid], subject=d.name(aid))
    r.summary = "; ".join(f"{t['item']}: ledger {fmt(t['books'])}, aging {fmt(t['other'])}, {t['status']}" for t in r.tieouts)
    return r


def t5_payroll(d: Data, ctx: dict) -> CheckResult:
    title = "Payroll in the books matches the payroll provider"
    if not d.cfg.get("uses_payroll_file"):
        return not_run("T5", title, "uses_payroll_file is false (payroll file is optional)")
    ids = d.ids_for("payroll_wages") | d.ids_for("payroll_taxes")
    if not ids:
        return not_run("T5", title, "roles payroll_wages / payroll_taxes are empty")
    r = CheckResult("T5", title)
    tol = threshold(d.cfg, "tie_tolerance")
    mat = threshold(d.cfg, "materiality_abs")
    p = d.period
    books = sum((l["amount"] for l in d.gl if l["account_id"] in ids), ZERO)
    pay_lines = [l for l in d.gl if l["account_id"] in ids]
    runs = d.payroll or []
    in_month = [x for x in runs if p.contains(x["check_date"])]
    not_booked = [x for x in in_month if not any(days_between(l["date"], x["check_date"]) <= 3 for l in pay_lines)]
    if d.payroll_has_periods:
        earned, unpaid = ZERO, []
        for x in runs:
            days = (x["period_end"] - x["period_start"]).days + 1
            share = Decimal(overlap_days(x["period_start"], x["period_end"], p.start, p.end)) / Decimal(days)
            if share == 0:
                continue
            cost = (x["gross"] + x["employer_taxes"]) * share
            earned += cost
            if x["check_date"] > p.end:
                unpaid.append((x, cost))
        slack = max(mat, earned * Decimal("0.02"))
        diff = books - earned
        status = "tie" if abs(diff) <= slack else "differs"
        r.tie("Payroll cost earned in the month (wages + employer taxes)", books, earned,
              "payroll file, pay periods prorated by days", status,
              f"compared within {fmt(slack)}, because accruals are usually by working days, not calendar days")
        if status == "differs":
            hints = [f"the {x['check_date']} run ({fmt(x['gross'] + x['employer_taxes'])}) has no payroll entry near its date"
                     for x in not_booked]
            hints += [f"the {x['period_start']} to {x['period_end']} period is paid {x['check_date']}, after month-end; "
                      f"about {fmt(c)} of it was earned in {p.label} (by calendar days)" for x, c in unpaid]
            r.flag("high", "payroll-differs", f"Payroll cost in the books differs from what was earned by {fmt(diff)}", diff,
                   f"Books {fmt(books)} vs earned {fmt(earned)}. " + ("; ".join(hints) + "." if hints else ""),
                   [f"GL account {d.name(a)}" for a in ids] + ["payroll.csv"],
                   "Book each pay run, and accrue wages earned this month but paid next month.",
                   keys=[key(x["check_date"], x["gross"] + x["employer_taxes"]) for x in not_booked],
                   accounts=ids | d.ids_for("accrued_payroll"), subject="payroll")
            if unpaid:
                acct, wages = pick(d, "accrued_payroll"), pick(d, "payroll_wages")
                r.draft(f"Accrue {p.label} payroll paid after month-end", [
                    (d.name(wages) if wages else "(wage expense account)", None, None),
                    (d.name(acct) if acct else "(accrued payroll account)", None, None)], status="needs your amount",
                    basis=f"about {fmt(sum((c for _, c in unpaid), ZERO))} by calendar days; use your payroll accrual method")
    else:
        r.part("payroll accrual", "not_run", "the payroll file has no pay-period dates (set period_start_col / period_end_col)")
        paid = sum((x["gross"] + x["employer_taxes"] for x in in_month), ZERO)
        acc_ids = d.ids_for("accrued_payroll")
        delta_acc = -sum((d.tb.get(a, ZERO) - d.tb_prior.get(a, ZERO) for a in acc_ids), ZERO)
        expected = paid + delta_acc
        diff = books - expected
        status = "tie" if abs(diff) <= tol else "differs"
        r.tie("Payroll cost (wages + employer taxes)", books, expected,
              "payroll file by check date" + (" + change in accrued payroll" if acc_ids else ""), status)
        if status == "differs":
            hint = "; ".join(f"the {x['check_date']} run ({fmt(x['gross'] + x['employer_taxes'])}) has no payroll entry near its date"
                             for x in not_booked)
            r.flag("high", "payroll-differs", f"Payroll cost in the books differs from the provider by {fmt(diff)}", diff,
                   f"Books {fmt(books)} vs {fmt(expected)} from the payroll file"
                   + (" and accrued payroll" if acc_ids else "") + (f". {hint}." if hint else
                   ". If payroll is accrued, add the accrued payroll account to roles.accrued_payroll."),
                   [f"GL account {d.name(a)}" for a in ids] + ["payroll.csv"],
                   "Find the run that's missing, doubled or in another month.",
                   keys=[key(x["check_date"], x["gross"] + x["employer_taxes"]) for x in not_booked],
                   accounts=ids, subject="payroll")
    r.summary = f"Payroll: books {fmt(books)}; {len(in_month)} run(s) paid this month"
    return r
