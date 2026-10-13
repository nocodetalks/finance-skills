# The gate and the checks

Every check prints its numbers. A check, or part of one, that lacks its data reports **NOT RUN** with the reason;
nothing is estimated. The thresholds below are the defaults.

## Gate (stops the run)

**G1: everything required is here.** It stops when any of these is missing:
- a required file, or its manifest record
- a QuickBooks pull that didn't finish
- confirmation of the roles (or the roles changed since they were confirmed)
- a statement for a cash or card account, or a statement for an account in no role
- last month's uncleared items
- a Bank or Credit Card account with activity that is in no role
- the Stripe balances

**G2: right dates.** It checks the labels *and* the contents:
- trial balances and aging are at the right date
- ledger and history lines are inside their months
- the P&L year to date runs from the fiscal year start
- bills, payroll and Stripe rows are inside their windows
- each statement ends inside the month and starts where last month's ended
- the basis matches the pull

**G3: right shape.** Missing columns, or values that aren't numbers or dates (`NaN` and `Infinity` are refused).

**G4: everything adds up.** It stops on any of these:
- the trial balance doesn't balance
- a balance-sheet account's trial-balance movement ≠ its ledger lines
- a transaction doesn't balance (a missing line or a misread sign)
- any account-month in the history and this month differs from QuickBooks' own P&L report
- a statement's opening + rows ≠ its ending balance
- Stripe's opening + net activity ≠ its closing balance

**G5: empty is missing.** An empty ledger, history month, Stripe, statement, payroll or bills file stops the run,
unless a person attests to it.

On a stop, `output/` holds only `gate-report.md`. The previous review moves to `output.previous/`.

## Tie-outs

**T1: cash and cards.** For each account, the book balance is compared with the month-end statement balance.
Matching runs in this order:
1. **Exact amount at the lowest cost,** chosen globally:
   - a matching check number costs nothing
   - otherwise the cost is the days apart, with lines that clear late (6 to 31 days) costing more
   - last month's uncleared items cost their age, so a recent line beats a very old one with the same amount
   - last month's items can clear against statement rows dated before the 1st (mid-month statements)
2. **Groups,** only when the descriptions share a word. Several statement rows can make up one book line
   (sweeps). Several book lines can make up one statement row (autopay).
3. **Possible typos.** A book line and a statement row on the same date (±2 days), not in transit, with digits
   swapped or the sign reversed. These are **medium**, with a *confirm first* draft. They are never treated as
   fact.

What it flags:
- Book lines still uncleared after the window, with neutral wording: "check it really went through this account".
- Statement rows not in the books: "find where it was booked before adding it".
- Uncleared items over 60 days old, as stale.
- Whatever no item explains, as unexplained.

Unmatched lines on both sides go to `outstanding.csv`, and `snapshot.json` records the month's ledger lines.
Next month uses both:
- An entry booked into this month *after* its review is matched against the carried items.
- A voided carried item is dropped.

You can set the window per account with `bank_formats[account].match_window_days`.

**T2: AR and AP.** The control account vs the aging total; on a gap it lists manual journal entries posted to the
control account. It also flags:
- journal entries posted straight to A/R or A/P when the aging ties. QuickBooks makes these carry a customer or
  vendor, which puts them in the aging, but no invoice or bill backs them. Reclasses that net to zero are skipped
- unapplied payments and credit memos ≥ $1,000
- AR over 90 days ≥ $1,000
- a non-zero Undeposited Funds balance

**T3: Stripe payouts and the clearing account.**
- Every payout must land on a statement within 5 days (or be in transit) and be in the books.
- A Stripe deposit on a statement with no matching payout in the Stripe data is flagged; the data may be
  incomplete.
- The tie-out compares payouts found in the books with Stripe's payouts.
- A `processor_clearing` account is tied to Stripe's closing balance.

On the Stripe side, the gate also stops on a negative balance, and on an opening balance that doesn't match last
month's closing balance.

**T4: Stripe vs the books.**
- Fees: any difference, and "booked at net" when the books show less than half of Stripe's fees.
- Refunds booked more often than Stripe refunded them.
- Sales vs revenue plus new deferrals:
  - **Expected sales:** Stripe gross − refunds − Stripe Tax, or invoices finalized in the month when
    `stripe_billing_via_ar` is set.
  - **Reported:** only what the other items don't already explain.
- Sales tax booked vs Stripe Tax collected.

**T5: payroll.** With pay-period dates, the books are compared with what was earned in the month (by calendar
days, within 2% or $1,000), and it names pay runs with no entry and the share earned but paid after month-end.
The gate stops if the file's pay periods don't reach month-end, unless a person attests `payroll-coverage`.
Without pay-period dates, it compares check-date totals plus the change in accrued payroll.

## Completeness

