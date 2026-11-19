"""Bring the inputs in: the books (from a QuickBooks connector or an export), the statement, Stripe, and last
period's uncleared items. Every input is checked here and saved in one plain shape under <month>/inputs/.

Each add-* command stops (StopRun) rather than guess: wrong columns, unreadable values, dates outside the statement
period, or lines that don't add up to the balances given.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from .util import (ZERO, StopRun, csv_header, fmt, iso_date, money, norm, parse_date, read_csv, read_json, slug,
                   write_csv, write_json)

BOOK_FIELDS = ["line_id", "txn_id", "txn_type", "date", "name", "memo", "doc_num", "amount", "split_account"]
BANK_FIELDS = ["row", "date", "description", "amount"]
STRIPE_FIELDS = ["id", "date", "category", "gross", "fee", "net", "description"]
OUT_FIELDS = ["side", "date", "description", "amount", "txn_id", "txn_type", "doc_num", "source_ref"]

# Column names we recognise without being told. Keys are our names; values are header spellings (lower case).
BOOK_ALIASES = {
    "txn_id": ["txn_id", "id", "transaction id", "record id"],
    "txn_type": ["txn_type", "type", "transaction type"],
    "date": ["date", "txn_date", "transaction date"],
    "account": ["account", "account name", "bank account"],
    "name": ["name", "payee", "customer/vendor", "entity"],
    "memo": ["memo", "memo/description", "description"],
    "doc_num": ["doc_num", "num", "no.", "ref no.", "check no.", "doc number"],
    "amount": ["amount", "signed amount"],
    "split_account": ["split_account", "split", "other account", "category"],
}
BANK_ALIASES = {
    "date": ["date", "posted date", "posting date", "transaction date", "date (utc)", "post date", "booking date"],
    "description": ["description", "details", "memo", "payee", "name", "transaction description", "merchant"],
    "amount": ["amount", "amount (usd)", "transaction amount", "net amount"],
    "debit": ["debit", "withdrawal", "withdrawals", "money out", "debit amount"],
    "credit": ["credit", "deposit", "deposits", "money in", "credit amount"],
    "status": ["status", "state", "transaction status"],
}
STRIPE_ALIASES = {
    "id": ["balance_transaction_id", "id", "balance transaction id"],
    "date": ["created_utc", "created", "created (utc)", "created date (utc)"],
    "category": ["reporting_category", "reporting category", "type"],
    "gross": ["gross", "amount"],
    "fee": ["fee"],
    "net": ["net"],
    "description": ["description"],
}
POSTED = {"sent", "posted", "completed", "complete", "cleared", "settled", "booked", "paid"}
NOT_POSTED = {"pending", "failed", "cancelled", "canceled", "declined", "reversed", "processing", "scheduled"}


# ---------- manifest ----------

class Manifest:
    """<month>/inputs/manifest.json: what was brought in, from where, and the balances."""

    def __init__(self, month_dir: Path):
        self.path = month_dir / "inputs" / "manifest.json"
        self.data = read_json(self.path) if self.path.exists() else {"accounts": {}}

    def account(self, name: str) -> dict:
        for k, v in self.data["accounts"].items():
            if norm(k) == norm(name):
                return v
        self.data["accounts"][name] = {}
        return self.data["accounts"][name]

    def save(self) -> None:
        write_json(self.path, self.data)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bal(v: str, what: str) -> Decimal:
    try:
        return money(v)
    except ValueError as e:
        raise StopRun(f"{what} must be a number copied from the source, got {v!r}.") from e


def _pick(header: list[str], aliases: list[str], given: str | None, what: str, required: bool) -> str | None:
    if given:
        if given not in header:
            raise StopRun(f"Column {given!r} (for {what}) isn't in the file. Its columns are: {', '.join(header)}.")
        return given
    low = {h.lower(): h for h in header}
    for a in aliases:
        if a in low:
            return low[a]
    if required:
        raise StopRun(f"Can't tell which column holds the {what}. Its columns are: {', '.join(header)}. "
                      f"Name it with --{what.replace(' ', '-')}-col.")
    return None


def _check_period(start: date, end: date) -> None:
    if end < start:
        raise StopRun(f"The statement period ends ({end}) before it starts ({start}).")
    if (end - start).days > 92:
        raise StopRun(f"The statement period {start} to {end} is longer than 3 months. Reconcile one statement at a time.")


def _add_up(opening: Decimal, rows: list[dict], closing: Decimal, what: str, flip_hint: str) -> None:
    tot = sum((r["amount"] for r in rows), ZERO)
    if opening + tot == closing:
        return
    if opening - tot == closing:
        raise StopRun(f"The {what} lines add up only with their signs reversed ({fmt(opening)} - {fmt(tot)} = "
                      f"{fmt(closing)}). {flip_hint}")
    raise StopRun(f"The {what} doesn't add up: opening {fmt(opening)} + lines {fmt(tot)} = {fmt(opening + tot)}, "
                  f"but the closing balance is {fmt(closing)} (off by {fmt(closing - opening - tot)}). A line is "
                  f"missing, extra or copied wrong, or a balance is from the wrong date. Fix the source and add it again.")


# ---------- books ----------

def add_books(month_dir: Path, account: str, src: Path, opening: str, closing: str, start: str, end: str,
              kind: str, source: str, cols: dict, no_activity: bool) -> str:
    if not src.exists():
        raise StopRun(f"No file at {src}.")
    if not (source or "").strip():
        raise StopRun("Say where the book lines came from with --source, e.g. \"QuickBooks connector, General Ledger "
                      "report\" or \"QuickBooks export, Transaction Detail by Account\".")
    if kind not in ("bank", "card"):
        raise StopRun(f"--kind must be bank or card, got {kind!r}.")
    s, e = iso_date(start, "--start"), iso_date(end, "--end")
    _check_period(s, e)
    ob, cb = _bal(opening, "--opening"), _bal(closing, "--closing")
    header = csv_header(src)
    col = {k: _pick(header, BOOK_ALIASES[k], cols.get(k), k.replace("_", " "), k in ("date", "amount"))
           for k in BOOK_ALIASES}
    raw = read_csv(src)
    if col["account"]:
        names = sorted({(r.get(col["account"]) or "").strip() for r in raw})
        raw = [r for r in raw if norm(r.get(col["account"])) == norm(account)]
        if not raw and not no_activity:
            raise StopRun(f"No lines for {account!r} in {src.name}. Accounts in the file: {', '.join(names) or 'none'}.")
    rows, bad = [], []
    for i, r in enumerate(raw, start=2):
        try:
            d = parse_date(r.get(col["date"]))
            amt = money(r.get(col["amount"]))
        except ValueError as ex:
            bad.append(f"line {i}: {ex}")
            continue
        if not (s <= d <= e):
            bad.append(f"line {i}: dated {d}, outside the statement period {s} to {e}")
            continue
        get = lambda k: (r.get(col[k]) or "").strip() if col[k] else ""  # noqa: E731
        rows.append({"line_id": f"B{len(rows) + 1}", "txn_id": get("txn_id"), "txn_type": get("txn_type"), "date": d,
                     "name": get("name"), "memo": get("memo"), "doc_num": get("doc_num"),
                     "amount": -amt if kind == "card" else amt, "split_account": get("split_account")})
    if bad:
        raise StopRun(f"{src.name} has lines that can't be used:\n  " + "\n  ".join(bad[:15])
                      + ("\n  ..." if len(bad) > 15 else "")
                      + "\nPull the account's lines for exactly the statement period and add them again.")
    if not rows and not no_activity:
        raise StopRun(f"No book lines for {account} between {s} and {e}. If QuickBooks truly shows no activity, add "
                      "--no-activity (the balances must then be equal).")
    hint = ("If this is a credit card, add --kind card." if kind == "bank" else
            "This was added as a card; if it's a bank account, use --kind bank.")
    _add_up(ob, rows, cb, f"book side of {account}", hint)
    out = month_dir / "inputs" / f"books-{slug(account)}.csv"
    write_csv(out, rows, BOOK_FIELDS)
    m = Manifest(month_dir)
    a = m.account(account)
    a["kind"] = kind
    a["books"] = {"file": out.name, "source_file": src.name, "source": source.strip(), "opening": str(ob),
                  "closing": str(cb), "start": s.isoformat(), "end": e.isoformat(), "lines": len(rows),
                  "has_split_account": bool(col["split_account"]), "has_ids": bool(col["txn_id"]), "added": _now()}
    st = a.get("statement")
    if st and (st["start"], st["end"]) != (s.isoformat(), e.isoformat()):
        a.pop("statement")
        note = " The statement added earlier covered different dates, so it was removed: add it again."
    else:
        note = ""
    m.save()
    return (f"{account}: {len(rows)} book lines from {s} to {e}, {fmt(ob)} to {fmt(cb)}, add up.{note}")


# ---------- statement ----------

def add_statement(month_dir: Path, account: str, src: Path, opening: str, closing: str, start: str, end: str,
                  flip_sign: bool, date_format: str | None, cols: dict, posted: list[str], trim: bool,
                  no_activity: bool) -> str:
    if not src.exists():
        raise StopRun(f"No file at {src}.")
    m = Manifest(month_dir)
    a = m.account(account)
    if not a.get("books"):
        raise StopRun(f"Add the book side of {account} first (add-books), so the statement can be checked against "
                      "the same dates.")
    s, e = iso_date(start, "--start"), iso_date(end, "--end")
    if (s.isoformat(), e.isoformat()) != (a["books"]["start"], a["books"]["end"]):
        raise StopRun(f"The statement period {s} to {e} isn't the period of the book lines ({a['books']['start']} to "
                      f"{a['books']['end']}). Both sides must cover the same dates.")
    ob, cb = _bal(opening, "--opening"), _bal(closing, "--closing")
    header = csv_header(src)
    c_date = _pick(header, BANK_ALIASES["date"], cols.get("date"), "date", True)
    c_desc = _pick(header, BANK_ALIASES["description"], cols.get("description"), "description", True)
    c_amt = _pick(header, BANK_ALIASES["amount"], cols.get("amount"), "amount", False)
    c_dr = c_cr = None
    if not c_amt:
        c_dr = _pick(header, BANK_ALIASES["debit"], cols.get("debit"), "debit", False)
        c_cr = _pick(header, BANK_ALIASES["credit"], cols.get("credit"), "credit", False)
        if not (c_dr and c_cr):
            raise StopRun(f"Can't find an amount column, or debit and credit columns, in {src.name}. Its columns are: "
                          f"{', '.join(header)}. Name them with --amount-col, or --debit-col and --credit-col.")
    c_status = _pick(header, BANK_ALIASES["status"], cols.get("status"), "status", False)
    keep = {norm(p) for p in posted} if posted else None
    raw = read_csv(src)
    rows, bad, dropped, outside = [], [], 0, 0
    unknown_status = set()
    for i, r in enumerate(raw, start=2):
        if c_status:
            st = norm(r.get(c_status))
            ok = st in keep if keep is not None else st in POSTED
            if not ok:
                if keep is None and st not in NOT_POSTED:
                    unknown_status.add(r.get(c_status) or "(blank)")
                dropped += 1
                continue
        try:
            d = parse_date(r.get(c_date), date_format)
            if c_amt:
                amt = money(r.get(c_amt))
            else:
                dr, cr = (r.get(c_dr) or "").strip(), (r.get(c_cr) or "").strip()
                if dr and cr:
                    raise ValueError("both debit and credit are filled")
                amt = money(cr) if cr else -money(dr)
        except ValueError as ex:
            bad.append(f"line {i}: {ex}")
            continue
        if not (s <= d <= e):
            if trim:
                outside += 1
                continue
            bad.append(f"line {i}: dated {d}, outside the statement period {s} to {e}")
            continue
        rows.append({"row": len(rows) + 1, "line": i, "date": d, "description": (r.get(c_desc) or "").strip(),
                     "amount": -amt if flip_sign else amt})
    if unknown_status:
        raise StopRun(f"The {c_status!r} column has values this doesn't know: {', '.join(sorted(unknown_status))}. "
                      "Say which values mean the transaction posted, e.g. --posted Sent --posted Completed.")
    if bad:
        raise StopRun(f"{src.name} has rows that can't be used:\n  " + "\n  ".join(bad[:15])
                      + ("\n  ..." if len(bad) > 15 else "")
                      + "\nIf dates are day-first (31/08/2026), add --date-format %d/%m/%Y. If the export covers more "
                        "than the statement period, export the exact period, or add --trim to drop the rows outside it.")
    if not rows and not no_activity:
        raise StopRun(f"No statement rows for {account} between {s} and {e}. If the statement truly shows no "
                      "activity, add --no-activity (the balances must then be equal).")
    _add_up(ob, rows, cb, f"statement for {account}",
            "Add --flip-sign: this export shows the signs the other way round (common for card statements).")
    out = month_dir / "inputs" / f"statement-{slug(account)}.csv"
    write_csv(out, rows, BANK_FIELDS + ["line"])
    a["statement"] = {"file": out.name, "source_file": src.name, "opening": str(ob), "closing": str(cb),
                      "start": s.isoformat(), "end": e.isoformat(), "rows": len(rows), "not_posted_dropped": dropped,
                      "outside_period_dropped": outside, "flip_sign": flip_sign, "added": _now()}
    m.save()
    extra = []
    if dropped:
        extra.append(f"{dropped} pending or failed rows left out")
    if outside:
        extra.append(f"{outside} rows outside the period dropped (--trim)")
    return (f"{account}: {len(rows)} statement rows, {fmt(ob)} to {fmt(cb)}, add up."
            + (f" ({'; '.join(extra)})" if extra else ""))


# ---------- Stripe ----------

def add_stripe(month_dir: Path, src: Path, clearing_account: str | None) -> str:
    if not src.exists():
        raise StopRun(f"No file at {src}.")
    header = csv_header(src)
    col = {k: _pick(header, STRIPE_ALIASES[k], None, k, k in ("id", "date", "category"))
           for k in STRIPE_ALIASES}
    if not (col["net"] or col["gross"]):
        raise StopRun(f"{src.name} has no net or amount column. Export Stripe's balance transactions report "
                      "(Reports → Balance → Balance change from activity, itemized), or balance transactions.")
    rows, bad = [], []
    for i, r in enumerate(read_csv(src), start=2):
        try:
            d = parse_date(r.get(col["date"]))
            gross = money(r.get(col["gross"])) if col["gross"] else None
            net = money(r.get(col["net"])) if col["net"] else gross
            fee = money(r.get(col["fee"]) or "0") if col["fee"] else ZERO
        except ValueError as ex:
            bad.append(f"line {i}: {ex}")
            continue
        rows.append({"id": (r.get(col["id"]) or "").strip(), "date": d, "category": norm(r.get(col["category"])),
                     "gross": gross if gross is not None else net, "fee": fee, "net": net,
                     "description": (r.get(col["description"]) or "").strip() if col["description"] else ""})
    if bad:
        raise StopRun(f"{src.name} has rows that can't be used:\n  " + "\n  ".join(bad[:15]))
    if not rows:
        raise StopRun(f"{src.name} has no rows. Export the Stripe activity for the statement period.")
    out = month_dir / "inputs" / "stripe.csv"
    write_csv(out, rows, STRIPE_FIELDS)
    m = Manifest(month_dir)
    m.data["stripe"] = {"file": out.name, "source_file": src.name, "rows": len(rows),
                        "first": min(r["date"] for r in rows).isoformat(),
                        "last": max(r["date"] for r in rows).isoformat(),
                        "clearing_account": (clearing_account or "").strip() or None, "added": _now()}
    m.save()
    payouts = sum(1 for r in rows if r["category"] == "payout")
    refunds = sum(1 for r in rows if r["category"] in ("refund", "refund_failure"))
    return f"Stripe: {len(rows)} rows ({payouts} payouts, {refunds} refunds), {rows and min(r['date'] for r in rows)} to {max(r['date'] for r in rows)}."


# ---------- last period's uncleared items ----------

def add_outstanding(month_dir: Path, account: str, src: Path | None, none_by: str | None) -> str:
    m = Manifest(month_dir)
    a = m.account(account)
    if none_by:
        a["outstanding"] = {"none": True, "by": none_by.strip(), "added": _now()}
        m.save()
        return f"{account}: recorded that nothing was uncleared at the start, confirmed by {none_by.strip()}."
    if not src or not src.exists():
        raise StopRun(f"No file at {src}.")
    header = [h.lower() for h in csv_header(src)]
    for need in ("date", "description", "amount", "side"):
        if need not in header:
            raise StopRun(f"{src.name} needs the columns date, description, amount and side (book or bank). "
                          f"It has: {', '.join(csv_header(src))}.")
    rows, bad = [], []
    for i, r in enumerate(read_csv(src), start=2):
        r = {k.lower(): v for k, v in r.items()}
        side = norm(r.get("side"))
        try:
            d = parse_date(r.get("date"))
            amt = money(r.get("amount"))
            if side not in ("book", "bank"):
                raise ValueError(f"side must be book or bank, got {r.get('side')!r}")
        except ValueError as ex:
            bad.append(f"line {i}: {ex}")
            continue
        rows.append({"side": side, "date": d, "description": (r.get("description") or "").strip(), "amount": amt,
                     "txn_id": (r.get("txn_id") or "").strip(), "txn_type": (r.get("txn_type") or "").strip(),
                     "doc_num": (r.get("doc_num") or "").strip(), "source_ref": f"{src.name} line {i}"})
    if bad:
        raise StopRun(f"{src.name} has rows that can't be used:\n  " + "\n  ".join(bad[:15]))
    out = month_dir / "inputs" / f"outstanding-{slug(account)}.csv"
    write_csv(out, rows, OUT_FIELDS)
    a["outstanding"] = {"file": out.name, "source_file": src.name, "rows": len(rows), "added": _now()}
    m.save()
    return f"{account}: {len(rows)} items uncleared at the start."
