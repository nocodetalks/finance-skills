"""QuickBooks and Stripe adapters, with the network replaced by fakes. Shapes follow the public API docs;
the first live sandbox run is still the real test."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (sys.path)

from mer import inputs, qbo  # noqa: E402
from mer.util import Period, StopRun  # noqa: E402

P = Period("2026-09")


def cols(*pairs):
    return {"Column": [{"ColTitle": t, "ColType": "Money", "MetaData": [{"Name": "ColKey", "Value": k}]} for t, k in pairs]}


GL_COLS = cols(("Date", "tx_date"), ("Transaction Type", "txn_type"), ("Num", "doc_num"), ("Name", "name"),
               ("Memo", "memo"), ("Split", "split_acc"), ("Amount", "subt_nat_amount"))


def gl_line(dt, amt, txn="501"):
    return {"type": "Data", "ColData": [{"value": dt}, {"value": "Deposit", "id": txn}, {"value": ""}, {"value": "Stripe", "id": "9"},
                                        {"value": "payout"}, {"value": "Sales"}, {"value": amt}]}


GL = {"Header": {"StartPeriod": "2026-09-01", "EndPeriod": "2026-09-30"}, "Columns": GL_COLS, "Rows": {"Row": [
    {"type": "Section", "Header": {"ColData": [{"value": "Checking", "id": "1"}]},
     "Rows": {"Row": [{"type": "Data", "ColData": [{"value": "Beginning Balance"}] + [{"value": ""}] * 6},
                      gl_line("2026-09-07", "100.00")]}},
    {"type": "Section", "Header": {"ColData": [{"value": "Income", "id": None}]}, "Rows": {"Row": [
        {"type": "Section", "Header": {"ColData": [{"value": "Sales", "id": "80"}]}, "Rows": {"Row": [gl_line("2026-09-07", "100.00")]}}]}}]}}
ACCOUNTS = {"1": {"classification": "Asset", "full_name": "Checking"}, "80": {"classification": "Revenue", "full_name": "Sales"}}
TB = {"Header": {"EndPeriod": "2026-09-30"}, "Columns": {"Column": [{"ColTitle": ""}, {"ColTitle": "Debit"}, {"ColTitle": "Credit"}]},
      "Rows": {"Row": [{"type": "Data", "ColData": [{"value": "Checking", "id": "1"}, {"value": "100.00"}, {"value": ""}]},
                       {"type": "Data", "ColData": [{"value": "Sales", "id": "80"}, {"value": ""}, {"value": "100.00"}]},
                       {"type": "Section", "Summary": {"ColData": [{"value": "TOTAL"}, {"value": "100.00"}, {"value": "100.00"}]}}]}}
PNL_M = {"Header": {}, "Columns": {"Column": [{"ColTitle": ""}, {"ColTitle": "Aug 2026", "MetaData": [{"Name": "StartDate", "Value": "2026-08-01"}]},
                                              {"ColTitle": "Sep 2026"}, {"ColTitle": "Total"}]},
         "Rows": {"Row": [{"type": "Data", "ColData": [{"value": "Sales", "id": "80"}, {"value": "50.00"}, {"value": "100.00"}, {"value": "150.00"}]}]}}
AGING = {"Header": {}, "Columns": {"Column": [{"ColTitle": ""}, {"ColTitle": "Current"}, {"ColTitle": "91 and over"}, {"ColTitle": "Total"}]},
         "Rows": {"Row": [{"type": "Section", "Summary": {"ColData": [{"value": "TOTAL"}, {"value": "70.00"}, {"value": "30.00"}, {"value": "100.00"}]}}]}}


class TestQboParsers(unittest.TestCase):
    def test_trial_balance(self):
        rows = qbo.parse_trial_balance(TB)
        self.assertEqual([(r["account_id"], str(r["debit"]), str(r["credit"])) for r in rows], [("1", "100.00", "0"), ("80", "0", "100.00")])

    def test_gl_nested_sections_signs_and_beginning_balance(self):
        rows = qbo.parse_general_ledger(GL, ACCOUNTS)
        self.assertEqual([(r["account_id"], str(r["amount"])) for r in rows], [("1", "100.00"), ("80", "-100.00")])

    def test_gl_system_lines_and_parent_accounts(self):
        acc = dict(ACCOUNTS, **{"37": {"classification": "Asset", "full_name": "Truck"}})
        rep = json.loads(json.dumps(GL))
        rep["Rows"]["Row"] += [
            {"type": "Section", "Header": {"ColData": [{"value": "Not Specified"}]}, "Rows": {"Row": [gl_line("2026-09-30", ".00", "74")]}},
            {"type": "Section", "Header": {"ColData": [{"value": "Truck"}]}, "Rows": {"Row": [gl_line("2026-09-12", "50.00", "77")]}}]
        rows = qbo.parse_general_ledger(rep, acc)
        self.assertEqual([r["account_id"] for r in rows][-1], "37")
        self.assertNotIn("74", [r["txn_id"] for r in rows])
        rep["Rows"]["Row"][-2]["Rows"]["Row"] = [gl_line("2026-09-30", "5.00", "74")]
        with self.assertRaises(StopRun):
            qbo.parse_general_ledger(rep, acc)

    def test_gl_line_without_txn_id_stops(self):
        bad = json.loads(json.dumps(GL))
        bad["Rows"]["Row"][0]["Rows"]["Row"][1]["ColData"][1].pop("id")
        with self.assertRaises(StopRun):
            qbo.parse_general_ledger(bad, ACCOUNTS)

    def test_pnl_monthly_metadata_and_title(self):
        rows = qbo.parse_pnl_monthly(PNL_M)
        self.assertEqual([(r["month"], str(r["amount"])) for r in rows], [("2026-08", "50.00"), ("2026-09", "100.00")])
        self.assertEqual(str(qbo.parse_pnl_total(PNL_M)[0]["amount"]), "150.00")

    def test_parent_account_amounts_on_heading_row(self):
        rep = {"Header": {}, "Columns": {"Column": [{"ColTitle": ""}, {"ColTitle": "Sep 2026", "MetaData": [{"Name": "StartDate", "Value": "2026-09-01"}]},
                                                    {"ColTitle": "Total"}]},
               "Rows": {"Row": [{"type": "Section", "Header": {"ColData": [{"value": "Landscaping", "id": "45"}, {"value": "190.00"}, {"value": "190.00"}]},
                                 "Rows": {"Row": [{"type": "Data", "ColData": [{"value": "Plants", "id": "49"}, {"value": "131.25"}, {"value": "131.25"}]}]},
                                 "Summary": {"ColData": [{"value": "Total Landscaping"}, {"value": "321.25"}, {"value": "321.25"}]}}]}}
        rows = {(r["account_id"], str(r["amount"])) for r in qbo.parse_pnl_monthly(rep)}
        self.assertEqual(rows, {("45", "190.00"), ("49", "131.25")})
        self.assertEqual({(r["account_id"], str(r["amount"])) for r in qbo.parse_pnl_total(rep)}, {("45", "190.00"), ("49", "131.25")})

    def test_aging_with_over_90_and_no_data(self):
        a = qbo.parse_aging(AGING, "ar", "2026-09-30")
        self.assertEqual((str(a["total"]), str(a["over_90"])), ("100.00", "30.00"))
        empty = {"Header": {}, "Columns": {"Column": []}, "Rows": {}, "_no_data": True}
        self.assertEqual(str(qbo.parse_aging(empty, "ap", "2026-09-30")["total"]), "0")

    def test_preferences(self):
        p = qbo.parse_preferences([{"AccountingInfoPrefs": {"FirstMonthOfFiscalYear": "April"}, "ReportPrefs": {"ReportBasis": "Cash"}}])
        self.assertEqual(p, {"fiscal_year_start_month": 4, "basis": "Cash"})

    def test_two_budgets_stop(self):
        b = {"Active": True, "BudgetType": "ProfitAndLoss", "StartDate": "2026-01-01", "EndDate": "2026-12-31", "BudgetDetail": []}
        with self.assertRaises(StopRun):
            qbo.parse_budget([dict(b, Name="A"), dict(b, Name="B")], P, {}, None)
        self.assertIsNone(qbo.parse_budget([dict(b, Name="A")], P, {}, "C"))


class CredsBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.environ["MER_CONFIG_HOME"] = self.home
        qbo.save_creds("default", {"client_id": "c", "client_secret": "s", "refresh_token": "r1", "realm_id": "9",
                                   "environment": "sandbox"})

    def tearDown(self):
        os.environ.pop("MER_CONFIG_HOME", None)
        shutil.rmtree(self.home)


class TestQboClient(CredsBase):
    def test_creds_file_is_private(self):
        p = qbo.cred_path("default")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        os.chmod(p, 0o644)
        with self.assertRaises(StopRun):
            qbo.load_creds("default")

    def test_token_rotation_saved(self):
        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"access_token": "a", "refresh_token": "r2"}).encode()
        with mock.patch("urllib.request.urlopen", return_value=R()):
            c = qbo.Client("default")
            c._refresh()
        self.assertEqual(qbo.load_creds("default")["refresh_token"], "r2")

    def test_query_pagination(self):
        c = qbo.Client("default")
        pages = [{"QueryResponse": {"Account": [{"Id": str(i)} for i in range(1000)]}},
                 {"QueryResponse": {"Account": [{"Id": "x"}] * 5}}]
        with mock.patch.object(c, "get", side_effect=pages) as g:
            out = c.query("SELECT * FROM Account")
        self.assertEqual(len(out), 1005)
        self.assertIn("STARTPOSITION 1001", g.call_args_list[1].args[1]["query"])

    def test_gl_columns_fallback(self):
        c = qbo.Client("default")
        with mock.patch.object(c, "report", side_effect=[qbo.QBOError("bad column", 400), GL]) as r:
            out = c.general_ledger(P.start, P.end, "Accrual")
        self.assertEqual(out, GL)
        self.assertNotIn("columns", r.call_args_list[1].kwargs)

    def test_only_get_requests(self):
        src = (Path(qbo.__file__)).read_text()
        self.assertEqual(src.count('method="POST"'), 1)           # the token refresh only
        self.assertNotIn('method="PUT"', src)
        self.assertNotIn('method="DELETE"', src)


class FakeClient:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on

    def query(self, q):
        if "Preferences" in q:
            return [{"AccountingInfoPrefs": {"FirstMonthOfFiscalYear": "January"}, "ReportPrefs": {"ReportBasis": "Accrual"}}]
        if "FROM Account WHERE Active = true" in q:
            return [{"Id": "1", "Name": "Checking", "FullyQualifiedName": "Checking", "AccountType": "Bank",
                     "AccountSubType": "Checking", "Classification": "Asset", "Active": True},
                    {"Id": "80", "Name": "Sales", "FullyQualifiedName": "Sales", "AccountType": "Income",
                     "AccountSubType": "SalesOfProductIncome", "Classification": "Revenue", "Active": True}]
        return []

    def report(self, name, **kw):
        if name == self.fail_on:
            raise StopRun(f"{name} failed")
        return {"TrialBalance": TB, "ProfitAndLoss": PNL_M, "AgedReceivables": AGING, "AgedPayables": AGING}[name]

    def general_ledger(self, s, e, basis):
        if self.fail_on == "GeneralLedger":
            raise StopRun("GL failed")
        return GL


class TestQboPull(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.month = self.root / "2026-09"
        (self.month / "inputs").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_success(self):
        with mock.patch.object(qbo, "Client", lambda profile: FakeClient()):
            qbo.pull(self.month, P, None, "default", inputs.Manifest(self.month, P))
        man = json.loads((self.month / "inputs" / "manifest.json").read_text())
        self.assertTrue(man["qbo_pull"]["complete"])
        self.assertEqual(man["files"]["gl_history.csv"]["months"], [P.shift(-n).key for n in range(6, 0, -1)])
        self.assertFalse((self.month / "inputs" / ".pull-tmp").exists())

    def test_failure_midway_leaves_nothing_half_done(self):
        with mock.patch.object(qbo, "Client", lambda profile: FakeClient(fail_on="GeneralLedger")):
            with self.assertRaises(StopRun):
                qbo.pull(self.month, P, None, "default", inputs.Manifest(self.month, P))
        man = json.loads((self.month / "inputs" / "manifest.json").read_text())
        self.assertFalse(man["qbo_pull"]["complete"])
        self.assertFalse((self.month / "inputs" / "accounts.csv").exists())

    def test_old_qbo_budget_removed_when_none_now(self):
        inp = self.month / "inputs"
        (inp / "budget.csv").write_text("account_id,account,month,amount\n")
        man = inputs.Manifest(self.month, P)
        man.record("budget.csv", source="quickbooks")
        with mock.patch.object(qbo, "Client", lambda profile: FakeClient()):
            qbo.pull(self.month, P, None, "default", man)
        self.assertFalse((inp / "budget.csv").exists())


def ts(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


class TestStripeApi(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.month = self.root / "2026-09"
        (self.month / "inputs").mkdir(parents=True)
        os.environ["STRIPE_API_KEY"] = "rk_live_abc"

    def tearDown(self):
        os.environ.pop("STRIPE_API_KEY", None)
        shutil.rmtree(self.root)

    def test_keys(self):
        os.environ["STRIPE_API_KEY"] = "sk_live_123"
        with self.assertRaises(StopRun):
            inputs.stripe_key()
        os.environ["STRIPE_API_KEY"] = "rk_test_123"
        with self.assertRaises(StopRun):
            inputs.stripe_key()
        self.assertEqual(inputs.stripe_key(allow_test=True), "rk_test_123")

    def test_key_from_env_file_wins_and_stays_private(self):
        env = self.root / ".env"
        env.write_text("QUICKBOOKS_CLIENT_ID=x\nSTRIPE_API_KEY=rk_live_fromenvfile\n")
        os.chmod(env, 0o644)
        self.assertEqual(inputs.stripe_key(self.root), "rk_live_fromenvfile")
        self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)
        env.write_text("STRIPE_API_KEY=sk_live_secretkey\n")
        with self.assertRaises(StopRun) as cm:
            inputs.stripe_key(self.root)
        self.assertNotIn("sk_live_secretkey", str(cm.exception))
        os.environ.pop("STRIPE_API_KEY")
        env.write_text("STRIPE_API_KEY=\n")
        with self.assertRaises(StopRun) as cm:
            inputs.stripe_key(self.root)
        self.assertIn(".env", str(cm.exception))

    def test_test_server_must_be_on_this_computer(self):
        for bad in ("https://evil.example.com", "http://10.0.0.5:8000", "http://127.0.0.1.evil.com"):
            with mock.patch.dict(os.environ, {"MER_STRIPE_API_BASE": bad}):
                with self.assertRaises(StopRun):
                    inputs.stripe_api()
        with mock.patch.dict(os.environ, {"MER_STRIPE_API_BASE": "http://127.0.0.1:8765"}):
            self.assertEqual(inputs.stripe_api(), "http://127.0.0.1:8765/v1")
        self.assertEqual(inputs.stripe_api(), inputs.STRIPE_API)

    def test_requests_pin_the_api_version(self):
        seen = {}

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"data": [], "has_more": false}'

        def fake(req, timeout=None):
            seen.update(req.headers)
            return R()
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            inputs._stripe_get("rk_live_x", "balance", {})
        self.assertEqual(seen.get("Stripe-version"), inputs.STRIPE_VERSION)

    def test_pull_balances_and_invoices(self):
        month_txn = [{"id": "txn_1", "created": ts("2026-09-02T10:00:00"), "amount": 1000, "fee": 59, "net": 941,
                      "currency": "usd", "reporting_category": "charge"}]
        after_txn = [{"id": "txn_2", "created": ts("2026-10-02T10:00:00"), "amount": 500, "fee": 0, "net": 500,
                      "currency": "usd", "reporting_category": "charge"}]
        invoices = [{"id": "in_1", "status": "paid", "currency": "usd", "customer": "cus_1", "total": 120000,
                     "total_excluding_tax": 120000, "status_transitions": {"finalized_at": ts("2026-09-02T00:00:00"),
                                                                           "paid_at": ts("2026-09-02T00:00:00")},
                     "lines": {"has_more": False, "data": [{"id": "il_1", "amount": 120000, "type": "subscription",
                                                            "period": {"start": ts("2026-09-02T00:00:00"), "end": ts("2027-09-02T00:00:00")}}]}}]

        def lst(key, path, params):
            if path == "invoices":
                return invoices
            return month_txn if "created[lt]" in params else after_txn

        with mock.patch.object(inputs, "_stripe_list", side_effect=lst), \
                mock.patch.object(inputs, "_stripe_get", return_value={"available": [{"amount": 300000, "currency": "usd"}],
                                                                       "pending": [{"amount": 0, "currency": "usd"}]}):
            inputs.pull_stripe(self.month, P, inputs.Manifest(self.month, P))
        man = json.loads((self.month / "inputs" / "manifest.json").read_text())
        self.assertEqual(man["stripe_balance"]["closing"], "2995.00")
        self.assertEqual(man["stripe_balance"]["opening"], "2985.59")
        lines = (self.month / "inputs" / "stripe_invoice_lines.csv").read_text()
        self.assertIn("2027-09-02", lines)


if __name__ == "__main__":
    unittest.main()
