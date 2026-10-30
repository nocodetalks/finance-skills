"""verify: no number in the written words that the checks didn't compute; nothing edited outside the markers."""

from __future__ import annotations

import json
import unittest

from helpers import Base, cli

from mer.verify import Facts, check_text, known_refs  # noqa: E402


class VerifyBase(Base):
    def setUp(self):
        super().setUp()
        rc, out = cli("run", "--root", str(self.root), "--month", "2026-09")
        self.assertEqual(rc, 0, out)
        self.out = self.month / "output"
        self.results = json.loads((self.out / "results.json").read_text())
        self.facts = Facts(self.results["facts"])
        self.refs = known_refs(self.results)

    def write(self, commentary="Fine.", questions="1. Anything else? [E1]", notes="Fine month."):
        r = (self.out / "review.md").read_text()
        r = r.replace("<!-- commentary:start -->\n_Not written yet._", "<!-- commentary:start -->\n" + commentary)
        r = r.replace("<!-- questions:start -->\n_Not written yet._", "<!-- questions:start -->\n" + questions)
        (self.out / "review.md").write_text(r)
        u = (self.out / "investor-update.md").read_text()
        (self.out / "investor-update.md").write_text(u.replace("<!-- notes:start -->\n_Not written yet._", "<!-- notes:start -->\n" + notes))

    def verify(self):
        return cli("verify", "--root", str(self.root), "--month", "2026-09")

    def bad(self, text, durations=False):
        return check_text(text, self.facts, self.refs, allow_durations=durations)


class TestVerifyWorkflow(VerifyBase):
    def test_unwritten_fails(self):
        self.assertEqual(self.verify()[0], 1)

    def test_good_commentary_passes(self):
        self.write("Revenue was $83,940.00, up $31,410 (59.8%) on August [E1]. Net income was -$52,460. "
                   "Stripe kept $1,480 in fees [E10]. Revenue was $83.9k.",
                   "1. Was the $2,400.00 Acme Design bill entered twice? [E9]\n2. Was HubSpot paid for 12 months? [E3]",
                   "Revenue $83,940; net loss of $52,460; runway 24.6 months.")
        rc, out = self.verify()
        self.assertEqual(rc, 0, out)

    def test_results_json_tamper(self):
        self.write()
        p = self.out / "results.json"
        res = json.loads(p.read_text())
        res["facts"].append({"label": "ARR", "value": "4200000", "unit": "usd"})
        p.write_text(json.dumps(res))
        rc, out = self.verify()
        self.assertEqual(rc, 1)
        self.assertIn("doesn't match what the inputs produce", out)

    def test_results_json_wording_tamper(self):
        # Not just the numbers: an edited title or status would reach the chat through `mer.py summary`.
        self.write()
        p = self.out / "results.json"
        res = json.loads(p.read_text())
        res["exceptions"][0]["title"] = "Nothing to see here"
        res["exceptions"][0]["severity"] = "low"
        p.write_text(json.dumps(res))
        rc, out = self.verify()
        self.assertEqual(rc, 1)
        self.assertIn("doesn't match what the inputs produce", out)

    def test_no_stored_hash_to_rewrite(self):
        # verify rebuilds the documents from the inputs; there is no hash file to rewrite after an edit.
        self.write()
        self.assertFalse((self.out / ".integrity.json").exists())
        p = self.out / "review.md"
        p.write_text(p.read_text().replace("| Mercury Treasury |", "| Mercury Treasury (fine) |", 1))
        rc, out = self.verify()
        self.assertEqual(rc, 1)
        self.assertIn("outside the marked sections", out)

    def test_edit_outside_markers(self):
        self.write()
        p = self.out / "review.md"
        p.write_text(p.read_text().replace("| Mercury Treasury |", "| Mercury Treasury (fine) |", 1))
        rc, out = self.verify()
        self.assertEqual(rc, 1)
        self.assertIn("outside the marked sections", out)

    def test_empty_investor_notes(self):
        self.write(notes="   ")
        rc, out = self.verify()
        self.assertEqual(rc, 1)
        self.assertIn("notes section has not been written", out)


