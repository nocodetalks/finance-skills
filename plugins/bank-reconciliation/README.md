# bank-reconciliation

Why doesn't QuickBooks match the bank? This finds every cause of the gap, bridges each account from book balance
to statement balance to the cent, and drafts the fixes. It never posts anything.

```
/plugin install bank-reconciliation --marketplace nocodetalks/finance-skills
```

Ask *"QuickBooks says Mercury Checking is $219,774 at September 30 and the statement says $155,734. Why?"*, or type
`/bank-reconciliation:bank-reconciliation`.

## What you get

For each month, `bank-rec/<month>/output/` with:
- **`reconciliation.md`:** for each account:
  - every item that makes up the difference, as timing, book error or needs a decision, with its source line
  - the standard layout: statement + in transit − outstanding = adjusted bank; books + drafted fixes = adjusted
    books
  - the draft fixes
  - what wasn't checked
- **`drafts.json`:** each fix with the QuickBooks record it applies to, for a person, or for a connector that asks
  before it writes.
- **`results.json`:** everything above, for tools.
- **`outstanding.csv`:** uncleared items, carried into next month by themselves.

## The causes

| Type | |
|---|---|
| Timing | Deposits in transit (Stripe payouts confirmed against Stripe) · checks and payments not cleared yet |
| Book errors | Statement items missing from the books · Stripe payouts not recorded · amounts typed wrong · entries booked twice · refunds paid from Stripe booked against the bank · transfers between your own accounts booked as expenses · payments booked to the wrong bank account · last month's fixes still not posted |
| Needs a decision | Book payments never on the statement · duplicates with no proof · stale items · opening balances that differ |

Details: [references/causes.md](skills/bank-reconciliation/references/causes.md).

## What it needs

- **The book side:** the account's lines and its balances on two dates, read through any QuickBooks connector (or
  a QuickBooks export). Claude copies them into one file. The script checks they add up before anything runs.
  See [the connector guide](skills/bank-reconciliation/references/books-from-a-connector.md).
- **The statement:** the bank's or card's CSV export, plus its opening and closing balances.
- **Stripe** (optional, but needed to settle refunds and payouts): the balance transactions export.

## How it differs from month-end-review

month-end-review checks the whole month (books, AR/AP, Stripe, payroll, accruals) and pulls QuickBooks with your own
Intuit keys. bank-reconciliation does one job, the bank and card reconciliation, and reads the books through
whatever QuickBooks connector you already have.

## Tests

`tests/test_bankrec.py` runs Northwind's September close ([testdata/bank-rec-northwind](../../testdata/bank-rec-northwind)),
the same company as the month-end-review tests. It has six planted causes across Checking and Treasury. With the
Stripe export, all six are found and every account bridges to $0.00. Without it, the two $600 refunds come back as
*needs a decision*, never as outstanding checks. The tests also cover the gate, wrong bank accounts, and carrying
items into October with and without the fixes posted.
