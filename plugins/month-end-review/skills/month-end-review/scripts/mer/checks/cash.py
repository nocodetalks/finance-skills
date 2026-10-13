"""T1 cash and cards vs statements; T3 Stripe payouts and the Stripe clearing account."""

from __future__ import annotations

from decimal import Decimal

from ..config import threshold
from ..data import Data
from ..util import ZERO, fmt
from .base import CheckResult, counter_account, key, not_run
from .matching import (match_account, prior_as_bank, prior_as_book,
                       statement_rows_before_period, statement_rows_in_period, total)
from ..util import add_days, norm

PAYOUTS = {"payout"}


def window_for(d: Data, aid: str) -> int:
    fmts = {norm(k): v for k, v in (d.cfg.get("bank_formats") or {}).items()}
    w = (fmts.get(norm(d.name(aid))) or {}).get("match_window_days")
    return int(w) if isinstance(w, int) and not isinstance(w, bool) and w >= 0 else int(threshold(d.cfg, "match_window_days"))


def prepare_matching(d: Data, ctx: dict) -> None:
    ctx["match"] = {}
    for aid, s in d.statements.items():
        window = window_for(d, aid)
        book = [l for l in d.gl if l["account_id"] == aid]
        prior_lines = (d.outstanding_prior.get(aid) or {}).get("lines", [])
        bank = (statement_rows_in_period(s, d.period) + prior_as_bank(aid, prior_lines)
                + statement_rows_before_period(s, d.period))
        ctx["match"][aid] = match_account(book, prior_as_book(aid, prior_lines), bank, window,
                                          add_days(d.period.end, -window))


def t3_stripe(d: Data, ctx: dict) -> CheckResult:
    title = "Stripe payouts reach the bank and the books; clearing account matches Stripe"
    if not d.cfg.get("uses_stripe"):
        return not_run("T3", title, "uses_stripe is false in the config")
    r = CheckResult("T3", title)
    window = int(threshold(d.cfg, "match_window_days"))
    tol = threshold(d.cfg, "tie_tolerance")
    ctx["t3_rows"] = set()
    used = set()
    booked_total = stripe_total = ZERO
    cash_ids = d.ids_for("cash")
    for p in sorted((x for x in d.stripe if x["category"] in PAYOUTS), key=lambda x: x["date"]):
        amt = -p["amount"]
        stripe_total += amt
        hit = None
        for aid in sorted(cash_ids & set(d.statements)):
            for row in statement_rows_in_period(d.statements[aid], d.period):
                if (aid, row["row"]) in used or row["amount"] != amt:
                    continue
                if 0 <= (row["date"] - p["date"]).days <= window:
                    hit = (aid, row)
                    break
            if hit:
                break
        if hit:
            aid, row = hit
            used.add((aid, row["row"]))
            matched = row["row"] in ctx["match"][aid].bank_matched
            if matched:
                booked_total += amt
            else:
                ctx["t3_rows"].add((aid, row["row"]))
                r.flag("high", "payout-not-booked",
                       f"Stripe payout {fmt(amt)} reached {d.name(aid)} on {row['date']} but isn't in the books",
                       amt, f"Payout {p['id']} created {p['date']}.",
                       [f"stripe {p['id']}", f"{d.statements[aid]['file']} row {row['row']}"],
                       "Record the deposit (or the transfer from the Stripe clearing account).",
                       keys=[key(row["date"], amt, aid), "g|t3-unbooked|"], accounts=[aid], subject=p["id"])
        else:
            booked = any(l["account_id"] in cash_ids and l["amount"] == amt and l["date"] >= p["date"] for l in d.gl)
            if (d.period.end - p["date"]).days < window:
                if booked:
                    booked_total += amt
                r.tie(f"Payout {p['id']}", amt if booked else ZERO, amt, "Stripe", "in transit",
                      f"created {p['date']}, arrives after month-end" + ("" if booked else "; not booked yet"))
            else:
                r.flag("high", "payout-missing",
                       f"Stripe payout {fmt(amt)} from {p['date']} never reached a bank statement", amt,
                       f"Payout {p['id']}. Not on any statement within {window} days.", [f"stripe {p['id']}"],
                       "Check which bank account it went to; it may be an account not in the review.",
                       keys=[key(p["date"], amt), "g|t3-unbooked|"], subject=p["id"])
    r.tie("Stripe payouts in the month", booked_total, stripe_total, "Stripe", "tie" if abs(booked_total - stripe_total) <= tol else "differs",
          "books = payouts found in the books (matched to a bank line, or booked in transit)")
    for aid in sorted(cash_ids & set(d.statements)):
        for row in statement_rows_in_period(d.statements[aid], d.period):
            if ((aid, row["row"]) not in used and row["amount"] > 0 and "stripe" in row["description"].lower()):
                ctx["t3_rows"].add((aid, row["row"]))
                r.flag("high", "deposit-without-payout",
                       f"{d.name(aid)}: a Stripe deposit of {fmt(row['amount'])} on {row['date']} isn't in Stripe's payout data",
                       row["amount"], f"\"{row['description']}\". Either the Stripe data is incomplete or the deposit came "
                       "from another Stripe account.", [f"{d.statements[aid]['file']} row {row['row']}"],
                       "Check the Stripe pull covers the right account and month.",
                       keys=[key(row["date"], row["amount"], aid)], accounts=[aid], subject=f"{aid}:{row['date']}:{row['amount']}")
    ctx["t3_unbooked"] = stripe_total - booked_total

    if d.ids_for("processor_clearing"):
        ids = d.ids_for("processor_clearing")
        books = sum((d.tb.get(a, ZERO) for a in ids), ZERO)
        stripe_bal = d.stripe_balance["closing"]
        move_books = sum((d.movement(d.gl, a) for a in ids), ZERO)
        move_stripe = d.stripe_balance["closing"] - d.stripe_balance["opening"]
        status = "tie" if abs(books - stripe_bal) <= tol else "differs"
        r.tie("Stripe clearing account vs Stripe balance", books, stripe_bal,
              f"Stripe balance at {d.period.end} ({d.stripe_balance['source']})", status,
              f"month's movement: books {fmt(move_books)}, Stripe {fmt(move_stripe)}")
        if status == "differs":
            r.flag("high", "clearing-differs",
                   f"Stripe clearing account is {fmt(books)}; Stripe's balance is {fmt(stripe_bal)}",
                   books - stripe_bal,
                   f"This month the clearing account moved {fmt(move_books)} and Stripe moved {fmt(move_stripe)}. "
                   "Usual causes: fees or refunds not synced, a payout booked to the wrong account, or a sync gap.",
                   [f"TB {d.name(a)}" for a in ids] + ["stripe.csv"],
                   "Find the transactions the sync missed or doubled.", accounts=ids, pin=True,
                   keys=["g|t3-unbooked|"], subject="clearing")
    r.summary = (f"Stripe payouts {fmt(stripe_total)}, found in the books {fmt(booked_total)}"
                 + ("; clearing account checked" if d.ids_for("processor_clearing") else ""))
    return r


