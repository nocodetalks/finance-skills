"""T4 Stripe sales, fees, refunds and sales tax vs the books."""

from __future__ import annotations

import re
from collections import Counter

from ..config import threshold
from ..data import Data
from ..util import ZERO, fmt
from .base import CheckResult, is_je, key, lines_on, not_run, pick

SALES = {"charge", "payment"}
REFUNDS = {"refund", "payment_refund", "partial_capture_reversal"}
DISPUTES = {"dispute", "dispute_reversal"}
PAYOUTS = {"payout", "payout_reversal", "payout_failure", "payout_cancel"}
FEE_ROWS = {"fee", "stripe_fee"}
REFUNDISH = re.compile(r"refund|credit memo", re.I)


def stripe_bridge(d: Data) -> dict:
    s = d.stripe or []
    gross = sum((r["amount"] for r in s if r["category"] in SALES), ZERO)
    refunds = -sum((r["amount"] for r in s if r["category"] in REFUNDS), ZERO)
    disputes = -sum((r["amount"] for r in s if r["category"] in DISPUTES), ZERO)
    fees = sum((r["fee"] for r in s), ZERO) - sum((r["amount"] for r in s if r["category"] in FEE_ROWS), ZERO)
    payouts = -sum((r["amount"] for r in s if r["category"] in PAYOUTS), ZERO)
    known = SALES | REFUNDS | DISPUTES | PAYOUTS | FEE_ROWS
    other = sum((r["amount"] for r in s if r["category"] not in known), ZERO)
    out = {"gross_sales": gross, "refunds": refunds, "disputes": disputes, "fees": fees, "other": other,
           "payouts": payouts, "net_change": sum((r["net"] for r in s), ZERO)}
    if d.stripe_balance:
        out.update(opening=d.stripe_balance["opening"], closing=d.stripe_balance["closing"])
    return out


