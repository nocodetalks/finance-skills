---
name: bank-reconciliation
description: >-
  Explains why the QuickBooks balance of a bank or credit card account doesn't match the bank statement, and drafts
  the fixes. It matches every book line to the statement and names each cause of the gap: deposits in transit and
  outstanding checks, statement items missing from the books (payroll, Stripe payouts, fees), amounts typed wrong
  (transposed digits), entries booked twice, refunds paid out of Stripe instead of the bank, transfers between your
  own accounts booked as expenses, and payments booked to the wrong bank account. Every account is bridged from book
  balance to statement balance to the cent, and each fix is a draft for a person to approve. Read-only: it never
  posts. Reads the books through a QuickBooks connector (or a QuickBooks export) and stops when an input is missing
  or doesn't add up. Use when someone asks why QuickBooks and the bank don't match, to reconcile a bank or card
  account, or to find the difference between the book and statement balance.
license: MIT
compatibility: Python 3.10+ (standard library only). Book lines from any QuickBooks connector or a QuickBooks export; bank and card statements as CSV; Stripe balance transactions export (optional).
metadata:
  version: 0.1.0
  author: nocodetalks
  repository: https://github.com/nocodetalks/finance-skills
---

# Bank reconciliation

This skill answers one question: **why don't QuickBooks and the bank agree, and what fixes it?**

The scripts in `scripts/` check the inputs add up, match both sides, name every cause and draft every fix. You
bring the inputs in, run them, and explain `output/` to the person. You don't do the matching or the arithmetic.

## Rules that never bend

1. **Missing input means stop.** If a command exits 2, tell the person what it says and ask for exactly that.
   Don't estimate, fill in a balance, carry on from an assumption, or make up data to get unstuck.
2. **Scripts compute, you copy.** The only numbers you write are copies:
   - book lines from the connector, into the books file, value for value
   - balances from a QuickBooks report or the statement, word for word
   Never add, net, round or work out a figure, in a file or in chat. If something needs computing, say so.
3. **The books must add up before anything runs.** `add-books` checks opening + lines = closing. If it stops, you
   copied a line wrong or missed one: pull again and fix the file. Never adjust a balance or a line to make it pass.
4. **Never guess a cause.** The causes come only from the scripts. Never drop, merge, soften or add one. An item
   marked *Needs a decision* stays that way until the person decides. Don't call it timing.
5. **Read-only.** Never post, edit, delete or void anything in QuickBooks yourself, even if the connector can
   write. The drafts are proposals (rule 6).
6. **A person approves each fix.** If the person wants a draft applied through a QuickBooks connector, apply that
   one draft only after they've said yes to it in this conversation. Use the connector's own preview or approval
   step if it has one. Never batch approvals ("all of them") into posts you haven't shown them one by one. Then run
   the reconciliation again.
7. **Say what wasn't checked.** Keep the "What this didn't check" list. Never call an account "clean". Say
   "reconciled" only when the status says so.
8. **Keys never enter the chat.** Never ask for a QuickBooks or Stripe key. If someone pastes one, don't repeat or
   use it.

## Where things live

- Engine: `python3 <this skill's folder>/scripts/bankrec.py <command> --root <work folder> --month YYYY-MM`
- `--month` is the month the statement closes in. Each statement's own dates are `--start` and `--end`; they
  default to the whole month.
- The work folder (default `./bank-rec/`) holds one folder per month with `inputs/`, `output/` and
  `output.previous/`. It holds financial data, so keep it out of git.
- Exit codes: `0` done · `2` stopped (read the message, rule 1) · `3` error (a bug or unexpected data: use nothing
  from the run and report it).

## Workflow

**1. Agree the account and the statement.** Which account(s), and the statement's start date, end date, opening
balance and closing balance. Ask the person to read the two balances off the statement. Reconcile every bank and
card account the money moves between in one run: a transfer booked wrong shows up only when both accounts are
there.

**2. Book side, through the QuickBooks connector.** See
[references/books-from-a-connector.md](references/books-from-a-connector.md). In short:
1. Confirm which company the connector is signed in to. Read the name back to the person.
2. Get the account's balance the day before the statement starts and on its end date, from a Balance Sheet (or
   the account's register) as of those dates.
3. Get every line in the account for exactly the statement period. For each line you need: record id, type, date,
   payee, memo, doc number, signed amount, and the other account (split) where the connector gives it.
