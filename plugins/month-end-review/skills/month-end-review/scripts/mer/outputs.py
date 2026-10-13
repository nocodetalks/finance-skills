"""Write the review into an output folder: results.json, CSVs, review.md, investor-update.md, .integrity.json.

The model later writes only between the markers. `verify` checks nothing outside the markers
changed (and results.json is untouched), then checks every number written between them.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from . import __version__
from .checks import CheckResult, stripe_bridge
from .data import Data
from .util import ZERO, fmt, norm, sha256, write_csv, write_json

MARKERS = {
    "review.md": [("<!-- commentary:start -->", "<!-- commentary:end -->"), ("<!-- questions:start -->", "<!-- questions:end -->")],
    "investor-update.md": [("<!-- notes:start -->", "<!-- notes:end -->")],
}
PLACEHOLDER = "_Not written yet._"

NOT_CHECKED = [
    "Whether a card charge was personal or business (a person has to judge that).",
    "Contracts, invoices or commitments that never reached the books.",
    "Stripe charge-by-charge and fee-by-fee matching (that is /stripe-to-books).",
    "Fixed-asset register and depreciation amounts (only whether the monthly entry was booked).",
    "Equity, SAFEs, loans and payroll liabilities against outside statements (they're listed under Balance sheet).",
    "Tax filings, 1099s, multi-entity, intercompany and foreign currency.",
    "Transactions on a card after its mid-month statement date can't be proven complete.",
]


def normalized(text: str, name: str) -> str:
    for s, e in MARKERS[name]:
        text = re.sub(re.escape(s) + r".*?" + re.escape(e), f"{s}\n{PLACEHOLDER}\n{e}", text, flags=re.S)
    return text


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _drafts(d: Data, results: list[CheckResult]) -> tuple[list, dict]:
    by_name = {}
    for aid, a in d.accounts.items():
        by_name[norm(a["full_name"])] = aid
        by_name.setdefault(norm(a["name"]), aid)
    drafts, delta_ni = [], ZERO
    n = 0
    for r in results:
        for x in r.drafts:
            n += 1
            lines = x["lines"]
            status = x["status"]
            if status in ("ready", "confirm first"):
                dr = sum((l["debit"] or ZERO for l in lines), ZERO)
                cr = sum((l["credit"] or ZERO for l in lines), ZERO)
                if dr != cr or any(norm(l["account"]) not in by_name for l in lines):
                    status = "check accounts"
            ready = status == "ready"
            je = {"id": f"D{n}", "check": x["check"], "date": d.period.end, "memo": x["memo"],
                  "status": status, "basis": x["basis"], "lines": lines}
            if ready:
                for l in lines:
                    aid = by_name[norm(l["account"])]
                    if d.is_pl(aid):
                        delta_ni += (l["credit"] or ZERO) - (l["debit"] or ZERO)
            drafts.append(je)
    return drafts, {"net_income_change_from_ready_drafts": delta_ni}


def _facts(res: dict) -> list:
    facts = []

    def add(label, v, unit="usd"):
        if v is None or isinstance(v, bool):
            return
        try:
            facts.append({"label": label, "value": str(Decimal(str(v))), "unit": unit})
        except Exception:
            pass

    for k, v in res["counts"].items():
        add(f"count {k}", v, "count")
    for m in res["metrics"]:
        add(m["label"], m["value"], m["unit"])
    f = res["flux"]
    if f:
        for name, t in f["totals"].items():
            for k in ("current", "prior", "change", "budget", "vs_budget", "not_in_budget", "ytd", "ytd_budget"):
                add(f"{name} {k}", t.get(k))
            add(f"{name} change %", t.get("change_pct"), "pct")
        for k, v in f["gross_margin_pct"].items():
            add(f"gross margin {k}", v, "pct")
        for row in f["rows"]:
            for k in ("current", "prior", "change", "budget", "vs_budget", "ytd", "ytd_budget"):
                add(f"{row['account']} {k}", row.get(k))
            for k in ("change_pct", "vs_budget_pct"):
                add(f"{row['account']} {k}", row.get(k), "pct")
            for dv in row.get("drivers", []):
                for k in ("current", "prior", "change"):
                    add(f"{row['account']} {dv['payee']} {k}", dv[k])
    for row in res["balance_sheet"]["rows"]:
        for k in ("prior", "current", "change"):
            add(f"{row['account']} {k}", row[k])
    for t in res["tieouts"]:
        for k in ("books", "other", "difference"):
            add(f"{t['check']} {t['item']} {k}", t.get(k))
    for e in res["exceptions"] + res["acknowledged"]:
        add(f"{e['id']} amount", e["amount"])
    for x in res["drafts"]:
        for l in x["lines"]:
            add(f"{x['id']} {l['account']} debit", l["debit"])
            add(f"{x['id']} {l['account']} credit", l["credit"])
    if res["stripe"]:
        for k, v in res["stripe"].items():
            add(f"stripe {k}", v)
    pf = res["proforma"]
    add("net income change from ready drafts", pf["net_income_change_from_ready_drafts"])
    if pf.get("net_income_after_ready_drafts") is not None:
        add("net income after ready drafts", pf["net_income_after_ready_drafts"])
    return facts


def build_results(d: Data, results: list[CheckResult], exc: dict, ctx: dict) -> dict:
    by_id = {r.id: r for r in results}
    tieouts = [t for r in results for t in r.tieouts]
    drafts, proforma = _drafts(d, results)
    v1 = by_id["V1"]
    flux = v1.data if v1.status != "not_run" else {}
    if flux:
        proforma["net_income_after_ready_drafts"] = flux["totals"]["net_income"]["current"] + proforma["net_income_change_from_ready_drafts"]
    open_ = exc["open"]
    for e in open_ + exc["acknowledged"]:
        e["can_acknowledge"] = not e["pin"]
    counts = {"checks_run": sum(1 for r in results if r.status != "not_run"),
              "checks_pass": sum(1 for r in results if r.status == "pass"),
              "checks_flag": sum(1 for r in results if r.status == "flag"),
              "checks_not_run": sum(1 for r in results if r.status == "not_run"),
              "open_items": len(open_), "open_high": sum(1 for e in open_ if e["severity"] == "high"),
              "open_medium": sum(1 for e in open_ if e["severity"] == "medium"),
              "open_low": sum(1 for e in open_ if e["severity"] == "low"), "acknowledged": len(exc["acknowledged"])}
    inputs = {p.name: sha256(p) for p in sorted((d.month_dir / "inputs").rglob("*.csv"))}
    res = {
        "meta": {"company": d.cfg.get("company", ""), "period": d.period.key, "period_label": d.period.label,
                 "basis": d.cfg["basis"], "fiscal_year_start_month": d.cfg["fiscal_year_start_month"],
                 "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "skill_version": __version__, "read_only": True, "input_sha256": inputs},
        "counts": counts,
        "checks": [{"id": r.id, "title": r.title, "status": r.status, "reason": r.reason, "summary": r.summary,
                    "parts": r.parts} for r in results],
        "exceptions": open_, "acknowledged": exc["acknowledged"], "tieouts": tieouts, "drafts": drafts,
        "proforma": proforma, "flux": flux, "balance_sheet": by_id["V2"].data, "metrics": by_id["V3"].data["metrics"],
        "stripe": stripe_bridge(d) if d.stripe is not None else None, "not_checked": NOT_CHECKED,
    }
    res["facts"] = _facts(res)
    return res


def write_all(d: Data, out: Path, results: list[CheckResult], exc: dict, ctx: dict) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    res = build_results(d, results, exc, ctx)
    tieouts, drafts, open_ = res["tieouts"], res["drafts"], res["exceptions"]
    flux = res["flux"]
    write_json(out / "results.json", res)
    money_ids = d.ids_for("cash") | d.ids_for("card")
    write_json(out / "snapshot.json", {"period": d.period.key, "accounts": {
        aid: {"tb": d.tb.get(aid, ZERO), "gl_line_ids": [l["line_id"] for l in d.gl if l["account_id"] == aid]}
        for aid in sorted(money_ids)}})
    exc_fields = ["id", "check", "severity", "title", "amount", "detail", "refs", "action", "fingerprint"]
    write_csv(out / "exceptions.csv", open_, exc_fields)
    write_csv(out / "acknowledged.csv", [dict(e, ack_by=e["acknowledged"]["by"], ack_reason=e["acknowledged"]["reason"])
                                         for e in exc["acknowledged"]], exc_fields + ["ack_by", "ack_reason"])
    write_csv(out / "tieouts.csv", tieouts, ["check", "item", "books", "other", "other_source", "difference", "status", "note"])
    write_csv(out / "draft-entries.csv", [dict(l, id=x["id"], check=x["check"], date=x["date"], memo=x["memo"],
                                               status=x["status"], basis=x["basis"]) for x in drafts for l in x["lines"]],
              ["id", "check", "date", "account", "debit", "credit", "memo", "status", "basis"])
    if flux:
        cols = ["account", "section", "current", "prior", "change", "change_pct", "direction", "material", "ytd"]
        if flux["has_budget"]:
            cols += ["budget", "vs_budget", "vs_budget_pct", "budget_direction", "material_budget"]
        if flux["has_ytd_budget"]:
            cols += ["ytd_budget"]
        write_csv(out / "flux.csv", flux["rows"], cols)
    write_csv(out / "bs-flux.csv", res["balance_sheet"]["rows"], ["account", "class", "prior", "current", "change", "reconciled_by"])
    write_csv(out / "outstanding.csv", ctx.get("outstanding", []), ["account_id", "side", "date", "description", "amount", "source_ref"])
    review = render(d, res, results)
    update = render_update(d, res)
    (out / "review.md").write_text(review, encoding="utf-8")
    (out / "investor-update.md").write_text(update, encoding="utf-8")
    write_json(out / ".integrity.json", {"results.json": sha256(out / "results.json"),
                                         "review.md": text_hash(normalized(review, "review.md")),
                                         "investor-update.md": text_hash(normalized(update, "investor-update.md"))})
    return res


def _refs(e) -> str:
    return "; ".join(e["refs"][:6]) + (" ..." if len(e["refs"]) > 6 else "")


def _pct(v):
    return "" if v is None else f"{v}%"


def render(d: Data, res: dict, results: list[CheckResult]) -> str:
    c, m = res["counts"], res["meta"]
    L = [f"# Month-end review: {m['company']}, {d.period.label}", "",
         f"Generated {m['generated_at']} by month-end-review {m['skill_version']}. {m['basis']} basis, fiscal year "
         f"starting month {m['fiscal_year_start_month']}. Read-only: nothing was posted, changed or deleted anywhere.", "",
         f"**{c['checks_run']} checks run · {c['checks_pass']} pass · {c['checks_flag']} need you · {c['checks_not_run']} not run** · "
         f"{c['open_items']} open item(s): {c['open_high']} high, {c['open_medium']} medium, {c['open_low']} low"
         + (f" · {c['acknowledged']} acknowledged" if c["acknowledged"] else ""), ""]

    L += ["## Needs you", ""]
    if not res["exceptions"]:
        L += ["Nothing open. See Coverage for what was and wasn't checked.", ""]
    for e in res["exceptions"]:
        L += [f"**{e['id']} · {e['severity']} · {e['check']}** {e['title']}", "", f"- {e['detail']}",
              f"- Sources: {_refs(e)}", f"- Fix: {e['action']}"]
        if e.get("also"):
            L.append("- Also seen by: " + "; ".join(f"{a['check']} ({a['title']})" for a in e["also"]))
        if not e["can_acknowledge"]:
            L.append("- Can't be acknowledged: something doesn't tie, so it can only be fixed.")
        L.append("")
    if res["acknowledged"]:
        L += ["## Acknowledged (still listed, not counted as open)", ""]
        for e in res["acknowledged"]:
            a = e["acknowledged"]
            L.append(f"- **{e['id']} · {e['check']}** {e['title']}. Acknowledged by {a['by']}: {a['reason']} (until {a['until']})")
        L.append("")

    L += ["## Tie-outs", "", "| Check | Item | Books | Other side | Difference | Status | Note |", "|---|---|---|---|---|---|---|"]
    for t in res["tieouts"]:
        L.append(f"| {t['check']} | {t['item']} | {fmt(t['books'])} | {fmt(t['other'])}: {t['other_source']} | "
                 f"{fmt(t['difference'])} | {t['status']} | {t['note']} |")
    L.append("")

    f = res["flux"]
    L += ["## P&L", ""]
    if not f:
        L += [f"NOT RUN: {next(r.reason for r in results if r.id == 'V1')}", ""]
    else:
        tl = f["totals"]
        hdr = "| | This month | Last month | Change | % |" + (" Budget | vs budget |" if f["has_budget"] else "") + " Year to date |" + (" YTD budget |" if f["has_ytd_budget"] else "")
        L += [hdr, "|" + "---|" * (hdr.count("|") - 1)]
        names = [("revenue", "Revenue"), ("cogs", "Cost of goods sold"), ("gross_profit", "Gross profit"), ("opex", "Operating expenses"),
                 ("operating_income", "Operating income"), ("other_income", "Other income"), ("other_expense", "Other expenses"),
                 ("net_income", "Net income")]
        present = {x["section"] for x in f["rows"]}
        for k, label in names:
            if (k in ("cogs", "gross_profit") and "cogs" not in present) or (k == "other_income" and "other_income" not in present) \
                    or (k == "other_expense" and "other_expense" not in present):
                continue
            t = tl[k]
            row = f"| {label} | {fmt(t['current'])} | {fmt(t['prior'])} | {fmt(t['change'], sign=True)} | {_pct(t['change_pct'])} |"
            if f["has_budget"]:
                row += f" {fmt(t['budget'])} | {fmt(t['vs_budget'], sign=True)} |"
            row += f" {fmt(t['ytd'])} |"
            if f["has_ytd_budget"]:
                row += f" {fmt(t['ytd_budget'])} |"
            L.append(row)
        gm = f["gross_margin_pct"]["current"]
        if f["has_budget"] and f.get("not_in_budget"):
            L += ["", "Accounts with no budget count as $0 budget in the totals above: "
                  + "; ".join(f"{u['account']} {fmt(u['current'])}" for u in f["not_in_budget"]) + "."]
        L += ["", (f"Gross margin {gm}%. " if gm is not None else "") +
              f"Bridge check (line changes add up to the net income change): {'yes' if f['bridge_check']['adds_up'] else 'NO'}.", ""]
        th = f["thresholds"]
        L += [f"**Material lines** (change of {fmt(th['abs_only'])}+, or {fmt(th['abs'])}+ and {th['pct']}%+). "
              "Lines marked *unreliable* are touched by an open item; explain the item, not the variance.", "",
              "| Account | This month | Last month | Change | % | Direction |" + (" Budget | vs budget |" if f["has_budget"] else ""),
              "|---|---|---|---|---|---|" + ("---|---|" if f["has_budget"] else "")]
        for x in f["rows"]:
            if not (x["material"] or x.get("material_budget")):
                continue
            row = (f"| {x['account']} | {fmt(x['current'])} | {fmt(x['prior'])} | {fmt(x['change'], sign=True)} | "
                   f"{_pct(x['change_pct']) or 'new'} | {x['direction']} |")
            if f["has_budget"]:
                row += f" {fmt(x.get('budget')) or 'not in budget'} | {fmt(x.get('vs_budget'), sign=True)} |"
            L.append(row)
        L.append("")

    bs = res["balance_sheet"]
    L += ["## Balance sheet", "", "| Account | Last month | This month | Change | Reconciled by |", "|---|---|---|---|---|"]
    for x in bs["rows"]:
        see = f" (see {', '.join(x['see'])})" if x.get("see") else ""
        L.append(f"| {x['account']} | {fmt(x['prior'])} | {fmt(x['current'])} | {fmt(x['change'], sign=True)} | {x['reconciled_by']}{see} |")
    if bs["unreconciled"]:
        L += ["", "**No check reconciles these balances**: tie them to their source documents: "
              + "; ".join(f"{u['account']} {fmt(u['balance'])}" for u in bs["unreconciled"]) + "."]
    L.append("")

    L += ["## Metrics", "", "| Metric | Value | How it's calculated |", "|---|---|---|"]
    for x in res["metrics"]:
        if x["status"] != "run" or x["value"] is None:
            val = f"NOT RUN: {x['reason']}"
        elif x["unit"] == "usd":
            val = fmt(x["value"])
        elif x["unit"] == "pct":
            val = f"{x['value']}%"
        else:
            val = f"{x['value']} {x['unit']}"
        L.append(f"| {x['label']} | {val} | {x['definition']} |")
    L.append("")

    L += ["## Commentary", "", "<!-- commentary:start -->", PLACEHOLDER, "<!-- commentary:end -->", "",
          "## Questions for the owner", "", "<!-- questions:start -->", PLACEHOLDER, "<!-- questions:end -->", ""]

    pf = res["proforma"]
    L += ["## Draft entries (not posted)", "",
          "Proposals for a person to review. *needs your amount* means the data doesn't decide the amount, so none is given.", ""]
    if pf.get("net_income_after_ready_drafts") is not None:
        L += [f"If every *ready* draft were booked, net income would change by {fmt(pf['net_income_change_from_ready_drafts'], sign=True)} "
              f"to {fmt(pf['net_income_after_ready_drafts'])}. The *needs your amount* drafts would change it further.", ""]
    L += ["| Entry | Check | Account | Debit | Credit | Memo | Status | Basis |", "|---|---|---|---|---|---|---|---|"]
    for x in res["drafts"]:
        for l in x["lines"]:
            L.append(f"| {x['id']} | {x['check']} | {l['account']} | {fmt(l['debit'])} | {fmt(l['credit'])} | {x['memo']} | {x['status']} | {x['basis']} |")
    L.append("")

    L += ["## Coverage", "", "| Check | What it checks | Result |", "|---|---|---|"]
    for r in results:
        result = f"NOT RUN: {r.reason}" if r.status == "not_run" else (r.summary or r.status)
        extra = [f"{p['part']}: {p['status'].upper().replace('_', ' ')}" + (f" ({p['reason']})" if p["reason"] else "")
                 for p in r.parts if p["status"] != "run"]
        if extra:
            result += ". " + "; ".join(extra)
        L.append(f"| {r.id} | {r.title} | {result} |")
    L += ["| V4 | Commentary, questions, investor notes | written by Claude from results.json; every number checked by `mer.py verify` |",
          "", "**Not checked by this skill:**", ""] + [f"- {x}" for x in NOT_CHECKED] + [""]
    return "\n".join(L)


def render_update(d: Data, res: dict) -> str:
    m = {x["id"]: x for x in res["metrics"]}
    c = res["counts"]
    L = [f"# {res['meta']['company']}: {d.period.label} update", ""]
    if c["open_high"] or c["open_medium"]:
        L += [f"**Draft: numbers before month-end adjustments ({c['open_high']} high and {c['open_medium']} medium "
              "items still open in the review).**", ""]
    L += ["| | |", "|---|---|"]
    f = res["flux"]
    for mid in ("revenue", "net_income", "cash", "net_burn", "avg_burn_3m", "runway_months", "mrr", "gross_margin_pct"):
        x = m.get(mid)
        if not x:
            continue
        if x["status"] != "run" or x["value"] is None:
            val = f"not available ({x['reason']})"
        elif x["unit"] == "usd":
            val = fmt(x["value"])
        elif x["unit"] == "pct":
            val = f"{x['value']}%"
        else:
            val = f"{x['value']} {x['unit']}"
        if mid == "revenue" and f:
            t = f["totals"]["revenue"]
            val += f" ({fmt(t['change'], sign=True)} vs last month)"
        L.append(f"| {x['label']} | {val} |")
    L += ["", "_Numbers computed by month-end-review from QuickBooks, Stripe and bank data; definitions in review.md._", "",
          "## Notes", "", "<!-- notes:start -->", PLACEHOLDER, "<!-- notes:end -->", ""]
    return "\n".join(L)


def summary(res: dict) -> str:
    """The chat summary, written from results.json so the chat can't drift from the files."""
    c, m = res["counts"], res["meta"]
    L = [f"{m['company']}, {m['period_label']}: {c['checks_run']} checks run, {c['checks_pass']} pass, {c['checks_flag']} need you, "
         f"{c['checks_not_run']} not run. {c['open_items']} open item(s): {c['open_high']} high, {c['open_medium']} medium, "
         f"{c['open_low']} low" + (f"; {c['acknowledged']} acknowledged" if c["acknowledged"] else "") + ".", ""]
    if res["exceptions"]:
        L.append("Open items (highest first):")
        for e in res["exceptions"]:
            lock = "" if e["can_acknowledge"] else " [can't be acknowledged; only fixed]"
            L.append(f"- {e['id']} [{e['severity']}] {e['title']}{lock}. Fix: {e['action']}")
        L.append("")
    L.append("Tie-outs:")
    for t in res["tieouts"]:
        L.append(f"- {t['check']} {t['item']}: books {fmt(t['books'])}, other side {fmt(t['other'])}, {t['status']}"
                 + (f" ({t['note']})" if t.get("note") else ""))
    unrec = res["balance_sheet"]["unreconciled"]
    if unrec:
        L.append("Not reconciled by any check: " + "; ".join(f"{u['account']} {fmt(u['balance'])}" for u in unrec) + ".")
    L += ["", "Metrics:"]
    for x in res["metrics"]:
        if x["status"] != "run" or x["value"] is None:
            L.append(f"- {x['label']}: NOT RUN ({x['reason']})")
        else:
            v = fmt(x["value"]) if x["unit"] == "usd" else (f"{x['value']}%" if x["unit"] == "pct" else f"{x['value']} {x['unit']}")
            L.append(f"- {x['label']}: {v}")
    pf = res["proforma"]
    if pf.get("net_income_after_ready_drafts") is not None:
        L.append(f"- Net income after the ready drafts: {fmt(pf['net_income_after_ready_drafts'])} "
                 "(drafts that need an amount would change it further)")
    parts = [f"{ch['id']} {p['part']}: {p['status'].upper().replace('_', ' ')}" + (f" ({p['reason']})" if p['reason'] else "")
             for ch in res["checks"] for p in ch["parts"] if p["status"] not in ("run",)]
    parts += [f"{ch['id']} {ch['title']}: NOT RUN ({ch['reason']})" for ch in res["checks"] if ch["status"] == "not_run"]
    if parts:
        L += ["", "Partly or not run:"] + [f"- {x}" for x in parts]
    L += ["", "Not checked by this skill:"] + [f"- {x}" for x in res["not_checked"]]
    return "\n".join(L)
