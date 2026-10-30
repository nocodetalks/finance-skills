"""C1 duplicates and missing recurring bills · C2 cutoff and accruals · C3 prepaids, deferred revenue,
annual plans · C4 missing recurring journal entries and loan interest · C5 one vendor under two names."""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from decimal import Decimal
from difflib import SequenceMatcher

from ..config import SUSPENSE_PATTERN, threshold
from ..data import Data
from ..util import ZERO, add_days, days_between, fmt, memo_key, norm, vendor_key
from .base import CheckResult, is_je, key, pick

ANNUAL = re.compile(r"\bannual|\byearly|\b12[- ]?month|\bone[- ]year|\b1[- ]year|\byear plan|\bper year", re.I)
PAYMENT_TYPES = re.compile(r"bill payment|payment", re.I)


def _history_months_with_data(d: Data) -> list[str]:
    return sorted({l["date"].strftime("%Y-%m") for l in d.gl_history})


def _skip_txns(d: Data) -> set:
    """Payroll, Stripe fees and clearing-account transactions are left to their own checks (and payroll keeps people
    out of the outputs)."""
    skip = d.payroll_ids() | d.ids_for("stripe_fees") | d.ids_for("processor_clearing")
    return {l["txn_id"] for l in d.gl + d.gl_history if l["account_id"] in skip}


def _expense_txns(d: Data, lines: list, skip: set) -> dict:
    tx = {}
    for l in lines:
        if d.cls(l["account_id"]) == "Expense" and l["amount"] > 0 and l["name"]:
            if l["txn_id"] in skip:
                continue
            t = tx.setdefault(l["txn_id"], {"name": l["name"], "vendor": vendor_key(l["name"]), "date": l["date"],
                                            "total": ZERO, "refs": [], "doc": l["doc_num"], "type": l["txn_type"],
                                            "accounts": set()})
            t["total"] += l["amount"]
            t["refs"].append(f"GL {l['line_id']}")
            t["accounts"].add(l["account_id"])
    return tx


def similar_vendors(a: str, b: str, cfg: dict) -> bool:
    """Two different vendor keys that are near-identical: one is the other with letters added or dropped (Acme Design
    / Acme Designs, Jonson / Johnson), at least vendor_similarity_pct alike, both long enough to judge, and with the
    same digits. A swapped letter (Smith / Smyth) or a different number (Building 1 / Building 2) doesn't count:
    those are usually different businesses."""
    short, long_ = sorted((a, b), key=len)
    if (a == b or len(short) < int(threshold(cfg, "vendor_match_min_length")) or len(long_) - len(short) > 3
            or re.findall(r"\d", a) != re.findall(r"\d", b)):
        return False
    rest = iter(long_)
    if not all(c in rest for c in short):
        return False                              # a letter differs, not just added or dropped
    return Decimal(str(SequenceMatcher(None, a, b).ratio())) * 100 >= threshold(cfg, "vendor_similarity_pct")


