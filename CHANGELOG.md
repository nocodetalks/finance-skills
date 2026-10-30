# Changelog

## Unreleased

Three checks from a real report of AI bookkeeping failures: transfers booked as income, one vendor under two
names, and payroll not split between cost of sales and admin.

- **K4: transfers between your own accounts booked as income.**
  - K3 only flags credits of $25,000 or more from new payers, so a $5,000 transfer from savings coded to revenue
    got through.
  - K4 flags a deposit or credit to revenue or other income when the same amount leaves another of your bank
    accounts within 3 days. That's a book line that isn't an ordinary payment, or a statement row that isn't in
    the books.
  - It also flags one whose payee, memo or bank description reads like a transfer, or is the company's own name.
  - Invoices, credit memos, and Stripe or other processor activity are left to T3 and T4.
  - The mirror case, a transfer or card payment booked to an expense, is flagged on the same evidence. Northwind's
    one-sided $50,000 treasury move is now led by K4, with K1 and T1 under it.
  - New thresholds: `transfer_window_days` (3) and `transfer_min_amount` ($100).
- **C5: the same vendor under two names, and C1 matches names the same way.**
  - `vendor_key` drops card-processor prefixes, store numbers, punctuation, a leading "The" and company suffixes.
  - C1 now finds duplicates across "Home Depot" and "THE HOME DEPOT #4521", and no longer reports a vendor renamed
    to "Notion, Inc." as a missing bill.
  - C5 flags vendor records whose names match once normalized, or are near-identical. Near-identical means letters
    added or dropped, at least 90% alike, the same digits, and the same expense account.
  - It's low or medium severity, and the fix is to merge the records in QuickBooks.
  - New thresholds: `vendor_similarity_pct` (90) and `vendor_match_min_length` (8).
- **K5: payroll split between cost of sales and operating expenses.**
  - It compares the month's cost-of-sales share of payroll with `payroll_cogs_pct`, or else with the last 6
    months. `payroll_cogs_pct` is a new review-config.json key: a share from 0 to 100 that a person fills in and
    confirms with `confirm-roles`.
  - It flags all payroll in operating expenses when part belongs in cost of sales, or a share more than 10 points
    off (`payroll_split_slack_pct`).
  - It's NOT RUN when there's neither an answer nor cost-of-sales payroll in the history, and says what to add.
  - Payroll stays a total.
- **Tests:** 227 (25 new). They include look-alikes that must not be flagged:
  - a customer's wire
  - a customer deposit and an unrelated check for the same amount on the same day
  - a correctly booked transfer next to a deposit of the same amount
  - Smith / Smyth Plumbing, Building 1 / Building 2 LLC, and Pacific Gas / Pacific Glass on different accounts

## month-end-review 0.1.0 (unreleased)

First version, reworked after two independent reviews (2026-10-11) and a re-review of the fixes (2026-10-12).

- **Gate G1–G5.**
  - Checks contents, not just labels.
  - Every transaction must balance, history must match QuickBooks' P&L report, statements and Stripe must roll
    from opening to closing, and each statement must follow on from last month's.
  - A pull that fails partway is detected.
  - A person must confirm the roles, and confirm again after any change.
- **Checks.**
  - T1–T5, C1–C4, K1–K3, V1–V3.
  - Bank matching carries uncleared items between months and handles sweeps, autopay and late clearing.
  - Covers Stripe clearing accounts, Stripe annual plans and MRR (via invoices), and payroll accrual.
  - Handles accruals that reverse each month and accruals that build up.
  - Adds a balance-sheet review and investor metrics.
- **Reporting.**
  - Severity follows the dollar amount, and one cause is reported once.
  - Acknowledgements are recorded with who, why and until when.
  - P&L lines touched by an open item are marked unreliable.
- **Verify.**
  - Exact-unit, sign-aware and precision-aware number checks.
  - Brackets may only hold references.
  - Edits to `results.json` or outside the marked sections are caught.
