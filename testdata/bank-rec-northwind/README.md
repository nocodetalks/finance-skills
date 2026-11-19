# Northwind, September 2026: bank reconciliation

The same synthetic company as `testdata/northwind`, cut down to what a bank reconciliation needs. Every number is
made up.

| File | What it is |
|---|---|
| `books-2026-09.csv` | QuickBooks lines for Mercury Checking, Mercury Treasury and Brex Card, as a connector returns them |
| `mercury-checking-2026-09.csv`, `mercury-treasury-2026-09.csv`, `brex-card-2026-09.csv` | statement exports |
| `stripe-balance-transactions-2026-09.csv` | Stripe balance transactions |

| Account | Books Aug 31 | Books Sep 30 | Statement opening | Statement closing |
|---|---|---|---|---|
| Mercury Checking | 312,684.00 | 219,774.00 | 312,684.00 | 155,734.00 |
| Mercury Treasury | 2,024,000.00 | 2,028,100.00 | 2,024,000.00 | 2,078,100.00 |
| Brex Card (owed) | 0.00 | 120.00 | 0.00 | 120.00 |

## Answer key

| # | Cause | Treatment |
|---|---|---|
| 1 | Stripe payout $11,920 booked 9/30 (po_04, created 9/30) | deposit in transit |
| 2 | Stripe payout $11,000 on the bank 9/17 (po_02), not in the books | book error: record it |
| 3 | Gusto payroll $64,500 on the bank 9/30, not in the books | book error: record it |
| 4 | Gamma LLC deposit $1,530 entered as $1,350 | book error: transposed digits, +$180 |
| 5 | $600 Stripe refund booked twice (#1039, #1040); Stripe refunded once | book error: delete #1040; move #1039 off Checking |
| 6 | $50,000 transfer to Treasury booked as an Expense | book error: make it a transfer |

Brex Card reconciles with nothing to adjust.
