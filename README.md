# finance-skills

Free, read-only Claude Code skills for startup finance teams on **QuickBooks Online + Stripe**.

These skills share three rules:
1. **They check the numbers are right before explaining them.** Tie-outs come first; commentary comes last.
2. **Every number traces back.** Scripts do the math, and each figure links to the rows it came from. The model only
   writes words, and a verifier rejects any number the scripts didn't compute, along with any edit to the
   computed parts.
3. **They stop when an input is missing.** If something is missing, from the wrong dates, the wrong shape, doesn't
   add up, or is empty, the run stops and says exactly what to fetch. It never guesses, never estimates, and never
   uses placeholder data.

| Skill | Status | What it does |
|---|---|---|
| [`month-end-review`](plugins/month-end-review) | v0.1.0 | Checks a closed month is right, then writes variance commentary and an investor-update draft |
| `stripe-to-books` | next | Stripe payouts, fees, refunds and disputes matched to the bank and the books |
| `cash-13-week` | next | 13-week cash forecast with a runway line |

## Install (Claude Code)

```
/plugin install month-end-review --marketplace nocodetalks/finance-skills
```

On older Claude Code versions, use two steps: `/plugin marketplace add nocodetalks/finance-skills`, then
`/plugin install month-end-review@finance-skills`.

Then ask: *"Review September's close."* The skill starts on its own from requests like that. To start it by
command, type `/month-end-review:month-end-review` (Claude Code's form for a plugin skill). The plain
`/month-end-review` works too, unless another command already uses that name.

**Other agents.** Each skill folder (`plugins/<name>/skills/<name>/`) follows the
[Agent Skills](https://agentskills.io) format. Copy it into your agent's skills folder.

**Requirements:** Python 3.10+ only. There are no packages to install.

## Safety

- **Read-only by design.**
  - The QuickBooks code sends only GET requests, plus the sign-in token exchange.
  - Stripe accepts only restricted `rk_` keys (scope them to Read), and test-mode keys are refused by default.
  - Nothing is ever posted. Fixes come out as draft entries for a person to approve.
- **Your data stays local.** Data is pulled by scripts straight to files on your machine, not pasted through
  the chat. Credentials files must be private (mode 600). Keep the `month-end/` work folder out of git.
- **Your keys stay out of the chat.**
  - You connect QuickBooks with your own Intuit app.
  - Its keys go in a `.env` file that only the scripts read.
  - There is no hosted nocodetalks service in between.
- **No personal data in the outputs.**
  - Payroll comes in as totals per pay run.
  - Payroll lines are shown as one total, never by person.
  - Bank account numbers are never written.

## Tests

```
python3 -m unittest discover -s tests -v
```

There are 227 tests on a small synthetic company ("Northwind", September 2026). They cover:
- **The 15 planted mistakes:** 14 caught, plus the one we expect to miss (a personal dinner on the company card
  needs a person's judgment).
- **Realistic scenarios:**
  - last month's uncleared checks
  - treasury sweeps
  - a Stripe clearing account
  - annual plans billed in Stripe
  - a SAFE booked as revenue
  - payroll accruals
  - accruals that build up month to month
  - missing depreciation
  - cards that close mid-month
  - transfers between your own accounts booked as income, one vendor under two names, and payroll in the
    wrong part of the P&L
- **Every way the gate must stop.**
- **Every known way to fool the number check.**
- **Re-runs, crashes and acknowledgements.**
- **Every finding from a second, independent review:**
  - grouping that can't chain unrelated items
  - fixes booked into last month after its review
  - unnamed financing
  - Stripe continuity between months
  - typos never treated as fact
  - accruals already booked
- **The QuickBooks and Stripe adapters,** with the network faked.
- **Connecting QuickBooks:**
  - keys read from `.env` and never printed
  - the pasted sign-in address, including Intuit's sign-in bounce
  - expired codes, wrong keys, and a second company on the same profile

The same company can be loaded into a free Intuit sandbox, with an answer key and a scorer: see
[testdata/northwind](testdata/northwind).

## Status, honestly

What v0.1.0 has been tested against:
- **The synthetic fixture and faked API responses.**
- **An Intuit sandbox company.**
  - The QuickBooks pull ran live, and the gate's data checks passed.
  - The full review ran on it.
  - The `connect` sign-in ran live, followed by a pull.
  - **Level 4:** the Northwind test company was loaded into the sandbox (`testdata/northwind/`). A real Claude
    session reviewed its September close from the user's side.
    - It caught 13 of 14 planted mistakes, plus the expected miss (#15).
    - It missed the manual journal entry to receivables. T2 now flags it, and a re-run caught 14 of 14.
- **Stripe's API path, against a local fake Stripe** (`testdata/northwind/fake_stripe.py`):
  - key from `.env`
  - paging
  - the balance worked back from today's balance
  - invoices for MRR and annual plans
- **Not yet run live:**
  - Stripe's real API
  - real bank and payroll export formats

If an adapter misreads anything, the gate's tie-outs stop the run rather than produce wrong numbers:
- every transaction must balance
- the ledger must equal the trial balance and QuickBooks' own P&L
- statements and Stripe must roll from opening to closing

## License

MIT
