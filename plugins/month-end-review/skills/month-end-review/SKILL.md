---
name: month-end-review
description: >-
  Reviews a closed month for a US startup on QuickBooks Online, Stripe and US banks before the numbers go to
  investors. It checks the month is right first: cash, card, AR/AP, Stripe and payroll tie-outs; duplicate,
  missing and late bills; accruals, prepaids, deferred revenue and annual plans; missing monthly entries; wrong
  accounts; transfers and financing booked as revenue; one vendor under two names; the payroll split between cost
  of sales and opex; and every balance-sheet account. Then it computes cash, burn, runway and MRR, and writes
  variance commentary and an investor-update draft in which every number traces to source rows and is
  machine-checked. Read-only, and it stops cold when a required input is missing. Use when someone asks to
  review, check or sanity-check a month-end close, or to write the month's variance commentary or investor update
  from QuickBooks.
license: MIT
compatibility: Python 3.10+ (standard library only). QuickBooks Online via the person's own Intuit app keys (read-only in code); Stripe via a restricted read-only key or CSV export; bank and card statements as CSV.
metadata:
  version: 0.1.0
  author: nocodetalks
  repository: https://github.com/nocodetalks/finance-skills
---

# Month-end review

This skill answers two questions in order: **is this month right?** and only then **what happened this month?**

The scripts in `scripts/` pull the data, run every check and do all the math. You run them, explain the results
to the person, and write the commentary, owner questions and investor notes from `output/results.json` only.

## Rules that never bend

1. **Missing input means stop.** If the gate fails, show `output/gate-report.md` as it is, ask for exactly what it
   lists, and do nothing else. No partial review. Don't estimate, fill in "typical" values, carry on from an
   assumption, or make up sample data to get unstuck.
2. **Scripts compute, you write.** Never type a number into an input file, the config, the commentary, or your chat
   replies that a script didn't produce. Don't do arithmetic in chat either; say what would need computing. Never copy figures from a connector, chat or screenshot into the inputs. The only numbers
   a person supplies are statement and Stripe balances, word for word from the statement or portal.
3. **Every number cites its source,** an exception `[E4]` or rows `[GL 1104-20-1]`. `mer.py verify` must pass
   before you present anything. Never edit `results.json` or text outside the marked sections; verify catches it.
4. **Never guess a cause.** If the rows don't show why something moved, say it's unknown and turn it into a question.
5. **Read-only.** Nothing is posted, voided or edited anywhere. Draft entries are proposals for a person.
6. **A person confirms.** You may run `confirm-roles`, `attest` and `acknowledge` only after the person has said,
   in this conversation, that the roles are right, that a source truly had no activity, or why an item is
   acknowledged. Pass their name in `--by`.
7. **Say what wasn't checked.** The coverage list stays in every review. Never describe the month as "clean".
8. **The checks decide what's unusual, not you.** Exceptions come only from the scripts. Never drop, merge, soften
   or explain away one, and never add your own. If you think something is wrong or missing, raise it as a question
   for the person. Only a person clears an item (`acknowledge`, rule 6) or posts a draft entry (rule 5), so the one
   who checks is never the one who did the work.
9. **Keys never enter the chat.**
   - Never open, read, print, search or copy `.env` or anything in `~/.config/finance-skills/`. Only the scripts
     read them.
   - Never ask for a Client ID, secret or API key in chat.
   - If someone pastes one anyway, don't repeat it or use it. Ask them to put it in the file instead.

## Where things live

- Engine: `python3 <this skill's folder>/scripts/mer.py <command> --root <work folder> --month YYYY-MM`
- Work folder (default `./month-end/`) contains `review-config.json`, `acknowledged.json`, and one folder per
  month with `inputs/`, `output/` and `output.previous/`. It holds financial data, so keep it out of git.
- Exit codes:
  - `0`: done
  - `1`: verify found problems
  - `2`: stopped (missing or wrong input; read the message)
  - `3`: error (a bug or unexpected data; use nothing from the run and report it)

## Workflow

**1. Agree the month.** Default to the last full calendar month, and confirm it.

**2. Connect QuickBooks before anything else.** Run `mer.py connect --check`.
- **Exit 0:** it names the connected company. Confirm it's the one to review.
- **Exit 2 (not connected):** set it up with the person. Nothing else runs until it's connected. Follow
  [references/quickbooks-setup.md](references/quickbooks-setup.md):
  1. **Which company.** Ask whether it's their real company (`production`) or an Intuit test company (`sandbox`).
  2. **Get the keys.** Walk them through part 1, one step at a time, to get their Client ID and Client Secret. Stop
     there until they have both. If Intuit hasn't approved the production keys yet, stop and say so.
  3. **Put them in `.env`.** Run `mer.py connect --environment production`. The first time, it creates
     `<work folder>/.env` and opens it. Ask them to paste both keys after the `=` signs, save, and say "done". If
     it didn't open, give them the path. Don't open it yourself.
  4. **Sign in.** Run the same command again. Their browser opens Intuit's sign-in. They sign in, choose the
     company and click Connect. Then they paste one of these from the page they land on:
     - its address
     - on Intuit's OAuth Playground, the Authorization Code and Realm ID

     They must not click Get tokens: that uses up the code.
  5. **Finish straight away.** Run `mer.py connect --finish '<what they pasted>'`, keeping the single quotes. Read them the
     company name it prints and confirm it's the right company.

  A stop from `connect` is rule 1: tell them what it says and wait.