def c1_duplicates_recurring(d: Data, ctx: dict) -> CheckResult:
    r = CheckResult("C1", "Duplicate bills and expenses; missing recurring bills")
    p = d.period
    win = int(threshold(d.cfg, "duplicate_window_days"))
    dmin = threshold(d.cfg, "duplicate_min_amount")
    skip_txn = _skip_txns(d)

    # Payees match by vendor_key, so "Home Depot" and "THE HOME DEPOT #4521" are one vendor here.
    tail = [l for l in d.gl_history if (p.start - l["date"]).days <= win]
    tx = _expense_txns(d, d.gl + tail, skip_txn)
    current = {t for t, v in tx.items() if p.contains(v["date"])}
    items = sorted(tx.items(), key=lambda kv: kv[1]["date"])
    seen, n_dup = set(), 0
    for i, (a_id, a) in enumerate(items):
        for b_id, b in items[i + 1:]:
            if (b_id in seen or a["vendor"] != b["vendor"] or a["total"] != b["total"] or a["total"] < dmin
                    or days_between(a["date"], b["date"]) > win or not ({a_id, b_id} & current)):
                continue
            seen.add(b_id)
            n_dup += 1
            names = a["name"] + (f" / {b['name']}" if norm(a["name"]) != norm(b["name"]) else "")
            r.flag("high", "duplicate", f"Possible duplicate: {names} {fmt(a['total'])} on {a['date']} and {b['date']}",
                   b["total"], f"{a['type']} {a['doc'] or ''} / {b['type']} {b['doc'] or ''}. Same payee and amount "
                   f"within {win} days.", a["refs"] + b["refs"], "If it was entered twice, void one.",
                   txns=[a_id, b_id], accounts=a["accounts"] | b["accounts"], subject=f"{a['name']}:{b['date']}")

    bwin = int(threshold(d.cfg, "bill_expense_window_days"))
    cur_tx = {t: v for t, v in _expense_txns(d, d.gl, skip_txn).items() if not v["type"].lower().startswith("bill")}
    n_be = 0
    for ob in d.open_bills:
        if ob["txn_date"] > p.end:
            continue
        for t, v in cur_tx.items():
            if (v["vendor"] == vendor_key(ob["vendor"]) and v["total"] in (ob["amount"], ob["balance"])
                    and v["total"] >= dmin and 0 <= (v["date"] - ob["txn_date"]).days <= bwin
                    and not PAYMENT_TYPES.search(v["type"])):
                n_be += 1
                r.flag("high", "bill-and-expense",
                       f"{ob['vendor']} {fmt(v['total'])}: a bill is still open in AP and the same amount was paid as "
                       f"a {v['type'].lower()} on {v['date']}", v["total"],
                       f"Bill {ob['doc_num'] or ob['bill_id']} dated {ob['txn_date']} is unpaid in QuickBooks, so the "
                       "expense is probably counted twice (the payment wasn't matched to the bill).",
                       [f"bill {ob['bill_id']}"] + v["refs"],
                       "Delete the separate expense and record the payment against the bill.",
                       txns=[t], accounts=v["accounts"], subject=f"{ob['bill_id']}")

    months = _history_months_with_data(d)
    need = int(threshold(d.cfg, "recurring_min_months"))
    n_missing = 0
    if len(months) < need:
        r.part("missing recurring bills", "not_run", f"only {len(months)} month(s) of history; needs {need}")
    else:
        min_amt = threshold(d.cfg, "recurring_min_amount")
        by_vendor = defaultdict(lambda: defaultdict(lambda: ZERO))
        display, accts = {}, defaultdict(set)
        for l in d.gl_history:
            if (d.cls(l["account_id"]) == "Expense" and l["amount"] > 0 and l["name"] and l["txn_id"] not in skip_txn):
                by_vendor[vendor_key(l["name"])][l["date"].strftime("%Y-%m")] += l["amount"]
                display[vendor_key(l["name"])] = l["name"]
                accts[vendor_key(l["name"])].add(l["account_id"])
        cur_accts, cur_display = defaultdict(set), {}
        for l in d.gl:
            if d.cls(l["account_id"]) == "Expense" and l["amount"] > 0:
                cur_accts[vendor_key(l["name"])].add(l["account_id"])
                cur_display[vendor_key(l["name"])] = l["name"]
        cutoff = {vendor_key(v) for v in ctx.get("cutoff_vendors", set())}
        for v, ms in by_vendor.items():
            if len(ms) < need or v in cur_accts or v in cutoff:
                continue
            med = statistics.median(ms.values())
            if med < min_amt:
                continue
            n_missing += 1
            # A rename that adds or drops a letter isn't the same key; say so, but don't hide the item on a guess.
            near = [cur_display[c] for c in sorted(cur_accts) if cur_accts[c] & accts[v] and similar_vendors(v, c, d.cfg)]
            r.flag("high", "missing-recurring", f"{display[v]} billed in {len(ms)} of the last {len(months)} months, "
                                                f"but nothing in {p.label} (usually about {fmt(med)})",
                   med, f"Last seen {max(ms)}. No bill dated in or just after {p.label} mentions this month."
                   + (f" \"{near[0]}\", a near-identical name, was billed to the same account this month (see C5); if "
                      "it's the same vendor, merge the two in QuickBooks." if near else ""),
                   [f"history {display[v]}"], "Find the bill or accrue it. If the service stopped, acknowledge this item.",
                   accounts=accts[v], subject=norm(display[v]))
            acc = pick(d, "accrued", reversing_only=True)
            r.draft(f"Accrue {display[v]} {p.label}", [(d.name(sorted(accts[v], key=d.name)[0]) if len(accts[v]) == 1
                                                        else "(expense account)", None, None),
                                                       (d.name(acc) if acc else "(accrued liabilities account)", None, None)],
                    status="needs your amount", basis=f"usual monthly amount {fmt(med)} (median of {len(ms)} months)")
        r.part("missing recurring bills", "run")
    r.summary = f"{n_dup} possible duplicate(s); {n_be} bill paid twice; {n_missing} recurring vendor(s) missing"
    return r


