"""Ingest paths: bank/card CSVs, Stripe CSVs, payroll, budgets, attestations, money and dates."""

from __future__ import annotations

import csv
import json
import unittest
from datetime import date
from decimal import Decimal as D

from helpers import Base, P

from mer import inputs  # noqa: E402
from mer.util import Period, StopRun, money  # noqa: E402


def write(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


class IngestBase(Base):
    def setUp(self):
        super().setUp()
        self.man = inputs.Manifest(self.month, P)

    def set_format(self, account, **fmt):
        c = self.cfg()
        c["bank_formats"][account] = dict({"date_col": "", "description_col": "", "amount_col": "", "debit_col": "",
                                           "credit_col": "", "date_format": "", "flip_sign": False, "status_col": "",
                                           "posted_values": []}, **fmt)
        self.save_cfg(c)
        from mer import config
        return config.load(self.root)


class TestStatementIngest(IngestBase):
    def test_debit_credit_columns_and_period_filter(self):
        cfg = self.set_format("Mercury Checking", date_col="Posted", description_col="Details", debit_col="Debit",
                              credit_col="Credit", date_format="%m/%d/%Y")
        src = self.root / "chk.csv"
        write(src, ["Posted", "Details", "Debit", "Credit"],
              [["09/03/2026", "STRIPE", "", "1,200.00"], ["09/04/2026", "AWS", "55.10", ""], ["08/30/2026", "OLD", "1.00", ""]])
        msg = inputs.ingest_statement(self.month, P, cfg, "mercury checking", src, "1144.90", "2026-09-30", "0", None, self.man)
        self.assertIn("2 rows", msg)
        rows = list(csv.DictReader(open(self.inp / "bank" / "1.csv")))
        self.assertEqual([r["amount"] for r in rows], ["1200.00", "-55.10"])

    def test_flip_sign_for_card(self):
        cfg = self.set_format("Brex Card", date_col="Date", description_col="Desc", amount_col="Amount", flip_sign=True)
        src = self.root / "brex.csv"
        write(src, ["Date", "Desc", "Amount"], [["2026-09-06", "FIGMA", "480.00"], ["2026-09-28", "PAYMENT", "-1080.00"]])
        inputs.ingest_statement(self.month, P, cfg, "Brex Card", src, "0", "2026-09-30", "600", None, self.man)
        rows = list(csv.DictReader(open(self.inp / "bank" / "3.csv")))
        self.assertEqual([r["amount"] for r in rows], ["-480.00", "1080.00"])

    def test_status_column_must_be_configured(self):
        cfg = self.set_format("Mercury Checking", date_col="Date", description_col="Desc", amount_col="Amount")
        src = self.root / "m.csv"
        write(src, ["Date", "Desc", "Amount", "Status"], [["2026-09-03", "X", "10", "Pending"]])
        with self.assertRaises(StopRun) as e:
            inputs.ingest_statement(self.month, P, cfg, "Mercury Checking", src, "10", "2026-09-30", "0", None, self.man)
        self.assertIn("pending", str(e.exception))

    def test_pending_rows_skipped(self):
        cfg = self.set_format("Mercury Checking", date_col="Date", description_col="Desc", amount_col="Amount",
                              status_col="Status", posted_values=["Sent"])
        src = self.root / "m.csv"
        write(src, ["Date", "Desc", "Amount", "Status"], [["2026-09-03", "X", "10", "Sent"], ["2026-09-04", "Y", "99", "Pending"],
                                                          ["2026-09-05", "Z", "5", "Failed"]])
        msg = inputs.ingest_statement(self.month, P, cfg, "Mercury Checking", src, "10", "2026-09-30", "0", None, self.man)
        self.assertIn("2 pending/failed skipped", msg)

    def test_opening_balance_required_first_time(self):
        cfg = self.set_format("Mercury Checking", date_col="Date", description_col="Desc", amount_col="Amount")
        src = self.root / "m.csv"
        write(src, ["Date", "Desc", "Amount"], [["2026-09-03", "X", "10"]])
        with self.assertRaises(StopRun) as e:
            inputs.ingest_statement(self.month, P, cfg, "Mercury Checking", src, "10", "2026-09-30", None, None, self.man)
        self.assertIn("opening balance", str(e.exception))

    def test_opening_balance_from_last_month(self):
        prior = self.root / "2026-08" / "inputs"
        prior.mkdir(parents=True)
        (prior / "manifest.json").write_text(json.dumps({"period": "2026-08", "files": {}, "attestations": {},
                                                         "statements": {"1": {"ending_balance": "500", "statement_end": "2026-08-31"}}}))
        cfg = self.set_format("Mercury Checking", date_col="Date", description_col="Desc", amount_col="Amount")
        src = self.root / "m.csv"
        write(src, ["Date", "Desc", "Amount"], [["2026-09-03", "X", "10"]])
        inputs.ingest_statement(self.month, P, cfg, "Mercury Checking", src, "510", "2026-09-30", None, None, self.man)
        self.assertEqual(self.man.statements["1"]["opening_source"], "last month's statement")

    def test_statement_end_outside_month(self):
        cfg = self.set_format("Mercury Checking", date_col="Date", description_col="Desc", amount_col="Amount")
        src = self.root / "m.csv"
        write(src, ["Date", "Desc", "Amount"], [["2026-09-03", "X", "10"]])
        with self.assertRaises(StopRun):
            inputs.ingest_statement(self.month, P, cfg, "Mercury Checking", src, "10", "2026-10-02", "0", None, self.man)

    def test_account_not_in_roles(self):
        cfg = self.set_format("Mercury Checking", date_col="Date", description_col="Desc", amount_col="Amount")
        with self.assertRaises(StopRun):
            inputs.ingest_statement(self.month, P, cfg, "Stripe Clearing", self.root / "x.csv", "1", "2026-09-30", "0", None, self.man)


class TestStripeCsv(IngestBase):
    HDR = ["balance_transaction_id", "created_utc", "reporting_category", "gross", "fee", "net", "currency"]

    def test_needs_utc_column(self):
        src = self.root / "s.csv"
        write(src, ["id", "created", "type", "amount", "fee", "net", "currency"], [["t1", "2026-09-02", "charge", "10", "1", "9", "usd"]])
        with self.assertRaises(StopRun) as e:
            inputs.ingest_stripe_csv(self.month, P, [src], "0", "9", self.man)
        self.assertIn("created_utc", str(e.exception))

    def test_fee_column_required(self):
        src = self.root / "s.csv"
        write(src, ["balance_transaction_id", "created_utc", "reporting_category", "gross", "currency"], [["t1", "2026-09-02", "charge", "10", "usd"]])
        with self.assertRaises(StopRun):
            inputs.ingest_stripe_csv(self.month, P, [src], "0", "10", self.man)

    def test_two_files_deduplicated_and_balances_recorded(self):
        a, b = self.root / "a.csv", self.root / "b.csv"
        write(a, self.HDR, [["t1", "2026-09-02", "charge", "10.00", "1.00", "9.00", "usd"]])
        write(b, self.HDR, [["t1", "2026-09-02", "charge", "10.00", "1.00", "9.00", "usd"],
                            ["po1", "2026-09-05", "payout", "-9.00", "0", "-9.00", "usd"]])
        inputs.ingest_stripe_csv(self.month, P, [a, b], "100", "100", self.man)
        rows = list(csv.DictReader(open(self.inp / "stripe.csv")))
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.man.data["stripe_balance"]["source"], "typed")