class TestVerifyRejects(VerifyBase):
    def test_number_hidden_in_brackets(self):
        self.assertTrue(self.bad("Revenue was [$999,999]."))

    def test_ref_brackets_are_ignored(self):
        e = self.results["exceptions"][0]
        self.assertEqual(self.bad(f"See [{e['id']}, {e['refs'][0]}]."), [])

    def test_unknown_or_stuffed_refs_rejected(self):
        self.assertTrue(self.bad("See [E99]."))
        self.assertTrue(self.bad("See [V1 ARR now $9.4 million]."))
        self.assertTrue(self.bad("See [TB cash 7777777]."))

    def test_date_fragments_and_ids_not_allowed_as_money(self):
        self.assertTrue(self.bad("Fees were $30."))
        self.assertTrue(self.bad("Fees were $2,026."))
        self.assertTrue(self.bad("Fees were $2026."))
        self.assertEqual(self.bad("Since 2026 the company has grown."), [])
        self.assertTrue(self.bad("Fees were $1,095."))

    def test_loss_written_as_profit(self):
        self.assertTrue(self.bad("Net income was $52,460, a profit."))
        self.assertTrue(self.bad("Net income was $52,460."))
        self.assertTrue(self.bad("Revenue fell $31,410."))
        self.assertEqual(self.bad("Revenue rose $31,410; net loss of $52,460."), [])
        self.assertEqual(self.bad("Net income was -$52,460."), [])
        self.assertEqual(self.bad("The month closed with a loss of $52,460."), [])

    def test_burn_is_an_outflow_not_a_loss(self):
        # Found in the live sandbox run: "net burn" (a positive figure) was rejected as a decrease.
        self.assertEqual(self.bad("Net burn was $88,810 this month."), [])
        self.assertEqual(self.bad("The company used $88,810 of cash."), [])
        self.assertEqual(self.bad("The 3-month average net burn is $90,900."), [])
        self.assertEqual(self.bad("The company burned $52,460 in September."), [])      # a loss, said with an outflow word
        self.assertTrue(self.bad("Net burn fell to $88,810."))                           # still a decrease word
        self.assertTrue(self.bad("Net income was $52,460."))

    def test_wrong_percent(self):
        self.assertTrue(self.bad("Revenue grew 18%."))
        self.assertEqual(self.bad("Revenue grew 59.8%."), [])

    def test_small_numbers_must_be_facts(self):
        self.assertTrue(self.bad("Runway is 30 months."))
        self.assertEqual(self.bad("Runway is 24.6 months."), [])
        self.assertTrue(self.bad("The team is 14 people."))
        self.assertTrue(self.bad("MRR grew 12 percent."))
        self.assertTrue(self.bad("We have 30 months of cash."))
        self.assertTrue(self.bad("We processed 87 charges."))

    def test_units_and_words(self):
        self.assertTrue(self.bad("ARR reached $9.4 million."))
        self.assertTrue(self.bad("ARR reached $3 million."))
        self.assertTrue(self.bad("Revenue doubled."))
        self.assertTrue(self.bad("Spend was five thousand dollars."))

    def test_ordinary_terms_pass(self):
        self.assertEqual(self.bad("Check the 401(k) match, Form 1099-NEC, ASC 606 and Section 174 in Q3 2026, net 30."), [])

    def test_plan_terms_and_counts_pass(self):
        self.assertEqual(self.bad("Was it a 12 months plan? There are 3 items to look at."), [])
        self.assertEqual(self.bad("Was HubSpot for 12 months?", durations=True), [])

    def test_ordinary_ranges_pass(self):
        self.assertEqual(self.bad("Pay for Sep 16-30 and the 3-month average; invoices 30-60 days old; top 3 customers."), [])

    def test_dates_pass(self):
        self.assertEqual(self.bad("Paid on 2026-09-30, Sep 30, September 2026 and 09/30/2026."), [])


if __name__ == "__main__":
    unittest.main()
