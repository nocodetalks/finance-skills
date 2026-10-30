"""K1 wrong account, suspense, impossible signs · K2 accounts vs their own history · K3 unusual inflows ·
K4 transfers between your own accounts booked as income · K5 payroll split between cost of sales and opex."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal

from ..config import SUSPENSE_PATTERN, threshold
from ..data import Data
from ..util import ZERO, days_between, fmt, norm, q2, vendor_key
from .base import CheckResult, key, not_run, sum_amt

PROCESSORS = re.compile(r"\b(stripe|paypal|square|shopify|braintree|adyen|gocardless|toast|clover)\b", re.I)
TRANSFER = re.compile(r"\b(transfer|xfer|trf)\b|\b(from|to) (savings|checking|treasury|money market|reserve)\b", re.I)
# A customer's wire or Zelle payment, or a fee for one, isn't a move between your own accounts.
NOT_TRANSFER = re.compile(r"\b((incoming|outgoing|domestic|international|intl|fed) )?wire transfer\b|"
                          r"\b(zelle|venmo|cash app)( payment)? transfer\b|\btransfer (fees?|agent|pricing|tax(es)?)\b", re.I)
CARD_PAYMENT = re.compile(r"\b(credit card|card|cc) (payment|pmt)\b", re.I)
CARD_PAYMENT_IN = re.compile(r"\b(credit card|card|cc) (payment|pmt)\b|\bpayment\W+(received|thank you)\b|\bautopay\b", re.I)


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


def k4_transfers(d: Data, ctx: dict) -> CheckResult:
    """Money moving between your own bank, card or cash accounts but booked to income (or, the mirror case, money
    leaving a bank account booked to an expense). The evidence is the same amount going the other way in another of
    your accounts within the window, or a payee, memo or bank description that reads like a transfer."""
    r = CheckResult("K4", "Transfers between your own accounts booked as income (or as an expense)")
    p = d.period
    win = int(threshold(d.cfg, "transfer_window_days"))
    floor = threshold(d.cfg, "transfer_min_amount")
    cash, card = d.ids_for("cash"), d.ids_for("card")
    own, clearing = cash | card, d.ids_for("processor_clearing")
    skip = d.payroll_ids() | d.ids_for("stripe_fees")
    company = vendor_key(d.cfg.get("company", ""))
    match = ctx.get("match", {})
    if len(own) < 2:
        r.part("matched against your other accounts", "not_run", "roles.cash and roles.card hold only one account, so "
               "there's nothing to match against; the payee and memo test still ran")
    elif len(cash) < 2:
        r.part("deposits matched against your other bank accounts", "not_run", "roles.cash holds one bank account, so a "
               "deposit into it is judged by its payee and memo only")
    bank_text = {}
    for m in match.values():
        for b, rows, _ in m.pairs:
            bank_text[b["line_id"]] = "; ".join(x["description"] for x in rows)
    lines = d.gl + [l for l in d.gl_history if 0 < (p.start - l["date"]).days <= win]
    by_txn, by_amount = defaultdict(list), defaultdict(list)
    for l in lines:
        by_txn[l["txn_id"]].append(l)
        if l["account_id"] in own:
            by_amount[l["amount"]].append(l)

    def texts(ls):
        out = {}
        for t in (t for l in ls for t in (l["name"], l["memo"], bank_text.get(l["line_id"], "")) if t):
            out.setdefault(norm(t), t)
        return list(out.values())

    def transferish(ls, card_words=None):
        t = NOT_TRANSFER.sub(" ", " · ".join(texts(ls)))
        return bool(TRANSFER.search(t) or (card_words and card_words.search(t))
                    or (company and any(vendor_key(x) == company for x in texts(ls))))

    def excluded(ls):
        """Invoices and credit memos are sales; Stripe and the other processors are T3's and T4's."""
        return (any(l["account_id"] in clearing or l["txn_type"].lower() in ("invoice", "credit memo") for l in ls)
                or bool(PROCESSORS.search(" ".join(texts(ls)))))

    def ordinary(ls):
        """A payment to or from someone else: a named payee that doesn't read like a transfer, and every other line is
        income, an expense, AR or AP (not suspense)."""
        if not any(l["name"] for l in ls) or transferish(ls, CARD_PAYMENT):
            return False
        others = [l for l in ls if l["account_id"] not in own]
        return bool(others) and all((d.is_pl(l["account_id"]) or d.atype(l["account_id"]) in ("Accounts Receivable",
                                                                                                "Accounts Payable"))
                                    and not SUSPENSE_PATTERN.search(d.name(l["account_id"])) for l in others)

    used, used_rows = set(), set()

    def counterpart(tid, line, accounts):
        """The same money going the other way in another of `accounts` within the window: a book line that isn't an
        ordinary payment, or a statement row that isn't in the books. Closest date first, then the books."""
        a, found = line["account_id"], []
        left = {b["line_id"] for b in match[a].book_left} if a in match else set()
        for l in by_amount.get(-line["amount"], []):
            ls = by_txn[l["txn_id"]]
            if (l["account_id"] == a or l["account_id"] not in accounts or l["txn_id"] in used or l["txn_id"] == tid
                    or days_between(l["date"], line["date"]) > win or excluded(ls) or ordinary(ls)):
                continue
            pair = [x for x in ls if x["account_id"] in own and x["account_id"] != l["account_id"]]
            # Already a whole transfer: it's only this money if it lands in this account and the bank shows it once.
            if pair and not (any(x["account_id"] == a for x in pair)
                             and ({line["line_id"]} | {x["line_id"] for x in pair}) & left):
                continue
            found.append((days_between(l["date"], line["date"]), 0, l["line_id"], "book", l))
        for b in sorted(accounts & set(match) - {a}):
            for x in match[b].bank_left:
                if (x["amount"] == -line["amount"] and (b, x["row"]) not in used_rows
                        and (b, x["row"]) not in ctx.get("t3_rows", set())
                        and days_between(x["date"], line["date"]) <= win and not PROCESSORS.search(x["description"])):
                    found.append((days_between(x["date"], line["date"]), 1, str(x["row"]), "statement", (b, x)))
        return min(found, key=lambda f: f[:3]) if found else None

    n = {"income": 0, "expense": 0}
    order = sorted({l["txn_id"] for l in d.gl}, key=lambda t: (min(l["date"] for l in by_txn[t]), t))
    for side in ("income", "expense"):
        for tid in order:
            ls = by_txn[tid]
            if tid in used or excluded(ls):
                continue
            if side == "income":
                booked = [l for l in ls if d.cls(l["account_id"]) == "Revenue" and l["amount"] < 0]
                money = [l for l in ls if l["account_id"] in own and l["amount"] > 0]
                words = CARD_PAYMENT_IN if any(l["account_id"] in card for l in money) else None
            else:
                booked = [l for l in ls if d.cls(l["account_id"]) == "Expense" and l["amount"] > 0
                          and l["account_id"] not in skip]
                money = [l for l in ls if l["account_id"] in cash and l["amount"] < 0]
                words = CARD_PAYMENT
            if not booked or not money:
                continue
            line = max(money, key=lambda l: (abs(l["amount"]), l["line_id"]))
            amt = min(abs(line["amount"]), abs(sum_amt(booked)))
            # Money into income came out of another bank account; money out to an expense may have paid a card too.
            hit = counterpart(tid, line, cash if side == "income" else own) if len(own) > 1 and amt >= floor else None
            said = amt >= floor and transferish(ls, words)
            if not (hit or said):
                continue
            used.add(tid)
            n[side] += 1
            a = line["account_id"]
            acct = max(booked, key=lambda l: (abs(l["amount"]), l["line_id"]))["account"]
            refs = [f"GL {l['line_id']}" for l in booked + [line]]
            txns, keys, evidence, b, other_side = [tid], [], [], None, None
            if hit and hit[3] == "book":
                l2 = hit[4]
                b, other = l2["account_id"], by_txn[l2["txn_id"]]
                used.add(l2["txn_id"])
                txns.append(l2["txn_id"])
                shown = texts(other)
                evidence.append(f"{d.name(b)} shows {fmt(l2['amount'])} on {l2['date']} in the books ({l2['txn_type']}"
                                + (f", \"{'; '.join(shown)}\"" if shown else ", no payee or memo") + f"), within {win} days")
                refs += [f"GL {x['line_id']}" for x in other]
                rest = {x["account_id"] for x in other if x["account_id"] != b}
                if len(rest) == 1 and not rest & own and p.contains(l2["date"]):
                    other_side = d.name(rest.pop())
            elif hit:
                b, x = hit[4]
                used_rows.add((b, x["row"]))
                evidence.append(f"{d.name(b)}'s statement shows {fmt(x['amount'])} on {x['date']} (\"{x['description']}\"), "
                                f"which isn't in the books, within {win} days")
                refs.append(x.get("source_ref") or f"{d.statements[b]['file']} row {x['row']}")
                keys.append(key(x["date"], x["amount"], b))
                other_side = d.name(b)
            if said:
                evidence.append("the payee, memo or bank description reads like a transfer between your own accounts")
            shown = texts(ls)
            head = (f"{line['txn_type']} {'into' if side == 'income' else 'out of'} {d.name(a)}"
                    + (f" (\"{'; '.join(shown)}\")" if shown else ", with no payee or memo") + ". "
                    + ". ".join(e[0].upper() + e[1:] for e in evidence) + ". ")
            if side == "income":
                title = (f"{fmt(amt)} booked to {acct} on {line['date']} looks like a transfer "
                         f"{'from ' + d.name(b) if b else 'between your own accounts'}, not income")
                detail = head + "Money moving between your own accounts isn't income; booked this way it overstates income."
                action = (f"If it's your own money moving between accounts, book it as a transfer from "
                          f"{d.name(b) if b else 'the account it came from'} to {d.name(a)} instead of {acct}.")
                lines_ = [(acct, amt, None), (other_side, None, amt)]
            else:
                title = (f"{fmt(amt)} booked to {acct} on {line['date']} looks like a transfer "
                         f"{'to ' + d.name(b) if b else 'between your own accounts'}, not an expense")
                detail = head + ("Money moving between your own accounts isn't an expense; booked this way it overstates "
                                 "expenses.")
                action = (f"If it paid off {d.name(b)}, book it as a credit card payment from {d.name(a)} instead of {acct}."
                          if b in card else f"If it's your own money moving between accounts, book it as a transfer from "
                          f"{d.name(a)} to {d.name(b) if b else 'the account it went to'} instead of {acct}.")
                lines_ = [(other_side, amt, None), (acct, None, amt)]
            r.flag("medium", f"transfer-{side}", title, amt, detail, list(dict.fromkeys(refs)), action, txns=txns,
                   keys=keys, accounts={l["account_id"] for l in booked} | ({b} if b else set()),
                   subject=f"{side}:{d.name(a)}:{line['date']}:{amt}")
            if other_side:
                r.draft(f"Reclass the {line['date']} {fmt(amt)} from {acct} to a transfer, if it's the same money",
                        lines_, status="confirm first")
    r.summary = (f"{n['income']} deposit(s) booked as income and {n['expense']} payment(s) booked as an expense look "
                 "like transfers between your own accounts")
    return r


