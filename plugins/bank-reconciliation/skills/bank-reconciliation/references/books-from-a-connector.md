# Getting the book side from a QuickBooks connector

The skill doesn't sign in to QuickBooks itself. It uses whatever QuickBooks connector the person has attached
(Intuit's, nocodetalks' Books Connector, or another). The connector supplies the data, you copy it into one file,
and `add-books` checks the copy adds up.

## 1. The right company

Ask the connector which company it's signed in to, and read the name back. If the person has a sandbox and a real
company, make sure it's the one they mean.

## 2. The two balances

Get the account's balance from QuickBooks on two dates:
- **the day before the statement starts** (Aug 31 for a September statement): this is `--opening`
- **the statement's end date:** this is `--closing`

Use a Balance Sheet report as of each date, or the account's register balance on that date. Copy the figures as
QuickBooks shows them. Don't work them out from the lines. They're the check on the lines.

For a credit card, QuickBooks shows the amount owed as a positive balance. Use that.

## 3. The lines

Every transaction line that touches the account, dated from the statement's start to its end date. Good sources:
- the General Ledger or Transaction Detail by Account report, filtered to the account and the dates
- a query of the transactions in the account for those dates

Write `books-source.csv` with this header, one row per line:

```
txn_id,txn_type,date,account,name,memo,doc_num,amount,split_account
```

| Column | What goes in | Required |
|---|---|---|
| `txn_id` | QuickBooks record id, so a draft can point at the record | strongly recommended |
| `txn_type` | Check, Expense, Deposit, Payment, Refund, Transfer, Journal Entry, Bill Payment (Check)… | strongly recommended |
| `date` | YYYY-MM-DD | yes |
| `account` | the bank or card account's name; lets one file hold several accounts | if the file has more than one account |
| `name` | payee or customer | recommended |
| `memo` | memo or description | recommended |
| `doc_num` | check or document number | if there is one |
| `amount` | the signed amount as the report shows it for this account: money in (debit) positive, money out (credit) negative. For a card account, charges are negative and payments positive. `--kind card` turns them round | yes |
| `split_account` | the other account on the entry (`-Split-` if several) | if the connector gives it |

Copying rules:
- One row per line, every line, in any order. Don't merge, net or skip lines, including $0 lines or voided ones
  that show an amount.
- Copy each amount exactly: same digits, same sign. Don't round.
- If a value has a comma, put the cell in double quotes.
- If the connector pages its results, get every page.

Then:

```
python3 <skill>/scripts/bankrec.py add-books --account "Mercury Checking" --file books-source.csv \
  --opening 312684.00 --closing 219774.00 --source "Books Connector, General Ledger report"
```

## If it doesn't add up

`add-books` stops when opening + lines ≠ closing. Usual causes:
- a page of results missed
- a line copied with the wrong sign or a typo
- a balance taken from the wrong date
- lines dated outside the period

Pull again and fix the file. Never change a balance or a line just to make it pass.

## No connector

The person exports the account's register, or the Transaction Detail by Account report, from QuickBooks as CSV.
Name the columns if they aren't the usual ones:

```
bankrec.py add-books ... --date-col "Transaction date" --amount-col "Amount" --name-col "Name" \
  --txn-type-col "Transaction type" --doc-num-col "Num" --memo-col "Memo/Description" --split-account-col "Split"
```

Remove report headers, subtotals and total rows from the export first, so that only transaction rows are left.
Exports have no record ids, so the drafts point at records by date, type, number and payee instead.
