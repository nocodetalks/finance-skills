"""Match book lines to statement lines for one bank or card account.

1. Exact amount, lowest cost first (globally). Cost = days apart; a matching check number costs 0;
   a current line outside the window costs extra (it cleared late); last month's uncleared items
   cost their age, so a recent line wins over a very old one with the same amount.
2. Groups, only when the descriptions share a word: several statement rows that add up to one book
   line (a monthly transfer swept daily), or several book lines that add up to one statement row
   (daily card autopay booked once).
3. Cleared late: the same amount on both sides, further apart than the window (either direction),
   within 31 days.
4. Leftover pairs on (almost) the same date are offered as possible typos, never as fact.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from ..util import ZERO, days_between, norm

STOP = {"the", "and", "for", "from", "to", "inc", "llc", "ltd", "co", "corp", "payment", "deposit", "debit", "credit",
        "ach", "wire", "online", "pos", "card", "purchase", "pmt", "trf", "xfer", "with", "via", "bank"}


def tokens(*texts) -> set:
    out = set()
    for t in texts:
        out |= {w for w in re.findall(r"[a-z]{3,}", (t or "").lower()) if w not in STOP}
    return out


@dataclass
class Match:
    pairs: list = field(default_factory=list)      # (book, [bank rows], how)
    late: list = field(default_factory=list)       # (book, bank)
    typos: list = field(default_factory=list)      # (book, bank, kind)
    book_left: list = field(default_factory=list)  # unmatched book lines (current + prior + newly booked prior)
    bank_left: list = field(default_factory=list)  # unmatched statement rows (period + carried)
    bank_matched: dict = field(default_factory=dict)


def _bid(b):
    return b["line_id"]


def _cost(b, s, window) -> int | None:
    gap = (s["date"] - b["date"]).days
    doc = (b.get("doc_num") or "").strip()
    check_no = bool(doc) and re.search(rf"\b{re.escape(doc)}\b", s.get("description", "")) is not None
    if b.get("prior"):
        if gap < -3:
            return None
        return 0 if check_no else max(gap, 0)
    if s.get("prior"):                     # last month's statement line booked now
        return None if gap > 3 else (0 if check_no else abs(gap))
    if s.get("pre"):                       # statement rows before the 1st only clear last month's items
        return None
    if check_no and abs(gap) <= 31:
        return 0
    if abs(gap) <= window:
        return abs(gap)
    if abs(gap) <= 31:
        return abs(gap) + 10               # cleared late: allowed, but a closer match wins
    return None


def match_account(book: list, prior: list, bank: list, window: int, in_transit_from) -> Match:
    m = Match()
    books = list(book) + list(prior)
    free_book = {_bid(b): b for b in books}
    free_bank = {s["row"]: s for s in bank}

    cands = []
    for b in books:
        for s in bank:
            if b["amount"] == s["amount"]:
                c = _cost(b, s, window)
                if c is not None:
                    cands.append((c, _bid(b), s["row"]))
    for _, bid, row in sorted(cands, key=lambda x: (x[0], str(x[1]), str(x[2]))):
        if bid in free_book and row in free_bank:
            b, s = free_book.pop(bid), free_bank.pop(row)
            late = not b.get("prior") and not s.get("prior") and days_between(b["date"], s["date"]) > window
            if late:
                m.late.append((b, s))
            m.pairs.append((b, [s], "late" if late else "exact"))

    # 2a. several statement rows (same description) = one book line sharing a word with them
    groups = defaultdict(list)
    for s in free_bank.values():
        if not s.get("pre"):
            groups[(norm(s["description"]), s["amount"] > 0)].append(s)
    for (desc, _), rows in groups.items():
        if len(rows) < 2:
            continue
        tot = sum((r["amount"] for r in rows), ZERO)
        hit = next((b for b in free_book.values() if b["amount"] == tot and not b.get("prior")
                    and tokens(b.get("name"), b.get("memo")) & tokens(desc)
                    and all(days_between(r["date"], b["date"]) <= 31 for r in rows)), None)
        if hit:
            free_book.pop(_bid(hit))
            for r in rows:
                free_bank.pop(r["row"])
            m.pairs.append((hit, rows, "group"))
    # 2b. several book lines (same payee) = one statement row sharing a word with them
    bgroups = defaultdict(list)
    for b in free_book.values():
        if not b.get("prior"):
            bgroups[(norm(b.get("name") or b.get("memo") or ""), b["amount"] > 0)].append(b)
    for (payee, _), items in bgroups.items():
        if len(items) < 2 or not payee:
            continue
        tot = sum((b["amount"] for b in items), ZERO)
        row = next((s for s in free_bank.values() if s["amount"] == tot and not s.get("pre")
                    and tokens(payee) & tokens(s["description"])
                    and all(days_between(s["date"], b["date"]) <= 31 for b in items)), None)
        if row:
            free_bank.pop(row["row"])
            for b in items:
                free_book.pop(_bid(b))
                m.pairs.append((b, [row], "group"))

    # 3. cleared late, either direction, within 31 days
    for bid, b in sorted(list(free_book.items()), key=lambda kv: kv[1]["date"]):
        if b.get("prior"):
            continue
        row = next((s for s in sorted(free_bank.values(), key=lambda s: days_between(s["date"], b["date"]))
                    if s["amount"] == b["amount"] and not s.get("pre") and days_between(s["date"], b["date"]) <= 31), None)
        if row:
            free_bank.pop(row["row"])
            free_book.pop(bid)
            m.late.append((b, row))
            m.pairs.append((b, [row], "late"))

    # 4. possible typos: current book line and statement row, same date (±2 days), not in transit
    for bid, b in list(free_book.items()):
        if b.get("prior") or b["date"] >= in_transit_from:
            continue
        for row, s in list(free_bank.items()):
            if s.get("prior") or s.get("pre") or days_between(b["date"], s["date"]) > 2:
                continue
            a, c = abs(b["amount"]), abs(s["amount"])
            same_sign = (b["amount"] > 0) == (s["amount"] > 0)
            digits = sorted(str(int(a * 100))) == sorted(str(int(c * 100)))
            if (same_sign and a != c and digits) or (b["amount"] == -s["amount"] and b["date"] == s["date"]):
                m.typos.append((b, s, "digits swapped" if same_sign else "sign reversed"))
                free_book.pop(bid)
                free_bank.pop(row)
                break

    for b, rows, _ in m.pairs:
        for r in rows:
            m.bank_matched[r["row"]] = _bid(b)
    m.book_left = list(free_book.values())
    m.bank_left = [s for s in free_bank.values() if not s.get("pre")]
    return m


def prior_as_book(aid: str, lines: list) -> list:
    """Last month's uncleared book items, shaped like book lines."""
    return [{"line_id": f"prior:{aid}:{i}", "txn_id": l.get("txn_id", ""), "txn_type": "uncleared from last month",
             "date": l["date"], "account_id": aid, "name": l.get("description", ""), "memo": "",
             "doc_num": l.get("doc_num", ""), "amount": l["amount"], "prior": True, "source_ref": l.get("source_ref", "")}
            for i, l in enumerate(lines) if (l.get("side") or "book") == "book"]


def prior_as_bank(aid: str, lines: list) -> list:
    """Last month's statement lines that weren't in the books yet, shaped like statement rows."""
    return [{"row": f"prior:{aid}:{i}", "date": l["date"], "description": l.get("description", ""),
             "amount": l["amount"], "prior": True, "source_ref": l.get("source_ref", "")}
            for i, l in enumerate(lines) if (l.get("side") or "book") == "bank"]


def statement_rows_in_period(s: dict, period) -> list:
    return [l for l in s["lines"] if period.contains(l["date"])]


def statement_rows_before_period(s: dict, period) -> list:
    """Rows between the statement start and the 1st (mid-month statements): they can only clear last month's items."""
    return [dict(l, pre=True) for l in s["lines"] if l["date"] < period.start]


def total(lines) -> Decimal:
    return sum((l["amount"] for l in lines), ZERO)
