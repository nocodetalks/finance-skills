# month-end-review

A month-end review for US startups on QuickBooks Online + Stripe. It checks the month is right first, then
explains it.

```
/plugin install month-end-review --marketplace nocodetalks/finance-skills
```

Ask *"Review September's close"*, or type `/month-end-review:month-end-review`. The plain `/month-end-review`
also works unless another command uses that name.

## What you get

For each month, a folder with:
- **`review.md`:**
  - the verdict line
  - each item that needs you, ranked by dollar impact, with source rows, the fix and "also seen by"
  - acknowledged items
  - tie-outs
  - P&L (month, budget, year to date)
  - balance sheet, with what nothing reconciles
  - metrics
  - commentary
  - questions for the owner
  - draft entries, with net income after the ready ones
  - coverage
- **`investor-update.md`:** a numbers table written by the script (cash, burn, runway, revenue, MRR, margin), plus
  notes in Claude's words, number-checked.
- **`results.json` and CSVs:** exceptions, tie-outs, P&L flux, balance-sheet flux, draft entries, and uncleared
  items carried to next month.

## The checks

| Group | |
|---|---|
| Gate | Everything required is here, for the right dates, in the right shape, adds up (trial balance, every transaction, history vs P&L, statements, Stripe), and isn't empty. Otherwise **stop** |
| Tie-outs | Cash and cards vs statements · AR/AP aging and receivables hygiene · Stripe payouts and the clearing account · Stripe fees, refunds, sales and tax vs books · payroll, accruals included |
| Completeness | Duplicates, and bills paid twice · missing recurring bills · cutoff and accruals · prepaids, deferred revenue, annual plans · missing monthly entries, loan interest |
| Classification | Wrong account, suspense, impossible signs · unusual vs history · financing booked as revenue |
| Variance | P&L flux · balance-sheet flux · cash, burn, runway, MRR · commentary, verified |

Details: [references/checks.md](skills/month-end-review/references/checks.md).

## What it needs

- **QuickBooks:** your own Intuit developer app's Client ID and Client Secret, pasted into `month-end/.env` (never
  the chat), then one sign-in in the browser. Real companies need Intuit's production keys, which Intuit issues
  after a short questionnaire ([setup](skills/month-end-review/references/quickbooks-setup.md)).
- **Stripe:** a restricted read-only key, or CSV exports plus the month's balances
  ([setup](skills/month-end-review/references/stripe-setup.md)).
- **Banks and cards:** a CSV for each, plus the statement balance. The first month also needs the opening balance
  and last month's uncleared items.
- **Optional:** a payroll export (with pay-period dates, for the accrual check) and a budget.

Full list: [references/inputs.md](skills/month-end-review/references/inputs.md).

## What it doesn't do

- **Not covered:**
  - categorizing bank feeds
  - tax filings and 1099s
  - multi-entity books and intercompany
  - foreign currency
  - matching Stripe charge by charge (that's `stripe-to-books`)
- **Judging whether a card charge was personal.** That stays with a person, and the review says so.
- **Posting anything.** It never posts.
