"""After the checks: severity by amount, one cause reported once, acknowledgements, flux caveats."""

from __future__ import annotations

import json
from pathlib import Path

from ..config import threshold
from ..data import Data
from .base import SEV_ORDER

# Which check leads a group: the most specific cause first (a K4 transfer explains its K3, K1 and T1 items).
PRIORITY = ["T3", "T5", "T4", "C2", "C1", "C3", "C4", "K4", "K3", "T2", "K1", "K5", "V2", "T1", "K2", "C5"]
LEVELS = ["high", "medium", "low"]


def _shift(sev: str, n: int) -> str:
    return LEVELS[max(0, min(2, LEVELS.index(sev) + n))]


def adjust_severity(d: Data, exceptions: list) -> None:
    low, high = threshold(d.cfg, "materiality_abs"), threshold(d.cfg, "materiality_abs_only")
    for e in exceptions:
        if e["pin"]:
            continue
        if e["amount"] < low:
            e["severity"] = _shift(e["severity"], 1)
        elif e["amount"] >= high:
            e["severity"] = _shift(e["severity"], -1)


def drop_k2_repeats(results: dict) -> None:
    """Drop a K2 range break only when other items on the same account explain at least half of it."""
    explained = {}
    for cid, r in results.items():
        if cid == "K2":
            continue
        for e in r.exceptions:
            for a in e["accounts"]:
                if a:
                    explained[a] = explained.get(a, 0) + e["amount"]
    k2 = results.get("K2")
    if not k2 or k2.status == "not_run":
        return
    keep = [e for e in k2.exceptions if not any(explained.get(a, 0) * 2 >= e["amount"] for a in e["accounts"])]
    dropped = len(k2.exceptions) - len(keep)
    k2.exceptions = keep
    k2.status = "flag" if keep else "pass"
    k2.summary = f"{len(keep)} account(s) outside their usual range" + (
        f" ({dropped} more already explained by other checks)" if dropped else "")


def _money(k: str) -> str:
    return k.split("|", 1)[1] if "|" in k else k


def group(exceptions: list) -> list:
    """One cause, one item.

    Merge items that share a transaction, the same account + date + amount, or an explicit tag. Then a
    bank line that isn't in the books (T1) attaches to the ONE other item with the same date + amount, if
    there is exactly one; nothing chains further than that.
    """
    parent = list(range(len(exceptions)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen = {}
    for i, e in enumerate(exceptions):
        tags = [f"t:{t}" for t in e["txns"]] + [f"k:{k}" for k in e["keys"] if not k.startswith("|")]
        for tag in tags:
            if tag in seen:
                parent[find(i)] = find(seen[tag])
            else:
                seen[tag] = i
    for i, e in enumerate(exceptions):
        if e["check"] != "T1" or e["kind"] != "statement-not-in-books":
            continue
        mine = {_money(k) for k in e["keys"] if not k.startswith("g|")}
        roots = {find(j) for j, o in enumerate(exceptions) if j != i and find(j) != find(i)
                 and mine & {_money(k) for k in o["keys"] if not k.startswith("g|")}}
        if len(roots) == 1:
            parent[find(i)] = roots.pop()
    buckets = {}
    for i, e in enumerate(exceptions):
        buckets.setdefault(find(i), []).append(e)
    out = []
    for members in buckets.values():
        members.sort(key=lambda e: (PRIORITY.index(e["check"]) if e["check"] in PRIORITY else 99, SEV_ORDER[e["severity"]]))
        lead = dict(members[0])
        lead["also"] = [{"check": m["check"], "title": m["title"]} for m in members[1:]]
        lead["severity"] = min((m["severity"] for m in members), key=lambda s: SEV_ORDER[s])
        lead["amount"] = max(m["amount"] for m in members)
        lead["pin"] = any(m["pin"] for m in members)
        for m in members[1:]:
            lead["accounts"] = sorted(set(lead["accounts"]) | set(m["accounts"]))
            lead["refs"] = lead["refs"] + [r for r in m["refs"] if r not in lead["refs"]]
        out.append(lead)
    return out


def load_acks(root: Path) -> list:
    p = root / "acknowledged.json"
    if not p.exists():
        return []
    from ..util import StopRun
    try:
        acks = json.loads(p.read_text())
    except json.JSONDecodeError:
        raise StopRun(f"{p} is not valid JSON. Fix it or delete it.")
    if not isinstance(acks, list) or not all(isinstance(a, dict) for a in acks):
        raise StopRun(f"{p} must be a list of acknowledgements. Fix it or delete it.")
    for a in acks:
        for k in ("fingerprint", "from", "until", "amount"):
            if not isinstance(a.get(k), str):
                raise StopRun(f"{p}: every acknowledgement needs text fields fingerprint, from, until and amount.")
    return acks


def apply_acks(d: Data, exceptions: list, root: Path) -> tuple[list, list]:
    """An acknowledgement covers the same item, from the month it was given to its end month, and only while the
    amount stays within 10% (or $100) of what was acknowledged. Integrity items can't be acknowledged."""
    from decimal import Decimal
    acks = {a["fingerprint"]: a for a in load_acks(root) if a["from"] <= d.period.key <= a["until"]}
    open_, acked = [], []
    for e in exceptions:
        a = acks.get(e["fingerprint"])
        if a and not e["pin"]:
            limit = max(Decimal(a["amount"]) * Decimal("1.1"), Decimal(a["amount"]) + 100)
            if e["amount"] <= limit:
                acked.append(dict(e, acknowledged={k: a.get(k) for k in ("by", "reason", "at", "from", "until", "amount")}))
                continue
            e = dict(e, detail=e["detail"] + f" (Acknowledged by {a.get('by')} at {a['amount']}, but the amount has grown.)")
        open_.append(e)
    return open_, acked


def finalize(d: Data, results: dict, root: Path) -> dict:
    drop_k2_repeats(results)
    every = [e for r in results.values() for e in r.exceptions]
    adjust_severity(d, every)
    grouped = group(every)
    open_, acked = apply_acks(d, grouped, root)
    key = lambda e: (SEV_ORDER[e["severity"]], -e["amount"])
    open_.sort(key=key)
    acked.sort(key=key)
    for i, e in enumerate(open_, 1):
        e["id"] = f"E{i}"
    for i, e in enumerate(acked, 1):
        e["id"] = f"A{i}"
    by_acct = {}
    for e in open_:
        for a in e["accounts"]:
            by_acct.setdefault(a, []).append(e["id"])
    v1, v2 = results.get("V1"), results.get("V2")
    if v1 and v1.status != "not_run":
        for row in v1.data["rows"]:
            ids = by_acct.get(row["account_id"])
            if ids:
                row["see"] = ids
                row["direction"] = "unreliable, see " + ", ".join(ids)
    if v2:
        for row in v2.data["rows"]:
            if by_acct.get(row["account_id"]):
                row["see"] = by_acct[row["account_id"]]
    return {"open": open_, "acknowledged": acked}