- **Safety.**
  - Read-only QuickBooks (GET only); Stripe restricted keys only, with test mode refused by default.
  - Private credential files are enforced.
  - No per-person payroll in the outputs.
- **Runs.**
  - Crashes exit with code 3 and leave no review behind.
  - Re-running needs `--force` once commentary is written, and keeps the previous output.
- **Re-review fixes:**
  - grouping no longer chains unrelated items
  - entries booked into the reviewed month after its review are matched next month
  - matching by cost (check numbers, recency) and groups that must share a word
  - typos are "confirm first"
  - C2 skips accruals already booked, and drafts pick the right account
  - C3 skips revenue-recognition entries
  - C4 accepts renamed entries
  - K3 flags unnamed or unusually large inflows
  - Stripe continuity and negative balances stop the run
  - payroll pay periods must reach month-end
  - MRR by billing interval
  - budget totals use budgeted lines only
  - acknowledgements are limited by amount and month
  - verify recomputes from the inputs and checks references and signs
- **Connecting QuickBooks (2026-10-12):**
  - `mer.py connect` uses the person's own Intuit app keys.
  - The keys go in `<work folder>/.env`, which the script creates (mode 600) and opens for pasting. The model never
    reads it.
  - The person signs in once in the browser, then pastes either the address they land on or the code and Realm ID
    shown there. The code is one-time and useless without the secret.
  - The script names the connected company.
  - `--check` confirms the connection.
  - A second company on the same profile needs `--replace`.
  - No Playground and no hand-edited JSON.
  - SKILL rule 8: keys never enter the chat.
- **Live sandbox fixes (2026-10-12):** zero written as ".00"; QuickBooks' zero-amount "Not Specified" lines; parent
  accounts with no id; parent accounts' own amounts on P&L heading rows.
- **Level-4 fixes (2026-10-12),** found by reviewing Northwind's September close in a live sandbox:
  - T2 flags journal entries posted straight to A/R or A/P even when the aging ties. QuickBooks puts them in the
    aging, so the gap never appears.
  - `verify` treats "burn", "used" and "spent" as outflow words. "Net burn of $88,810" no longer fails as a
    decrease, and a loss can still be described as money burned.
- **Stripe key in `.env` (2026-10-12):**
  - `pull-stripe` reads `STRIPE_API_KEY` from the work folder's `.env`, next to the QuickBooks keys. The
    environment variable still works; `stripe.json` is gone.
  - Requests pin Stripe's API version, because newer versions moved the invoice-line fields the parser reads.
  - `MER_STRIPE_API_BASE` lets a test point the pull at a fake Stripe server, on this computer only.
  - Tested end to end against `testdata/northwind/fake_stripe.py`:
    - 32 paged requests
    - the closing balance worked back from today's balance
    - MRR computed
    - annual-plan detection run
    - the same 14 of 14 score
- **Security fixes (2026-10-13):**
  - **CSV formula injection.** Text cells from QuickBooks, Stripe or a bank that start with `=`, `+`, `-` or `@` are
    written with a leading `'`. Excel and Sheets then show them as text instead of running them; plain numbers are
    unchanged. `read_csv` removes the `'` again, so carried-forward files round-trip.
  - **Integrity check.** `verify` no longer trusts a hash file stored next to the outputs, which could be rewritten
    along with them. It rebuilds `results.json`, every field and not just the numbers, and both documents from the
    inputs, then compares. `.integrity.json` is gone.
- **Tests:** 202, on the synthetic Northwind fixture and faked APIs. These have also run against an Intuit sandbox
  company:
  - the QuickBooks pull, gate and review
  - the `connect` sign-in, then a pull
  - level 4, Northwind loaded into the sandbox (`testdata/northwind/`). A real session caught 14 of 14 planted
    mistakes after the fixes, plus the expected miss.
