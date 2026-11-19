"""Write the run's outputs: reconciliation.md (for people), results.json and drafts.json (for tools), and
outstanding.csv (carried into next period). The summary printed to chat comes from results.json only."""

from __future__ import annotations

from pathlib import Path

from .util import ZERO, fmt, read_json, write_csv, write_json

KIND_LABEL = {"timing": "Timing (clears by itself)", "error": "Book error (fix drafted)",
              "decide": "Needs a decision"}
OUT_FIELDS = ["account", "side", "date", "description", "amount", "txn_id", "txn_type", "doc_num", "kind", "item", "title",
              "draft", "fix_effect", "source_ref"]


def _clean_item(i: dict) -> dict:
    return {k: v for k, v in i.items() if k not in ("book", "bank")}


def write_all(out_dir: Path, period_label: str, res: dict, generated: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results = {"period": period_label, "generated": generated, "accounts": res["accounts"],
               "items": [_clean_item(i) for i in res["items"]], "drafts": res["drafts"],
               "stripe": res["stripe"], "thresholds": res["thresholds"]}
    write_json(out_dir / "results.json", results)
    write_json(out_dir / "drafts.json", {"period": period_label, "note": "Proposals only. Nothing here has been posted. "
                                         "A person approves each one before it goes into QuickBooks.",
                                         "drafts": res["drafts"]})
    write_csv(out_dir / "outstanding.csv", res["outstanding"], OUT_FIELDS)
    md = render(results)
    (out_dir / "reconciliation.md").write_text(md, encoding="utf-8")
    return [out_dir / n for n in ("reconciliation.md", "results.json", "drafts.json", "outstanding.csv")]


def _effect_word(e) -> str:
    return "books higher" if e > 0 else "books lower"


def render(r: dict) -> str:
    L = [f"# Bank reconciliation: {r['period']}", "",
         f"Generated {r['generated']}. Read-only: nothing was posted. Fixes are drafts for a person to approve.", "",
         "| Account | Books | Statement | Difference | Status |", "|---|---:|---:|---:|---|"]
    for a in r["accounts"]:
        L.append(f"| {a['account']} | {fmt(a['book_closing'])} | {fmt(a['statement_closing'])} | {fmt(a['difference'])} "
                 f"| {a['status']} |")
    for a in r["accounts"]:
        its = [i for i in r["items"] if i["account"] == a["account"]]
        L += ["", f"## {a['account']} ({a['start']} to {a['end']})", ""]
        if its:
            L.append(f"Books {fmt(a['book_closing'])}, statement {fmt(a['statement_closing'])}: "
                     f"{fmt(a['difference'])} apart. {len(its)} item{'s explain' if len(its) != 1 else ' explains'} "
                     "it to the cent.")
        else:
            L.append(f"Books {fmt(a['book_closing'])}, statement {fmt(a['statement_closing'])}: every line matched; "
                     "nothing to explain.")
        if its:
            L += ["", "### What makes up the difference", "",
                  "*Effect* is each item's share of books − statement: an item only in the books pushes the books "
                  "up (+), an item only on the statement pushes them down (−). The fix moves the books the other way.",
                  "",
                  "| # | Type | Date | What | Effect on the difference | Source | Fix |", "|---|---|---|---|---:|---|---|"]
            for i in its:
                L.append(f"| {i['id']} | {KIND_LABEL[i['kind']]} | {i.get('date', '')} | {i['title']} | "
                         f"{fmt(i['effect'], True)} | {'; '.join(i['refs'])} | {i.get('draft') or ''} |")
            L.append(f"| | | | **Total** | **{fmt(sum((_d(i['effect']) for i in its), ZERO), True)}** | | |")
            L += ["", "Why each one:", ""]
            for i in its:
                L.append(f"- **{i['id']}** {i['why']}")
        L += ["", "### Reconciliation", "", "| | Amount |", "|---|---:|",
              f"| Statement balance, {a['end']} | {fmt(a['statement_closing'])} |"]
        for i in [i for i in its if i["kind"] == "timing"]:
            L.append(f"| {'+' if _d(i['effect']) > 0 else '−'} {i['title']} ({i['id']}) | {fmt(i['effect'], True)} |")
        L.append(f"| **Adjusted bank balance** | **{fmt(a['adjusted_bank'])}** |")
        L.append(f"| Book balance, {a['end']} | {fmt(a['book_closing'])} |")
        for d in r["drafts"]:
            e = d["effects"].get(a["account"])
            if e:
                L.append(f"| {d['id']} {d['title']} ({d['status']}) | {fmt(e, True)} |")
        L.append(f"| **Book balance after the drafted fixes** | **{fmt(a['books_after_drafts'])}** |")
        left = _d(a["left_after_drafts"])
        L.append(f"| Left over | {fmt(left)} |")
        if left != 0:
            L += ["", f"{fmt(left)} is still unexplained after the drafted fixes: the items marked *Needs a decision* "
                  "above. Nothing was guessed for them."]
        others = [d for d in r["drafts"] if a["account"] not in d["effects"]
                  and (d.get("record") or {}).get("account") == a["account"]]
        if others:
            L += ["", "Fixes that change a record in this account but not its balance: "
                  + "; ".join(f"{d['id']} ({d['title']})" for d in others) + "."]
        L += ["", f"Matched {a['matched']} book line{'' if a['matched'] == 1 else 's'} to the statement"
              + (f" ({a['matched_late']} cleared late, {a['matched_groups']} as groups)" if a['matched_late'] or a['matched_groups'] else "")
              + f". Book lines: {a['book_lines']} from {a['books_source']} ({a['books_file']}). Statement rows: "
              f"{a['statement_rows']} ({a['statement_file']}). Uncleared at the start: {a['prior_source']}."]
    if r["drafts"]:
        L += ["", "## Draft fixes", "",
              "Nothing here has been posted. **ready** means two sources agree. **confirm first** means a person checks "
              "the source document before posting. Post each one in QuickBooks yourself, or approve it one at a time "
              "through a QuickBooks connector that asks before it writes. Then run the reconciliation again.", ""]
        for d in r["drafts"]:
            L.append(f"- **{d['id']} ({d['status']})**: {d['change']} Evidence: {'; '.join(d['evidence'])}.")
    L += ["", "## What this didn't check", "",
          "- Whether each matched line is booked to the right income or expense account. Only this account's side was "
          "reconciled.",
          "- Bank-feed items still waiting in QuickBooks' For Review list. They aren't in the books yet, so they show "
          "as statement items not in the books.",
          "- The reconciliation in QuickBooks itself: ticking cleared items in the Reconcile screen is done in "
          "QuickBooks.",
          ]
    if not r.get("stripe"):
        L.append("- Stripe: no Stripe export was added, so Stripe payouts in transit and refunds paid from the Stripe "
                 "balance couldn't be confirmed.")
    if not all(a.get("has_split_account") for a in r["accounts"]):
        L.append("- The other side of each entry (split account) wasn't in the book lines for every account, so "
                 "transfers booked to the wrong place were found only from the statements.")
    return "\n".join(L) + "\n"


def _d(v):
    from decimal import Decimal
    return v if isinstance(v, Decimal) else Decimal(str(v))


def summary(out_dir: Path) -> str:
    p = out_dir / "results.json"
    if not p.exists():
        return "No results yet. Run `bankrec.py run` first."
    r = read_json(p)
    L = [f"Bank reconciliation, {r['period']} (read-only; nothing posted)", ""]
    for a in r["accounts"]:
        its = [i for i in r["items"] if i["account"] == a["account"]]
        L.append(f"{a['account']}: books {fmt(_d(a['book_closing']))}, statement {fmt(_d(a['statement_closing']))}, "
                 f"difference {fmt(_d(a['difference']))}. Status: {a['status']}.")
        for i in its:
            L.append(f"  {i['id']} [{i['kind']}] {i['title']} ({fmt(_d(i['effect']), True)})"
                     + (f" → fix {i['draft']}" if i.get("draft") else ""))
        L.append(f"  Book balance after the drafted fixes {fmt(_d(a['books_after_drafts']))}; adjusted bank balance "
                 f"{fmt(_d(a['adjusted_bank']))}; left over {fmt(_d(a['left_after_drafts']))}.")
        L.append("")
    if r["drafts"]:
        L.append("Draft fixes (not posted):")
        for d in r["drafts"]:
            L.append(f"  {d['id']} ({d['status']}): {d['change']}")
        L.append("")
    if not r.get("stripe"):
        L.append("Not checked: no Stripe export was added.")
    return "\n".join(L).rstrip() + "\n"