- **C1:**
  - Duplicates: same payee and same expense total ≥ $100 within 5 days, including across month-end.
  - A bill dated by month-end, still open in AP, and paid afterwards as an expense or check (within 60 days).
  - Missing recurring bills: a payee billed in 4 of the last 6 months, with nothing this month and no
    next-month bill naming this month.
  - Payroll, Stripe fees and clearing-account transactions are left to their own checks.
- **C2:**
  - Bills dated in the month but entered after it.
  - Bills dated in the next 20 days that name this month (a bare "May" doesn't count), unless an accrual for that
    vendor or amount is already booked in the month. Drafts credit the one *reversing* accrued account, or ask you
    to choose.
  - Reversing accruals whose balance last month wasn't released, or was released twice.
  - `rollforward` accounts are skipped, and the skip is listed.
- **C3:**
  - Expense lines ≥ $3,000 with a payee that say "annual" or come from a new payee (journal entries are excluded).
  - Prepaid and deferred-revenue movement below half its usual monthly amount, or none at all.
  - Revenue lines ≥ $3,000 that say "annual", not journal entries and not releases from deferred revenue.
  - With Stripe invoice data: plans billed this month for service beyond month-end, whose unearned share (by days)
    is bigger than the new deferred revenue booked.
- **C4:**
  - A journal entry (same memo, ignoring dates) seen in 4 of the last 6 months but missing this month, such as
    depreciation, amortization or stock comp. It's reported once per entry. A same-account entry within 10% of
    the usual amount counts as booked, even if the memo changed.
  - Loans outstanding with no interest booked.

## Classification

- **K1:**
  - Non-zero suspense balances.
  - Impossible signs.
  - A payee booked to a different account than the one it uses in ≥ 70% of at least 3 earlier lines.
- **K2:** P&L accounts more than 25% (and ≥ $1,000) outside their own range over the last 6 months. Accounts
  another check already flagged are dropped.
- **K3:** possible financing (a SAFE, equity or a loan) booked as revenue. It flags credits ≥ $25,000 to revenue
  (not invoices) from a payer with no revenue history (an unnamed payer counts as new), or more than twice
  anything that account has received before.

## Variance and metrics

- **V1, P&L:**
  - Every account vs last month, vs budget (if the budget has the month), and year to date (with a YTD budget if
    the budget covers every month).
  - Accounts not in the budget are shown as "not in budget", and totals vs budget compare budgeted lines only.
  - Revenue excludes other income. Gross margin needs cost-of-goods-sold accounts.
  - **Material:** ≥ $5,000, or ≥ $1,000 and ≥ 10%.
  - Payroll lines are shown as one payroll total, never by person.
  - The bridge must add up.
- **V2, balance sheet:**
  - Every account's prior and current balance, change, and which check reconciles it.
  - It lists the balances nothing reconciles (equity, SAFEs, loans, fixed assets, payroll liabilities…).
  - A move ≥ $5,000 in one of those is flagged.
- **V3, metrics:**
  - **Cash:** bank statements.
  - **Net burn,** this month and the 3-month average, both from the books: −(change in the cash accounts −
    financing).
  - **Runway:** cash ÷ average burn.
  - **Revenue, growth, gross margin, net income.**
  - **MRR:** from Stripe subscription invoice lines covering month-end, after discounts, divided by their billing
    interval (annual ÷ 12). API route only, and NOT RUN if a line has no interval.

## After the checks

- **Severity follows the amount.** An item under $1,000 drops one level; one over $5,000 rises one level. The
  integrity items (unexplained differences, clearing mismatch, aging gaps) never drop.
- **One cause is reported once.**
  - Items merge only when they share a transaction, the same account + date + amount, or an explicit link (for
    example, an unbooked payout and the clearing-account gap).
  - A bank line that isn't in the books then attaches to *one* other item with the same date + amount, if there
    is exactly one. Nothing chains further.
- **Acknowledged items** (`mer.py acknowledge`, saved in `acknowledged.json`) are listed separately and don't count
  as open. An acknowledgement:
  - covers the same item from the month it was given to its end month
  - lapses if the amount grows beyond 10% (or $100)
  - can't be given for an integrity item
- **P&L lines touched by an open item** are marked "unreliable, see E#".

## Draft entries

Drafts are balanced journal entries dated the month-end.
- **ready:** the data fixes the amount and the accounts: a fee gross-up, an accrual for an existing bill, or a
  reclass. The review shows net income as it would be after the ready drafts.
- **confirm first:** the amounts are shown, but a person must check them (a possible typo, or a role with more
  than one account). These aren't counted.
- **needs your amount:** prepaids, deferrals, accruals with no bill, payroll accrual. The basis is shown, but no
  amount.

Nothing is posted. The QuickBooks import format comes after the first live sandbox test.
