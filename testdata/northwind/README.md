# Northwind in a QuickBooks sandbox (level-4 test)

This puts the synthetic test company from `tests/fixture_northwind.py` into a real Intuit sandbox. It then scores a
real review of its September 2026 close against the answer key. The company is Northwind SaaS Inc, with 15
planted mistakes.

This is test tooling, not part of the skill. `load_sandbox.py` writes to QuickBooks, so it refuses any connection
that isn't a sandbox.

## Steps

1. **Connect a sandbox for the loader**, separate from the skill's own connection:
   ```bash
   python3 plugins/month-end-review/skills/month-end-review/scripts/mer.py connect --profile sandbox-loader --environment sandbox
   ```
2. **Empty the sandbox.** This deletes every transaction, Intuit's sample data included:
   ```bash
   python3 testdata/northwind/load_sandbox.py wipe
   ```
   Intuit's API can't delete sales-tax payments in US companies, and QuickBooks won't delete them by hand
   either. `load` cancels each one with a journal entry on the same day.
3. **Load Northwind.** This loads 118 transactions from March to October 2026, renames the company, and makes
   the sample company's leftover accounts and inventory inactive:
   ```bash
   python3 testdata/northwind/load_sandbox.py load
   ```
   It ends by comparing QuickBooks' own trial balances at Aug 31 and Sep 30 with the fixture's. It must print
   "Northwind's accounts match the fixture." It lists any sample leftovers separately.

   The sample company's starting inventory can't be deleted or moved through the API. It stays as a small
   "Inventory Starting Value", dated the month Intuit created the sandbox: $567.50 on Sep 16, 2026 in ours. That
   can show up as one small extra finding.
4. **Write the files a finance person would download:**
   ```bash
   python3 testdata/northwind/load_sandbox.py files --out <folder>
   ```
   This writes:
   - Mercury and Brex exports
   - a Stripe balance-transactions export
   - Gusto payroll
   - a budget
   - `statements.md`, with the balances to type in
5. **Run the review as a user would,** in a fresh Claude Code session with the plugin loaded: *"Review the September
   2026 close."*
6. **Optional: Stripe's API path, with no Stripe account.** Start the fake Stripe server, put a made-up key in the
   work folder's `.env`, and pull through it. Then run the review again:
   ```bash
   python3 testdata/northwind/fake_stripe.py --port 18765
   ```
   ```bash
   echo 'STRIPE_API_KEY=rk_live_northwindFakeKey000111' >> <work folder>/.env
   ```
   ```bash
   MER_STRIPE_API_BASE=http://127.0.0.1:18765 python3 plugins/month-end-review/skills/month-end-review/scripts/mer.py pull-stripe --root <work folder> --month 2026-09
   ```
   The skill only accepts a test server on this computer, and labels the pull "TEST SERVER".
7. **Score it:**
   ```bash
   python3 testdata/northwind/score.py <work folder>/2026-09/output/results.json
   ```

## What a live run can't reproduce exactly

- **#13, a bill entered after close.** QuickBooks stamps each bill with the day it was created, which can't be
  backdated. So every September bill in the sandbox looks entered late.
- **#11, a manual journal entry to receivables.** QuickBooks requires a customer on a journal entry to A/R, which
  puts it in the aging, so the aging still ties to the ledger.
- **Stripe** stays on the export path. Stripe test data can't be dated into a past month.