Then run `mer.py pull-qbo --month 2026-09`.

**3. First time only: roles.**
1. Run `mer.py init --month 2026-09 --company "Name"`.
2. Show the proposed roles, basis and fiscal year, and let the person correct them in `review-config.json`. Pay
   particular attention to:
   - `processor_clearing` for a Stripe clearing account
   - `accrual_behavior` for accruals that build up (bonus, audit)
   - `bank_formats` for each CSV's columns, and `status_col` for exports that include pending rows
   - `payroll_cogs_pct`, if part of payroll (field, delivery or support staff) belongs in cost of sales: the share
     from 0 to 100, or 0 if none does. It's the person's answer, typed by them, never yours; leave it `null` if
     they don't know. Those wage accounts go in `payroll_wages`
3. When they agree, run `mer.py confirm-roles --by "<their name>"`. Any later change to the roles (or to
   `payroll_cogs_pct`) needs confirming again.

See [references/inputs.md](references/inputs.md).

**4. Stripe** (if `uses_stripe`). Prefer `mer.py pull-stripe`. It uses a restricted key, which the person pastes
after `STRIPE_API_KEY=` in the work folder's `.env`, never in chat. This also pulls invoices for annual plans and
MRR. The fallback is an export plus the balances the person reads off Stripe's Balance report:
`mer.py ingest-stripe --file <csv> --opening-balance <x> --closing-balance <y>`.
See [references/stripe-setup.md](references/stripe-setup.md).

**5. Every bank and card account:** run `mer.py ingest-statement --account "<name>" --file <csv>
--ending-balance <from statement> --statement-end <date>`.
- **First month only:** add `--opening-balance`. After that it carries forward.
- **Cards that close mid-month:** use the statement's own closing date and balance. Rows after it roll the balance
  forward.
- **First month only:** give last month's uncleared items with `mer.py ingest-outstanding --account "<name>"
  --file <csv>`, or record that there were none with `mer.py attest --source "outstanding:<name>" --by "<name>"`.
  After the first month they carry forward automatically.

**6. Optional inputs:**
- `ingest-payroll`: pay-period columns make the payroll accrual check possible.
- `ingest-budget`: long format, or one column per month.

If they're missing, the checks that need them show NOT RUN, never an estimate.

**7. Run:** `mer.py run --month 2026-09`.
- Exit `2`: go to rule 1.
- Exit `0`: read `output/results.json` and `output/review.md`.

Running again on a month whose commentary is written needs `--force`. The old review is kept in `output.previous/`.

**8. Write, then verify.** Follow [references/commentary.md](references/commentary.md):
1. Write between the markers in `review.md` (Commentary, Questions) and `investor-update.md` (Notes).
2. Run `mer.py verify`.
3. Fix whatever it reports.

**9. Present.** Run `mer.py summary` and present what it prints, plus the file paths. You may shorten the wording
and lay it out as tables, but:
- don't change any number, status or "NOT RUN" reason
- don't say which items can be acknowledged except as the summary marks them
- don't add claims that aren't in it

The chat isn't checked by `verify`, so the summary is what keeps it honest.

If the person says an open item is expected (a cancelled vendor, say), offer `mer.py acknowledge --id E# --by
<name> --reason "..."`. It stays listed but stops counting as open, and it reopens if the amount grows. Items marked
*can't be acknowledged* (something doesn't tie) can only be fixed.

If the person wants to add their own figures (headcount, a forecast), they add them to the copy they send, not to
`output/`. `verify` treats `output/` as the record of what was checked.

Drafts marked *confirm first* need a person to check them, and only *ready* drafts count in "net income after
drafts". Never offer to post anything.

## The checks

Details and limits: [references/checks.md](references/checks.md).

| Group | Checks |
|---|---|
| Gate | **G1** everything required is here · **G2** right dates, in content and labels · **G3** right shape · **G4** everything adds up (trial balance, each transaction, history vs P&L report, each statement, Stripe) · **G5** empty counts as missing |
| Tie-outs | **T1** cash and cards vs statements (carrying uncleared items forward) · **T2** AR/AP aging, unapplied payments, AR over 90 days, Undeposited Funds · **T3** Stripe payouts reach the bank and the books; clearing account vs the Stripe balance · **T4** Stripe fees, refunds, sales and tax vs the books · **T5** payroll vs provider, accruals included |
| Completeness | **C1** duplicates, and bills also paid as expenses; missing recurring bills · **C2** cutoff and accrual reversals · **C3** prepaids, deferred revenue, annual plans (including those billed in Stripe) · **C4** missing monthly journal entries, loan interest · **C5** the same vendor under two names |
| Classification | **K1** wrong account, suspense, impossible signs · **K2** accounts against their own history · **K3** large inflows from new payers booked as revenue · **K4** transfers between your own accounts booked as income (or as an expense) · **K5** payroll split between cost of sales and operating expenses, vs `payroll_cogs_pct` or history |
| Variance | **V1** P&L month, budget and year to date · **V2** every balance-sheet account, and what nothing reconciles · **V3** cash, burn, runway, revenue, margin, MRR · **V4** your commentary, then `verify` |

After the checks:
- **Items that share a transaction or the same money are grouped as one cause.**
- **Severity follows the dollar amount.**
- **Acknowledged items are listed separately.**
- **P&L lines touched by an open item are marked "unreliable".**