def c2_cutoff_accruals(d: Data, ctx: dict) -> CheckResult:
    r = CheckResult("C2", "Cutoff and accrual reversals")
    p = d.period
    late = [b for b in d.bills if p.contains(b["txn_date"]) and b["created_date"] and b["created_date"] > p.end]
    for b in late:
        r.flag("low", "entered-late", f"Bill from {b['vendor']} dated {b['txn_date']} was entered on {b['created_date']}, after month-end",
               b["amount"], f"{fmt(b['amount'])} to {b['account']}. It is in the books now; check it was in anything already "
               "sent out for the month.", [f"bill {b['bill_id']}"], "No fix needed if nothing was sent yet.",
               subject=f"{b['bill_id']}")
    ctx["cutoff_vendors"] = set()
    accrued = pick(d, "accrued", reversing_only=True)
    accrual_credits = [l for l in d.gl if l["account_id"] in d.ids_for("accrued") and l["amount"] < 0]
    for b in [b for b in d.bills if b["txn_date"] > p.end]:
        if p.mentioned_in(f"{b['description']} {b['doc_num']}"):
            already = [l for l in accrual_credits if -l["amount"] == b["amount"]
                       or norm(b["vendor"]) in norm(f"{l['name']} {l['memo']}")]
            if already:
                ctx["cutoff_vendors"].add(norm(b["vendor"]))
                r.part(f"cutoff: {b['vendor']} bill {b['bill_id']}", "run", "already accrued in the month")
                continue
            ctx["cutoff_vendors"].add(norm(b["vendor"]))
            acct = next((a for a, x in d.accounts.items() if norm(x["full_name"]) == norm(b["account"])
                         or norm(x["name"]) == norm(b["account"])), None)
            r.flag("high", "cutoff", f"{b['vendor']} bill dated {b['txn_date']} looks like {p.label} cost: {fmt(b['amount'])}",
                   b["amount"], f"Description: \"{b['description']}\". It is booked in the following month, so {p.label} "
                   "is understated.", [f"bill {b['bill_id']}"], f"Accrue it in {p.label} and reverse next month.",
                   accounts=[acct] if acct else [], subject=f"{b['bill_id']}")
            r.draft(f"Accrue {b['vendor']} {p.label} (bill {b['bill_id']})",
                    [(b["account"], b["amount"], None),
                     (d.name(accrued) if accrued else "(choose a reversing accrued-liabilities account)", None, b["amount"])],
                    status="ready" if accrued else "confirm first")
    behavior = {norm(k): v for k, v in (d.cfg.get("accrual_behavior") or {}).items()}
    rollforward = []
    for aid in sorted(d.ids_for("accrued")):
        if behavior.get(norm(d.name(aid)), "reversing") == "rollforward":
            rollforward.append(d.name(aid))
            continue
        prior = -d.tb_prior.get(aid, ZERO)
        releases = [l for l in d.gl if l["account_id"] == aid and l["amount"] > 0]
        released = sum((l["amount"] for l in releases), ZERO)
        if prior > 0 and released + Decimal("0.01") < prior:
            r.flag("high", "not-reversed", f"{d.name(aid)}: {fmt(prior - released)} of last month's accruals was never reversed",
                   prior - released, f"Balance at {p.prior.end}: {fmt(prior)}. Released in {p.label}: {fmt(released)}. "
                   "If this account builds up on purpose (bonus, audit, PTO), set it to \"rollforward\" in accrual_behavior.",
                   [f"TB {d.name(aid)}"] + [f"GL {l['line_id']}" for l in releases],
                   "Reverse last month's accrual (the real bill is now booked), or explain why it stays.",
                   txns=[l["txn_id"] for l in releases], accounts=[aid], subject=d.name(aid))
        amounts = defaultdict(list)
        for l in releases:
            amounts[l["amount"]].append(l)
        for amt, ls in amounts.items():
            if len(ls) >= 2 and prior > 0 and amt >= prior / 2:
                r.flag("high", "reversed-twice", f"{d.name(aid)}: the same {fmt(amt)} release is booked {len(ls)} times",
                       amt * (len(ls) - 1), "An accrual reversal looks duplicated.", [f"GL {l['line_id']}" for l in ls],
                       "Void the duplicate reversal.", txns=[l["txn_id"] for l in ls], accounts=[aid],
                       subject=f"{d.name(aid)}:{amt}")
    if rollforward:
        r.part("reversal check", "skipped", "set to rollforward (builds up on purpose): " + ", ".join(rollforward))
    r.summary = (f"{len(late)} bill(s) entered after month-end; {len(ctx['cutoff_vendors'])} next-month bill(s) "
                 f"that look like {p.label} cost")
    return r


