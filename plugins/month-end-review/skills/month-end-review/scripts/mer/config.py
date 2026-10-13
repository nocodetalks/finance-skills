"""review-config.json: account roles, file formats, settings and thresholds.

The skill proposes a config from the chart of accounts and QuickBooks preferences.
A person confirms the roles (`mer.py confirm-roles --by NAME`). Until then nothing runs.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .util import StopRun, norm, read_json, to_bool, write_json

ROLES = {
    # role: (required?, meaning)
    "cash": (True, "bank accounts (checking, savings, treasury)"),
    "card": (True, "credit card accounts ([] if none)"),
    "processor_clearing": (False, "a Stripe clearing account that mirrors the Stripe balance (Synder, A2X, QuickBooks' Stripe app)"),
    "ar": (True, "accounts receivable ([] if none)"),
    "ap": (True, "accounts payable ([] if none)"),
    "undeposited_funds": (False, "Undeposited Funds / Payments to deposit"),
    "deferred_revenue": (True, "deferred / unearned revenue ([] if none)"),
    "prepaid": (True, "prepaid expenses ([] if none)"),
    "accrued": (True, "accrued liabilities ([] if none)"),
    "accrued_payroll": (False, "accrued wages / payroll"),
    "stripe_revenue": (False, "income accounts Stripe sales are booked to"),
    "stripe_fees": (False, "expense account for Stripe fees"),
    "refunds": (False, "contra-revenue account for refunds"),
    "sales_tax_payable": (False, "sales tax payable (Stripe Tax users)"),
    "payroll_wages": (False, "wage and salary expense accounts"),
    "payroll_taxes": (False, "employer payroll tax expense accounts"),
    "loans": (False, "loans / venture debt"),
    "loan_interest": (False, "interest expense on those loans"),
}

DEFAULT_THRESHOLDS = {
    "materiality_abs": "1000",
    "materiality_pct": "10",
    "materiality_abs_only": "5000",
    "tie_tolerance": "1.00",
    "match_window_days": 5,
    "duplicate_window_days": 5,
    "duplicate_min_amount": "100",
    "bill_expense_window_days": 60,
    "history_months": 6,
    "recurring_min_months": 4,
    "recurring_min_amount": "200",
    "prepaid_min_amount": "3000",
    "amortization_floor": "100",
    "range_floor": "1000",
    "range_slack_pct": "25",
    "annual_revenue_min": "3000",
    "unusual_inflow_min": "25000",
    "drift_min_history_lines": 3,
    "deferral_min_period_days": 45,
    "ack_months": 3,
}
INT_KEYS = {k for k, v in DEFAULT_THRESHOLDS.items() if isinstance(v, int)}

BOOL_KEYS = ["uses_stripe", "uses_payroll_file", "stripe_billing_via_ar", "uses_stripe_tax"]

SUSPENSE_PATTERN = re.compile(r"uncategori[sz]ed|ask my accountant|suspense|opening balance equity", re.I)


def config_path(root: Path) -> Path:
    return root / "review-config.json"


def load(root: Path) -> dict:
    p = config_path(root)
    if not p.exists():
        raise StopRun(
            f"No config at {p}.\nRun `mer.py init` after pulling QuickBooks, review the proposed account roles "
            "with a person, then `mer.py confirm-roles --by NAME`. Nothing runs until then."
        )
    cfg = read_json(p)
    if not isinstance(cfg, dict):
        raise StopRun(f"{p} must be a JSON object.")
    for k, typ in (("roles", dict), ("bank_formats", dict), ("payroll_format", dict), ("thresholds", dict),
                   ("accrual_behavior", dict)):
        if cfg.get(k) is not None and not isinstance(cfg[k], typ):
            raise StopRun(f'"{k}" in review-config.json must be a JSON object ({{...}}).')
    for k in BOOL_KEYS:
        cfg[k] = to_bool(cfg.get(k, False), k)
    basis = cfg.get("basis")
    if basis not in ("Accrual", "Cash"):
        raise StopRun('"basis" in review-config.json must be "Accrual" or "Cash".')
    fy = cfg.get("fiscal_year_start_month")
    if not isinstance(fy, int) or isinstance(fy, bool) or not 1 <= fy <= 12:
        raise StopRun('"fiscal_year_start_month" in review-config.json must be a number from 1 to 12.')
    th = dict(DEFAULT_THRESHOLDS)
    th.update(cfg.get("thresholds") or {})
    norm_th = {}
    for k, v in th.items():
        if k not in DEFAULT_THRESHOLDS:
            raise StopRun(f'Unknown threshold "{k}" in review-config.json.')
        try:
            if k in INT_KEYS:
                if isinstance(v, bool) or int(v) != Decimal(str(v)) or int(v) < 0:
                    raise ValueError
                norm_th[k] = int(v)
            else:
                d = Decimal(str(v))
                if not d.is_finite() or d < 0:
                    raise ValueError
                norm_th[k] = d
        except (ValueError, InvalidOperation):
            raise StopRun(f'Threshold "{k}" in review-config.json must be a non-negative number, got {v!r}.')
    if norm_th["ack_months"] < 1:
        raise StopRun('Threshold "ack_months" must be at least 1.')
    cfg["thresholds"] = norm_th
    beh = cfg.get("accrual_behavior") or {}
    for k, v in beh.items():
        if v not in ("reversing", "rollforward"):
            raise StopRun(f'accrual_behavior["{k}"] must be "reversing" or "rollforward".')
    cfg["accrual_behavior"] = beh
    return cfg


def threshold(cfg: dict, key: str):
    return cfg["thresholds"][key]


def _roles_sha(cfg: dict) -> str:
    blob = json.dumps({"roles": cfg.get("roles"), "accrual_behavior": cfg.get("accrual_behavior") or {}},
                      sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def confirmed(cfg: dict) -> str:
    """'' when confirmed and unchanged since; otherwise the reason it isn't."""
    c = cfg.get("accounts_confirmed")
    if not (isinstance(c, dict) and str(c.get("by", "")).strip()):
        return "The account roles in review-config.json have not been confirmed by a person."
    if c.get("roles_sha") != _roles_sha(cfg):
        return (f"The account roles changed after {c['by']} confirmed them on {c.get('at', '?')[:10]}. "
                "They need confirming again.")
    return ""


