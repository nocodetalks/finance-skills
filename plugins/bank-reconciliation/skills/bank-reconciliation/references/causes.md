# How each cause is found, and its limits

The difference is **book balance − statement balance**. Each item's *effect* is its share of that difference:
- a book line that isn't on the statement adds its amount
- a statement row that isn't in the books subtracts its amount
- a typo pair adds book − statement
- the opening gap that last period's uncleared items don't explain is its own item

The effects always add up to the difference. The run checks this and exits 3 if they don't.

Defaults can be changed in `<work folder>/bankrec-config.json`, as whole numbers of days:
- `window_days`: 5
- `outstanding_check_days`: 90
- `duplicate_days`: 7
- `transfer_days`: 3
- `stripe_arrival_days`: 5

## The gate (stops before anything runs)

- **Books:** opening + lines = closing, every date inside the statement period, every amount a plain number. If
  the lines only add up with their signs reversed, it says to use `--kind card`.
- **Statement:** opening + rows = closing, the same period as the books, and no unknown status values. Pending and
  failed rows are left out. If the rows only add up reversed, it says to use `--flip-sign`.
- **Opening balances differ and nothing explains why:** asks for last period's uncleared items.
- **Empty files:** need `--no-activity`.

## Matching

Same engine as month-end-review's T1:
1. **Exact amount, lowest cost first.** A matching check number costs nothing, otherwise the cost is the days
   apart. Lines that cleared 6 to 31 days later cost more.
2. **Groups,** when the descriptions share a word: several statement rows to one book line, or the other way round.
3. **Possible typos:** same date (±2 days), with digits swapped or the sign reversed. They're never treated as
   fact, so their draft is always *confirm first*.

## Timing (no fix)

- **Deposit in transit:** a book deposit within `window_days` of the statement end. A Stripe deposit counts as in
  transit only if the Stripe export has a payout of that amount created by the end. A Stripe deposit with no payout
  behind it *needs a decision*.
- **Outstanding payment:** a check up to `outstanding_check_days` old, or any payment within `window_days` of the
  end. A refund, expense or transfer with a document number isn't a check.

## Book errors (draft written)

| Cause | Found when | Draft | Status |
|---|---|---|---|
| Transfer booked wrong | A statement row in account A matches, within `transfer_days`, a line in account B for the opposite amount. That line is an Expense, Deposit or Check rather than a Transfer, or a Transfer to another account. It also needs the line on B's statement, or transfer words | change-type to a Transfer between A and B | ready if B's statement has it too |
| Wrong bank account | A statement row in A is in B's books, but not on B's statement | move it to A | confirm first |
| Amount typed wrong | A typo pair (above). It notes when the gap divides by 9 | edit the amount | confirm first |
| Booked twice | Same payee, type and amount within `duplicate_days`. With Stripe, it compares the count with Stripe's refunds; without Stripe, one copy on the statement and one not | delete the extra copy | ready with Stripe proof, else confirm first |
| Refund paid from Stripe | A refund in the bank's books that Stripe has | move to the Stripe clearing account, or remove if payouts are booked net | confirm first |
| Stripe payout not recorded | A Stripe deposit on the statement that matches a payout created up to `stripe_arrival_days` before it | create, set up like the last Stripe deposit | ready |
| Statement item not in the books | Any other statement row. The template is the book line with the same words and direction, preferring the same amount | create, set up like the template; with no template, ask which account | confirm first |
| Carried from last period | Last period's error items whose fix isn't in the books yet | the same draft again | as before |

## Needs a decision (no draft)

- **A book payment that never reached the statement and isn't a check.** Without Stripe, a refund booked twice
  looks exactly like this. It's never treated as timing.
- **Two or more copies with none on the statement and no Stripe proof.** One copy is offered as a possible
  duplicate (*confirm first*); the other copies stay *needs a decision*.
- **Uncleared items carried from last period that are past their window.**
- **A Stripe deposit with no Stripe payout of that amount.**
- **An opening difference.** Usual causes: an earlier, already-reconciled period was changed, or last period's
  uncleared list was incomplete.

## Carrying forward

`output/outstanding.csv` lists every unmatched line with its item, type and draft. Next month's run picks it up
from the previous month's folder:
- Items that clear are matched.
- Items still open come back, errors with their draft.
- If the opening balances show that every drafted fix was posted with its original date, the carried errors are
  dropped.
- A fix posted with a new date matches the carried line instead.

If only some fixes were posted with their original dates, the rest shows as an opening difference.

## What it can't do

- **See the bank feed.** It can't see QuickBooks' For Review list or what's been ticked in the Reconcile screen.
- **Check the other side of matched entries.** A matched line booked to the wrong expense account isn't checked.
  Use month-end-review for that.
- **Settle Stripe items without the Stripe export.**
- **Recognise a statement row that combines unrelated book lines** unless the descriptions share a word.