4. Write them to one CSV (any path; `<work folder>/<month>/books-source.csv` is a good place) with the columns
   `txn_id,txn_type,date,account,name,memo,doc_num,amount,split_account`. Copy each value exactly as the connector
   returned it. Several accounts can share one file: the `account` column keeps them apart.
5. Run `add-books` once per account, against the same file if they share one. `add-books` copies what it needs
   into the work folder:
   `bankrec.py add-books --account "<name>" --file <csv> --opening <balance> --closing <balance> --source
   "<connector name>, <report or query used>"`. For a credit card, add `--kind card` to this `add-books` command
   (`add-statement` has no `--kind`).

No connector? The person exports the account's register or a Transaction Detail by Account report from
QuickBooks as CSV. Map its columns with `--date-col`, `--amount-col`, `--name-col` and so on.

**3. Statement.** The person downloads the statement's CSV export. Run `bankrec.py add-statement --account
"<name>" --file <csv> --opening <from statement> --closing <from statement>`. It finds the usual columns itself and
drops pending rows. If it stops, do what it says: `--flip-sign` for card exports with reversed signs,
`--date-format %d/%m/%Y` for day-first dates (US month-first dates are the default), `--posted <value>` for an unfamiliar status column, `--trim` for an
export longer than the statement.

**4. Stripe (if the company takes payments through Stripe).** Ask for Stripe's balance transactions export for the
period, and the name of the Stripe clearing account in QuickBooks if there is one: `bankrec.py add-stripe --file
<csv> --clearing-account "<name>"`. Without it, a refund booked twice looks like two uncleared payments, and a
payout in transit can't be told from a payout booked with the wrong amount. Say so if they skip it.

**5. First run only, if the opening balances differ.** The run stops and asks for what was uncleared at the start:
- `add-outstanding --account "<name>" --file <csv>`, from last period's reconciliation report (columns date,
  description, amount, side = book or bank), or
- if there was no reconciliation before, `add-outstanding --account "<name>" --none --by "<their name>"` after
  they've said so. The gap then shows as an opening difference nobody has traced.

After the first month, the uncleared items carry forward by themselves.

**6. Run:** `bankrec.py run --month 2026-09`. It prints the same summary as `bankrec.py summary` and writes
`output/reconciliation.md`.

**7. Present.** Lead with the answer to the question asked: "QuickBooks and the bank are $X apart because of N
items", naming the biggest first. Then show the summary, plus the path to `reconciliation.md`. You may lay it out as
tables and shorten the wording, but don't change any number, type, status or draft. For each account, cover:
- the difference
- the items, grouped as *timing*, *book errors* and *needs a decision*
- the line "book balance after the drafted fixes" against the "adjusted bank balance"

*Effect* is each item's share of books − statement, so a statement row missing from the books shows as −, and its
fix adds it to the books (+). Say this once if you show both. A fix in another account's record (a transfer booked
as an expense in Checking) changes that record but only the other account's balance; the report notes it under
both accounts.

For each *needs a decision* item, ask the person the question it raises.

**8. Fixes.** Go through the drafts with the person. *ready* means two sources agree (two statements, or Stripe
and the bank). *confirm first* means a person checks the source document first: the deposit slip, the payroll
run, the vendor. They post the fixes in QuickBooks, or approve them one at a time through a connector (rule 6).
Then run steps 2 and 6 again: the account should show *reconciled*, or *reconciled (timing items only)*.

## The causes

Details and limits: [references/causes.md](references/causes.md).

| Type | Cause | Fix |
|---|---|---|
| Timing | Deposit in transit (within 5 days of the end; a Stripe payout created by the end) · check or payment not cleared yet (checks up to 90 days) | none: it clears next period |
| Book error | Statement item not in the books (template: the payee's last entry) · Stripe payout not recorded · amount typed wrong (transposed digits divide by 9) · booked twice · refund paid from Stripe booked against the bank · transfer booked as an expense or deposit · booked to the wrong bank account · last period's fix still not posted | draft: create, edit, delete, move or change-type; *ready* when two sources agree (Stripe payout, duplicate proven by Stripe, transfer on both statements), otherwise *confirm first* |
| Needs a decision | In the books but never on the statement, and not a check · possible duplicate with no proof · old uncleared items · a Stripe deposit with no Stripe payout behind it · opening balances that differ with nothing listed | none: ask the person |
