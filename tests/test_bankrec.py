"""bank-reconciliation: Northwind's September close (the six planted causes), the gate, and carry-forward."""

from __future__ import annotations

import csv
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal as D
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins" / "bank-reconciliation" / "skills" / "bank-reconciliation" / "scripts"))

from bankrec.cli import main  # noqa: E402

DATA = REPO / "testdata" / "bank-rec-northwind"


def cli(*args) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main(list(args))
    return rc, out.getvalue() + err.getvalue()


def write(path: Path, header: list[str], rows: list[list]) -> Path:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


class Base(unittest.TestCase):
    month = "2026-09"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ok(self, *args, month=None):
        rc, out = cli(*args, "--root", str(self.root), "--month", month or self.month)
        self.assertEqual(rc, 0, out)
        return out

    def stop(self, *args, month=None):
        rc, out = cli(*args, "--root", str(self.root), "--month", month or self.month)
        self.assertEqual(rc, 2, out)
        return out

    def northwind(self, stripe=True, accounts=("Mercury Checking", "Mercury Treasury", "Brex Card")):
        books = str(DATA / "books-2026-09.csv")
        spec = {"Mercury Checking": ("312684.00", "219774.00", "mercury-checking-2026-09.csv", "312684.00", "155734.00", "bank"),
                "Mercury Treasury": ("2024000", "2028100", "mercury-treasury-2026-09.csv", "2024000", "2078100", "bank"),
                "Brex Card": ("0", "120", "brex-card-2026-09.csv", "0", "120", "card")}
        for name in accounts:
            bo, bc, f, so, sc, kind = spec[name]
            self.ok("add-books", "--account", name, "--file", books, "--opening", bo, "--closing", bc,
                    "--kind", kind, "--source", "test fixture")
            self.ok("add-statement", "--account", name, "--file", str(DATA / f), "--opening", so, "--closing", sc)
        if stripe:
            self.ok("add-stripe", "--file", str(DATA / "stripe-balance-transactions-2026-09.csv"),
                    "--clearing-account", "Stripe Clearing")

    def results(self, month=None):
        return json.loads((self.root / (month or self.month) / "output" / "results.json").read_text())

    def acct(self, r, name):
        return next(a for a in r["accounts"] if a["account"] == name)

    def items(self, r, name):
        return [i for i in r["items"] if i["account"] == name]

    def draft(self, r, did):
        return next(d for d in r["drafts"] if d["id"] == did)


class TestNorthwindWithStripe(Base):
    """The answer key: six causes, every one named right, and each account bridged to the cent."""

    def setUp(self):
        super().setUp()
        self.northwind()
        self.ok("run")
        self.r = self.results()

    def test_checking_causes(self):
        its = {i["cause"]: i for i in self.items(self.r, "Mercury Checking")}
        self.assertEqual(set(its), {"amount-typo", "duplicate", "paid-from-stripe", "missing-deposit", "missing-entry",
                                    "deposit-in-transit"})
        self.assertEqual(D(its["amount-typo"]["effect"]), D("-180"))
        self.assertIn("divides by 9", its["amount-typo"]["why"])
        self.assertEqual(its["deposit-in-transit"]["kind"], "timing")
        self.assertIn("po_04", its["deposit-in-transit"]["why"])
        self.assertIn("po_02", its["missing-deposit"]["why"])
        self.assertEqual(D(its["missing-entry"]["effect"]), D("64500"))

    def test_duplicate_refund_is_not_an_outstanding_check(self):
        its = self.items(self.r, "Mercury Checking")
        refunds = [i for i in its if i["cause"] in ("duplicate", "paid-from-stripe")]
        self.assertEqual(len(refunds), 2)
        self.assertFalse([i for i in its if i["cause"] in ("outstanding-payment", "outstanding-carried")])
        dup = next(i for i in refunds if i["cause"] == "duplicate")
        d = self.draft(self.r, dup["draft"])
        self.assertEqual((d["status"], d["action"], d["record"]["txn_id"]), ("ready", "delete", "248"))
        moved = self.draft(self.r, next(i for i in refunds if i["cause"] == "paid-from-stripe")["draft"])
        self.assertIn("Stripe Clearing", moved["change"])
        self.assertEqual(moved["status"], "confirm first")

    def test_treasury_transfer_booked_as_expense(self):
        its = self.items(self.r, "Mercury Treasury")
        self.assertEqual([i["cause"] for i in its], ["transfer-booked-wrong"])
        d = self.draft(self.r, its[0]["draft"])
        self.assertEqual((d["status"], d["action"], d["record"]["txn_id"]), ("ready", "change-type", "253"))
        self.assertEqual(d["effects"], {"Mercury Treasury": "50000.00"})

    def test_every_account_bridges_to_zero_after_drafts(self):
        for a in self.r["accounts"]:
            self.assertEqual(D(a["left_after_drafts"]), 0, a["account"])
            total = sum(D(i["effect"]) for i in self.items(self.r, a["account"]))
            self.assertEqual(total, D(a["difference"]), a["account"])
        self.assertEqual(self.acct(self.r, "Brex Card")["status"], "reconciled")
        self.assertEqual(D(self.acct(self.r, "Mercury Checking")["books_after_drafts"]), D("167654"))

    def test_report_and_nothing_posted(self):
        md = (self.root / "2026-09" / "output" / "reconciliation.md").read_text()
        self.assertIn("Nothing here has been posted", md)
        self.assertIn("Adjusted bank balance", md)
        self.assertIn("$167,654.00", md)
        rc, out = cli("summary", "--root", str(self.root), "--month", "2026-09")
        self.assertEqual(rc, 0)
        self.assertIn("Mercury Treasury", out)