def _monthly_movement(d: Data, aid: str, sign: int) -> dict:
    """{month: amount} of credits (sign=-1) or debits (sign=+1) to an account in the history."""
    out = defaultdict(lambda: ZERO)
    for l in d.gl_history:
        if l["account_id"] == aid and (l["amount"] * sign) > 0:
            out[l["date"].strftime("%Y-%m")] += abs(l["amount"])
    return out


def c3_prepaid_deferred(d: Data, ctx: dict) -> CheckResult:
    r = CheckResult("C3", "Prepaid expenses, deferred revenue and annual plans")
    p = d.period
    pmin = threshold(d.cfg, "prepaid_min_amount")
    rmin = threshold(d.cfg, "annual_revenue_min")
    floor = threshold(d.cfg, "amortization_floor")
    hist_names = {norm(l["name"]) for l in d.gl_history if l["name"]}
    prepaid_acct = pick(d, "prepaid")
    ctx.setdefault("c3_accounts", set())
    for l in d.gl:
        if (d.cls(l["account_id"]) == "Expense" and l["amount"] >= pmin and l["name"] and not is_je(l)
                and l["account_id"] not in d.payroll_ids() and not SUSPENSE_PATTERN.search(l["account"])):
            text = f"{l['memo']} {l['name']}"
            new_payee = norm(l["name"]) not in hist_names
            if ANNUAL.search(text) or new_payee:
                why = "the memo says it covers a year" if ANNUAL.search(text) else "this payee is new in the last months"
                r.flag("medium", "maybe-prepaid", f"{fmt(l['amount'])} to {l['name']} expensed in {p.label}; may be a prepaid",
                       l["amount"], f"Booked to {l['account']} on {l['date']}; {why}. If it covers several months, only "
                       "this month's share is an expense.", [f"GL {l['line_id']}"],
                       "If it covers N months: move (N-1)/N of it to prepaid and expense 1/N each month.",
                       txns=[l["txn_id"]], accounts=[l["account_id"]], subject=f"{l['name']}:{l['date']}")
                r.draft(f"Prepaid: {l['name']} {l['date']}", [(d.name(prepaid_acct) if prepaid_acct else "(prepaid account)", None, None),
                                                               (l["account"], None, None)],
                        status="needs your amount", basis=f"{fmt(l['amount'])} paid; enter the unused part")
    for role, sign, label, verb in (("prepaid", -1, "Prepaid", "amortized"), ("deferred_revenue", 1, "Deferred revenue", "released")):
        for aid in sorted(d.ids_for(role)):
            prior = d.tb_prior.get(aid, ZERO) * (1 if role == "prepaid" else -1)
            now = sum((abs(l["amount"]) for l in d.gl if l["account_id"] == aid and l["amount"] * sign > 0), ZERO)
            hist = _monthly_movement(d, aid, sign)
            regular = [v for v in hist.values() if v > 0]
            hist_txns = {l["txn_id"] for l in d.gl_history if l["account_id"] == aid and l["amount"] * sign > 0}
            other_side = {l["account_id"] for l in d.gl_history if l["txn_id"] in hist_txns and l["account_id"] != aid}
            if len(regular) >= 3:
                med = statistics.median(regular)
                if med >= floor and now < med / 2 and prior >= floor:
                    ctx["c3_accounts"].add(aid)
                    r.flag("medium", f"{role}-short", f"{d.name(aid)} {verb} {fmt(now)} in {p.label}; usually about {fmt(med)}",
                           med - now, f"Balance at {p.prior.end}: {fmt(prior)}. At least one schedule seems to have been "
                           "skipped this month.", [f"TB {d.name(aid)}"], f"Book this month's {verb[:-1]} entry for each item.",
                           accounts={aid} | other_side, subject=d.name(aid))
            elif prior >= floor and now == 0:
                ctx["c3_accounts"].add(aid)
                r.flag("medium", f"{role}-none", f"{d.name(aid)} {verb} nothing in {p.label}", prior,
                       f"Balance at {p.prior.end} was {fmt(prior)}.", [f"TB {d.name(aid)}"], f"Book this month's entry.",
                       accounts={aid} | other_side, subject=d.name(aid))
    def_txns = {l["txn_id"] for l in d.gl if l["account_id"] in d.ids_for("deferred_revenue") and l["amount"] > 0}
    for l in d.gl:
        if (d.cls(l["account_id"]) == "Revenue" and -l["amount"] >= rmin and l["name"] and not is_je(l)
                and l["txn_id"] not in def_txns and ANNUAL.search(f"{l['memo']} {l['name']}")):
            dr = pick(d, "deferred_revenue")
            r.flag("high", "annual-as-revenue", f"{fmt(-l['amount'])} from {l['name']} booked as {p.label} revenue looks like an annual plan",
                   -l["amount"], f"Memo: \"{l['memo']}\". Only the part earned this month is revenue.", [f"GL {l['line_id']}"],
                   "Move the unearned part to deferred revenue.", txns=[l["txn_id"]], accounts=[l["account_id"]],
                   subject=f"{l['name']}:{l['date']}")
            r.draft(f"Defer {l['name']} {l['date']}", [(l["account"], None, None), (d.name(dr) if dr else "(deferred revenue account)", None, None)],
                    status="needs your amount", basis=f"{fmt(-l['amount'])} billed; enter the unearned part")
    # Annual / multi-month plans billed in Stripe
    if d.invoice_lines is None:
        r.part("plans billed in Stripe", "not_run",
               "needs Stripe invoice data (API pull)" if d.cfg.get("uses_stripe") else "uses_stripe is false")
    else:
        minp = int(threshold(d.cfg, "deferral_min_period_days"))
        billed = {i["invoice_id"] for i in d.invoices if i["finalized_date"] and p.contains(i["finalized_date"])}
        expected, lines = ZERO, []
        for ln in d.invoice_lines:
            if ln["invoice_id"] not in billed or not ln["period_start"] or not ln["period_end"]:
                continue
            days = (ln["period_end"] - ln["period_start"]).days
            if days < minp or ln["period_end"] <= p.end:
                continue
            after = (ln["period_end"] - max(p.end, ln["period_start"])).days
            unearned = ln["amount"] * Decimal(after) / Decimal(days)
            expected += unearned
            lines.append(ln)
        booked = -sum((l["amount"] for l in d.gl if l["account_id"] in d.ids_for("deferred_revenue") and l["amount"] < 0), ZERO)
        tol = max(threshold(d.cfg, "tie_tolerance"), expected * Decimal("0.01"))
        if expected > 0:
            st = "tie" if booked + tol >= expected else "differs"
            r.tie("New deferred revenue vs Stripe plans billed for service beyond month-end", booked, expected,
                  "Stripe invoices (unearned share by days)", st)
            if st == "differs":
                r.flag("high", "stripe-plans-not-deferred",
                       f"Stripe billed {len(lines)} plan line(s) covering service after {p.end}; about {fmt(expected)} is "
                       f"unearned, but deferred revenue only received {fmt(booked)}", expected - booked,
                       "Annual or multi-month plans were recognized as revenue up front.",
                       [f"stripe line {ln['line_id']}" for ln in lines[:8]],
                       "Move the unearned part to deferred revenue and release it monthly.",
                       accounts=d.ids_for("deferred_revenue") | d.ids_for("stripe_revenue"), subject="stripe-plans")
        r.part("plans billed in Stripe", "run")
    r.summary = f"{len(r.exceptions)} prepaid/deferred item(s) to look at"
    return r


