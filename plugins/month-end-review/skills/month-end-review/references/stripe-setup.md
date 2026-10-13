# Connect Stripe (read-only)

## Option A: restricted key (recommended)

1. **Create the key.** In the Stripe Dashboard, go to **Developers → API keys → Create restricted key**.
2. **Set its access.** Give it **Read** access to **Balance** (this covers balance transactions) and to
   **Invoices**. Set every other resource to **None**.
3. **Paste it into `.env`.** Use the same `.env` file in the work folder that holds the QuickBooks keys
   (`month-end/.env`; `mer.py connect` creates it). Paste the key after `STRIPE_API_KEY=` and save:
   ```
   STRIPE_API_KEY=rk_live_...
   ```
   Don't paste the key into the chat. Only the script reads this file, and it keeps the file private to you
   (mode 600). For scripted setups, the `STRIPE_API_KEY` environment variable also works.
4. **Pull.** Run `mer.py pull-stripe --root month-end --month 2026-09`.

What the pull gets:
- The month's balance transactions.
- The balances. Today's balance is fetched, and everything after month-end is subtracted, which gives the closing
  balance with nothing typed.
- 13 months of invoices with their lines. These are used for annual plans billed in Stripe, Stripe Tax and MRR.

About keys:
- **Restricted keys (`rk_...`) only.** The skill refuses secret keys (`sk_...`). It only ever calls read endpoints.
  A restricted key *could* be given write access, so scope it to Read as above.
- **Test-mode keys (`rk_test_...`)** are refused unless you pass `--allow-test-mode`, because test data isn't your
  books.

Every request pins Stripe's API version (`2024-06-20`), so the data has the same shape whatever version your
account defaults to.

The month is taken in UTC. Transactions created late on the last day in US time land in the next UTC month. That's
fine as long as it's the same every month.

## Option B: CSV export (no invoice data, so annual plans, Stripe Tax and MRR show NOT RUN)

1. Export the month's **balance transactions, including payouts**, with UTC timestamps and the fee and net
   columns. Stripe's "Balance change from activity" report leaves payouts out. Use an export that has every
   transaction type, or add a second `--file` *with the same columns*. Duplicates across files are dropped.
2. From Stripe's **Balance** report, read the starting and ending balance for the month.
3. Run `mer.py ingest-stripe --root month-end --month 2026-09 --file <csv> [--file <payouts csv>]
   --opening-balance <start> --closing-balance <end>`.

The ingest recognises these columns, case-insensitively:
- `balance_transaction_id` or `id`
- `created_utc` or `created (utc)` (UTC only)
- `reporting_category` or `type`
- `gross` or `amount`
- `fee`, `net`, `currency`, and optionally `description`

Any other layout stops with the columns it found. The gate then checks that opening + net activity = closing: if
the export is missing rows (payouts, a few days, anything), the run stops instead of guessing.

v0.1 handles USD only. Other currencies stop the run.