class TestNorthwindWithoutStripe(Base):
    """Without Stripe the refunds can't be settled. They must be questions, never timing."""

    def test_refunds_need_a_decision(self):
        self.northwind(stripe=False, accounts=("Mercury Checking",))
        self.ok("run")
        r = self.results()
        its = self.items(r, "Mercury Checking")
        refunds = [i for i in its if "Stripe customer" in i["title"]]
        self.assertEqual(len(refunds), 2)
        self.assertTrue(all(i["kind"] == "decide" for i in refunds))
        self.assertTrue(any("Stripe export" in i["why"] for i in refunds))
        a = self.acct(r, "Mercury Checking")
        self.assertEqual(a["status"], "not reconciled: items need a decision")
        self.assertEqual(D(a["left_after_drafts"]), D("-600"))   # one refund nobody has decided on
        md = (self.root / "2026-09" / "output" / "reconciliation.md").read_text()
        self.assertIn("no Stripe export was added", md)

    def test_treasury_alone_has_no_cross_account_evidence(self):
        self.northwind(stripe=False, accounts=("Mercury Treasury",))
        self.ok("run")
        its = self.items(self.results(), "Mercury Treasury")
        self.assertEqual([i["cause"] for i in its], ["missing-entry"])


class TestGate(Base):
    def test_books_that_dont_add_up_stop(self):
        out = self.stop("add-books", "--account", "Mercury Checking", "--file", str(DATA / "books-2026-09.csv"),
                        "--opening", "312684.00", "--closing", "219000.00", "--source", "x")
        self.assertIn("doesn't add up", out)

    def test_card_without_kind_says_so(self):
        out = self.stop("add-books", "--account", "Brex Card", "--file", str(DATA / "books-2026-09.csv"),
                        "--opening", "0", "--closing", "120", "--source", "x")
        self.assertIn("--kind card", out)

    def test_statement_signs_reversed(self):
        self.ok("add-books", "--account", "Brex Card", "--kind", "card", "--file", str(DATA / "books-2026-09.csv"),
                "--opening", "0", "--closing", "120", "--source", "x")
        out = self.stop("add-statement", "--account", "Brex Card", "--file", str(DATA / "brex-card-2026-09.csv"),
                        "--opening", "0", "--closing", "-120")
        self.assertIn("--flip-sign", out)

    def test_source_required_and_unknown_account(self):
        self.stop("add-books", "--account", "Mercury Checking", "--file", str(DATA / "books-2026-09.csv"),
                  "--opening", "1", "--closing", "1", "--source", " ")
        out = self.stop("add-books", "--account", "Chase", "--file", str(DATA / "books-2026-09.csv"),
                        "--opening", "1", "--closing", "1", "--source", "x")
        self.assertIn("Mercury Checking", out)

    def test_rows_outside_period_and_unknown_status(self):
        self.ok("add-books", "--account", "Mercury Treasury", "--file", str(DATA / "books-2026-09.csv"),
                "--opening", "2024000", "--closing", "2028100", "--source", "x")
        f = write(self.tmp / "s.csv", ["Date", "Description", "Amount", "Status"],
                  [["2026-09-20", "TRANSFER", "50000", "Sent"], ["2026-09-25", "INTEREST", "4100", "Sent"],
                   ["2026-10-01", "INTEREST", "5", "Sent"]])
        self.assertIn("outside the statement period", self.stop("add-statement", "--account", "Mercury Treasury",
                      "--file", str(f), "--opening", "2024000", "--closing", "2078100"))
        self.ok("add-statement", "--account", "Mercury Treasury", "--file", str(f), "--opening", "2024000",
                "--closing", "2078100", "--trim")
        f2 = write(self.tmp / "s2.csv", ["Date", "Description", "Amount", "Status"],
                   [["2026-09-20", "TRANSFER", "50000", "Sent"], ["2026-09-25", "INTEREST", "4100", "Wobbly"]])
        self.assertIn("Wobbly", self.stop("add-statement", "--account", "Mercury Treasury", "--file", str(f2),
                                          "--opening", "2024000", "--closing", "2078100"))

    def test_pending_rows_left_out(self):
        self.ok("add-books", "--account", "Mercury Treasury", "--file", str(DATA / "books-2026-09.csv"),
                "--opening", "2024000", "--closing", "2028100", "--source", "x")
        f = write(self.tmp / "s.csv", ["Date", "Description", "Amount", "Status"],
                  [["09/20/2026", "TRANSFER", "50000", "Sent"], ["09/25/2026", "INTEREST", "4100", "Sent"],
                   ["09/30/2026", "INTEREST", "5", "Pending"]])
        out = self.ok("add-statement", "--account", "Mercury Treasury", "--file", str(f), "--opening", "2024000",
                      "--closing", "2078100")
        self.assertIn("1 pending or failed", out)

    def test_opening_gap_needs_last_periods_list(self):
        f = write(self.tmp / "b.csv", ["date", "amount", "name"], [["2026-09-10", "-100", "Fee"]])
        s = write(self.tmp / "s.csv", ["Date", "Description", "Amount"], [["2026-09-10", "FEE", "-100"]])
        self.ok("add-books", "--account", "Ops", "--file", str(f), "--opening", "1000", "--closing", "900",
                "--source", "x")
        self.ok("add-statement", "--account", "Ops", "--file", str(s), "--opening", "1250", "--closing", "1150")
        self.assertIn("add-outstanding", self.stop("run"))
        self.ok("add-outstanding", "--account", "Ops", "--none", "--by", "Pat")
        self.ok("run")
        its = self.items(self.results(), "Ops")
        self.assertEqual([(i["cause"], D(i["effect"])) for i in its], [("opening-difference", D("-250"))])

    def test_outstanding_list_explains_opening_gap(self):
        f = write(self.tmp / "b.csv", ["date", "amount", "name"], [["2026-09-10", "-100", "Fee"]])
        s = write(self.tmp / "s.csv", ["Date", "Description", "Amount"],
                  [["2026-09-03", "CHECK 880", "-250"], ["2026-09-10", "FEE", "-100"]])
        o = write(self.tmp / "o.csv", ["date", "description", "amount", "side", "doc_num", "txn_type"],
                  [["2026-08-28", "Landlord", "-250", "book", "880", "Check"]])
        self.ok("add-books", "--account", "Ops", "--file", str(f), "--opening", "1000", "--closing", "900",
                "--source", "x")
        self.ok("add-statement", "--account", "Ops", "--file", str(s), "--opening", "1250", "--closing", "900")
        self.ok("add-outstanding", "--account", "Ops", "--file", str(o))
        self.ok("run")
        r = self.results()
        self.assertEqual(self.items(r, "Ops"), [])
        self.assertEqual(self.acct(r, "Ops")["status"], "reconciled")


