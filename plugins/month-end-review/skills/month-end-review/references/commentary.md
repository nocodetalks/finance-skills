# Writing the commentary, questions and investor notes

You write only between the markers:
- the **Commentary** and **Questions for the owner** sections of `output/review.md`
- the **Notes** section of `output/investor-update.md`

The investor update's numbers table, and its "Draft" line when items are open, are written by the script. Don't
change them. `mer.py verify` rejects any edit outside the markers and any number not computed by the checks.

## Numbers
- **Use only figures that appear in `results.json`.** Rounding to the precision you write is fine: `$83,940.00`,
  `$83,940` and `$83.9k` all pass. Shortened figures need enough digits to check: at least three with "k", two
  with "million".
- **Percentages** must be computed ones (`change_pct`, metrics). A margin or growth rate you work out yourself
  fails.
- **Signs matter.**
  - A negative figure must be written as `-$52,460` or `($52,460)`, or with a word such as loss, down or fell in
    the same clause.
  - A positive figure can't be described as falling.
  - "Net income was $52,460" fails when the figure is negative.
  - Use the sign *or* the word, not both: "a loss of $52,460" or "net income of -$52,460", never "a loss of -$52,460".
- **Small numbers about the business** (people, customers, charges, months of runway or cash) must be computed
  facts. These are fine:
  - counts of review items ("3 items")
  - plan terms ("a 12 months plan"; in Questions, any duration)
  - dates and date ranges ("Sep 16-30")
  - "3-month", "30-60 days", "top 3"
  - years
- **No number words for amounts** ("doubled", "five thousand").
- **Revenue excludes other income** (interest). Use the P&L totals and metrics as labelled.

## Citations
Cite every claim:
- an open item: `[E4]`
- rows: `[GL 1104-20-1]`, `[bank/1.csv row 13]`
- a P&L line: `[V1 Software]`

Brackets may hold only references that exist in this review. An unknown reference (`[E99]`) fails, and anything
else in brackets is checked like ordinary text. `verify` also re-runs the checks from the inputs, so the numbers
can't be changed by editing `results.json`.

## Commentary (in review.md)
1. **Start with the state of the books.** Give the number of open high items and the biggest two. Say plainly that
   the figures are *before* those fixes. If the review lists "net income after ready drafts", you may quote it.
2. **Headline:** revenue, expenses and net income vs last month (and vs budget when there is one).
3. **Material lines, largest first.** For each, give the dollar and % change, then what moved (the `drivers`, with
   refs).
   - A line marked *unreliable* is touched by an open item: explain the item, not the variance.
   - Label each driver **timing** (the rows show another month), **one-off** (no history), **structural** (it will
     repeat) or **unknown**.
   - An unknown driver becomes a question. Never write "timing" just because nothing else fits.
4. Under 250 words, in plain English.

## Questions for the owner (in review.md)
- A numbered list, one question per item that needs a person's knowledge, each citing `[E#]` or `[V1 ...]`.
- Ask questions that can be answered in a sentence.
- If the answer makes an item expected (a cancelled vendor, say), it can be acknowledged with
  `mer.py acknowledge --id E# --by <name> --reason "..."` after the person agrees.

## Notes (in investor-update.md)
- Under 120 words: two or three drivers, plus one line on anything still open.
- Use the metrics table's figures, and no adjectives the numbers don't support ("strong", "great").

## Then
Run `mer.py verify`. Fix each line it reports by using a figure from `results.json` or removing the claim. Never edit
`results.json`.
