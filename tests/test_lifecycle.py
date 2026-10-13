"""Re-runs, crashes, acknowledgements and the month-to-month carry of uncleared items."""

from __future__ import annotations

import csv
import json
import unittest
from unittest import mock

from helpers import Base, cli


class TestRerun(Base):
    def run_(self, *extra):
        return cli("run", "--root", str(self.root), "--month", "2026-09", *extra)

    def test_rerun_refuses_to_overwrite_commentary(self):
        self.assertEqual(self.run_()[0], 0)
        p = self.month / "output" / "review.md"
        p.write_text(p.read_text().replace("<!-- commentary:start -->\n_Not written yet._", "<!-- commentary:start -->\nWritten."))
        rc, out = self.run_()
        self.assertEqual(rc, 2)
        self.assertIn("--force", out)
        self.assertEqual(self.run_("--force")[0], 0)
        self.assertIn("Written.", (self.month / "output.previous" / "review.md").read_text())
        self.assertNotIn("Written.", p.read_text())

    def test_no_stale_investor_update_after_rerun(self):
        self.run_()
        upd = self.month / "output" / "investor-update.md"
        upd.write_text("OLD")
        self.run_("--force")
        self.assertNotEqual(upd.read_text(), "OLD")

    def test_crash_leaves_no_review_and_exits_3(self):
        self.assertEqual(self.run_()[0], 0)
        with mock.patch("mer.cli.run_all", side_effect=RuntimeError("boom")):
            rc, out = self.run_("--force")
        self.assertEqual(rc, 3)
        out_dir = self.month / "output"
        self.assertTrue((out_dir / "error-report.md").exists())
        self.assertFalse((out_dir / "review.md").exists())
        self.assertFalse((out_dir / "results.json").exists())

    def test_exit_codes_are_distinct(self):
        self.assertEqual(self.run_()[0], 0)
        self.assertEqual(cli("verify", "--root", str(self.root), "--month", "2026-09")[0], 1)


class TestAcknowledge(Base):
    def test_acknowledged_item_leaves_open_count(self):
        cli("run", "--root", str(self.root), "--month", "2026-09")
        res = json.loads((self.month / "output" / "results.json").read_text())
        notion = next(e for e in res["exceptions"] if "Notion" in e["title"])
        before = res["counts"]["open_items"]
        rc, out = cli("acknowledge", "--root", str(self.root), "--month", "2026-09", "--id", notion["id"],
                      "--by", "Ankur", "--reason", "Cancelled Notion in August")
        self.assertEqual(rc, 0, out)
        cli("run", "--root", str(self.root), "--month", "2026-09", "--force")
        res = json.loads((self.month / "output" / "results.json").read_text())
        self.assertEqual(res["counts"]["open_items"], before - 1)
        self.assertEqual(res["counts"]["acknowledged"], 1)
        self.assertIn("Notion", res["acknowledged"][0]["title"])
        self.assertIn("Acknowledged by Ankur", (self.month / "output" / "review.md").read_text())

    def test_acknowledgement_expires(self):
        cli("run", "--root", str(self.root), "--month", "2026-09")
        res = json.loads((self.month / "output" / "results.json").read_text())
        notion = next(e for e in res["exceptions"] if "Notion" in e["title"])
        cli("acknowledge", "--root", str(self.root), "--month", "2026-09", "--id", notion["id"], "--by", "A", "--reason", "x",
            "--until", "2026-08")
        cli("run", "--root", str(self.root), "--month", "2026-09", "--force")
        res = json.loads((self.month / "output" / "results.json").read_text())
        self.assertEqual(res["counts"]["acknowledged"], 0)


