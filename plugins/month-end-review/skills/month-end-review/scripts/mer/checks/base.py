"""Shared check plumbing. A check never guesses: no data -> NOT RUN with the reason."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..data import Data
from ..util import ZERO, norm

SEV_ORDER = {"high": 0, "medium": 1, "low": 2}


def key(d: date, amount: Decimal, account: str = "") -> str:
    """Join key for grouping. With an account, items sharing it are the same money and are merged.
    Without one, the key is only used to attach a bank line that isn't in the books to exactly one other item."""
    return f"{account}|{d.isoformat()}|{abs(amount):.2f}"


@dataclass
class CheckResult:
    id: str
    title: str
    status: str = "pass"            # pass | flag | not_run
    reason: str = ""                # why not_run
    summary: str = ""
    parts: list = field(default_factory=list)       # sub-checks: {part, status, reason}
    tieouts: list = field(default_factory=list)
    exceptions: list = field(default_factory=list)
    drafts: list = field(default_factory=list)
    data: dict = field(default_factory=dict)

    def flag(self, severity, kind, title, amount, detail, refs, action, *, txns=(), keys=(), accounts=(),
             subject="", pin=False):
        self.exceptions.append({
            "check": self.id, "kind": kind, "severity": severity, "base_severity": severity, "pin": pin,
            "title": title, "amount": abs(Decimal(amount)), "detail": detail, "refs": list(refs), "action": action,
            "txns": sorted({t for t in txns if t}), "keys": sorted(set(keys)), "accounts": sorted({a for a in accounts if a}),
            "fingerprint": f"{self.id}:{kind}:{norm(subject or title)}",
        })
        self.status = "flag"

    def tie(self, item, books, other, source, status, note=""):
        self.tieouts.append({"check": self.id, "item": item, "books": books, "other": other, "other_source": source,
                             "difference": (books - other) if books is not None and other is not None else None,
                             "status": status, "note": note})

    def part(self, name, status, reason=""):
        self.parts.append({"part": name, "status": status, "reason": reason})

    def draft(self, memo, lines, status="ready", basis=""):
        """lines: [(account name, debit or None, credit or None)].
        status: ready (the data decides it) · confirm first (amounts shown, not counted) · needs your amount."""
        self.drafts.append({"check": self.id, "memo": memo, "status": status, "basis": basis,
                            "lines": [{"account": a, "debit": dr, "credit": cr} for a, dr, cr in lines]})


def not_run(cid, title, reason) -> CheckResult:
    return CheckResult(cid, title, status="not_run", reason=reason)


def lines_on(d: Data, ids: set, lines=None) -> list:
    return [l for l in (d.gl if lines is None else lines) if l["account_id"] in ids]


def txn_lines(d: Data, txn_id: str, lines=None) -> list:
    return [l for l in (d.gl if lines is None else lines) if l["txn_id"] == txn_id]


def counter_account(d: Data, line) -> str | None:
    others = {l["account_id"] for l in txn_lines(d, line["txn_id"]) if l["account_id"] != line["account_id"]}
    return d.name(others.pop()) if len(others) == 1 else None


def is_je(line) -> bool:
    return line["txn_type"].lower().startswith("journal")


def payee(line) -> str:
    return line["name"] or ""


def sum_amt(lines) -> Decimal:
    return sum((l["amount"] for l in lines), ZERO)


def pick(d: Data, role: str, reversing_only: bool = False):
    """The one account for a role, by name. None if the role is empty or ambiguous (then a person chooses)."""
    ids = sorted(d.ids_for(role), key=lambda a: d.name(a))
    if reversing_only:
        beh = {norm(k): v for k, v in (d.cfg.get("accrual_behavior") or {}).items()}
        ids = [a for a in ids if beh.get(norm(d.name(a)), "reversing") == "reversing"]
    return ids[0] if len(ids) == 1 else None