class TestWrongBankAccount(Base):
    def test_payment_booked_to_the_other_account(self):
        b = write(self.tmp / "b.csv", ["txn_id", "txn_type", "date", "account", "name", "amount"],
                  [["1", "Expense", "2026-09-10", "Ops", "AWS", "-500"]])
        self.ok("add-books", "--account", "Ops", "--file", str(b), "--opening", "1000", "--closing", "500", "--source", "x")
        self.ok("add-statement", "--account", "Ops", "--file", str(write(self.tmp / "s1.csv", ["Date", "Description", "Amount"], [])),
                "--opening", "1000", "--closing", "1000", "--no-activity")
        self.ok("add-books", "--account", "Payroll", "--file", str(b), "--opening", "2000", "--closing", "2000",
                "--source", "x", "--no-activity")
        self.ok("add-statement", "--account", "Payroll", "--file",
                str(write(self.tmp / "s2.csv", ["Date", "Description", "Amount"], [["2026-09-11", "AWS", "-500"]])),
                "--opening", "2000", "--closing", "1500")
        self.ok("run")
        r = self.results()
        self.assertEqual([i["cause"] for i in self.items(r, "Ops")], ["wrong-bank-account"])
        self.assertEqual([i["cause"] for i in self.items(r, "Payroll")], ["wrong-bank-account"])
        d = r["drafts"][0]
        self.assertEqual((d["action"], d["effects"]), ("move", {"Ops": "500.00", "Payroll": "-500.00"}))
        for a in r["accounts"]:
            self.assertEqual(D(a["left_after_drafts"]), 0)


