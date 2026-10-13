"""Load canonical inputs into one object the gate and checks share.

Loading never crashes on bad data: every problem is recorded as a gate error
(G1 missing, G3 wrong shape) and the gate stops the run.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .config import confirmed, resolve_roles
from .inputs import Manifest
from .util import ZERO, Period, csv_header, money, money_or_zero, norm, parse_date, read_csv

REQUIRED_FILES = ["accounts.csv", "tb_current.csv", "tb_prior.csv", "gl.csv", "gl_history.csv", "pnl_monthly.csv",
                  "pnl_ytd.csv", "aging.csv", "bills.csv", "open_bills.csv", "ar_credits.csv"]

COLUMNS = {
    "accounts.csv": ["account_id", "name", "full_name", "account_type", "account_subtype", "classification", "active"],
    "tb_current.csv": ["account_id", "account", "debit", "credit"],
    "tb_prior.csv": ["account_id", "account", "debit", "credit"],
    "gl.csv": ["line_id", "txn_id", "txn_type", "date", "account_id", "account", "name", "memo", "doc_num", "amount"],
    "gl_history.csv": ["line_id", "txn_id", "txn_type", "date", "account_id", "account", "name", "memo", "doc_num", "amount"],
    "pnl_monthly.csv": ["account_id", "month", "amount"],
    "pnl_ytd.csv": ["account_id", "amount"],
    "aging.csv": ["kind", "as_of", "total", "over_90"],
    "bills.csv": ["bill_id", "vendor", "txn_date", "created_date", "account", "amount", "description"],
    "open_bills.csv": ["bill_id", "vendor", "txn_date", "amount", "balance"],
    "ar_credits.csv": ["kind", "id", "customer", "date", "amount"],
    "stripe.csv": ["id", "date", "category", "amount", "fee", "net", "currency"],
    "stripe_invoices.csv": ["invoice_id", "status", "finalized_date", "paid_date", "subtotal_ex_tax", "tax", "total"],
    "stripe_invoice_lines.csv": ["invoice_id", "line_id", "amount", "period_start", "period_end", "subscription"],
    "payroll.csv": ["check_date", "gross", "employer_taxes", "period_start", "period_end"],
    "budget.csv": ["account", "month", "amount"],
    "outstanding_prior.csv": ["account_id", "date", "description", "amount"],
    "bank": ["row", "date", "description", "amount"],
}

PL_CLASSES = ("Revenue", "Expense")


@dataclass
class Data:
    period: Period
    month_dir: Path
    cfg: dict
    manifest: Manifest
    accounts: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)
    role_problems: list = field(default_factory=list)
    tb: dict = field(default_factory=dict)
    tb_prior: dict = field(default_factory=dict)
    tb_totals: tuple = (ZERO, ZERO)
    gl: list = field(default_factory=list)
    gl_history: list = field(default_factory=list)
    pnl_monthly: dict = field(default_factory=dict)       # (account_id, 'YYYY-MM') -> natural amount
    pnl_ytd: dict = field(default_factory=dict)           # account_id -> natural amount
    aging: dict = field(default_factory=dict)             # 'ar'/'ap' -> {total, over_90, as_of}
    bills: list = field(default_factory=list)
    open_bills: list = field(default_factory=list)
    ar_credits: list = field(default_factory=list)
    stripe: list | None = None
    stripe_balance: dict | None = None
    invoices: list | None = None
    invoice_lines: list | None = None
    statements: dict = field(default_factory=dict)        # account_id -> meta + lines + balances
    outstanding_prior: dict = field(default_factory=dict)  # account_id -> {"source": str, "lines": [...]}
    payroll: list | None = None
    payroll_has_periods: bool = False
    budget: list | None = None
    load_errors: list = field(default_factory=list)

    # ---- helpers used by checks
    def name(self, aid: str) -> str:
        return self.accounts.get(aid, {}).get("full_name", aid)

    def cls(self, aid: str) -> str:
        return self.accounts.get(aid, {}).get("classification", "")

    def atype(self, aid: str) -> str:
        return self.accounts.get(aid, {}).get("account_type", "")

    def is_pl(self, aid: str) -> bool:
        return self.cls(aid) in PL_CLASSES

    def ids_for(self, role: str) -> set:
        return self.roles.get(role, set())

    def natural(self, aid: str, debit_positive: Decimal) -> Decimal:
        """P&L natural sign: revenue positive, expense positive."""
        return -debit_positive if self.cls(aid) == "Revenue" else debit_positive

    def movement(self, lines: list, aid: str) -> Decimal:
        return sum((l["amount"] for l in lines if l["account_id"] == aid), ZERO)

    def history_months(self) -> list[str]:
        n = int(self.cfg["thresholds"]["history_months"])
        return [self.period.shift(-k).key for k in range(n, 0, -1)]

    def monthly_pl(self) -> dict:
        """{month: {account_id: natural amount}} for P&L accounts, from GL + history."""
        out: dict = defaultdict(lambda: defaultdict(lambda: ZERO))
        for l in self.gl_history + self.gl:
            if self.is_pl(l["account_id"]):
                out[l["date"].strftime("%Y-%m")][l["account_id"]] += self.natural(l["account_id"], l["amount"])
        return out

    def payroll_ids(self) -> set:
        ids = self.ids_for("payroll_wages") | self.ids_for("payroll_taxes")
        ids |= {a for a, x in self.accounts.items() if "payroll" in (x.get("account_subtype") or "").lower()}
        return ids


def _shape(path: Path, name: str, err: list) -> bool:
    need = COLUMNS.get(name, [])
    have = [h.lower() for h in csv_header(path)]
    missing = [c for c in need if c.lower() not in have]
    if missing:
        err.append(("G3", f"{name} is missing column(s) {missing}."))
        return False
    return True


def _rows(path: Path, name: str, err: list, conv) -> list:
    """Read a canonical CSV, converting each row with conv(row) -> row. Bad rows become G3 errors."""
    out = []
    if not _shape(path, name, err):
        return out
    for i, r in enumerate(read_csv(path), 2):
        r = {(k or "").strip().lower(): (v or "") for k, v in r.items()}
        try:
            out.append(conv(r))
        except (ValueError, KeyError) as e:
            err.append(("G3", f"{name} row {i}: {e}"))
            if len([x for x in err if x[0] == "G3"]) > 50:
                break
    return out


def _gl(r):
    r["date"] = parse_date(r["date"])
    r["amount"] = money(r["amount"])
    if not r["txn_id"] or not r["account_id"]:
        raise ValueError("missing txn_id or account_id")
    return r


def load(month_dir: Path, period: Period, cfg: dict) -> Data:
    inp = month_dir / "inputs"
    d = Data(period=period, month_dir=month_dir, cfg=cfg, manifest=Manifest(month_dir, period))
    err = d.load_errors

    why = confirmed(cfg)
    if why:
        err.append(("G1", why))
    for f in REQUIRED_FILES:
        if not (inp / f).exists():
            err.append(("G1", f"{f} is missing."))
    if not (inp / "accounts.csv").exists() or not _shape(inp / "accounts.csv", "accounts.csv", err):
        return d

    d.accounts = {a["account_id"]: a for a in read_csv(inp / "accounts.csv")}
    d.roles, d.role_problems = resolve_roles(cfg, list(d.accounts.values()))

    for fname, target in (("tb_current.csv", "tb"), ("tb_prior.csv", "tb_prior")):
        if not (inp / fname).exists():
            continue
        rows = _rows(inp / fname, fname, err, lambda r: dict(r, debit=money_or_zero(r["debit"]),
                                                              credit=money_or_zero(r["credit"])))
        bal = {}
        for r in rows:
            bal[r["account_id"]] = bal.get(r["account_id"], ZERO) + r["debit"] - r["credit"]
        setattr(d, target, bal)
        if target == "tb":
            d.tb_totals = (sum((r["debit"] for r in rows), ZERO), sum((r["credit"] for r in rows), ZERO))

    if (inp / "gl.csv").exists():
        d.gl = _rows(inp / "gl.csv", "gl.csv", err, _gl)
    if (inp / "gl_history.csv").exists():
        d.gl_history = _rows(inp / "gl_history.csv", "gl_history.csv", err, _gl)
    if (inp / "pnl_monthly.csv").exists():
        for r in _rows(inp / "pnl_monthly.csv", "pnl_monthly.csv", err, lambda r: dict(r, amount=money(r["amount"]))):
            d.pnl_monthly[(r["account_id"], r["month"])] = d.pnl_monthly.get((r["account_id"], r["month"]), ZERO) + r["amount"]
    if (inp / "pnl_ytd.csv").exists():
        for r in _rows(inp / "pnl_ytd.csv", "pnl_ytd.csv", err, lambda r: dict(r, amount=money(r["amount"]))):
            d.pnl_ytd[r["account_id"]] = d.pnl_ytd.get(r["account_id"], ZERO) + r["amount"]
    if (inp / "aging.csv").exists():
        for r in _rows(inp / "aging.csv", "aging.csv", err,
                       lambda r: dict(r, total=money(r["total"]), over_90=money_or_zero(r["over_90"]),
                                      as_of=parse_date(r["as_of"]))):
            d.aging[r["kind"]] = r
    if (inp / "bills.csv").exists():
        d.bills = _rows(inp / "bills.csv", "bills.csv", err, lambda r: dict(
            r, txn_date=parse_date(r["txn_date"]), amount=money(r["amount"]),
            created_date=parse_date(r["created_date"]) if r["created_date"] else None))
    if (inp / "open_bills.csv").exists():
        d.open_bills = _rows(inp / "open_bills.csv", "open_bills.csv", err, lambda r: dict(
            r, txn_date=parse_date(r["txn_date"]), amount=money(r["amount"]), balance=money(r["balance"])))
    if (inp / "ar_credits.csv").exists():
        d.ar_credits = _rows(inp / "ar_credits.csv", "ar_credits.csv", err, lambda r: dict(
            r, date=parse_date(r["date"]), amount=money(r["amount"])))

    if (inp / "stripe.csv").exists():
        d.stripe = _rows(inp / "stripe.csv", "stripe.csv", err, lambda r: dict(
            r, date=parse_date(r["date"]), amount=money(r["amount"]), fee=money(r["fee"]), net=money(r["net"]),
            category=r["category"].lower()))
        sb = d.manifest.data.get("stripe_balance")
        if sb:
            try:
                d.stripe_balance = {"opening": money(sb["opening"]), "closing": money(sb["closing"]),
                                    "source": sb.get("source", "")}
            except (ValueError, KeyError) as e:
                err.append(("G3", f"manifest stripe_balance: {e}"))
    if (inp / "stripe_invoices.csv").exists() and (inp / "stripe_invoice_lines.csv").exists():
        dt = lambda v: parse_date(v) if v else None
        d.invoices = _rows(inp / "stripe_invoices.csv", "stripe_invoices.csv", err, lambda r: dict(
            r, finalized_date=dt(r["finalized_date"]), paid_date=dt(r["paid_date"]),
            subtotal_ex_tax=money(r["subtotal_ex_tax"]), tax=money(r["tax"]), total=money(r["total"])))
        d.invoice_lines = _rows(inp / "stripe_invoice_lines.csv", "stripe_invoice_lines.csv", err, lambda r: dict(
            r, amount=money(r["amount"]), period_start=dt(r["period_start"]), period_end=dt(r["period_end"]),
            subscription=r["subscription"] == "yes", interval=r.get("interval", ""),
            interval_count=r.get("interval_count", "")))

    for aid, meta in d.manifest.statements.items():
        p = inp / meta.get("file", "")
        if not meta.get("file") or not p.exists():
            err.append(("G1", f"statement file for {meta.get('account', aid)} is listed but missing."))
            continue
        lines = _rows(p, "bank", err, lambda r: dict(r, date=parse_date(r["date"]), amount=money(r["amount"])))
        try:
            kind = meta["kind"]
            sign = -1 if kind == "card" else 1
            open_dp, end_dp = sign * money(meta["opening_balance"]), sign * money(meta["ending_balance"])
            s_start, s_end = parse_date(meta["statement_start"]), parse_date(meta["statement_end"])
        except (ValueError, KeyError) as e:
            err.append(("G3", f"manifest statement for {meta.get('account', aid)}: {e}"))
            continue
        after = sum((l["amount"] for l in lines if l["date"] > s_end), ZERO)
        d.statements[aid] = dict(meta, lines=lines, kind=kind, open_dp=open_dp, end_dp=end_dp,
                                 statement_start=s_start, statement_end=s_end,
                                 month_end_dp=end_dp + after,
                                 window_sum=sum((l["amount"] for l in lines if l["date"] <= s_end), ZERO))

    # Last month's uncleared items, per account: explicit file > attestation > last month's review output.
    explicit = {}
    if (inp / "outstanding_prior.csv").exists():
        for r in _rows(inp / "outstanding_prior.csv", "outstanding_prior.csv", err, lambda r: dict(
                r, date=parse_date(r["date"]), amount=money(r["amount"]))):
            explicit.setdefault(r["account_id"], []).append(r)
    explicit_ids = set((d.manifest.data.get("outstanding_prior") or {}).keys())
    prior_dir = month_dir.parent / period.prior.key
    prior_out = prior_dir / "output" / "outstanding.csv"
    carried, snapshot, prior_statements = {}, {}, {}
    if prior_out.exists():
        for r in _rows(prior_out, "outstanding_prior.csv", err, lambda r: dict(
                r, date=parse_date(r["date"]), amount=money(r["amount"]))):
            carried.setdefault(r["account_id"], []).append(r)
        try:
            snapshot = json.loads((prior_dir / "output" / "snapshot.json").read_text()) if (prior_dir / "output" / "snapshot.json").exists() else {}
            pm = prior_dir / "inputs" / "manifest.json"
            prior_statements = (json.loads(pm.read_text()).get("statements") or {}) if pm.exists() else {}
        except json.JSONDecodeError:
            err.append(("G3", f"last month's review files in {prior_dir} are not valid JSON."))
    tag = f"h{period.prior.key}-"
    core = lambda lid: lid[len(tag):] if lid.startswith(tag) else lid
    for aid in d.ids_for("cash") | d.ids_for("card"):
        if aid in explicit_ids:
            d.outstanding_prior[aid] = {"source": "provided", "lines": explicit.get(aid, [])}
        elif f"outstanding:{aid}" in d.manifest.attestations:
            by = d.manifest.attestations[f"outstanding:{aid}"]["by"]
            d.outstanding_prior[aid] = {"source": f"none, confirmed by {by}", "lines": []}
        elif prior_out.exists() and aid in prior_statements:
            lines = list(carried.get(aid, []))
            note = "last month's review"
            snap = (snapshot.get("accounts") or {}).get(aid)
            has_prior_month = any(l["date"].strftime("%Y-%m") == period.prior.key for l in d.gl_history)
            if snap is not None and has_prior_month:
                then = set(snap.get("gl_line_ids", []))
                hist_prior = [l for l in d.gl_history if l["account_id"] == aid and l["date"].strftime("%Y-%m") == period.prior.key]
                now = {core(l["line_id"]) for l in hist_prior}
                removed = then - now
                kept = [l for l in lines if not (str(l.get("source_ref", "")).startswith("GL ") and l["source_ref"][3:] in removed)]
                added = [{"account_id": aid, "side": "book", "date": l["date"], "description": l["name"] or l["memo"],
                          "amount": l["amount"], "txn_id": l["txn_id"], "doc_num": l["doc_num"],
                          "source_ref": f"GL {l['line_id']} (booked into {period.prior.key} after its review)"}
                         for l in hist_prior if core(l["line_id"]) not in then]
                if added or len(kept) != len(lines):
                    note += f" (+{len(added)} entr(ies) booked into {period.prior.key} after it was reviewed"
                    note += f", {len(lines) - len(kept)} voided)" if len(kept) != len(lines) else ")"
                lines = kept + added
            d.outstanding_prior[aid] = {"source": note, "lines": lines}

    if (inp / "payroll.csv").exists():
        dt = lambda v: parse_date(v) if v else None
        d.payroll = _rows(inp / "payroll.csv", "payroll.csv", err, lambda r: dict(
            r, check_date=parse_date(r["check_date"]), gross=money(r["gross"]),
            employer_taxes=money(r["employer_taxes"]), period_start=dt(r["period_start"]), period_end=dt(r["period_end"])))
        d.payroll_has_periods = bool(d.payroll) and all(p["period_start"] and p["period_end"] for p in d.payroll)
    if (inp / "budget.csv").exists():
        by_name = {norm(a["full_name"]): k for k, a in d.accounts.items()}
        short_counts = defaultdict(list)
        for k, a in d.accounts.items():
            short_counts[norm(a["name"])].append(k)
        by_short = {n: ks[0] for n, ks in short_counts.items() if len(ks) == 1}
        raw = _rows(inp / "budget.csv", "budget.csv", err, lambda r: dict(r, amount=money(r["amount"])))
        d.budget = []
        for r in raw:
            aid = r.get("account_id") or by_name.get(norm(r["account"])) or by_short.get(norm(r["account"]))
            if not aid:
                err.append(("G3", f'budget.csv account "{r["account"]}" is not in the chart of accounts '
                                  "(or the short name matches more than one account; use the full name)."))
                continue
            d.budget.append({"account_id": aid, "month": r["month"][:7], "amount": r["amount"]})
    return d