def c4_recurring_entries(d: Data, ctx: dict) -> CheckResult:
    r = CheckResult("C4", "Monthly journal entries (depreciation, amortization, accruals) and loan interest")
    months = _history_months_with_data(d)
    need = int(threshold(d.cfg, "recurring_min_months"))
    floor = threshold(d.cfg, "amortization_floor")
    if len(months) < need:
        r.part("recurring journal entries", "not_run", f"only {len(months)} month(s) of history; needs {need}")
    else:
        seen = defaultdict(lambda: defaultdict(lambda: ZERO))
        label, accts = {}, defaultdict(set)
        for l in d.gl_history:
            if is_je(l) and l["amount"] > 0 and memo_key(l["memo"]):
                k = memo_key(l["memo"])
                seen[k][l["date"].strftime("%Y-%m")] += l["amount"]
                label[k] = l["memo"]
                accts[k].add(l["account_id"])
        cur_je = [l for l in d.gl if is_je(l) and l["amount"] > 0]
        now = {memo_key(l["memo"]) for l in cur_je}
        skip = ctx.get("c3_accounts", set())
        for k, ms in seen.items():
            if len(ms) < need or k in now:
                continue
            all_accts = {l["account_id"] for l in d.gl_history if is_je(l) and memo_key(l["memo"]) == k}
            if all_accts & skip:
                continue
            med = statistics.median(ms.values())
            if med < floor:
                continue
            renamed = [l for l in cur_je if l["account_id"] in accts[k] and abs(l["amount"] - med) <= med * Decimal("0.1")]
            if renamed:
                continue   # same account, similar amount, different memo: treat as booked
            r.flag("high", "missing-je", f"Monthly entry \"{label[k]}\" is missing in {d.period.label}",
                   med, f"Booked in {len(ms)} of the last {len(months)} months ({', '.join(sorted(d.name(a) for a in accts[k]))}), "
                   f"usually about {fmt(med)}.", [f"history {label[k]}"],
                   "Book this month's entry, or acknowledge it if it has ended.",
                   accounts=all_accts, subject=k)
        r.part("recurring journal entries", "run")
    loans, interest = d.ids_for("loans"), d.ids_for("loan_interest")
    if loans and interest:
        bal = -sum((d.tb.get(a, ZERO) for a in loans), ZERO)
        booked = sum((l["amount"] for l in d.gl if l["account_id"] in interest), ZERO)
        if bal > 0 and booked <= 0:
            r.flag("high", "no-interest", f"Loans of {fmt(bal)} are outstanding but no interest was booked in {d.period.label}",
                   bal, "Interest accrues every month on a loan balance.", [f"TB {d.name(a)}" for a in loans],
                   "Accrue this month's interest from the loan agreement or lender statement.", accounts=loans | interest,
                   subject="interest")
    elif loans:
        r.part("loan interest", "not_run", "roles.loan_interest is empty")
    r.summary = f"{len(r.exceptions)} missing monthly entr(ies)"
    return r


