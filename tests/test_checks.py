"""The checks: the 15 planted mistakes, plus the realistic scenarios from the 2026-10-11 review."""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal as D

from helpers import Base, d


def everything(exc):
    """Lead titles plus the 'also seen by' titles grouped under them."""
    out = []
    for e in exc["open"] + exc["acknowledged"]:
        out.append((e["check"], e["title"]))
        out += [(a["check"], a["title"]) for a in e.get("also", [])]
    return out


class TestPlantedMistakes(Base):
    @classmethod
    def setUpClass(cls):
        import shutil, tempfile
        from pathlib import Path
        from fixture_northwind import build
        from mer import config
        from mer.checks import run_all
        from mer.data import load
        from mer.gate import run_gate
        from helpers import P
        root = Path(tempfile.mkdtemp())
        month = build(root)
        data = load(month, P, config.load(root))
        assert run_gate(data) == [], run_gate(data)
        results, exc, _ = run_all(data, root)
        cls.results = {r.id: r for r in results}
        cls.exc = exc
        cls.all = everything(exc)
        shutil.rmtree(root)

    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_01_duplicate_bill(self):
        self.assertFlag(self.all, "C1", "duplicate", "Acme Design")

    def test_02_missing_recurring_bill(self):
        self.assertFlag(self.all, "C2", "Amazon Web Services", "September")
        self.assertFlag(self.all, "C1", "Notion", "nothing in September")

    def test_03_payout_not_in_books(self):
        self.assertFlag(self.all, "T3", "11,000", "isn't in the books")

    def test_04_refund_booked_twice(self):
        self.assertFlag(self.all, "T4", "Refund", "2 times")

    def test_05_wrong_account(self):
        self.assertFlag(self.all, "K1", "Figma", "usually Software")

    def test_06_annual_tool_expensed(self):
        self.assertFlag(self.all, "C3", "HubSpot", "prepaid")

    def test_07_annual_plan_as_revenue(self):
        self.assertFlag(self.all, "C3", "Acme Corp", "annual plan")

    def test_08_revenue_at_net(self):
        self.assertFlag(self.all, "T4", "1,480", "fees")

    def test_09_accrual_not_reversed(self):
        self.assertFlag(self.all, "C2", "Accrued Liabilities", "never reversed")

    def test_10_payroll_wrong_month(self):
        e = next(e for e in self.exc["open"] if e["check"] == "T5")
        self.assertIn("2026-09-30", e["detail"])

    def test_11_manual_je_to_ar(self):
        self.assertFlag(self.all, "T2", "receivable", "3,000")

    def test_12_transposed_deposit_and_one_sided_transfer(self):
        self.assertFlag(self.all, "T1", "1,350", "1,530", "digits swapped")
        self.assertFlag(self.all, "T1", "Treasury", "not in the books", "50,000")

    def test_13_bill_entered_after_close(self):
        self.assertFlag(self.all, "C2", "Cooley", "after month-end")

    def test_14_prepaid_not_amortized(self):
        self.assertFlag(self.all, "C3", "Prepaid Expenses", "amortized")

    def test_15_personal_dinner_is_missed(self):
        self.assertNoFlag(self.all, "nobu")

    def test_one_cause_reported_once(self):
        leads = [e["check"] for e in self.exc["open"]]
        payroll = next(e for e in self.exc["open"] if e["check"] == "T5")
        self.assertIn("T1", [a["check"] for a in payroll["also"]])
        self.assertEqual(leads.count("T5"), 1)

    def test_severity_follows_amount(self):
        sev = {e["title"]: e["severity"] for e in self.exc["open"]}
        notion = next(s for t, s in sev.items() if "Notion" in t)
        uncategorized = next(s for t, s in sev.items() if "Uncategorized" in t)
        self.assertEqual(notion, "medium")
        self.assertEqual(uncategorized, "high")
        amounts = [e["amount"] for e in self.exc["open"] if e["severity"] == "high"]
        self.assertEqual(amounts, sorted(amounts, reverse=True))

    def test_flux_rows_marked_unreliable(self):
        wages = next(r for r in self.results["V1"].data["rows"] if r["account"] == "Payroll Expenses:Wages")
        self.assertTrue(wages["direction"].startswith("unreliable"))

    def test_revenue_excludes_other_income(self):
        t = self.results["V1"].data["totals"]
        self.assertEqual(t["other_income"]["current"], D("4100"))
        self.assertEqual(t["revenue"]["current"] + t["other_income"]["current"] - t["opex"]["current"], t["net_income"]["current"])

    def test_stripe_tieout_is_real(self):
        tie = next(t for t in self.results["T3"].tieouts if t["item"] == "Stripe payouts in the month")
        self.assertEqual(tie["books"], D("36920"))
        self.assertEqual(tie["other"], D("47920"))

    def test_t4_reports_only_the_remainder(self):
        e = next(e for e in self.exc["open"] if e["check"] == "T4" and "still doesn" in e["title"])
        self.assertEqual(e["amount"], D("2400"))

    def test_metrics(self):
        m = {x["id"]: x for x in self.results["V3"].data["metrics"]}
        self.assertEqual(m["revenue"]["value"], D("83940"))
        self.assertIsNotNone(m["runway_months"]["value"])
        self.assertEqual(m["mrr"]["status"], "not_run")

    def test_bs_unreconciled_listed(self):
        self.assertIn("Preferred Stock", [u["account"] for u in self.results["V2"].data["unreconciled"]])

    def test_bridges_add_up(self):
        self.assertTrue(self.results["V1"].data["bridge_check"]["adds_up"])

    # The one-sided $50k treasury move, booked to Uncategorized Expense, is K4's mirror case.
    def test_k4_mirror_leads_the_treasury_item(self):
        e = next(e for e in self.exc["open"] if "Uncategorized" in e["title"])
        self.assertEqual(e["check"], "K4")
        self.assertIn("transfer to Mercury Treasury, not an expense", e["title"])
        self.assertEqual(sorted(a["check"] for a in e["also"]), ["K1", "T1"])
        self.assertIn("bank/2.csv row 2", e["refs"])
        draft = next(x for x in self.results["K4"].drafts)
        self.assertEqual(draft["status"], "confirm first")
        self.assertEqual([(l["account"], l["debit"], l["credit"]) for l in draft["lines"]],
                         [("Mercury Treasury", D("50000"), None), ("Uncategorized Expense", None, D("50000"))])

    def test_k4_leaves_stripe_deposits_to_t3_t4(self):
        # Northwind's bank calls Stripe payouts "STRIPE TRANSFER"; they're processor activity, not own-account moves.
        self.assertEqual([e["kind"] for e in self.results["K4"].exceptions], ["transfer-expense"])

    def test_new_checks_in_coverage(self):
        self.assertEqual(self.results["C5"].status, "pass")
        self.assertEqual(self.results["K5"].status, "not_run")
        self.assertIn('"payroll_cogs_pct"', self.results["K5"].reason)