def _share(part: Decimal, whole: Decimal) -> Decimal:
    return (part / whole * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if whole else ZERO


def k5_payroll_split(d: Data, ctx: dict) -> CheckResult:
    """The share of payroll booked to cost-of-sales accounts, against the split a person gave in review-config.json
    (payroll_cogs_pct) or, without one, against the company's own last months. Payroll stays a total."""
    title = "Payroll split between cost of sales and operating expenses"
    ids = {a for a in d.payroll_ids() if d.cls(a) == "Expense"}
    if not ids:
        return not_run("K5", title, "no payroll expense accounts (roles payroll_wages and payroll_taxes are empty)")
    cogs_ids = {a for a in ids if d.atype(a) == "Cost of Goods Sold"}
    p = d.period

    def split(lines):
        return (sum((l["amount"] for l in lines if l["account_id"] in ids), ZERO),
                sum((l["amount"] for l in lines if l["account_id"] in cogs_ids), ZERO))

    total, cogs = split(d.gl)
    if total <= 0:
        return not_run("K5", title, f"no payroll booked in {p.label}")
    hist = []
    for m in d.history_months():
        t, c = split([l for l in d.gl_history if l["date"].strftime("%Y-%m") == m])
        if t > 0:
            hist.append((m, c, _share(c, t)))
    expected = d.cfg.get("payroll_cogs_pct")
    ask = ("add \"payroll_cogs_pct\" to review-config.json: the share of payroll (0 to 100) that belongs in cost of sales, "
           "such as field, delivery or support staff, or 0 if none does. List those wage accounts in roles.payroll_wages, "
           "then confirm the roles again")
    if expected is None and not any(c > 0 for _, c, _ in hist):
        return not_run("K5", title, f"no payroll was booked to a cost-of-sales account in the last {len(hist)} month(s) "
                                    "and there's no expected split to compare with. To check it, " + ask)
    if expected is None and len(hist) < 3:
        return not_run("K5", title, f"only {len(hist)} month(s) of payroll history; needs 3. Or " + ask)
    r = CheckResult("K5", title)
    share = _share(cogs, total)
    slack = threshold(d.cfg, "payroll_split_slack_pct")
    lo, hi = (min(s for _, _, s in hist), max(s for _, _, s in hist)) if hist else (None, None)
    past = (f"{lo}%" if lo == hi else f"{lo}% to {hi}%") + f" in the last {len(hist)} months" if hist else ""
    if expected is not None:
        target, gap = f"review-config.json expects {expected}% in cost of sales", abs(share - expected)
        off = gap > slack or (cogs == 0 and expected > 0)
        low = share < expected
    else:
        target = f"the cost-of-sales share was {past}"
        gap = lo - share if share < lo else (share - hi if share > hi else ZERO)
        off = share < lo - slack or share > hi + slack or (cogs == 0 and lo > 0)
        low = share < lo
    amount = q2(total * gap / 100)
    r.data = {"payroll": total, "cogs_payroll": cogs, "cogs_share_pct": share, "expected_pct": expected,
              "history": [{"month": m, "cogs_share_pct": s} for m, _, s in hist]}
    r.summary = (f"Payroll {fmt(total)}, {fmt(cogs)} of it ({share}%) in cost of sales; "
                 + (f"expected {expected}% (review-config.json)" if expected is not None else past))
    if not off or amount < threshold(d.cfg, "range_floor"):
        return r
    if cogs == 0:
        head = f"All of {p.label}'s payroll ({fmt(total)}) is in operating expenses"
    else:
        head = f"{'Only ' if low else ''}{share}% of {p.label}'s payroll is in cost of sales"
    cogs_names = ", ".join(sorted(d.name(a) for a in cogs_ids)) or "none (no cost-of-sales account is in the payroll roles)"
    top = sorted((l for l in d.gl if l["account_id"] in ids), key=lambda l: (-abs(l["amount"]), l["line_id"]))[:8]
    r.flag("medium", "payroll-split", f"{head}; {target}", amount,
           f"{fmt(cogs)} of {fmt(total)} payroll is in cost of sales this month ({share}%)."
           + (f" It was {past}." if past and expected is not None else "")
           + (f" review-config.json says about {expected}% belongs there (payroll_cogs_pct)." if expected is not None
              else "") + f" Cost-of-sales payroll accounts: {cogs_names}. Gross margin depends on this split. Payroll "
           "is shown as totals only, never by person.",
           [f"GL {l['line_id']}" for l in top] + [f"P&L {d.name(a)}" for a in sorted(cogs_ids, key=d.name)],
           "Check where the field, delivery or support wages went this month and move the cost-of-sales share to "
           "those accounts. If the team's mix really changed, acknowledge this item"
           + (", or ask the person to update payroll_cogs_pct and confirm the roles again." if expected is not None
              else "."), accounts=ids, subject="payroll-split")
    wages = sorted((a for a in ids - cogs_ids if a in d.ids_for("payroll_wages")), key=d.name)
    cogs_wages = sorted(cogs_ids, key=d.name)
    to_cogs = (d.name(cogs_wages[0]) if len(cogs_wages) == 1 else "(cost-of-sales payroll account)", None, None)
    to_opex = (d.name(wages[0]) if len(wages) == 1 else "(payroll expense account)", None, None)
    r.draft(f"Move {p.label} payroll {'into' if low else 'out of'} cost of sales", [to_cogs, to_opex] if low else
            [to_opex, to_cogs], status="needs your amount",
            basis=f"payroll {fmt(total)}, {share}% in cost of sales; {target}; use your payroll allocation")
    return r
