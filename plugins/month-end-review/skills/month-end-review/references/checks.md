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
  - Payees are compared by their normalized name (see C5), so "Home Depot" and "THE HOME DEPOT #4521" are one
    payee for duplicates, and a vendor renamed from "Notion" to "Notion, Inc." isn't reported missing. A rename
    that adds or drops letters is still reported missing, but the item names the near-identical name used this
    month.
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
- **C5: the same vendor under two names.** It compares the payee names on expense lines, this month and the last 6
  months (payroll, Stripe fees and clearing-account lines are left out). It flags vendor records, at least one of
  them used this month, when:
  - **the names match once normalized.** It drops a card-processor prefix (`SQ *`, `TST*`, `PAYPAL *`…), a store
    or location number and anything after it (`#4521 ATLANTA GA`, `Store 12`), trailing numbers of 3+ digits, a
    web domain (`.com`), `&` and "and", punctuation, a leading "The", company suffixes (Inc, LLC, LLP, Corp, Co,
    Ltd…) and spaces. Or
  - **the names are near-identical:** one is the other with up to 3 letters added or dropped, at least 90% alike
    (`vendor_similarity_pct`), both at least 8 characters once normalized (`vendor_match_min_length`), with the same
    digits, *and* both used for at least one same expense account.

  Severity starts at low, and the amount is what's booked under the less-used name(s), so it reaches medium at
  $5,000. The fix is to merge the records in QuickBooks. The item doesn't mark P&L lines as unreliable.

  Limits:
  - On purpose, it doesn't match these, which are usually different businesses:
    - a swapped letter (Smith / Smyth Plumbing)
    - a different number (Building 1 / Building 2 LLC)
    - near-identical names on different accounts (Pacific Gas / Pacific Glass)
    - short names that differ at all (Stripe / Stripes)
  - It misses renames that add a word (Notion / Notion Labs), abbreviations (AMZN Mktp / Amazon) and typos that
    swap letters.
  - Two businesses can share a normalized name (Delta Inc / Delta LLC, or Cafe 101 / Cafe 202, since trailing
    numbers of 3+ digits are dropped). Acknowledge those.
  - Customers and employees aren't compared.

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
- **K4: transfers between your own accounts booked as income.** Money moving between the accounts in `roles.cash`
  and `roles.card`, but booked this month to an income account (revenue or other income). A deposit or credit to
  income is flagged when either:
  - **the money matches.** The same amount leaves another of your bank accounts (`roles.cash`) within 3 days
    (`transfer_window_days`), as one of:
    - a book line that isn't an ordinary payment (a named payee whose other side is income, an expense, AR or AP)
    - a statement row that isn't in the books
    - a transfer that's already booked, but only when it lands in the same account and the bank shows the money
      once (the transfer, plus a copy of it booked as revenue)
  - **the words say so.** The payee, memo or bank description says transfer, xfer or trf, or "from" or "to"
    savings, checking, treasury, money market or reserve. Or it *is* the company's own name, normalized like a
    vendor name. On a card account, "payment received", "payment - thank you" and "autopay" count too.

  It skips amounts under $100 (`transfer_min_amount`), invoices and credit memos, anything touching
  `processor_clearing`, and anything naming Stripe or another payment processor (T3 and T4 cover those).

  **The mirror case:** money leaving a bank account but booked to an expense is flagged on the same evidence: the
  same amount arriving in another bank or card account (a card payment), or the same words, plus "credit card
  payment" or "card payment". Payroll and Stripe-fee lines are left out. One transfer booked wrong on both sides is
  one item.

  Severity starts at medium and follows the amount. The item leads the T1, K1 or K3 items about the same money. When
  the other account's side is known, a *confirm first* draft moves the amount to it.

  Limits:
  - "Wire transfer", "Zelle transfer" and "transfer fee", "agent", "pricing" or "tax" don't count as transfer
    words, so a customer's wire isn't flagged on its wording. A transfer described only as a wire is found only if
    the money matches.
  - The company's name must be the whole payee or memo, not part of it, since customers write it on payments.
  - Money from an account that isn't in the review (another bank, a personal account) is found only by its
    wording.
  - The other side is missed if it's booked as an ordinary payment to a named payee.
  - With one bank account, deposits into it are judged by their wording only, and the review says so. With one
    bank or card account in all, the matching part is NOT RUN.
- **K5: payroll split between cost of sales and operating expenses.** Payroll means the payroll expense accounts:
  roles `payroll_wages` and `payroll_taxes`, plus any account with a payroll subtype. Its cost-of-sales part is
  those of type Cost of Goods Sold. This month's cost-of-sales share of payroll is compared with:
  - **`payroll_cogs_pct` in review-config.json:** the share (0 to 100) a person says belongs in cost of sales. The
    check flags a share more than 10 percentage points away (`payroll_split_slack_pct`).
  - **Without it, the last 6 months** (at least 3 with payroll). The check flags a share more than 10 points
    outside their range.

  Either way, all of the month's payroll in operating expenses is flagged, however small the expected share, when
  `payroll_cogs_pct` or every past month says part belongs in cost of sales. The amount is the payroll that would
  have to move to reach the expected share (or the edge of the range). Under $1,000 (`range_floor`) isn't flagged.
  Severity starts at medium.

  It's NOT RUN when no payroll was booked to a cost-of-sales account in the last 6 months and `payroll_cogs_pct`
  isn't set. The reason says what to add. `payroll_cogs_pct` is a person's answer and part of what `confirm-roles`
  records, so changing it needs confirming again. Payroll shows as totals only, and the draft is *needs your
  amount*.

  Limits:
  - It sees the total split, not which people belong where.
  - A cost-of-sales wage account must be in `roles.payroll_wages` (or have a payroll subtype) to count.
  - Contractors paid outside payroll, on an expense account such as Contractors, aren't payroll here. Adding that
    account to `roles.payroll_wages` would break T5's tie to the payroll provider.

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
  - The most specific check leads the item. A K4 transfer leads the T1, K1 and K3 items about the same money.
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
