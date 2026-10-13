"""The gate: missing, wrong-dated, wrong-shaped, incomplete or empty input stops the run. Never a guess."""

from __future__ import annotations

import csv
import json
import unittest

from helpers import Base, cli


def rewrite_csv(path, fn):
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
        fields = list(rows[0].keys()) if rows else []
    rows = fn(rows)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


class TestClean(Base):
    def test_clean_fixture_passes(self):
        self.assertEqual(self.gates(), [])


class TestG1Missing(Base):
    def test_s1_missing_statement(self):
        m = self.manifest()
        del m["statements"]["2"]
        self.save_manifest(m)
        self.assertIn("G1", self.gates())

    def test_missing_required_file(self):
        (self.inp / "pnl_monthly.csv").unlink()
        self.assertIn("G1", self.gates())

    def test_file_without_manifest_record(self):
        m = self.manifest()
        del m["files"]["aging.csv"]
        self.save_manifest(m)
        self.assertIn("manifest has no record", self.gate_text())

    def test_qbo_pull_not_finished(self):
        m = self.manifest()
        m["qbo_pull"]["complete"] = False
        self.save_manifest(m)
        self.assertIn("did not finish", self.gate_text())

    def test_s6_roles_not_confirmed(self):
        c = self.cfg()
        c["accounts_confirmed"] = False
        self.save_cfg(c, reconfirm=False)
        self.assertIn("G1", self.gates())

    def test_roles_changed_after_confirmation(self):
        c = self.cfg()
        c["roles"]["prepaid"] = []
        self.save_cfg(c, reconfirm=False)
        self.assertIn("changed after", self.gate_text())

    def test_no_prior_outstanding_info(self):
        m = self.manifest()
        del m["attestations"]["outstanding:1"]
        self.save_manifest(m)
        self.assertIn("uncleared at", self.gate_text())

    def test_unlisted_bank_account_with_activity(self):
        c = self.cfg()
        c["roles"]["cash"] = ["Mercury Checking"]
        self.save_cfg(c)
        self.assertIn("G1", self.gates())

    def test_leftover_statement_for_account_not_in_roles(self):
        m = self.manifest()
        m["statements"]["13"] = dict(m["statements"]["2"], account="Stripe Clearing", file="bank/2.csv")
        self.save_manifest(m)
        self.assertIn("not in roles.cash", self.gate_text())

    def test_stripe_balances_missing(self):
        m = self.manifest()
        del m["stripe_balance"]
        self.save_manifest(m)
        self.assertIn("opening and closing", self.gate_text())


class TestG2Dates(Base):
    def test_s2_wrong_month_trial_balance(self):
        m = self.manifest()
        m["files"]["tb_current.csv"]["as_of"] = "2026-08-31"
        self.save_manifest(m)
        self.assertIn("G2", self.gates())

    def test_aging_content_wrong_date(self):
        rewrite_csv(self.inp / "aging.csv", lambda rows: [dict(r, as_of="2026-06-30") for r in rows])
        self.assertIn("aging is as of 2026-06-30", self.gate_text())

    def test_bills_wrong_dates(self):
        rewrite_csv(self.inp / "bills.csv", lambda rows: [dict(r, txn_date="2026-08-03") for r in rows])
        self.assertIn("bills.csv has", self.gate_text())

    def test_payroll_from_another_month(self):
        m = self.manifest()
        m["files"]["payroll.csv"]["start"] = "2026-08-01"
        self.save_manifest(m)
        self.assertIn("payroll.csv was prepared", self.gate_text())

    def test_gl_line_outside_month(self):
        txt = (self.inp / "gl.csv").read_text().replace("2026-09-05", "2026-10-05", 1)
        (self.inp / "gl.csv").write_text(txt)
        self.assertIn("G2", self.gates())

    def test_card_statement_ending_outside_month(self):
        m = self.manifest()
        m["statements"]["3"]["statement_end"] = "2026-08-22"
        self.save_manifest(m)
        self.assertIn("G2", self.gates())

    def test_statement_gap_from_last_month(self):
        prior = self.root / "2026-08" / "inputs"
        prior.mkdir(parents=True)
        (prior / "manifest.json").write_text(json.dumps({"period": "2026-08", "files": {}, "attestations": {},
                                                         "statements": {"1": {"statement_end": "2026-08-25"}}}))
        self.assertIn("follow on", self.gate_text())