def t1_cash(d: Data, ctx: dict) -> CheckResult:
    r = CheckResult("T1", "Cash and cards tie to statements")
    tol = threshold(d.cfg, "tie_tolerance")
    skip_rows = ctx.get("t3_rows", set())
    outstanding = ctx.setdefault("outstanding", [])
    for aid in sorted(d.statements, key=d.name):
        s = d.statements[aid]
        acct = d.name(aid)
        window = window_for(d, aid)
        m = ctx["match"][aid]
        books_bal = d.tb.get(aid, ZERO)
        stmt_bal = s["month_end_dp"]
        diff = books_bal - stmt_bal
        # Books = statement + items in the books not yet on the statement - items on the statement not in
        # the books. Typo pairs are unmatched on both sides, so they count on both sides.
        unexplained = diff - total(m.book_left) - sum((b["amount"] for b, _, _ in m.typos), ZERO) \
            + total(m.bank_left) + sum((x["amount"] for _, x, _ in m.typos), ZERO)

        for b, x, kind in m.typos:
            delta = x["amount"] - b["amount"]
            r.flag("medium", "typo", f"{acct}: books show {fmt(b['amount'])} and the bank {fmt(x['amount'])} on the same "
                                     f"day; possibly one transaction typed wrong ({kind})",
                   delta, f"{b['date']} {b['name'] or b['memo']} vs \"{x['description']}\". If they're the same transaction, "
                   f"the books are off by {fmt(delta)}; if not, both are unmatched items.",
                   [f"GL {b['line_id']}", f"{s['file']} row {x['row']}"],
                   "Open both. If they're the same, correct the amount in the books.",
                   txns=[b.get("txn_id")], keys=[key(b["date"], b["amount"], aid), key(x["date"], x["amount"], aid)],
                   accounts=[aid], subject=f"{acct}:{b['date']}:{b['amount']}")
            other = counter_account(d, b) if b.get("txn_id") else None
            if other:
                dr, cr = (delta, None) if delta > 0 else (None, -delta)
                r.draft(f"Correct {b['date']} entry to the bank amount ({kind}), if it's the same transaction",
                        [(acct, dr, cr), (other, cr, dr)], status="confirm first")
        for b in m.book_left:
            outstanding.append({"account_id": aid, "side": "book", "date": b["date"], "description": b["name"] or b["memo"],
                                "amount": b["amount"], "source_ref": b.get("source_ref") or f"GL {b['line_id']}"})
            age = (d.period.end - b["date"]).days
            if b.get("prior"):
                if age > 60:
                    r.flag("medium", "stale-uncleared",
                           f"{acct}: {fmt(b['amount'])} from {b['date']} is still uncleared after {age} days",
                           b["amount"], f"{b['name']} ({b.get('source_ref', '')}). Old uncleared items are usually "
                           "voided checks or payments booked twice.", [b.get("source_ref") or "outstanding"],
                           "Find out whether it will ever clear; if it was cancelled, void it in the books.",
                           keys=[key(b["date"], b["amount"], aid)], accounts=[aid], subject=f"{acct}:{b['date']}:{b['amount']}")
                continue
            if age < window:
                continue
            r.flag("medium", "book-not-on-statement",
                   f"{acct}: {fmt(b['amount'])} on {b['date']} is in the books but not on the statement",
                   b["amount"], f"{b['txn_type']} {b['name'] or b['memo']}, not cleared after {age} days. A payment made "
                   "from a different account (or from the Stripe balance) looks like this.",
                   [f"GL {b['line_id']}"], "Check it really went through this account before changing anything.",
                   txns=[b["txn_id"]], keys=[key(b["date"], b["amount"], aid)], accounts=[aid],
                   subject=f"{acct}:{b['date']}:{b['amount']}")
        for x in m.bank_left:
            outstanding.append({"account_id": aid, "side": "bank", "date": x["date"], "description": x["description"],
                                "amount": x["amount"], "source_ref": x.get("source_ref") or f"{s['file']} row {x['row']}"})
            if (aid, x["row"]) in skip_rows:
                continue
            if x.get("prior"):
                r.flag("high", "statement-still-not-in-books",
                       f"{acct}: {fmt(x['amount'])} from {x['date']} has been on the statement since last month and "
                       "still isn't in the books", x["amount"], f"\"{x['description']}\" ({x.get('source_ref', '')}).",
                       [x.get("source_ref") or "outstanding"],
                       "Find where it was booked (another account or date) or book it.",
                       keys=[key(x["date"], x["amount"], aid)], accounts=[aid], subject=f"{acct}:{x['date']}:{x['amount']}")
                continue
            r.flag("high", "statement-not-in-books",
                   f"{acct}: {fmt(x['amount'])} on {x['date']} is on the statement but not in the books",
                   x["amount"], f"\"{x['description']}\".", [f"{s['file']} row {x['row']}"],
                   "Find where it was booked (another account or date) before adding it, so it isn't booked twice.",
                   keys=[key(x["date"], x["amount"], aid)], accounts=[aid], subject=f"{acct}:{x['date']}:{x['amount']}")
        if abs(unexplained) > tol:
            r.flag("high", "unexplained", f"{acct}: {fmt(unexplained)} difference nothing explains", unexplained,
                   "After matching every statement line and last month's uncleared items, this much is unaccounted "
                   "for. Usual causes: a transaction posted into an earlier, already-reconciled month, or an "
                   "uncleared list from last month that was incomplete.", [f"TB {acct}", s["file"]],
                   "Look for changes to earlier months in this account.", accounts=[aid], pin=True, subject=acct)
        flagged_here = any(e["title"].startswith(f"{acct}:") for e in r.exceptions)
        in_transit = [b for b in m.book_left if not b.get("prior") and (d.period.end - b["date"]).days < window]
        if diff == 0 and not flagged_here:
            status = "tie"
        elif abs(unexplained) > tol:
            status = "differs, unexplained"
        elif flagged_here:
            status = "differs, traced to items above"
        else:
            status = "explained (in transit)"
        notes = []
        if in_transit:
            notes.append(f"{len(in_transit)} in transit: " + ", ".join(f"{fmt(b['amount'])} {b['date']}" for b in in_transit))
        if m.late:
            notes.append(f"{len(m.late)} cleared late: " + ", ".join(f"{fmt(b['amount'])} {b['date']}→{x['date']}" for b, x in m.late))
        groups = [p for p in m.pairs if p[2] == "group"]
        if groups:
            notes.append(f"{len(groups)} matched as groups")
        if s["statement_end"] < d.period.end:
            notes.append(f"statement closed {s['statement_end']}; month-end balance rolled forward with "
                         f"{s.get('rows_after_statement', 0)} later CSV rows (those rows can't be proven complete)")
        prior_src = (d.outstanding_prior.get(aid) or {}).get("source", "")
        if prior_src:
            notes.append(f"last month's uncleared items: {prior_src}")
        r.tie(acct, books_bal, stmt_bal, f"statement {s['source_file']}", status, "; ".join(notes))
    r.summary = "; ".join(f"{t['item']}: books {fmt(t['books'])}, bank {fmt(t['other'])}, {t['status']}" for t in r.tieouts)
    return r