def confirm(root: Path, by: str) -> dict:
    if not by.strip():
        raise StopRun("Say who confirmed the roles (--by NAME).")
    raw = read_json(config_path(root))
    raw["accounts_confirmed"] = {"by": by.strip(), "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                 "roles_sha": _roles_sha(raw)}
    write_json(config_path(root), raw)
    return raw


def propose(accounts: list[dict], company: str, prefs: dict) -> dict:
    """Propose roles from the chart of accounts. Everything here is a proposal for a person."""
    roles = {r: [] for r in ROLES}
    for a in accounts:
        if a.get("active", "true").lower() == "false":
            continue
        name, t, st = a["full_name"], (a["account_type"] or ""), (a["account_subtype"] or "")
        n = norm(name)
        if t == "Bank" and re.search(r"stripe|clearing", n):
            roles["processor_clearing"].append(name)
        elif t == "Bank":
            roles["cash"].append(name)
        elif t == "Credit Card":
            roles["card"].append(name)
        elif t == "Accounts Receivable":
            roles["ar"].append(name)
        elif t == "Accounts Payable":
            roles["ap"].append(name)
        elif st == "UndepositedFunds" or "undeposited" in n or "payments to deposit" in n:
            roles["undeposited_funds"].append(name)
        elif t in ("Other Current Liability", "Long Term Liability") and re.search(r"deferred|unearned", n):
            roles["deferred_revenue"].append(name)
        elif t == "Other Current Liability" and re.search(r"accru\w* (wage|payroll|salar)", n):
            roles["accrued_payroll"].append(name)
        elif t == "Other Current Liability" and re.search(r"accru", n):
            roles["accrued"].append(name)
        elif t == "Other Current Liability" and (st in ("SalesTaxPayable", "GlobalTaxPayable")
                                                 or re.search(r"sales tax|equalization", n)):
            roles["sales_tax_payable"].append(name)
        elif t in ("Long Term Liability", "Other Current Liability") and re.search(r"loan|venture debt|notes? payable", n):
            roles["loans"].append(name)
        elif t == "Other Current Asset" and re.search(r"prepaid", n):
            roles["prepaid"].append(name)
        elif t == "Income" and re.search(r"refund", n):
            roles["refunds"].append(name)
        elif t == "Income" and re.search(r"stripe|subscription|saas|software revenue", n):
            roles["stripe_revenue"].append(name)
        elif t in ("Expense", "Cost of Goods Sold") and re.search(r"stripe|merchant|processing fee|payment processing", n):
            roles["stripe_fees"].append(name)
        elif t in ("Expense", "Cost of Goods Sold") and re.search(r"payroll tax|employer tax", n):
            roles["payroll_taxes"].append(name)
        elif t in ("Expense", "Cost of Goods Sold") and re.search(r"wage|salar", n):
            roles["payroll_wages"].append(name)
        elif t in ("Expense", "Other Expense") and re.search(r"interest", n):
            roles["loan_interest"].append(name)
    accrued = {name: "reversing" for name in roles["accrued"]}
    return {
        "schema": 2,
        "company": company,
        "currency": "USD",
        "ledger": "quickbooks",
        "basis": prefs.get("basis") or "Accrual",
        "fiscal_year_start_month": prefs.get("fiscal_year_start_month") or 1,
        "accounts_confirmed": False,
        "uses_stripe": bool(roles["stripe_revenue"] or roles["stripe_fees"] or roles["processor_clearing"]),
        "stripe_billing_via_ar": False,
        "uses_stripe_tax": bool(roles["sales_tax_payable"]),
        "uses_payroll_file": False,
        "roles": roles,
        "accrual_behavior": accrued,
        "bank_formats": {
            acct: {"date_col": "", "description_col": "", "amount_col": "", "debit_col": "", "credit_col": "",
                   "date_format": "", "flip_sign": False, "status_col": "", "posted_values": []}
            for acct in roles["cash"] + roles["card"]
        },
        "payroll_format": {"date_col": "", "gross_col": "", "employer_tax_col": "", "period_start_col": "",
                           "period_end_col": "", "date_format": ""},
        "thresholds": {},
    }


def save(root: Path, cfg: dict) -> Path:
    p = config_path(root)
    write_json(p, cfg)
    return p


def resolve_roles(cfg: dict, accounts: list[dict]) -> tuple[dict, list[str]]:
    """Map role names to account ids. Returns (role -> set of ids, problems)."""
    by_name, short = {}, {}
    for a in accounts:
        by_name[norm(a["full_name"])] = a["account_id"]
        short.setdefault(norm(a["name"]), []).append(a["account_id"])
    for n, ids in short.items():
        if n not in by_name:
            by_name[n] = ids[0] if len(ids) == 1 else None
    resolved, problems = {}, []
    roles = cfg.get("roles") or {}
    for role in roles:
        if role not in ROLES:
            problems.append(f'unknown role "{role}" in review-config.json.')
    for role, (required, meaning) in ROLES.items():
        if role not in roles:
            if required:
                problems.append(f'role "{role}" ({meaning}) is missing from the config. Use [] if there is none.')
            resolved[role] = set()
            continue
        if not isinstance(roles[role], list):
            problems.append(f'role "{role}" must be a list of account names.')
            resolved[role] = set()
            continue
        ids = set()
        for name in roles[role]:
            if norm(name) in by_name and by_name[norm(name)] is None:
                problems.append(f'role "{role}" lists "{name}", which matches more than one account. Use the full name.')
                continue
            aid = by_name.get(norm(name))
            if aid is None:
                problems.append(f'role "{role}" lists "{name}", which is not in the chart of accounts.')
            else:
                ids.add(aid)
        resolved[role] = ids
    if not resolved.get("cash"):
        problems.append('role "cash" is empty. At least one bank account is required.')
    if cfg.get("uses_stripe"):
        for r in ("stripe_revenue", "stripe_fees"):
            if not resolved.get(r):
                problems.append(f'uses_stripe is true but role "{r}" is empty.')
    overlap = resolved["cash"] & (resolved["card"] | resolved["processor_clearing"])
    if overlap:
        problems.append("an account is in more than one of cash / card / processor_clearing.")
    return resolved, problems


def account_id_for(cfg: dict, accounts: list[dict], name: str, roles_allowed: tuple) -> tuple[str, str, str]:
    """Resolve a user-typed account name (any case) to (id, canonical name, role)."""
    resolved, _ = resolve_roles(cfg, accounts)
    by_id = {a["account_id"]: a for a in accounts}
    full = [a["account_id"] for a in accounts if norm(a["full_name"]) == norm(name)]
    leaf = [a["account_id"] for a in accounts if norm(a["name"]) == norm(name)]
    hits = full or leaf
    if len(hits) > 1:
        raise StopRun(f'"{name}" matches more than one account. Use the full name (with its parent).')
    if not hits:
        raise StopRun(f'"{name}" is not in the chart of accounts.')
    target = hits[0]
    for role in roles_allowed:
        if target in resolved.get(role, set()):
            return target, by_id[target]["full_name"], role
    raise StopRun(f'"{name}" is not listed under {" or ".join("roles." + r for r in roles_allowed)} in review-config.json.')