def t4_stripe_books(d: Data, ctx: dict) -> CheckResult:
    title = "Stripe sales, fees, refunds and tax match the books"
    if not d.cfg.get("uses_stripe"):
        return not_run("T4", title, "uses_stripe is false in the config")
    r = CheckResult("T4", title)
    b = stripe_bridge(d)
    r.data["bridge"] = b
    tol = threshold(d.cfg, "tie_tolerance")
    def_ids = d.ids_for("deferred_revenue")
    rev_ids = d.ids_for("stripe_revenue") | d.ids_for("refunds")
    je_deferral = {l["txn_id"] for l in d.gl if l["account_id"] in def_ids and is_je(l)}

    # fees
    gl_fees = sum((l["amount"] for l in lines_on(d, d.ids_for("stripe_fees"))), ZERO)
    gap = b["fees"] - gl_fees
    r.tie("Stripe fees", gl_fees, b["fees"], "Stripe", "tie" if abs(gap) <= tol else "differs")
    at_net = b["fees"] > 0 and gl_fees < b["fees"] / 2
    if abs(gap) > tol:
        rev, fee = pick(d, "stripe_revenue"), pick(d, "stripe_fees")
        if at_net:
            r.flag("high", "fees-missing", f"Stripe kept {fmt(b['fees'])} in fees; the books show {fmt(gl_fees)}", gap,
                   f"Revenue was probably booked at the payout amount (net of fees). Revenue and fee expense are both "
                   f"understated by {fmt(gap)}.", [f"GL account {d.name(a)}" for a in d.ids_for("stripe_fees")] + ["stripe.csv"],
                   "Book Stripe sales gross, with fees as an expense.", accounts=d.ids_for("stripe_fees") | d.ids_for("stripe_revenue"),
                   subject="fees")
            if gap > 0:
                r.draft(f"Stripe fees {d.period.label} (gross-up)",
                        [(d.name(fee) if fee else "(choose the Stripe fees account)", gap, None),
                         (d.name(rev) if rev else "(choose the Stripe revenue account)", None, gap)],
                        status="ready" if (rev and fee) else "confirm first")
        else:
            r.flag("medium", "fees-differ", f"Stripe fees in the books ({fmt(gl_fees)}) differ from Stripe ({fmt(b['fees'])})",
                   gap, "Part of the month's fees is missing or doubled in the books.",
                   [f"GL account {d.name(a)}" for a in d.ids_for("stripe_fees")] + ["stripe.csv"],
                   "Compare the fee lines with Stripe's fee total for the month.", accounts=d.ids_for("stripe_fees"),
                   subject="fees")

    # refunds booked more often than Stripe refunded
    stripe_refunds = Counter(-x["amount"] for x in d.stripe if x["category"] in REFUNDS)
    gl_ref = [l for l in d.gl if l["account_id"] in rev_ids and l["amount"] > 0 and l["txn_id"] not in je_deferral
              and (REFUNDISH.search(l["txn_type"]) or l["account_id"] in d.ids_for("refunds"))]
    refund_extra = ZERO
    for amt, n in Counter(l["amount"] for l in gl_ref).items():
        if amt in stripe_refunds and n > stripe_refunds[amt]:
            refund_extra += (n - stripe_refunds[amt]) * amt
            lines = [l for l in gl_ref if l["amount"] == amt]
            r.flag("high", "refund-twice", f"Refund of {fmt(amt)} is booked {n} times; Stripe refunded it {stripe_refunds[amt]} time(s)",
                   (n - stripe_refunds[amt]) * amt, ", ".join(f"{l['date']} {l['name'] or l['memo']}" for l in lines),
                   [f"GL {l['line_id']}" for l in lines],
                   "Remove the extra refund entry (Stripe refunds come out of the Stripe balance, not the bank).",
                   txns=[l["txn_id"] for l in lines], accounts=rev_ids, subject=f"refund:{amt}")

    # billings bridge
    tax_collected = None
    if d.invoices is not None:
        tax_collected = sum((i["tax"] for i in d.invoices if i["paid_date"] and d.period.contains(i["paid_date"])), ZERO)
    via_ar = d.cfg.get("stripe_billing_via_ar")
    expected = None
    if via_ar:
        if d.invoices is None:
            r.part("sales vs books", "not_run", "billing through AR needs Stripe invoice data (API pull)")
        else:
            expected = sum((i["subtotal_ex_tax"] for i in d.invoices
                            if i["finalized_date"] and d.period.contains(i["finalized_date"])), ZERO)
            basis = "Stripe invoices finalized this month (before tax)"
    elif d.cfg.get("uses_stripe_tax") and tax_collected is None:
        r.part("sales vs books", "not_run", "Stripe Tax is on, and the tax collected needs Stripe invoice data (API pull)")
    else:
        expected = b["gross_sales"] - b["refunds"] - (tax_collected or ZERO)
        basis = "Stripe gross - refunds" + (" - tax collected" if tax_collected else "")
    if expected is not None:
        billed = ZERO
        for l in d.gl:
            if l["txn_id"] in je_deferral:
                continue
            if l["account_id"] in rev_ids or (l["account_id"] in def_ids and not is_je(l)):
                billed += -l["amount"]
        diff = billed - expected
        # Take out what the other items already explain: payouts missing from the books (T3), fees
        # never booked (booked at net), refunds booked more than once.
        known = []
        if ctx.get("t3_unbooked"):
            known.append(("Stripe payouts not in the books (T3)", ctx["t3_unbooked"]))
        if at_net:
            known.append(("fees never booked", gap))
        if refund_extra:
            known.append(("refunds booked more than once", refund_extra))
        remainder = diff + sum((v for _, v in known), ZERO)
        mat = threshold(d.cfg, "materiality_abs")
        status = "tie" if abs(diff) <= tol else ("explained by items above" if abs(remainder) < mat else "differs")
        note = ("explained: " + ", ".join(f"{k} {fmt(v)}" for k, v in known)) if known else ""
        if known and status == "differs":
            note += f"; {fmt(remainder)} left over"
        r.tie(f"Sales ({basis}) vs revenue + new deferrals booked", billed, expected, "Stripe", status, note)
        if status == "differs":
            other = sorted((l for l in d.gl if l["account_id"] in rev_ids and l["txn_id"] not in je_deferral
                            and not (l["name"].lower().startswith("stripe") and l["txn_type"] == "Deposit")),
                           key=lambda l: abs(l["amount"]), reverse=True)[:5]
            causes = ["revenue from other sources (wires, checks, journal entries) booked to the Stripe accounts"]
            if not via_ar:
                causes.append("Stripe Billing invoices paid through AR (set stripe_billing_via_ar)")
            if not d.cfg.get("uses_stripe_tax"):
                causes.append("Stripe Tax collected and booked to revenue")
            r.flag("medium", "sales-differ",
                   f"After the items above, {fmt(remainder)} of revenue on the Stripe accounts still doesn't match Stripe",
                   remainder, f"Books {fmt(billed)} vs {basis} {fmt(expected)}"
                   + (f"; already explained: {note[len('explained: '):]}" if known else "") + ". Largest other lines: "
                   + (", ".join(f"{fmt(-l['amount'])} {l['txn_type']} {l['date']} ({l['memo'] or l['name']})" for l in other) or "none")
                   + ". Possible causes: " + "; ".join(causes) + ".",
                   [f"GL {l['line_id']}" for l in other] or [f"GL account {d.name(a)}" for a in rev_ids],
                   "Work out which cause applies before changing anything.",
                   txns=[], accounts=rev_ids, subject="sales")
    # sales tax
    if d.ids_for("sales_tax_payable"):
        if tax_collected is None:
            r.part("sales tax vs Stripe Tax", "not_run", "needs Stripe invoice data (API pull)")
        else:
            booked = -sum((l["amount"] for l in lines_on(d, d.ids_for("sales_tax_payable")) if l["amount"] < 0), ZERO)
            st = "tie" if abs(booked - tax_collected) <= tol else "differs"
            r.tie("Sales tax collected (Stripe) vs booked", booked, tax_collected, "Stripe invoices", st)
            if st == "differs":
                r.flag("medium", "tax-differs", f"Sales tax booked ({fmt(booked)}) differs from tax Stripe collected ({fmt(tax_collected)})",
                       booked - tax_collected, "Tax collected must go to the liability, not revenue.",
                       [f"GL account {d.name(a)}" for a in d.ids_for("sales_tax_payable")], "Reclass the difference.",
                       accounts=d.ids_for("sales_tax_payable"), subject="sales-tax")
    r.summary = f"Stripe fees: books {fmt(gl_fees)} vs Stripe {fmt(b['fees'])}; gross {fmt(b['gross_sales'])}, refunds {fmt(b['refunds'])}"
    return r