class TestCarryForward(Base):
    """October after Northwind's September: in-transit items clear; unposted fixes stay open."""

    def october(self, book_open, extra_books=()):
        b = write(self.tmp / "ob.csv", ["txn_id", "txn_type", "date", "name", "memo", "amount"],
                  [["300", "Check", "2026-10-05", "WeWork", "Rent", "-6000"], *extra_books])
        s = write(self.tmp / "os.csv", ["Date", "Description", "Amount"],
                  [["2026-10-02", "STRIPE TRANSFER", "11920"], ["2026-10-06", "CHECK 1050 WEWORK", "-6000"]])
        close = str(D(book_open) + sum(D(r[-1]) for r in [["", "-6000"], *extra_books]))
        self.ok("add-books", "--account", "Mercury Checking", "--file", str(b), "--opening", book_open,
                "--closing", close, "--source", "x", month="2026-10")
        self.ok("add-statement", "--account", "Mercury Checking", "--file", str(s), "--opening", "155734",
                "--closing", "161654", month="2026-10")
        self.ok("run", month="2026-10")
        return self.results("2026-10")

    def setUp(self):
        super().setUp()
        self.northwind(accounts=("Mercury Checking",))
        self.ok("run")

    def test_nothing_fixed_keeps_the_errors_open(self):
        r = self.october("219774")
        its = self.items(r, "Mercury Checking")
        self.assertTrue(its)
        self.assertTrue(all(i["cause"] == "carried-error" for i in its), [i["cause"] for i in its])
        self.assertFalse([i for i in its if "11,920" in i["title"]])   # cleared on 10/2
        self.assertEqual(D(self.acct(r, "Mercury Checking")["left_after_drafts"]), 0)

    def test_fixes_posted_into_september_close_it(self):
        r = self.october("167654")   # every September draft posted with its September date
        self.assertEqual(self.items(r, "Mercury Checking"), [])
        self.assertIn("drafted fixes are in the books", self.acct(r, "Mercury Checking")["prior_source"])

    def test_missing_payroll_booked_in_october_matches_the_carried_row(self):
        r = self.october("219774", extra_books=[["301", "Check", "2026-10-01", "Gusto", "Payroll", "-64500"]])
        titles = [i["title"] for i in self.items(r, "Mercury Checking")]
        self.assertFalse([t for t in titles if "GUSTO" in t], titles)


class TestCsvSafety(Base):
    def test_formula_text_is_escaped_in_outputs(self):
        b = write(self.tmp / "b.csv", ["date", "amount", "name"], [["2026-09-10", "-100", "=HYPERLINK(\"x\")"]])
        self.ok("add-books", "--account", "Ops", "--file", str(b), "--opening", "1000", "--closing", "900", "--source", "x")
        self.ok("add-statement", "--account", "Ops", "--file",
                str(write(self.tmp / "s.csv", ["Date", "Description", "Amount"], [])), "--opening", "1000",
                "--closing", "1000", "--no-activity")
        self.ok("run")
        text = (self.root / "2026-09" / "output" / "outstanding.csv").read_text()
        self.assertIn("'=HYPERLINK", text)


if __name__ == "__main__":
    unittest.main()