# ---------------------------------------------------------------- scenarios

def prior_check_clears(stage, b, x):
    if stage == "history":
        b.txn("Check", d("2026-08", 15), "Delaware", "Franchise tax", [("41", 4000), ("1", -4000)])
    else:
        x["checking"].append((d("2026-09", 3), "CHECK 1050", -4000))
        x["opening_adjust"]["checking"] = 4000
        x["outstanding"]["checking"] = [{"date": "2026-08-15", "description": "Delaware", "amount": "-4000", "side": "book"}]


class TestPriorOutstandingClears(Base):
    scenario = staticmethod(prior_check_clears)

    def test_no_double_booking_advice(self):
        results, exc, _ = self.review()
        titles = everything(exc)
        self.assertNoFlag(titles, "4,000")
        chk = next(t for t in results["T1"].tieouts if t["item"] == "Mercury Checking")
        self.assertNotIn("unexplained", chk["status"])


def prior_check_stale(stage, b, x):
    if stage == "history":
        b.txn("Check", d("2026-07", 10), "Old vendor", "Check 1001", [("41", 4000), ("1", -4000)])
    else:
        x["opening_adjust"]["checking"] = 4000
        x["outstanding"]["checking"] = [{"date": "2026-07-10", "description": "Old vendor", "amount": "-4000", "side": "book"}]


class TestPriorOutstandingStale(Base):
    scenario = staticmethod(prior_check_stale)

    def test_stale_item_flagged_without_unexplained(self):
        results, exc, _ = self.review()
        titles = everything(exc)
        self.assertFlag(titles, "T1", "still uncleared")
        self.assertNoFlag(titles, "nothing explains")


def prior_bank_item_booked(stage, b, x):
    if stage == "september":
        b.txn("Expense", d("2026-09", 2), "Mercury", "Bank fee", [("35", 25), ("1", -25)])
        x["opening_adjust"]["checking"] = -25
        x["outstanding"]["checking"] = [{"date": "2026-08-31", "description": "BANK FEE", "amount": "-25", "side": "bank"}]


class TestPriorBankItemBookedLate(Base):
    scenario = staticmethod(prior_bank_item_booked)

    def test_matched(self):
        _, exc, _ = self.review()
        self.assertNoFlag(everything(exc), "$25.00")


def rollforward_bonus(stage, b, x):
    months = ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"] if stage == "history" else ["2026-09"]
    for m in months:
        b.txn("Journal Entry", d(m, 28), "", "Bonus accrual", [("43", 10000), ("15", -10000)])


class TestRollforwardAccrual(Base):
    scenario = staticmethod(rollforward_bonus)

    def test_not_flagged_as_unreversed(self):
        results, exc, _ = self.review()
        self.assertNoFlag(everything(exc), "Accrued Bonus", "never reversed")
        self.assertTrue(any("rollforward" in p["reason"] for p in results["C2"].parts))


def safe_as_revenue(stage, b, x):
    if stage == "september":
        b.txn("Deposit", d("2026-09", 18), "Hustle Fund", "SAFE", [("1", 500000), ("20", -500000)])
        x["checking"].append((d("2026-09", 18), "WIRE HUSTLE FUND", 500000))


class TestSafeBookedAsRevenue(Base):
    scenario = staticmethod(safe_as_revenue)

    def test_flagged(self):
        _, exc, _ = self.review()
        self.assertFlag(everything(exc), "K3", "500,000", "Hustle Fund")


def safe_as_equity(stage, b, x):
    if stage == "september":
        b.txn("Deposit", d("2026-09", 18), "Hustle Fund", "SAFE", [("1", 500000), ("14", -500000)])
        x["checking"].append((d("2026-09", 18), "WIRE HUSTLE FUND", 500000))


class TestSafeBookedToEquity(Base):
    scenario = staticmethod(safe_as_equity)

    def test_financing_and_bs(self):
        results, exc, _ = self.review()
        self.assertNoFlag(everything(exc), "Hustle Fund")
        m = {x["id"]: x for x in results["V3"].data["metrics"]}
        self.assertEqual(m["financing"]["value"], D("500000"))
        self.assertIn("SAFE Notes", [u["account"] for u in results["V2"].data["unreconciled"]])


def stripe_annual_plan(stage, b, x):
    if stage == "september":
        x["stripe"].append({"id": "txn_annual", "date": d("2026-09", 2), "category": "charge", "amount": D("12000"),
                            "fee": D("348"), "net": D("11652"), "currency": "usd", "description": "Annual plan"})
        x["invoices"] = ([{"invoice_id": "in_1", "customer": "cus_1", "status": "paid", "finalized_date": "2026-09-02",
                           "paid_date": "2026-09-02", "subtotal_ex_tax": "12000", "tax": "0", "total": "12000"}],
                         [{"invoice_id": "in_1", "line_id": "il_1", "amount": "12000", "period_start": "2026-09-02",
                           "period_end": "2027-09-02", "subscription": "yes", "interval": "year", "interval_count": "1"}])


class TestStripeAnnualPlan(Base):
    scenario = staticmethod(stripe_annual_plan)

    def test_deferral_missing_flagged_and_mrr(self):
        results, exc, _ = self.review()
        self.assertFlag(everything(exc), "C3", "Stripe billed", "unearned")
        mrr = next(m for m in results["V3"].data["metrics"] if m["id"] == "mrr")
        self.assertEqual(mrr["status"], "run")
        self.assertEqual(mrr["value"], D("1000.00"))


def payroll_accrued(stage, b, x):
    if stage == "september":
        b.txn("Journal Entry", d("2026-09", 30), "", "Accrue payroll Sep 16-30", [("31", 60000), ("32", 4500), ("16", -64500)])
        x["checking"] = [r for r in x["checking"] if not (r[1] == "GUSTO PAYROLL" and r[0] == d("2026-09", 30))]
        x["payroll"][1].update(check_date="2026-10-05")
        x["roles"]["accrued_payroll"] = ["Accrued Payroll"]