class TestCarryForward(Base):
    def test_outstanding_written_for_next_month(self):
        cli("run", "--root", str(self.root), "--month", "2026-09")
        with open(self.month / "output" / "outstanding.csv") as f:
            rows = list(csv.DictReader(f))
        sides = {(r["account_id"], r["side"], r["amount"]) for r in rows}
        self.assertIn(("1", "book", "11920.00"), sides)       # payout in transit
        self.assertIn(("2", "bank", "50000.00"), sides)       # treasury transfer not in the books
        # next month picks it up automatically
        from helpers import P
        from mer import config
        from mer.data import load
        from mer.util import Period
        oct_dir = self.root / "2026-10" / "inputs"
        oct_dir.mkdir(parents=True)
        import shutil
        shutil.copy(self.inp / "accounts.csv", oct_dir / "accounts.csv")
        d = load(self.root / "2026-10", Period("2026-10"), config.load(self.root))
        src = {a: v["source"] for a, v in d.outstanding_prior.items()}
        self.assertTrue(src.get("1", "").startswith("last month's review"), src)


if __name__ == "__main__":
    unittest.main()


class TestKeepsLastGoodReview(Base):
    def test_two_stops_keep_previous(self):
        self.assertEqual(cli("run", "--root", str(self.root), "--month", "2026-09")[0], 0)
        m = self.manifest()
        del m["statements"]["2"]
        self.save_manifest(m)
        cli("run", "--root", str(self.root), "--month", "2026-09")
        cli("run", "--root", str(self.root), "--month", "2026-09")
        self.assertTrue((self.month / "output.previous" / "review.md").exists())


class TestAcknowledgementRules(Base):
    def _ack(self, title_word, **kw):
        cli("run", "--root", str(self.root), "--month", "2026-09", "--force")
        res = json.loads((self.month / "output" / "results.json").read_text())
        e = next(e for e in res["exceptions"] if title_word in e["title"])
        args = ["acknowledge", "--root", str(self.root), "--month", "2026-09", "--id", e["id"], "--by", "A", "--reason", "r"]
        for k, v in kw.items():
            args += [f"--{k}", v]
        return cli(*args), e

    def test_integrity_items_cannot_be_acknowledged(self):
        (rc, out), _ = self._ack("receivable")
        self.assertEqual(rc, 2)
        self.assertIn("integrity", out)

    def test_until_in_past_rejected(self):
        (rc, _), _ = self._ack("Notion", until="2026-08")
        self.assertEqual(rc, 2)

    def test_amount_growth_reopens(self):
        (rc, _), e = self._ack("Notion")
        self.assertEqual(rc, 0)
        p = self.root / "acknowledged.json"
        acks = json.loads(p.read_text())
        acks[0]["amount"] = "10.00"            # acknowledged when it was much smaller
        p.write_text(json.dumps(acks))
        cli("run", "--root", str(self.root), "--month", "2026-09", "--force")
        res = json.loads((self.month / "output" / "results.json").read_text())
        self.assertEqual(res["counts"]["acknowledged"], 0)

    def test_bad_ack_file_is_a_stop(self):
        (self.root / "acknowledged.json").write_text('["oops"]')
        self.assertEqual(cli("run", "--root", str(self.root), "--month", "2026-09")[0], 2)


class TestSummary(Base):
    def test_summary_matches_results(self):
        cli("run", "--root", str(self.root), "--month", "2026-09")
        rc, out = cli("summary", "--root", str(self.root), "--month", "2026-09")
        self.assertEqual(rc, 0)
        res = json.loads((self.month / "output" / "results.json").read_text())
        locked = [e["id"] for e in res["exceptions"] if not e["can_acknowledge"]]
        open_ = [e["id"] for e in res["exceptions"] if e["can_acknowledge"]]
        for i in locked:
            self.assertRegex(out, rf"- {i} \[[a-z]+\] .*can't be acknowledged")
        for i in open_:
            self.assertNotRegex(out, rf"- {i} \[[a-z]+\] [^\n]*can't be acknowledged")
        self.assertIn("V1 budget, year to date: NOT RUN", out)
        self.assertNotIn("V1 vs budget: NOT RUN", out)