class TestPayrollAndBudget(IngestBase):
    def test_payroll_periods(self):
        c = self.cfg()
        c["payroll_format"] = {"date_col": "Check date", "gross_col": "Gross", "employer_tax_col": "ER tax",
                               "period_start_col": "Start", "period_end_col": "End", "date_format": ""}
        self.save_cfg(c)
        from mer import config
        src = self.root / "p.csv"
        write(src, ["Check date", "Gross", "ER tax", "Start", "End", "Employee"],
              [["2026-09-15", "1000", "80", "2026-09-01", "2026-09-15", "Jane Doe"],
               ["2026-09-15", "2000", "160", "2026-09-01", "2026-09-15", "John Roe"],
               ["2026-10-05", "3000", "240", "2026-09-16", "2026-09-30", "Jane Doe"]])
        inputs.ingest_payroll(self.month, P, config.load(self.root), src, self.man)
        txt = (self.inp / "payroll.csv").read_text()
        self.assertNotIn("Jane", txt)
        rows = list(csv.DictReader(open(self.inp / "payroll.csv")))
        self.assertEqual([(r["check_date"], r["gross"]) for r in rows], [("2026-09-15", "3000.00"), ("2026-10-05", "3000.00")])

    def test_wide_budget(self):
        src = self.root / "b.csv"
        write(src, ["Account", "Jul 2026", "Aug 2026", "Sep 2026", "Total"], [["Software", "800", "800", "900", "2500"]])
        inputs.ingest_budget(self.month, P, src, self.man)
        rows = list(csv.DictReader(open(self.inp / "budget.csv")))
        self.assertEqual([(r["month"], r["amount"]) for r in rows], [("2026-07", "800.00"), ("2026-08", "800.00"), ("2026-09", "900.00")])

    def test_long_budget_and_bad_column(self):
        src = self.root / "b.csv"
        write(src, ["account", "month", "amount"], [["Software", "2026-09", "900"]])
        inputs.ingest_budget(self.month, P, src, self.man)
        write(src, ["Account", "Sep 2026", "Notes"], [["Software", "900", "x"]])
        with self.assertRaises(StopRun):
            inputs.ingest_budget(self.month, P, src, self.man)

    def test_attest_rejects_unknown_source(self):
        with self.assertRaises(StopRun):
            inputs.attest(self.man, "anything", "me")