class TestG3Shape(Base):
    def test_bad_value_in_bank_csv(self):
        p = self.inp / "bank" / "1.csv"
        p.write_text(p.read_text().replace("-12000.00", "n/a", 1))
        self.assertIn("G3", self.gates())

    def test_missing_column(self):
        rewrite_csv(self.inp / "gl.csv", lambda rows: [{k: v for k, v in r.items() if k != "account_id"} for r in rows])
        p = self.inp / "gl.csv"
        lines = p.read_text().splitlines()
        lines[0] = lines[0].replace("account_id,", "")
        p.write_text("\n".join(lines))
        self.assertIn("G3", self.gates())

    def test_nan_balance_rejected(self):
        m = self.manifest()
        m["statements"]["1"]["ending_balance"] = "NaN"
        self.save_manifest(m)
        self.assertIn("G3", self.gates())


class TestG4AddsUp(Base):
    def test_s4_truncated_gl(self):
        lines = (self.inp / "gl.csv").read_text().splitlines()
        (self.inp / "gl.csv").write_text("\n".join(lines[: len(lines) // 2]) + "\n")
        self.assertIn("G4", self.gates())

    def test_sign_flip_on_revenue_caught(self):
        def flip(rows):
            for r in rows:
                if r["account_id"] == "20":
                    r["amount"] = str(-float(r["amount"]))
            return rows
        rewrite_csv(self.inp / "gl.csv", flip)
        self.assertIn("G4", self.gates())

    def test_history_month_missing_payroll(self):
        rewrite_csv(self.inp / "gl_history.csv",
                    lambda rows: [r for r in rows if not (r["date"].startswith("2026-08") and r["name"] == "Gusto")])
        self.assertIn("G4", self.gates())

    def test_s16_bank_csv_missing_a_row(self):
        rewrite_csv(self.inp / "bank" / "1.csv", lambda rows: [r for r in rows if r["description"] != "ACME DESIGN"])
        self.assertIn("rows don't add up", self.gate_text())

    def test_stripe_doesnt_roll(self):
        rewrite_csv(self.inp / "stripe.csv", lambda rows: [r for r in rows if r["id"] != "txn_ch03"])
        self.assertIn("Stripe doesn't add up", self.gate_text())

    def test_stripe_export_without_payouts(self):
        rewrite_csv(self.inp / "stripe.csv", lambda rows: [r for r in rows if r["category"] != "payout"])
        self.assertIn("no payout rows", self.gate_text())


class TestG5Empty(Base):
    def test_s5_empty_stripe(self):
        rewrite_csv(self.inp / "stripe.csv", lambda rows: [])
        self.assertIn("G5", self.gates())

    def test_empty_history_month(self):
        rewrite_csv(self.inp / "gl_history.csv", lambda rows: [r for r in rows if not r["date"].startswith("2026-03")])
        self.assertIn("G5", self.gates())

    def test_no_bills_needs_attestation(self):
        rewrite_csv(self.inp / "bills.csv", lambda rows: [])
        self.assertIn("returned no bills", self.gate_text())
        m = self.manifest()
        m["attestations"]["bills"] = {"by": "Test", "at": "x"}
        self.save_manifest(m)
        self.assertNotIn("returned no bills", self.gate_text())


class TestS7NoBudget(Base):
    with_budget = False

    def test_does_not_stop(self):
        self.assertEqual(self.gates(), [])
        results, _, _ = self.review()
        self.assertFalse(results["V1"].data["has_budget"])
        self.assertTrue(any(p["status"] == "not_run" for p in results["V1"].parts))


class TestStopWritesOnlyGateReport(Base):
    def test_stop_retires_old_outputs(self):
        self.assertEqual(cli("run", "--root", str(self.root), "--month", "2026-09")[0], 0)
        m = self.manifest()
        del m["statements"]["2"]
        self.save_manifest(m)
        self.assertEqual(cli("run", "--root", str(self.root), "--month", "2026-09")[0], 2)
        out = self.month / "output"
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["gate-report.md"])
        self.assertTrue((self.month / "output.previous" / "review.md").exists())


class TestConfigValidation(Base):
    def test_string_false_rejected(self):
        c = self.cfg()
        c["uses_stripe"] = "false"
        self.save_cfg(c, reconfirm=False)
        rc, out = cli("run", "--root", str(self.root), "--month", "2026-09")
        self.assertEqual(rc, 2)
        self.assertIn("true or false", out)

    def test_threshold_as_json_number_ok(self):
        c = self.cfg()
        c["thresholds"] = {"range_slack_pct": 25, "materiality_abs": 1000}
        self.save_cfg(c)
        self.assertEqual(cli("run", "--root", str(self.root), "--month", "2026-09")[0], 0)

    def test_bad_threshold_rejected(self):
        c = self.cfg()
        c["thresholds"] = {"materiality_abs": "lots"}
        self.save_cfg(c, reconfirm=False)
        rc, out = cli("run", "--root", str(self.root), "--month", "2026-09")
        self.assertEqual(rc, 2)

    def test_corrupt_manifest_is_a_stop_not_a_crash(self):
        (self.inp / "manifest.json").write_text("{not json")
        self.assertEqual(cli("run", "--root", str(self.root), "--month", "2026-09")[0], 2)


if __name__ == "__main__":
    unittest.main()


class TestReReviewGate(Base):
    def test_stripe_continuity_with_last_month(self):
        prior = self.root / "2026-08" / "inputs"
        prior.mkdir(parents=True)
        (prior / "manifest.json").write_text(json.dumps({"period": "2026-08", "files": {}, "attestations": {}, "statements": {},
                                                         "stripe_balance": {"opening": "0", "closing": "1500.00"}}))
        self.assertIn("doesn't match last month's closing", self.gate_text())

    def test_negative_stripe_balance(self):
        m = self.manifest()
        m["stripe_balance"] = {"opening": "-10.00", "closing": "-10.00", "source": "typed"}
        self.save_manifest(m)
        self.assertIn("negative", self.gate_text())

    def test_payroll_periods_must_reach_month_end(self):
        rewrite_csv(self.inp / "payroll.csv", lambda rows: rows[:1])
        self.assertIn("pay periods end", self.gate_text())

    def test_new_account_needs_its_own_uncleared_info(self):
        out = self.root / "2026-08" / "output"
        out.mkdir(parents=True)
        (out / "outstanding.csv").write_text("account_id,side,date,description,amount,source_ref\n")
        (self.root / "2026-08" / "inputs").mkdir(parents=True)
        (self.root / "2026-08" / "inputs" / "manifest.json").write_text(json.dumps(
            {"period": "2026-08", "files": {}, "attestations": {}, "statements": {"1": {"statement_end": "2026-08-31"}}}))
        m = self.manifest()
        del m["attestations"]["outstanding:2"]
        self.save_manifest(m)
        self.assertIn("Mercury Treasury items were still uncleared", self.gate_text())

    def test_user_edited_files_are_stops(self):
        for bad in ({"thresholds": ["x"]}, {"accrual_behavior": ["x"]}):
            c = self.cfg()
            c.update(bad)
            self.save_cfg(c, reconfirm=False)
            self.assertEqual(cli("run", "--root", str(self.root), "--month", "2026-09")[0], 2, bad)
        m = self.manifest()
        m["statements"] = []
        self.save_manifest(m)
        self.assertEqual(cli("gate", "--root", str(self.root), "--month", "2026-09")[0], 2)

    def test_utf16_csv_is_a_stop(self):
        p = self.inp / "bank" / "1.csv"
        p.write_bytes(p.read_text().encode("utf-16"))
        self.assertEqual(cli("run", "--root", str(self.root), "--month", "2026-09")[0], 2)