class TestPayrollAccruedCorrectly(Base):
    scenario = staticmethod(payroll_accrued)

    def test_no_payroll_flag(self):
        _, exc, _ = self.review()
        self.assertNoFlag(everything(exc), "payroll cost")


def treasury_sweeps(stage, b, x):
    if stage == "september":
        b.txn("Transfer", d("2026-09", 30), "", "Sweep to treasury", [("2", 30000), ("1", -30000)])
        for day in (10, 20, 30):
            x["checking"].append((d("2026-09", day), "MERCURY SWEEP", -10000))
            x["treasury"].append((d("2026-09", day), "SWEEP IN", 10000))


class TestTreasurySweeps(Base):
    scenario = staticmethod(treasury_sweeps)

    def test_grouped_match(self):
        _, exc, _ = self.review()
        titles = everything(exc)
        self.assertNoFlag(titles, "10,000")
        self.assertNoFlag(titles, "30,000")


def bill_and_expense(stage, b, x):
    if stage == "history":
        b.txn("Bill", d("2026-08", 20), "Rippling", "HR software", [("34", 1500), ("6", -1500)])
    else:
        b.txn("Expense", d("2026-09", 12), "Rippling", "HR software", [("34", 1500), ("1", -1500)])
        x["checking"].append((d("2026-09", 12), "RIPPLING", -1500))
        x["open_bills"].append({"bill_id": "B9", "vendor": "Rippling", "txn_date": "2026-08-20", "doc_num": "R-1",
                                "amount": "1500.00", "balance": "1500.00"})


class TestBillPaidAsExpense(Base):
    scenario = staticmethod(bill_and_expense)

    def test_flagged(self):
        _, exc, _ = self.review()
        self.assertFlag(everything(exc), "C1", "Rippling", "still open in AP")


def clearing_ok(stage, b, x):
    if stage == "history":
        b.txn("Journal Entry", d("2026-08", 31), "Stripe", "Stripe balance", [("13", 2000), ("20", -2000)])
    else:
        x["roles"]["processor_clearing"] = ["Stripe Clearing"]


def clearing_off(stage, b, x):
    clearing_ok(stage, b, x)
    if stage == "september":
        b.txn("Journal Entry", d("2026-09", 29), "Stripe", "Sync", [("13", 500), ("20", -500)])


class TestClearingAccountTies(Base):
    scenario = staticmethod(clearing_ok)

    def test_no_statement_needed_and_ties(self):
        results, exc, _ = self.review()
        tie = next(t for t in results["T3"].tieouts if "clearing" in t["item"].lower())
        self.assertEqual(tie["status"], "tie")
        self.assertNoFlag(everything(exc), "clearing")


class TestClearingAccountDiffers(Base):
    scenario = staticmethod(clearing_off)

    def test_flagged(self):
        _, exc, _ = self.review()
        self.assertFlag(everything(exc), "T3", "clearing")


def depreciation_missing(stage, b, x):
    if stage == "history":
        b.txn("Check", d("2026-03", 3), "Apple", "Laptops", [("11", 36000), ("1", -36000)])
        for m in ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"]:
            b.txn("Journal Entry", d(m, 30), "", "Depreciation", [("42", 1000), ("12", -1000)])


class TestDepreciationMissing(Base):
    scenario = staticmethod(depreciation_missing)

    def test_flagged(self):
        _, exc, _ = self.review()
        self.assertFlag(everything(exc), "C4", "Depreciation", "missing")


def greedy(stage, b, x):
    if stage == "september":
        b.txn("Expense", d("2026-09", 5), "Vendor X", "", [("35", 777), ("1", -777)])
        b.txn("Expense", d("2026-09", 8), "Vendor X", "", [("35", 777), ("1", -777)])
        x["checking"] += [(d("2026-09", 7), "VENDOR X", -777), (d("2026-09", 2), "VENDOR X", -777)]


class TestMatchingNotGreedy(Base):
    scenario = staticmethod(greedy)

    def test_no_false_flags(self):
        _, exc, _ = self.review()
        titles = [t for c, t in everything(exc) if c == "T1"]
        self.assertFalse([t for t in titles if "777" in t], titles)


def mid_month_card(stage, b, x):
    if stage == "september":
        x["statement_end"]["brex"] = date(2026, 9, 22)


class TestMidMonthCardStatement(Base):
    scenario = staticmethod(mid_month_card)

    def test_rolled_forward_and_ties(self):
        results, _, _ = self.review()
        t = next(t for t in results["T1"].tieouts if t["item"] == "Brex Card")
        self.assertEqual(t["status"], "tie")
        self.assertIn("rolled forward", t["note"])


def two_payouts_one_booked(stage, b, x):
    if stage == "september":
        x["stripe"].append({"id": "txn_big", "date": d("2026-09", 8), "category": "charge", "amount": D("10000"),
                            "fee": D("0"), "net": D("10000"), "currency": "usd", "description": "Big sale"})
        for pid, day in (("po_a", 10), ("po_b", 12)):
            x["stripe"].append({"id": pid, "date": d("2026-09", day), "category": "payout", "amount": D("-5000"),
                                "fee": D("0"), "net": D("-5000"), "currency": "usd", "description": "STRIPE PAYOUT"})
        x["checking"] += [(d("2026-09", 11), "STRIPE TRANSFER", 5000), (d("2026-09", 13), "STRIPE TRANSFER", 5000)]
        b.txn("Deposit", d("2026-09", 11), "Stripe", "Stripe payout", [("1", 5000), ("20", -5000)])


class TestTwoPayoutsOneBooked(Base):
    scenario = staticmethod(two_payouts_one_booked)

    def test_unbooked_one_flagged(self):
        _, exc, _ = self.review()
        hits = [t for c, t in everything(exc) if "5,000" in t and "isn't in the books" in t]
        self.assertEqual(len(hits), 1, hits)


def partial_fees(stage, b, x):
    if stage == "september":
        b.txn("Journal Entry", d("2026-09", 30), "Stripe", "Stripe fees", [("30", 900), ("20", -900)])


class TestPartialFeeShortfall(Base):
    scenario = staticmethod(partial_fees)

    def test_flagged(self):
        _, exc, _ = self.review()
        self.assertFlag(everything(exc), "T4", "fees", "differ")


def employee_named(stage, b, x):
    if stage == "september":
        b.txn("Paycheck", d("2026-09", 16), "Jane Doe", "Bonus", [("31", 5000), ("1", -5000)])
        x["checking"].append((d("2026-09", 16), "PAYROLL JANE DOE", -5000))


