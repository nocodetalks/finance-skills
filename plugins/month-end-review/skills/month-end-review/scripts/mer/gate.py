"""The gate (G1-G5). Runs before any check. If anything fails, the run stops.

G1 everything required is here · G2 every file is for the right dates (contents, not
just labels) · G3 every file has the right shape · G4 everything adds up (trial
balance, transactions, history vs P&L, statements, Stripe) · G5 empty means missing.
"""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal

from .data import Data
from .qbo import fy_start
from .util import ZERO, add_days, fmt

TOL = Decimal("0.01")


def run_gate(d: Data) -> list[dict]:
    """Return a list of failures: {gate, problem, fix}. Empty list = pass."""
    fails: list[dict] = []

    def stop(gate, problem, fix):
        fails.append({"gate": gate, "problem": problem, "fix": fix})

    p, cfg, man = d.period, d.cfg, d.manifest
    f, att = man.files, man.attestations

    # ---------------- G1: everything required is here
    for gate, msg in d.load_errors:
        if gate == "G1":
            stop("G1", msg, _fix_for(msg))
    for prob in d.role_problems:
        stop("G1", prob, "Fix review-config.json so each role names accounts from the chart of accounts, then confirm "
                         "the roles again (`mer.py confirm-roles --by NAME`).")
    if not d.accounts:
        return fails
    pull = man.data.get("qbo_pull") or {}
    if pull.get("complete") is not True:
        stop("G1", "The QuickBooks pull for this month did not finish.", "Run `mer.py pull-qbo` again.")
    for name in ("tb_current.csv", "tb_prior.csv", "gl.csv", "gl_history.csv", "pnl_monthly.csv", "pnl_ytd.csv",
                 "aging.csv", "bills.csv"):
        if (d.month_dir / "inputs" / name).exists() and name not in f:
            stop("G1", f"{name} is in the folder but the manifest has no record of where it came from.",
                 "Pull it again so its source and dates are recorded.")

    money_accts = d.ids_for("cash") | d.ids_for("card")
    listed = set(man.statements)
    for aid in sorted(money_accts, key=d.name):
        if aid not in d.statements and aid not in listed:
            stop("G1", f"No statement for {d.name(aid)}.",
                 f"Download {d.name(aid)}'s transactions as CSV and run `mer.py ingest-statement --account "
                 f"\"{d.name(aid)}\" --file <csv> --ending-balance <amount> --statement-end <date>` with the balance "
                 "from the statement or portal.")
        if aid not in d.outstanding_prior:
            stop("G1", f"Nothing says which {d.name(aid)} items were still uncleared at {p.prior.end}.",
                 "This is the first review for this account. Export the uncleared items from your last bank "
                 f"reconciliation and run `mer.py ingest-outstanding --account \"{d.name(aid)}\" --file <csv>`, or if "
                 f"there were none, `mer.py attest --source \"outstanding:{d.name(aid)}\" --by <name>`.")
    for aid, s in d.statements.items():
        if aid not in money_accts:
            stop("G1", f"There is a statement for {s.get('account', aid)}, which is not in roles.cash or roles.card.",
                 "Add the account to a role (and confirm the roles), or delete its statement from inputs/bank/.")
    covered = money_accts | d.ids_for("processor_clearing")
    for aid, a in d.accounts.items():
        if a["account_type"] in ("Bank", "Credit Card") and aid not in covered:
            if d.tb.get(aid, ZERO) != 0 or any(l["account_id"] == aid for l in d.gl):
                stop("G1", f"{a['full_name']} has a balance or activity but is in no role.",
                     "Add it to roles.cash, roles.card or roles.processor_clearing so it is reconciled.")
    if d.ids_for("processor_clearing") and not cfg.get("uses_stripe"):
        stop("G1", "roles.processor_clearing is set but uses_stripe is false.",
             "A clearing account is reconciled to the Stripe balance, so Stripe data is needed. Set uses_stripe.")
    if cfg.get("uses_stripe"):
        if d.stripe is None:
            stop("G1", "Stripe is in use but stripe.csv is missing.",
                 "Run `mer.py pull-stripe` with a restricted read-only key, or export Stripe's balance transactions "
                 "(including payouts) and run `mer.py ingest-stripe`.")
        elif d.stripe_balance is None:
            stop("G1", "The Stripe opening and closing balances for the month are missing.",
                 "Re-run `mer.py pull-stripe`, or pass --opening-balance and --closing-balance to ingest-stripe "
                 "from Stripe's Balance report.")
    if cfg.get("uses_payroll_file") and d.payroll is None:
        stop("G1", "uses_payroll_file is true but payroll.csv is missing.",
             "Export the payroll register for the month and run `mer.py ingest-payroll`, or set uses_payroll_file "
             "to false.")
    if (d.month_dir / "inputs" / "aging.csv").exists():
        for kind in ("ar", "ap"):
            if kind not in d.aging:
                stop("G1", f"aging.csv has no {kind.upper()} total.", "Pull QuickBooks again.")
    if cfg.get("uses_payroll_file") and d.payroll_has_periods and "payroll-coverage" not in att:
        last = max(x["period_end"] for x in d.payroll)
        if last < p.end:
            stop("G1", f"The payroll file's pay periods end {last}, before {p.end}, so wages earned at the end of "
                       f"{p.label} can't be checked.",
                 "Export the payroll register through the first pay run after month-end. If no one was paid for the "
                 "rest of the month, `mer.py attest --source payroll-coverage --by <name>`.")

    # ---------------- G2: the right dates, in the contents and the labels
    for name, as_of in (("tb_current.csv", p.end), ("tb_prior.csv", p.prior.end)):
        meta = f.get(name, {})
        if meta and (meta.get("as_of") != as_of.isoformat() or (meta.get("report_end") and meta["report_end"] != as_of.isoformat())):
            stop("G2", f"{name} is as of {meta.get('report_end') or meta.get('as_of')}, not {as_of}.", "Pull it again.")
    gm = f.get("gl.csv", {})
    if gm and (gm.get("start") != p.start.isoformat() or gm.get("end") != p.end.isoformat()):
        stop("G2", f"gl.csv was pulled for {gm.get('start')} to {gm.get('end')}, not {p.start} to {p.end}.", "Pull it again.")
    outside = [l for l in d.gl if not p.contains(l["date"])]
    if outside:
        stop("G2", f"gl.csv has {len(outside)} lines dated outside {p.label} (first: {outside[0]['date']}).",
             "Pull the general ledger for exactly this month.")
    want = d.history_months()
    hm = f.get("gl_history.csv", {})
    if hm and hm.get("months") != want:
        stop("G2", f"gl_history.csv covers {hm.get('months')}, expected {want}.", "Pull it again.")
    bad = [l for l in d.gl_history if l["date"].strftime("%Y-%m") not in want]
    if bad:
        stop("G2", f"gl_history.csv has {len(bad)} lines outside {want[0]} to {want[-1]}.", "Pull it again.")
    ym = f.get("pnl_ytd.csv", {})
    ys = fy_start(p, cfg["fiscal_year_start_month"])
    if ym and (ym.get("start") != ys.isoformat() or ym.get("end") != p.end.isoformat()):
        stop("G2", f"pnl_ytd.csv covers {ym.get('start')} to {ym.get('end')}, not the fiscal year to date "
                   f"({ys} to {p.end}).", "Check fiscal_year_start_month, then pull again.")
    if (man.data.get("qbo_pull") or {}).get("basis") not in (None, cfg["basis"]):
        stop("G2", f"QuickBooks was pulled on a {man.data['qbo_pull']['basis']} basis but the config says {cfg['basis']}.",
             "Pull again after fixing basis in review-config.json.")
    for kind, row in d.aging.items():
        if row["as_of"] != p.end:
            stop("G2", f"The {kind.upper()} aging is as of {row['as_of']}, not {p.end}.", "Pull it again.")
    to = add_days(p.end, 20)
    odd = [b for b in d.bills if not (p.start <= b["txn_date"] <= to)]
    if odd:
        stop("G2", f"bills.csv has {len(odd)} bill(s) dated outside {p.start} to {to}.", "Pull it again.")
    if d.stripe is not None:
        sm = f.get("stripe.csv", {})
        if sm.get("start") != p.start.isoformat() or sm.get("end") != p.end.isoformat():
            stop("G2", f"stripe.csv was prepared for {sm.get('start')} to {sm.get('end')}, not {p.label}.",
                 "Pull or export Stripe for exactly this month.")
        out_s = [r for r in d.stripe if not p.contains(r["date"])]
        if out_s:
            stop("G2", f"stripe.csv has {len(out_s)} rows dated outside {p.label} (UTC) (first: {out_s[0]['date']}).",
                 "Export exactly this month, in UTC.")
    if d.payroll is not None:
        pm = f.get("payroll.csv", {})
        if pm.get("start") != p.start.isoformat():
            stop("G2", f"payroll.csv was prepared for a month starting {pm.get('start')}, not {p.start}.",
                 "Ingest the payroll export again for this month.")
        odd = [x for x in d.payroll if not (p.start <= x["check_date"] <= add_days(p.end, 35))]
        if odd:
            stop("G2", f"payroll.csv has {len(odd)} pay run(s) dated outside this month and the 35 days after.",
                 "Ingest the payroll export again for this month.")
    for aid, s in d.statements.items():
        acct = s.get("account", aid)
        if not p.contains(s["statement_end"]):
            stop("G2", f"The {acct} statement ends {s['statement_end']}, outside {p.label}.", "Ingest the right statement.")
        prior = _prior_statement_end(d, aid)
        if prior and prior != s["statement_start"].isoformat():
            stop("G2", f"The {acct} statement starts after {s['statement_start']}, but last month's ended {prior}. "
                       "Statements must follow on with no gap or overlap.",
                 "Ingest the statement that starts where last month's ended.")
    for aid, info in d.outstanding_prior.items():
        late = [l for l in info["lines"] if l["date"] > p.prior.end]
        if late:
            stop("G2", f"{len(late)} outstanding item(s) for {d.name(aid)} are dated after {p.prior.end}.",
                 "Outstanding items must be from last month or earlier.")

    # ---------------- G3: the right shape
    for gate, msg in d.load_errors:
        if gate == "G3":
            stop("G3", msg, "Re-export or pull the file. A file with the wrong shape counts as missing.")

    # ---------------- G4: everything adds up
    dr, cr = d.tb_totals
    if abs(dr - cr) > TOL:
        stop("G4", f"tb_current.csv does not balance: debits {fmt(dr)}, credits {fmt(cr)}.", "Pull it again.")
    ids = set(d.tb) | set(d.tb_prior) | {l["account_id"] for l in d.gl}
    broken = []
    for aid in sorted(ids):
        if d.is_pl(aid) or d.accounts.get(aid, {}).get("account_subtype") == "RetainedEarnings":
            continue
        tb_move = d.tb.get(aid, ZERO) - d.tb_prior.get(aid, ZERO)
        gl_move = d.movement(d.gl, aid)
        if abs(tb_move - gl_move) > TOL:
            broken.append(f"{d.name(aid)}: trial balance moved {fmt(tb_move)}, ledger lines add to {fmt(gl_move)}")
    if broken:
        stop("G4", f"The general ledger doesn't match the trial balance movement for {len(broken)} account(s): "
                   + "; ".join(broken[:6]) + (" ..." if len(broken) > 6 else ""),
             "The export is cut off or mixes periods. Pull again. If it still fails, something was posted into a "
             "closed period: look before going further.")
    for label, lines in (("gl.csv", d.gl), ("gl_history.csv", d.gl_history)):
        sums = defaultdict(lambda: ZERO)
        for l in lines:
            sums[l["txn_id"]] += l["amount"]
        unbalanced = [t for t, v in sums.items() if abs(v) > TOL]
        if unbalanced:
            stop("G4", f"{len(unbalanced)} transaction(s) in {label} don't balance (debits ≠ credits), e.g. "
                       f"{', '.join(unbalanced[:5])}. Lines are missing or a sign was read wrong.",
                 "Pull again. If it persists, report it: the export is incomplete.")
    if d.pnl_monthly or d.gl or d.gl_history:
        mpl = d.monthly_pl()
        months = want + [p.key]
        pnl_months = {m for (_, m) in d.pnl_monthly}
        bad = []
        for m in months:
            accts = {a for (a, mm) in d.pnl_monthly if mm == m} | set(mpl.get(m, {}))
            for aid in accts:
                gl_v = mpl.get(m, {}).get(aid, ZERO)
                rep_v = d.pnl_monthly.get((aid, m), ZERO)
                if abs(gl_v - rep_v) > TOL:
                    bad.append(f"{m} {d.name(aid)}: ledger {fmt(gl_v)}, P&L report {fmt(rep_v)}")
        if bad:
            stop("G4", f"The ledger doesn't match the P&L report for {len(bad)} account-month(s): "
                       + "; ".join(bad[:6]) + (" ..." if len(bad) > 6 else ""),
                 "History or this month's ledger is incomplete, or a sign was read wrong. Pull again.")
        if not pnl_months and (d.gl or d.gl_history):
            stop("G4", "pnl_monthly.csv is empty, so the ledger can't be proven complete.", "Pull again.")
    for aid, s in d.statements.items():
        acct = s.get("account", aid)
        got = s["open_dp"] + s["window_sum"]
        if abs(got - s["end_dp"]) > TOL:
            stop("G4", f"The {acct} rows don't add up: opening balance + transactions = {fmt(_show(s, got))}, but the "
                       f"statement says {fmt(_show(s, s['end_dp']))} at {s['statement_end']} "
                       f"(off by {fmt(abs(got - s['end_dp']))}).",
                 "The CSV is missing rows (or has extra ones), the sign setting (flip_sign) is wrong, or a balance "
                 "was typed wrong. Fix the export or the balance and ingest again.")
    if d.stripe is not None and d.stripe_balance is not None:
        net = sum((r["net"] for r in d.stripe), ZERO)
        o, c = d.stripe_balance["opening"], d.stripe_balance["closing"]
        if o < 0 or c < 0:
            stop("G4", f"The Stripe balance comes out negative ({fmt(o)} at the start, {fmt(c)} at the end).",
                 "Transactions are missing from the Stripe data. Pull again (or re-export).")
        prior_close = _prior_stripe_close(d)
        if prior_close is not None and abs(prior_close - o) > TOL:
            stop("G4", f"Stripe's opening balance ({fmt(o)}) doesn't match last month's closing balance ({fmt(prior_close)}).",
                 "One of the two months' Stripe data is incomplete. Pull both again.")
        if abs(o + net - c) > TOL:
            no_payouts = not any(r["category"] in ("payout", "payout_reversal") for r in d.stripe)
            stop("G4", f"Stripe doesn't add up: opening {fmt(o)} + net activity {fmt(net)} = {fmt(o + net)}, but the "
                       f"closing balance is {fmt(c)}." + (" The export has no payout rows." if no_payouts else ""),
                 "The Stripe export is incomplete (use the balance-transactions export that includes payouts, for the "
                 "whole month in UTC) or a balance was typed wrong.")

    # ---------------- G5: an empty source is missing, not zero
    if not d.gl and (d.month_dir / "inputs" / "gl.csv").exists():
        stop("G5", f"gl.csv has no lines for {p.label}.", "A month with no transactions is almost certainly a bad pull.")
    have = {l["date"].strftime("%Y-%m") for l in d.gl_history}
    for m in want:
        if m not in have and f"history:{m}" not in att:
            stop("G5", f"The ledger history has no transactions in {m}.",
                 f"Pull again. If the company didn't exist or had no activity then, run "
                 f"`mer.py attest --source history:{m} --by <name>`, or lower thresholds.history_months.")
    if d.stripe is not None and not d.stripe and "stripe" not in att:
        stop("G5", f"Stripe has no transactions for {p.label}.",
             "Check the date range and key. If there truly was no activity, `mer.py attest --source stripe --by <name>`.")
    for aid, s in d.statements.items():
        if not any(l["date"] <= s["statement_end"] for l in s["lines"]) and f"statement:{aid}" not in att:
            stop("G5", f"The {s.get('account', aid)} statement has no transactions.",
                 "Check the export's dates. If there was truly no activity, "
                 f"`mer.py attest --source \"statement:{s.get('account', aid)}\" --by <name>`.")
    if cfg.get("uses_payroll_file") and d.payroll is not None and not any(p.contains(x["check_date"]) for x in d.payroll) \
            and "payroll" not in att:
        stop("G5", f"payroll.csv has no pay run in {p.label}.",
             "Check the export. If no payroll ran this month, `mer.py attest --source payroll --by <name>`.")
    if not d.bills and (d.month_dir / "inputs" / "bills.csv").exists() and "bills" not in att:
        stop("G5", f"QuickBooks returned no bills dated {p.start} to {to}.",
             "If the company really enters no bills (everything on cards), `mer.py attest --source bills --by <name>`.")
    return fails