def c5_vendor_names(d: Data, ctx: dict) -> CheckResult:
    """Vendor records whose names are the same once normalized (vendor_key), or near-identical and used for the same
    expense account. At least one of the names must have been used this month."""
    r = CheckResult("C5", "The same vendor under two names (duplicate vendor records)")
    skip_txn = _skip_txns(d)
    vendors = {}                                  # one QuickBooks name record per normalized name
    for lines, now in ((d.gl_history, False), (d.gl, True)):
        for l in lines:
            if d.cls(l["account_id"]) != "Expense" or l["amount"] <= 0 or not l["name"] or l["txn_id"] in skip_txn:
                continue
            v = vendors.setdefault(norm(l["name"]), {"name": l["name"], "key": vendor_key(l["name"]), "total": ZERO,
                                                     "lines": 0, "now": False, "last": l["date"], "refs": [],
                                                     "accounts": set()})
            v["total"] += l["amount"]
            v["lines"] += 1
            v["now"] = v["now"] or now
            v["last"] = max(v["last"], l["date"])
            v["refs"].append(f"GL {l['line_id']}")
            v["accounts"].add(l["account_id"])
    by_key = defaultdict(list)
    for n in sorted(vendors):
        by_key[vendors[n]["key"]].append(n)
    parent = {k: k for k in by_key}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    accts = {k: set().union(*(vendors[n]["accounts"] for n in ns)) for k, ns in by_key.items()}
    keys = sorted(by_key)
    for a in (k for k in keys if any(vendors[n]["now"] for n in by_key[k])):
        for b in keys:
            if a != b and accts[a] & accts[b] and similar_vendors(a, b, d.cfg):
                parent[find(b)] = find(a)
    clusters = defaultdict(list)
    for k, ns in by_key.items():
        clusters[find(k)] += ns
    for root in sorted(clusters):
        names = clusters[root]
        if len(names) < 2 or not any(vendors[n]["now"] for n in names):
            continue
        recs = sorted((vendors[n] for n in names), key=lambda v: (-v["total"], v["name"]))
        why = ("They match once punctuation, store numbers, a leading \"The\", company suffixes and card-processor "
               "prefixes are dropped." if len({v["key"] for v in recs}) == 1 else
               "The names are near-identical (letters added or dropped) and are used for the same expense account.")
        listed = [f"\"{v['name']}\"" for v in recs]
        r.flag("low", "vendor-names",
               "Possible duplicate vendor records: " + (" and ".join(listed) if len(listed) == 2 else ", ".join(listed)),
               sum((v["total"] for v in recs[1:]), ZERO),
               "; ".join(f"\"{v['name']}\" {fmt(v['total'])} over {v['lines']} line(s), last {v['last']}, to "
                         + ", ".join(sorted(d.name(a) for a in v["accounts"])) for v in recs)
               + f". {why} Two records for one vendor split its spend (and its 1099 total), can hide a bill paid "
               "twice, and make a renamed vendor look like a missing bill.",
               [ref for v in recs for ref in v["refs"][-3:]],
               "If they're the same vendor, merge them in QuickBooks (rename one to exactly the other's name; "
               "QuickBooks offers to merge). If they're different businesses, acknowledge this item.",
               subject="vendors:" + "|".join(sorted(names)))
    r.summary = f"{len(r.exceptions)} possible duplicate vendor record(s) among {len(vendors)} vendor name(s)"
    return r