class TestMoneyAndDates(unittest.TestCase):
    def test_money_strict(self):
        self.assertEqual(money("(1,234.56)"), D("-1234.56"))
        self.assertEqual(money("1234.56-"), D("-1234.56"))
        self.assertEqual(money(".00"), D("0"))           # as QuickBooks writes zero
        self.assertEqual(money("-.50"), D("-0.50"))
        for bad in ("NaN", "Infinity", "12 34", "(-5)", "1e5", "", "abc"):
            with self.assertRaises(ValueError, msg=bad):
                money(bad)

    def test_period_edges(self):
        self.assertEqual(Period("2026-12").shift(1).key, "2027-01")
        self.assertEqual(Period("2026-01").prior.key, "2025-12")
        self.assertEqual(Period("2028-02").end, date(2028, 2, 29))
        with self.assertRaises(StopRun):
            Period("0000-01")
        with self.assertRaises(StopRun):
            Period("2026-13")

    def test_month_mentions(self):
        may, sep = Period("2026-05"), Period("2026-09")
        self.assertFalse(Period("2026-06").mentioned_in("late fee may apply"))
        self.assertFalse(may.mentioned_in("late fee may apply"))
        self.assertTrue(may.mentioned_in("Services May 2026"))
        self.assertTrue(sep.mentioned_in("AWS usage - September 2026"))
        self.assertTrue(sep.mentioned_in("Invoice for September 30, 2026"))
        self.assertFalse(sep.mentioned_in("September 2025 services"))


if __name__ == "__main__":
    unittest.main()


class TestWideBudgetDuplicates(IngestBase):
    def test_duplicate_month_columns_stop(self):
        src = self.root / "b.csv"
        write(src, ["Account", "Sep 2026", "2026-09"], [["Software", "900", "800"]])
        with self.assertRaises(StopRun):
            inputs.ingest_budget(self.month, P, src, self.man)


class TestMatchingUnits(unittest.TestCase):
    def setUp(self):
        from mer.checks.matching import match_account
        self.match = match_account

    def b(self, lid, dt, amt, **kw):
        return dict({"line_id": lid, "txn_id": lid, "date": date.fromisoformat(dt), "amount": D(amt), "name": kw.pop("name", "X"),
                     "memo": "", "doc_num": ""}, **kw)

    def s(self, row, dt, amt, desc="X", **kw):
        return dict({"row": row, "date": date.fromisoformat(dt), "amount": D(amt), "description": desc}, **kw)

    def test_prior_item_clears_against_row_before_the_1st(self):
        prior = [self.b("p1", "2026-08-20", "1080", prior=True)]
        m = self.match([], prior, [self.s(9, "2026-08-24", "1080", pre=True)], 5, date(2026, 9, 25))
        self.assertFalse(m.book_left)

    def test_booked_late_either_direction(self):
        m = self.match([self.b("b1", "2026-09-20", "-300")], [], [self.s(2, "2026-09-12", "-300")], 5, date(2026, 9, 25))
        self.assertEqual(len(m.late), 1)
        self.assertFalse(m.book_left or m.bank_left)

    def test_carried_bank_item_matches_entry_booked_into_last_month(self):
        added = [self.b("h1", "2026-09-20", "50000", prior=True)]
        m = self.match([], added, [self.s("prior:2:0", "2026-09-20", "50000", prior=True)], 5, date(2026, 10, 26))
        self.assertFalse(m.book_left or m.bank_left)