def _prior_stripe_close(d: Data):
    p = d.month_dir.parent / d.period.prior.key / "inputs" / "manifest.json"
    if not p.exists():
        return None
    try:
        sb = json.loads(p.read_text()).get("stripe_balance") or {}
        from .util import money
        return money(sb["closing"]) if "closing" in sb else None
    except (json.JSONDecodeError, ValueError):
        return None


def _show(s, dp):
    return -dp if s["kind"] == "card" else dp


def _prior_statement_end(d: Data, aid: str):
    p = d.month_dir.parent / d.period.prior.key / "inputs" / "manifest.json"
    if not p.exists():
        return None
    try:
        return ((json.loads(p.read_text()).get("statements") or {}).get(aid) or {}).get("statement_end")
    except json.JSONDecodeError:
        return None


def _fix_for(msg: str) -> str:
    if msg.startswith("statement file for"):
        return ("Export that account's transactions for the month as CSV again and re-run `mer.py ingest-statement` "
                "with the ending balance from the statement or portal.")
    if "confirm" in msg:
        return ("Show the person the roles in review-config.json, correct them, and once they agree run "
                "`mer.py confirm-roles --by <their name>`.")
    return "Pull it again (`mer.py pull-qbo`) or provide the file."


def gate_report(d: Data, fails: list[dict]) -> str:
    lines = [f"# Stopped: {d.cfg.get('company', '')} {d.period.label}", "",
             "The review did not run. Nothing was checked and no review, entries or commentary were written, because "
             "required inputs are missing, from the wrong dates, the wrong shape, don't add up, or are empty. "
             "Nothing was assumed or filled in.", "", "## What to fix", ""]
    for i, f in enumerate(fails, 1):
        lines += [f"{i}. **[{f['gate']}] {f['problem']}**", f"   - Fix: {f['fix']}", ""]
    lines += ["## Then", "", f"Run `mer.py run --month {d.period.key}` again.", ""]
    return "\n".join(lines)
