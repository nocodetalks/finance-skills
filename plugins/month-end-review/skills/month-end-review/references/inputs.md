# Inputs

Everything lands in `<work folder>/<YYYY-MM>/inputs/` in one canonical shape. `inputs/manifest.json` records:
- where each file came from
- which dates it covers
- the balances a person typed
- what a person attested

The gate checks all of it, including the file *contents*.

## From QuickBooks (`pull-qbo`, read-only, all or nothing)

| File | What |
|---|---|
| `accounts.csv` | Chart of accounts (active and inactive) |
| `tb_current.csv`, `tb_prior.csv` | Trial balance at this month-end and last month-end |
| `gl.csv`, `gl_history.csv` | Ledger lines for the month, and for each of the 6 months before (debit-positive) |
| `pnl_monthly.csv`, `pnl_ytd.csv` | P&L by month, which proves the ledger history is complete, and P&L for the fiscal year to date |
| `aging.csv` | AR and AP aging totals at month-end, with the over-90-days bucket |
| `bills.csv`, `open_bills.csv` | Bills dated in the month and the 20 days after (with created dates), and every open bill |
| `ar_credits.csv` | Unapplied customer payments and open credit memos (as of the pull) |
| `budget.csv` | Only when a QuickBooks budget covers the month |

If a pull fails partway, nothing is swapped in and the gate stops ("the pull did not finish").

## Bank and card statements (`ingest-statement`, every account in `roles.cash` and `roles.card`)

- **The CSV:** its column names go in `bank_formats` in the config:
  - `date_col`, `description_col`
  - `amount_col`, or both `debit_col` and `credit_col`
  - `date_format`
  - `flip_sign`
  - `status_col` + `posted_values`, if the export includes pending or failed rows (the ingest stops if it sees a
    status column you haven't configured)
  - optional `match_window_days`, to override the matching window for that account
- **Sign:** after ingest, money *into* a bank account is positive and a card *charge* is negative. Set
  `flip_sign: true` if the export is the other way round.
- **`--ending-balance` and `--statement-end`,** from the statement or portal, typed by a person:
  - A card's balance owed is a positive number.
  - Statements that close mid-month are fine: give that day's balance, and the CSV rows after it roll forward to
    month-end.
- **`--opening-balance`:** needed the first month only. Later months take it from last month's statement.
- **The gate checks** opening + rows = ending (so a missing or extra row stops the run), and that each statement
  starts where last month's ended.

## Last month's uncleared items (first month only)

Matching needs to know what was still uncleared at last month-end:
- **Items in the books not yet on the statement** (side `book`).
- **Items on the statement not yet in the books** (side `bank`).

After the first review, they carry forward from `output/outstanding.csv`, and `output/snapshot.json` catches
entries added to or voided in the reviewed month after its review. For the first month, choose one:
- `ingest-outstanding --account "<name>" --file <csv>`. Columns: `date`, `description`, `amount` (book effect:
  deposit +, payment -) and an optional `side` (`book` or `bank`). Take the items from your last reconciliation report.
- `attest --source "outstanding:<name>" --by "<name>"` if there were none.

## Stripe (if `uses_stripe`)

- **`pull-stripe`** (restricted key) gives:
  - the month's balance transactions
  - the opening and closing balances (closing = today's balance minus everything after month-end)
  - invoices and their lines for the last 13 months (used for annual plans, Stripe Tax and MRR)
- **`ingest-stripe`** (CSV export) takes a balance-transactions export that includes payouts, with UTC timestamps,
  plus `--opening-balance` and `--closing-balance` from Stripe's Balance report.
  - You can pass several `--file`s; duplicates are dropped.
  - There is no invoice data on this route, so annual-plan detection, Stripe Tax and MRR show NOT RUN.
- **The gate checks** opening + net activity = closing.

## Optional

| Input | Without it |
|---|---|
| `ingest-payroll` (set `uses_payroll_file`; add `period_start_col`/`period_end_col` for the accrual check) | T5 shows NOT RUN |
| Budget (QuickBooks, or `ingest-budget`, long or wide) | "vs budget" shows NOT RUN. A budget with no amounts for the month is treated as missing, not $0. Accounts not in the budget show "not in budget" |

## review-config.json

`init` proposes it from the chart of accounts and QuickBooks preferences. A person checks it, then
`confirm-roles --by NAME` records who and when. Any later change to the roles, `accrual_behavior` or
`payroll_cogs_pct` needs confirming again.

- **`basis`:** `"Accrual"` or `"Cash"`.
- **`fiscal_year_start_month`:** a number from 1 to 12.
- **`uses_stripe`, `stripe_billing_via_ar`, `uses_stripe_tax`, `uses_payroll_file`:** `true` or `false`, never
  `"false"` in quotes.
- **`roles`:**
  - `cash`, `card`, `processor_clearing` (a Stripe clearing account set up as a Bank account)
  - `ar`, `ap`, `undeposited_funds`
  - `deferred_revenue`, `prepaid`, `accrued`, `accrued_payroll`
  - `stripe_revenue`, `stripe_fees`, `refunds`, `sales_tax_payable`
  - `payroll_wages`, `payroll_taxes`
  - `loans`, `loan_interest`

  Use account names exactly as in the chart of accounts, including parents (`Payroll Expenses:Wages`). Use `[]`
  for none. Every Bank or Credit Card account with activity must be in `cash`, `card` or `processor_clearing`.
- **`accrual_behavior`:** `"reversing"` (default: last month's balance should be released) or `"rollforward"`
  (builds up on purpose: bonus, audit fees, PTO), per accrued account.
- **`payroll_cogs_pct`:** the share of payroll, from 0 to 100, that belongs in cost of sales (field, delivery or
  support staff), or `0` if none does. It's a person's answer, and K5 compares each month's split with it. Leave
  it `null` if nobody knows: K5 then compares with the last 6 months, and shows NOT RUN if none of that payroll
  was in cost of sales. List the cost-of-sales wage accounts in `roles.payroll_wages`.
- **`bank_formats`, `payroll_format`, `budget_name`:** as above.
- **`thresholds`:** see `scripts/mer/config.py`. Numbers may be written with or without quotes.

## Attestations (a person's word, recorded with their name)

`attest --source <what> --by <name>`. The sources are:
- `stripe`, `payroll` or `bills`: there was no activity this month
- `payroll-coverage`: no one was paid for the rest of the month after the last pay period in the file
- `statement:<account>`: no transactions this month
- `outstanding:<account>`: nothing uncleared at last month-end
- `history:YYYY-MM`: the company had no activity that month

## Canonical columns (for new adapters)

- `gl.csv`: `line_id, txn_id, txn_type, date, account_id, account, name, memo, doc_num, amount` (debit-positive)
- `bank/<account_id>.csv`: `row, date, description, amount` (debit-positive for that account)
- `stripe.csv`: `id, date, category, amount, fee, net, currency, description, source` (category = Stripe reporting category)
- `payroll.csv`: `check_date, gross, employer_taxes, period_start, period_end` (totals per pay run, no people)
- `budget.csv`: `account_id, account, month, amount` (revenue and expenses both positive)