class TestPrivacy(Base):
    scenario = staticmethod(employee_named)

    def test_no_person_in_outputs(self):
        from helpers import cli
        rc, _ = cli("run", "--root", str(self.root), "--month", "2026-09")
        self.assertEqual(rc, 0)
        out = self.month / "output"
        for name in ("results.json", "review.md", "flux.csv", "bs-flux.csv", "exceptions.csv"):
            self.assertNotIn("Jane Doe", (out / name).read_text(), name)


class TestRecurringNotRunIsHonest(Base):
    def test_part_not_run(self):
        c = self.cfg()
        c["thresholds"] = {"recurring_min_months": 7}
        self.save_cfg(c)
        results, _, _ = self.review()
        self.assertTrue(any(p["status"] == "not_run" for p in results["C1"].parts))
        self.assertNotIn("Notion", str(results["C1"].exceptions))


class TestBudgetWrongMonth(Base):
    def test_not_stop_but_not_run(self):
        import csv
        p = self.inp / "budget.csv"
        with open(p) as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            r["month"] = "2025-09"
        with open(p, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        results, _, _ = self.review()
        self.assertFalse(results["V1"].data["has_budget"])
        self.assertTrue(any("2026-09" in p["reason"] for p in results["V1"].parts))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- re-review (2026-10-12) scenarios

def chain_bait(stage, b, x):
    if stage == "september":
        # A $5k uncashed check and a $5k uncategorized card charge on the same day: different causes.
        b.txn("Check", d("2026-09", 10), "Landlord", "Deposit", [("35", 5000), ("1", -5000)], doc="1077")
        b.txn("Expense", d("2026-09", 10), "", "Unknown", [("40", 5000), ("3", -5000)])
        x["brex"].append((d("2026-09", 10), "UNKNOWN MERCHANT", -5000))


class TestGroupingDoesNotChain(Base):
    scenario = staticmethod(chain_bait)

    def test_separate_items(self):
        _, exc, _ = self.review()
        leads = [e for e in exc["open"] if "5,000" in e["title"] or any("5,000" in a["title"] for a in e.get("also", []))]
        treasury = next(e for e in exc["open"] if "Uncategorized" in e["title"])
        self.assertTrue(all("Landlord" not in a["title"] for a in treasury.get("also", [])))
        self.assertGreaterEqual(len(leads), 1)


def unnamed_safe(stage, b, x):
    if stage == "september":
        b.txn("Deposit", d("2026-09", 18), "", "Incoming wire", [("1", 500000), ("22", -500000)])
        x["checking"].append((d("2026-09", 18), "WIRE IN", 500000))


class TestUnnamedFinancing(Base):
    scenario = staticmethod(unnamed_safe)

    def test_flagged(self):
        _, exc, _ = self.review()
        self.assertFlag(everything(exc), "K3", "500,000")


def accrued_already(stage, b, x):
    if stage == "september":
        b.txn("Journal Entry", d("2026-09", 30), "Amazon Web Services", "Accrue AWS September", [("33", 8200), ("7", -8200)])


class TestCutoffAlreadyAccrued(Base):
    scenario = staticmethod(accrued_already)

    def test_not_flagged_and_no_double_accrual(self):
        results, exc, _ = self.review()
        self.assertNoFlag(everything(exc), "Amazon Web Services", "looks like")
        self.assertFalse([x for x in results["C2"].drafts if "Amazon" in x["memo"]])


class TestDraftsUseReversingAccount(Base):
    def test_accrual_credits_reversing_account(self):
        results, _, _ = self.review()
        aws = next(x for x in results["C2"].drafts if "Amazon" in x["memo"])
        self.assertEqual(aws["lines"][1]["account"], "Accrued Liabilities")


def recognition_je(stage, b, x):
    if stage == "september":
        b.txn("Journal Entry", d("2026-09", 30), "Beta Corp", "Annual plan recognition", [("8", 5000), ("20", -5000)])


class TestRecognitionNotFlagged(Base):
    scenario = staticmethod(recognition_je)

    def test_no_annual_flag(self):
        _, exc, _ = self.review()
        self.assertNoFlag(everything(exc), "Beta Corp", "annual plan")


def typo_bait(stage, b, x):
    if stage == "september":
        b.txn("Deposit", d("2026-09", 29), "Customer Y", "Wire", [("1", 2100), ("22", -2100)])
        x["checking"].append((d("2026-09", 29), "WIRE CUSTOMER Z", 1200))


class TestTypoNeverReady(Base):
    scenario = staticmethod(typo_bait)

    def test_in_transit_not_paired_and_no_ready_typo_draft(self):
        results, exc, _ = self.review()
        self.assertFalse([x for x in results["T1"].drafts if x["status"] == "ready"])
        self.assertNoFlag(everything(exc), "2,100", "typed wrong")


def bill_after_month(stage, b, x):
    if stage == "september":
        b.txn("Check", d("2026-09", 25), "Gusto", "Benefits", [("35", 1700), ("1", -1700)])
        x["checking"].append((d("2026-09", 25), "GUSTO BENEFITS", -1700))
        x["open_bills"].append({"bill_id": "B7", "vendor": "Gusto", "txn_date": "2026-10-02", "doc_num": "G-10",
                                "amount": "1700.00", "balance": "1700.00"})


class TestBillDatedAfterExpense(Base):
    scenario = staticmethod(bill_after_month)

    def test_no_delete_advice(self):
        _, exc, _ = self.review()
        self.assertNoFlag(everything(exc), "still open in AP")


def wire_vs_payouts(stage, b, x):
    if stage == "september":
        b.txn("Deposit", d("2026-09", 30), "Customer X", "Wire", [("1", 11000), ("22", -11000)])
        x["stripe"].append({"id": "txn_c", "date": d("2026-09", 8), "category": "charge", "amount": D("11000"),
                            "fee": D("0"), "net": D("11000"), "currency": "usd", "description": "x"})
        for pid, day, amt in (("po_x", 10, "4000"), ("po_y", 12, "7000")):
            x["stripe"].append({"id": pid, "date": d("2026-09", day), "category": "payout", "amount": -D(amt), "fee": D("0"),
                                "net": -D(amt), "currency": "usd", "description": "STRIPE PAYOUT"})
        x["checking"] += [(d("2026-09", 11), "STRIPE TRANSFER", 4000), (d("2026-09", 13), "STRIPE TRANSFER", 7000)]


class TestGroupMatchNeedsSharedWord(Base):
    scenario = staticmethod(wire_vs_payouts)

    def test_payouts_not_matched_to_wire(self):
        _, exc, _ = self.review()
        titles = everything(exc)
        self.assertFlag(titles, "T3", "4,000", "isn't in the books")
        self.assertFlag(titles, "T3", "7,000", "isn't in the books")


def recurring_prior(stage, b, x):
    if stage == "history":
        b.txn("Check", d("2026-07", 10), "Contractor", "Check 2001", [("39", 2000), ("1", -2000)], doc="2001")
    else:
        b.txn("Check", d("2026-09", 3), "Contractor", "Check 2002", [("39", 2000), ("1", -2000)], doc="2002")
        x["checking"].append((d("2026-09", 10), "CHECK 2002", -2000))
        x["opening_adjust"]["checking"] = 2000
        x["outstanding"]["checking"] = [{"date": "2026-07-10", "description": "Contractor", "amount": "-2000",
                                         "side": "book", "doc_num": "2001"}]


class TestCheckNumbersBeatOldItems(Base):
    scenario = staticmethod(recurring_prior)

    def test_old_check_stays_stale(self):
        _, exc, _ = self.review()
        titles = everything(exc)
        self.assertFlag(titles, "T1", "2026-07-10", "still uncleared")
        self.assertNoFlag(titles, "2,000", "2026-09-03")


class TestJournalEntryToReceivablesWhenAgingTies(Base):
    """Live QuickBooks: a journal entry to A/R must name a customer, so it's in the aging and everything ties."""

    def test_flagged_even_though_aging_ties(self):
        import csv as _csv
        with open(self.inp / "tb_current.csv") as f:
            ar = next(r for r in _csv.DictReader(f) if r["account_id"] == "4")
        rows = (self.inp / "aging.csv").read_text().splitlines()
        rows = [rows[0]] + [f"ar,2026-09-30,{ar['debit']},0" if r.startswith("ar,") else r for r in rows[1:]]
        (self.inp / "aging.csv").write_text("\n".join(rows) + "\n")
        titles = self.titles()
        self.assertFlag(titles, "T2", "receivable", "3,000", "journal entries")
        self.assertNoFlag(titles, "ledger and aging differ")


class TestEmptyAgingStops(Base):
    def test_stop(self):
        (self.inp / "aging.csv").write_text("kind,as_of,total,over_90\n")
        self.assertIn("G1", self.gates())


class TestAmbiguousName(Base):
    def test_leaf_name_matching_two_accounts(self):
        import csv as _csv
        p = self.inp / "accounts.csv"
        with open(p) as f:
            rows = list(_csv.DictReader(f))
        rows.append(dict(rows[-1], account_id="99", name="Wages", full_name="Contractors:Wages", account_type="Expense"))
        with open(p, "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        c = self.cfg()
        c["roles"]["payroll_wages"] = ["Wages"]
        self.save_cfg(c)
        self.assertIn("more than one account", self.gate_text())


def depreciation_renamed(stage, b, x):
    depreciation_missing(stage, b, x)
    if stage == "september":
        b.txn("Journal Entry", d("2026-09", 30), "", "Depr. - laptops", [("42", 1000), ("12", -1000)])


class TestRenamedMonthlyEntryCounts(Base):
    scenario = staticmethod(depreciation_renamed)

    def test_no_flag(self):
        _, exc, _ = self.review()
        self.assertNoFlag(everything(exc), "Depreciation", "missing")


class TestMissingEntryReportedOnce(Base):
    scenario = staticmethod(depreciation_missing)

    def test_once(self):
        _, exc, _ = self.review()
        self.assertEqual(len([t for c, t in everything(exc) if c == "C4"]), 1)


class TestBudgetTotalsUseBudgetedLines(Base):
    def test_totals_add_up_and_unbudgeted_listed(self):
        results, _, _ = self.review()
        t = results["V1"].data["totals"]
        for k in ("revenue", "opex", "net_income"):
            self.assertEqual(t[k]["vs_budget"], t[k]["current"] - t[k]["budget"], k)
        self.assertEqual(t["opex"]["vs_budget"], D("-6500"))
        names = [u["account"] for u in results["V1"].data["not_in_budget"]]
        self.assertIn("Uncategorized Expense", names)
        self.assertEqual(t["opex"]["not_in_budget"], D("52480"))


class TestClearingGroupedWithUnbookedPayout(Base):
    scenario = staticmethod(clearing_off)

    def test_one_item(self):
        _, exc, _ = self.review()
        payout = next(e for e in exc["open"] if "11,000" in e["title"] or any("11,000" in a["title"] for a in e.get("also", [])))
        joined = [payout["title"]] + [a["title"] for a in payout.get("also", [])]
        self.assertTrue(any("clearing" in t.lower() for t in joined), joined)


class TestBookedIntoLastMonthAfterReview(Base):
    """September is reviewed; the $50k treasury transfer is then booked dated Sep 20; October must pair it with the
    carried statement line instead of flagging it again."""

    def test_carry(self):
        import shutil as _sh
        from helpers import cli
        from mer import config
        from mer.checks.cash import prepare_matching
        from mer.data import load
        from mer.util import Period, write_csv
        self.assertEqual(cli("run", "--root", str(self.root), "--month", "2026-09")[0], 0)
        oct_in = self.root / "2026-10" / "inputs"
        oct_in.mkdir(parents=True)
        _sh.copy(self.inp / "accounts.csv", oct_in / "accounts.csv")
        with open(self.inp / "gl.csv") as f:
            sep = list(csv.DictReader(f))
        sep = [dict(r, line_id=f"h2026-09-{r['line_id']}") for r in sep]
        sep += [{"line_id": "h2026-09-9999-2-1", "txn_id": "9999", "txn_type": "Transfer", "date": "2026-09-20",
                 "account_id": "2", "account": "Mercury Treasury", "name": "", "memo": "Transfer", "doc_num": "", "amount": "50000.00"},
                {"line_id": "h2026-09-9999-40-1", "txn_id": "9999", "txn_type": "Transfer", "date": "2026-09-20",
                 "account_id": "40", "account": "Uncategorized Expense", "name": "", "memo": "Transfer", "doc_num": "", "amount": "-50000.00"}]
        write_csv(oct_in / "gl_history.csv", sep, list(sep[0].keys()))
        d = load(self.root / "2026-10", Period("2026-10"), config.load(self.root))
        lines = d.outstanding_prior["2"]["lines"]
        self.assertTrue(any(str(l.get("source_ref", "")).endswith("after its review)") for l in lines), lines)
        from mer.checks.matching import match_account, prior_as_bank, prior_as_book
        m = match_account([], prior_as_book("2", lines), prior_as_bank("2", lines), 5, date(2026, 10, 26))
        self.assertFalse([x for x in m.bank_left if x["amount"] == D("50000")])


import csv  # noqa: E402


# ---------------------------------------------------------------- AI-bookkeeping failures (2026-10-30)
# From a real report: transfers booked as income, one vendor under two names, contractor payroll not split
# between cost of sales and admin.

from fixture_northwind import HISTORY  # noqa: E402
from mer import config as _config  # noqa: E402
from mer.checks.completeness import similar_vendors  # noqa: E402
from mer.util import StopRun, vendor_key  # noqa: E402


def k4(exc):
    return [(c, t) for c, t in everything(exc) if c == "K4"]


def transfer_as_revenue(stage, b, x):
    if stage == "september":
        b.txn("Deposit", d("2026-09", 10), "", "Deposit", [("1", 5000), ("22", -5000)])
        x["checking"].append((d("2026-09", 10), "DEPOSIT", 5000))
        x["treasury"].append((d("2026-09", 8), "WITHDRAWAL", -5000))     # two days earlier, not in the books


class TestTransferBookedAsRevenue(Base):
    scenario = staticmethod(transfer_as_revenue)

    def test_flagged_and_grouped_with_the_missing_withdrawal(self):
        results, exc, _ = self.review()
        self.assertFlag(everything(exc), "K4", "$5,000.00", "Services Revenue", "transfer from Mercury Treasury",
                        "not income")
        e = next(e for e in exc["open"] if e["check"] == "K4" and "5,000" in e["title"])
        self.assertIn("T1", [a["check"] for a in e["also"]])          # Treasury's statement row isn't in the books
        self.assertTrue(any(r.startswith("GL ") for r in e["refs"]))
        self.assertIn("transfer from Mercury Treasury to Mercury Checking", e["action"])
        self.assertEqual(e["severity"], "high")                       # medium, raised at $5,000
        draft = next(x for x in results["K4"].drafts if "5,000" in x["memo"])
        self.assertEqual([(l["account"], l["debit"], l["credit"]) for l in draft["lines"]],
                         [("Services Revenue", D("5000"), None), ("Mercury Treasury", None, D("5000"))])

    def test_window_is_configurable(self):
        c = self.cfg()
        c["thresholds"] = {"transfer_window_days": 1}
        self.save_cfg(c)
        _, exc, _ = self.review()
        self.assertFalse([t for c, t in k4(exc) if "5,000" in t])


def transfer_by_memo_and_own_name(stage, b, x):
    if stage == "september":
        b.txn("Deposit", d("2026-09", 11), "", "Internal transfer from savings", [("1", 2000), ("21", -2000)])
        x["checking"].append((d("2026-09", 11), "INTERNAL TRANSFER", 2000))
        b.txn("Deposit", d("2026-09", 16), "Northwind SaaS", "", [("1", 3500), ("22", -3500)])
        x["checking"].append((d("2026-09", 16), "NORTHWIND SAAS INC", 3500))
        x["config"]["company"] = "Northwind SaaS Inc"


class TestTransferByWording(Base):
    scenario = staticmethod(transfer_by_memo_and_own_name)

    def test_memo_and_company_name(self):
        _, exc, _ = self.review()
        titles = everything(exc)
        self.assertFlag(titles, "K4", "$2,000.00", "Interest Income", "between your own accounts", "not income")
        self.assertFlag(titles, "K4", "$3,500.00", "Services Revenue", "between your own accounts")


def transfer_both_sides_miscoded(stage, b, x):
    if stage == "september":
        b.txn("Expense", d("2026-09", 12), "", "Withdrawal", [("35", 8000), ("2", -8000)])
        x["treasury"].append((d("2026-09", 12), "WITHDRAWAL", -8000))
        b.txn("Deposit", d("2026-09", 13), "", "Deposit", [("1", 8000), ("22", -8000)])
        x["checking"].append((d("2026-09", 13), "DEPOSIT", 8000))


class TestTransferBothSidesMiscoded(Base):
    scenario = staticmethod(transfer_both_sides_miscoded)

    def test_one_item_for_both_sides(self):
        results, exc, _ = self.review()
        mine = [e for e in results["K4"].exceptions if "8,000" in e["title"]]
        self.assertEqual(len(mine), 1, mine)                          # not flagged again as the mirror case
        self.assertIn("transfer from Mercury Treasury, not income", mine[0]["title"])
        self.assertEqual(len(mine[0]["txns"]), 2)
        draft = next(x for x in results["K4"].drafts if "8,000" in x["memo"])
        self.assertEqual([l["account"] for l in draft["lines"]], ["Services Revenue", "Office Supplies"])


def transfer_double_booked(stage, b, x):
    if stage == "september":
        b.txn("Transfer", d("2026-09", 16), "", "Transfer", [("1", 4000), ("2", -4000)])
        b.txn("Deposit", d("2026-09", 16), "", "Deposit", [("1", 4000), ("22", -4000)])  # the feed's copy, as revenue
        x["checking"].append((d("2026-09", 16), "FROM TREASURY", 4000))
        x["treasury"].append((d("2026-09", 16), "TO CHECKING", -4000))


class TestTransferAlsoBookedAsRevenue(Base):
    scenario = staticmethod(transfer_double_booked)

    def test_flagged(self):
        _, exc, _ = self.review()
        self.assertFlag(everything(exc), "K4", "$4,000.00", "transfer from Mercury Treasury")


def card_payment_as_expense(stage, b, x):
    if stage == "september":
        b.txn("Expense", d("2026-09", 26), "", "Credit card payment", [("35", 1200), ("1", -1200)])
        x["checking"].append((d("2026-09", 26), "BREX PAYMENT", -1200))
        x["brex"].append((d("2026-09", 27), "PAYMENT", 1200))


class TestCardPaymentBookedAsExpense(Base):
    scenario = staticmethod(card_payment_as_expense)

    def test_flagged(self):
        _, exc, _ = self.review()
        self.assertFlag(everything(exc), "K4", "$1,200.00", "Office Supplies", "transfer to Brex Card", "not an expense")
        e = next(e for e in exc["open"] if "1,200" in e["title"] or any("1,200" in a["title"] for a in e["also"]))
        self.assertIn("credit card payment", e["action"])


def transfer_lookalikes(stage, b, x):
    if stage == "september":
        # A customer deposit and an unrelated check of the same amount on the same day.
        b.txn("Deposit", d("2026-09", 14), "Customer Q", "Implementation fee", [("1", 2500), ("22", -2500)])
        x["checking"].append((d("2026-09", 14), "ACH CREDIT CUSTOMER Q", 2500))
        b.txn("Check", d("2026-09", 14), "Bob Builder", "Office repairs", [("39", 2500), ("2", -2500)])
        x["treasury"].append((d("2026-09", 14), "CHECK 2201", -2500))
        # A customer's wire.
        b.txn("Deposit", d("2026-09", 15), "Globex", "Incoming wire transfer", [("1", 7000), ("22", -7000)])
        x["checking"].append((d("2026-09", 15), "INCOMING WIRE TRANSFER GLOBEX", 7000))
        # A transfer booked right, next to a customer deposit of the same amount.
        b.txn("Transfer", d("2026-09", 16), "", "Sweep from treasury", [("1", 3000), ("2", -3000)])
        x["checking"].append((d("2026-09", 16), "FROM TREASURY", 3000))
        x["treasury"].append((d("2026-09", 16), "TO CHECKING", -3000))
        b.txn("Deposit", d("2026-09", 17), "Customer R", "Setup fee", [("1", 3000), ("22", -3000)])
        x["checking"].append((d("2026-09", 17), "ACH CREDIT CUSTOMER R", 3000))


class TestTransferLookalikesNotFlagged(Base):
    scenario = staticmethod(transfer_lookalikes)

    def test_no_false_flags(self):
        _, exc, _ = self.review()
        titles = [t for _, t in k4(exc)]
        for amount in ("2,500", "7,000", "3,000"):
            self.assertFalse([t for t in titles if amount in t], titles)


class TestVendorKey(unittest.TestCase):
    def test_variants_match(self):
        for a, b in [("The Home Depot #4521", "Home Depot"), ("THE HOME DEPOT #4521 ATLANTA GA", "Home Depot, Inc."),
                     ("SQ *BLUE BOTTLE COFFEE", "Blue Bottle Coffee"), ("TST* Joe's Pizza", "Joes Pizza"),
                     ("PAYPAL *ADOBE", "Adobe"), ("Notion, Inc.", "Notion"), ("Acme & Co.", "ACME"),
                     ("Amazon.com", "Amazon"), ("WALGREENS STORE 1234", "Walgreens"), ("AT&T", "ATT"),
                     ("Cooley L.L.P.", "Cooley LLP"), ("We Work", "WeWork"), ("7-Eleven #12345", "7-Eleven")]:
            self.assertEqual(vendor_key(a), vendor_key(b), (a, b))

    def test_different_vendors_stay_apart(self):
        for a, b in [("Building 1 LLC", "Building 2 LLC"), ("Amazon", "Amazon Web Services"), ("7-Eleven", "Eleven"),
                     ("Smith Plumbing", "Smyth Plumbing"), ("Notion", "Notion Labs")]:
            self.assertNotEqual(vendor_key(a), vendor_key(b), (a, b))

    def test_near_identical(self):
        cfg = {"thresholds": {"vendor_match_min_length": 8, "vendor_similarity_pct": D("90")}}
        self.assertTrue(similar_vendors("acmedesign", "acmedesigns", cfg))
        self.assertTrue(similar_vendors("johnsonconsulting", "jonsonconsulting", cfg))
        self.assertFalse(similar_vendors("smithplumbing", "smythplumbing", cfg))      # a swapped letter
        self.assertFalse(similar_vendors("unit4properties", "unit7properties", cfg))  # a different number
        self.assertFalse(similar_vendors("stripe", "stripes", cfg))                   # too short to judge
        self.assertFalse(similar_vendors("notion", "notionlabs", cfg))


def vendor_variants(stage, b, x):
    if stage == "history":
        for m in HISTORY[1:]:
            b.txn("Expense", d(m, 10), "Home Depot", "Supplies", [("35", 450), ("3", -450)])
    else:
        b.txn("Expense", d("2026-09", 10), "Home Depot", "Supplies", [("35", 450), ("3", -450)])
        b.txn("Expense", d("2026-09", 12), "THE HOME DEPOT #4521", "Supplies", [("35", 450), ("3", -450)])
        b.txn("Expense", d("2026-09", 7), "Notion, Inc.", "Notion workspace", [("34", 300), ("3", -300)])
        x["brex"] += [(d("2026-09", 10), "HOME DEPOT", -450), (d("2026-09", 12), "THE HOME DEPOT #4521", -450),
                      (d("2026-09", 7), "NOTION", -300)]


class TestVendorNameVariants(Base):
    scenario = staticmethod(vendor_variants)

    def test_duplicate_found_across_names(self):
        _, exc, _ = self.review()
        self.assertFlag(everything(exc), "C1", "Possible duplicate", "Home Depot / THE HOME DEPOT #4521", "$450.00")

    def test_renamed_vendor_is_not_missing(self):
        _, exc, _ = self.review()
        self.assertNoFlag(everything(exc), "Notion", "nothing in September")

    def test_duplicate_vendor_records(self):
        results, exc, _ = self.review()
        titles = everything(exc)
        self.assertFlag(titles, "C5", "duplicate vendor records", "\"Home Depot\"", "\"THE HOME DEPOT #4521\"")
        self.assertFlag(titles, "C5", "duplicate vendor records", "\"Notion\"", "\"Notion, Inc.\"")
        e = next(e for e in results["C5"].exceptions if "Notion" in e["title"])
        self.assertEqual(e["severity"], "low")
        self.assertEqual(e["amount"], D("300"))                    # what's booked under the less-used name
        self.assertTrue(e["refs"] and all(r.startswith("GL ") for r in e["refs"]))
        self.assertIn("merge them in QuickBooks", e["action"])
        self.assertEqual(e["accounts"], [])                         # a vendor record doesn't make a P&L line unreliable


def vendor_near_names(stage, b, x):
    if stage == "history":
        for m in HISTORY[1:]:
            b.txn("Expense", d(m, 11), "Linear Software", "Linear seats", [("34", 400), ("3", -400)])
            b.txn("Check", d(m, 15), "Smith Plumbing", "Repairs", [("35", 600), ("1", -600)])
            b.txn("Check", d(m, 16), "Building 1 LLC", "Storage unit", [("36", 900), ("1", -900)])
            b.txn("Check", d(m, 17), "Pacific Gas", "Utilities", [("35", 250), ("1", -250)])
    else:
        b.txn("Expense", d("2026-09", 11), "Linear Softwares", "Linear seats", [("34", 400), ("3", -400)])
        b.txn("Bill", d("2026-09", 8), "Acme Designs", "Logo refresh", [("39", 900), ("6", -900)])
        b.txn("Check", d("2026-09", 15), "Smyth Plumbing", "Repairs", [("35", 700), ("1", -700)])
        b.txn("Check", d("2026-09", 16), "Building 2 LLC", "Storage unit", [("36", 900), ("1", -900)])
        b.txn("Check", d("2026-09", 17), "Pacific Glass", "Window", [("39", 260), ("1", -260)])
        x["brex"].append((d("2026-09", 11), "LINEAR", -400))
        x["checking"] += [(d("2026-09", 15), "CHECK", -700), (d("2026-09", 16), "CHECK", -900),
                          (d("2026-09", 17), "CHECK", -260)]


class TestNearIdenticalVendorNames(Base):
    scenario = staticmethod(vendor_near_names)

    def test_near_names_flagged_and_lookalikes_not(self):
        results, exc, _ = self.review()
        c5 = [(c, t) for c, t in everything(exc) if c == "C5"]
        self.assertFlag(c5, "C5", "\"Linear Software\"", "\"Linear Softwares\"")
        self.assertFlag(c5, "C5", "\"Acme Design\"", "\"Acme Designs\"")
        for other in ("Smyth", "Building 2", "Pacific Glass"):   # a swapped letter, a different number, another account
            self.assertNoFlag(c5, other)
        self.assertEqual(len(c5), 2, c5)

    def test_missing_recurring_points_at_the_near_name(self):
        results, _, _ = self.review()
        e = next(e for e in results["C1"].exceptions if e["kind"] == "missing-recurring" and "Linear" in e["title"])
        self.assertIn("\"Linear Softwares\", a near-identical name", e["detail"])


def payroll_split(sep_cogs=0, person=False, expected=None, history=True):
    """Support wages in cost of sales: $20,000 a month in the history, `sep_cogs` in September."""
    def scenario(stage, b, x):
        if stage == "history":
            b.account("44", "Cost of Revenue:Support Wages", "Cost of Goods Sold", "CostOfLaborCos", "Expense")
            x["roles"]["payroll_wages"] = ["Payroll Expenses:Wages", "Cost of Revenue:Support Wages"]
            if expected is not None:
                x["config"]["payroll_cogs_pct"] = expected
            for m in HISTORY if history else []:
                b.txn("Payroll Check", d(m, 14), "Gusto", "Payroll", [("44", 20000), ("1", -20000)])
            return
        if sep_cogs:
            b.txn("Payroll Check", d("2026-09", 15), "Gusto", "Payroll", [("44", sep_cogs), ("1", -sep_cogs)])
            x["checking"].append((d("2026-09", 15), "GUSTO PAYROLL", -sep_cogs))
        if person:
            b.txn("Paycheck", d("2026-09", 16), "Jane Doe", "Support shift pay", [("44", 3000), ("1", -3000)])
            x["checking"].append((d("2026-09", 16), "PAYROLL JANE DOE", -3000))
    return scenario


def k5_text(results):
    return " ".join(f"{e['title']} {e['detail']} {e['refs']}" for e in results["K5"].exceptions) + results["K5"].summary


class TestPayrollAllInOpex(Base):
    scenario = staticmethod(payroll_split())

    def test_flagged(self):
        results, exc, _ = self.review()
        self.assertFlag(everything(exc), "K5", "All of September 2026's payroll ($64,500.00) is in operating expenses",
                        "13.4% in the last 6 months")
        e = results["K5"].exceptions[0]
        self.assertEqual(e["amount"], D("8643.00"))                  # 13.4% of $64,500, to the bottom of the range
        self.assertEqual(e["severity"], "high")
        self.assertTrue(any(r.startswith("GL ") for r in e["refs"]))
        self.assertEqual(results["K5"].drafts[0]["status"], "needs your amount")
        self.assertEqual(results["K5"].drafts[0]["lines"][0]["account"], "Cost of Revenue:Support Wages")


class TestPayrollSplitAsUsual(Base):
    scenario = staticmethod(payroll_split(sep_cogs=10000))

    def test_not_flagged(self):
        results, _, _ = self.review()
        self.assertEqual(results["K5"].status, "pass", results["K5"].exceptions)
        self.assertIn("13.4%", results["K5"].summary)


class TestPayrollTooMuchInCostOfSales(Base):
    scenario = staticmethod(payroll_split(sep_cogs=40000, person=True))

    def test_flagged_without_people(self):
        results, exc, _ = self.review()
        self.assertFlag(everything(exc), "K5", "40.0% of September 2026's payroll is in cost of sales")
        self.assertNotIn("Jane Doe", k5_text(results))


class TestPayrollSplitFromConfig(Base):
    scenario = staticmethod(payroll_split(expected=30, history=False))

    def test_expected_split_without_history(self):
        results, exc, _ = self.review()
        self.assertFlag(everything(exc), "K5", "in operating expenses", "review-config.json expects 30%")


class TestPayrollSplitConfigSaysNone(Base):
    scenario = staticmethod(payroll_split(expected=0, history=False))

    def test_runs_and_passes(self):
        results, _, _ = self.review()
        self.assertEqual(results["K5"].status, "pass")


class TestPayrollSplitConfigKey(Base):
    def test_bad_values_stop(self):
        for bad in (150, -1, "about a third", True):
            c = self.cfg()
            c["payroll_cogs_pct"] = bad
            self.save_cfg(c, reconfirm=False)
            with self.assertRaises(StopRun, msg=bad):
                _config.load(self.root)

    def test_a_person_confirms_it(self):
        c = self.cfg()
        c["payroll_cogs_pct"] = 25
        self.save_cfg(c, reconfirm=False)
        self.assertIn("payroll_cogs_pct) changed after", self.gate_text())
        config_ok = self.cfg()
        self.save_cfg(config_ok)                                     # confirm-roles again
        self.assertEqual(self.gates(), [])
