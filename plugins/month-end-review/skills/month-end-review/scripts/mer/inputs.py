"""Canonical input files, the manifest, and ingest for the non-QuickBooks sources.

Every source lands in <month>/inputs/ in one canonical shape, so the checks never
care where the data came from. The manifest records where each file came from,
which dates it claims to cover, and any balances a person typed; the gate checks
all of it before anything runs.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .config import account_id_for
from .util import (MONTH_NAMES, Period, StopRun, add_days, csv_header, money,
                   norm, parse_date, q2, read_csv, read_json, write_csv, write_json)

STRIPE_FIELDS = ["id", "date", "category", "amount", "fee", "net", "currency", "description", "source"]
INVOICE_FIELDS = ["invoice_id", "customer", "status", "finalized_date", "paid_date", "subtotal_ex_tax", "tax", "total"]
INVOICE_LINE_FIELDS = ["invoice_id", "line_id", "amount", "period_start", "period_end", "subscription", "interval",
                       "interval_count"]
BANK_FIELDS = ["row", "date", "description", "amount"]
PAYROLL_FIELDS = ["check_date", "gross", "employer_taxes", "period_start", "period_end"]
OUTSTANDING_FIELDS = ["account_id", "side", "date", "description", "amount", "source_ref"]


class Manifest:
    def __init__(self, month_dir: Path, period: Period):
        self.path = month_dir / "inputs" / "manifest.json"
        base = {"period": period.key, "files": {}, "statements": {}, "attestations": {}}
        self.data = read_json(self.path) if self.path.exists() else base
        if not isinstance(self.data, dict):
            raise StopRun(f"{self.path} is not a manifest. Delete it and pull again.")
        for k, v in base.items():
            self.data.setdefault(k, v if k == "period" else type(v)())
            if k != "period" and not isinstance(self.data[k], dict):
                raise StopRun(f'{self.path}: "{k}" must be an object. Delete the manifest and pull/ingest again.')
        for k in ("stripe_balance", "qbo_pull", "outstanding_prior"):
            if k in self.data and not isinstance(self.data[k], dict):
                raise StopRun(f'{self.path}: "{k}" must be an object. Delete the manifest and pull/ingest again.')
        if self.data.get("period") != period.key:
            raise StopRun(f"{self.path} belongs to {self.data.get('period')}, not {period.key}. "
                          "Use a separate folder per month.")

    def record(self, name: str, **meta) -> None:
        self.data["files"][name] = meta
        self.save()

    def save(self) -> None:
        write_json(self.path, self.data)

    @property
    def files(self) -> dict:
        return self.data["files"]

    @property
    def statements(self) -> dict:
        return self.data["statements"]

    @property
    def attestations(self) -> dict:
        return self.data["attestations"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _accounts(month_dir: Path) -> list[dict]:
    p = month_dir / "inputs" / "accounts.csv"
    if not p.exists():
        raise StopRun("No chart of accounts for this month yet. Run `mer.py pull-qbo` first.")
    return read_csv(p)


# ---------------------------------------------------------------- Stripe

STRIPE_API = "https://api.stripe.com/v1"
# Pinned so the response shape is the one parsed below, whatever the account's default API version is. Newer
# versions moved invoice lines' price and subscription fields.
STRIPE_VERSION = "2024-06-20"


def stripe_api() -> str:
    """Stripe's API, or a local stand-in for tests (MER_STRIPE_API_BASE, loopback addresses only, so a key can never
    be sent anywhere but Stripe or this computer)."""
    base = os.environ.get("MER_STRIPE_API_BASE", "").strip().rstrip("/")
    if not base:
        return STRIPE_API
    if not re.fullmatch(r"http://(127\.0\.0\.1|localhost)(:\d+)?(/v1)?", base):
        raise StopRun("MER_STRIPE_API_BASE may only point at a test server on this computer (http://127.0.0.1:PORT).")
    return base if base.endswith("/v1") else base + "/v1"


def stripe_key(root: Path | None = None, allow_test: bool = False) -> str:
    """The restricted key from <work folder>/.env (STRIPE_API_KEY=rk_live_...), or the environment. Never printed."""
    key, src = "", ""
    env = (root / ".env") if root else None
    if env and env.exists():
        from .connect import make_private, read_env
        make_private(env)
        key, src = read_env(env).get("STRIPE_API_KEY", "").strip(), str(env)
    if not key:
        key, src = os.environ.get("STRIPE_API_KEY", "").strip(), "the STRIPE_API_KEY environment variable"
    if not key:
        where = env or "the work folder's .env"
        raise StopRun(f"No Stripe key. Add a line STRIPE_API_KEY=rk_live_... to {where} with a restricted, read-only "
                      "key (see references/stripe-setup.md), or export CSVs and use ingest-stripe.")
    if not re.fullmatch(r"rk_(live|test)_[A-Za-z0-9]+", key):
        raise StopRun(f"The Stripe key in {src} is not a restricted key (rk_live_...). This skill only accepts "
                      "restricted keys with Read access. See references/stripe-setup.md.")
    if key.startswith("rk_test_") and not allow_test:
        raise StopRun("That is a TEST-mode Stripe key, so its data isn't your real books. Use a live restricted key, "
                      "or pass --allow-test-mode if you are deliberately reviewing test data.")
    return key


def _stripe_get(key: str, path: str, params: dict) -> dict:
    req = urllib.request.Request(f"{stripe_api()}/{path}?{urllib.parse.urlencode(params, doseq=True)}",
                                 headers={"Authorization": f"Bearer {key}", "Stripe-Version": STRIPE_VERSION})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise StopRun(f"Stripe returned {e.code} for {path}: {e.read().decode(errors='replace')[:300]}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise StopRun(f"Could not reach Stripe ({getattr(e, 'reason', e)}). Nothing was saved.") from e
    except json.JSONDecodeError as e:
        raise StopRun(f"Stripe returned something that isn't JSON for {path}. Nothing was saved.") from e


def _stripe_list(key: str, path: str, params: dict) -> list[dict]:
    out, after = [], None
    while True:
        p = dict(params, limit=100)
        if after:
            p["starting_after"] = after
        page = _stripe_get(key, path, p)
        data = page.get("data", [])
        out.extend(data)
        if not page.get("has_more") or not data:
            return out
        after = data[-1]["id"]


def _ts(d) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _cents(v) -> Decimal:
    return Decimal(int(v)) / 100


def _txn_row(t: dict) -> dict:
    cur = (t.get("currency") or "").lower()
    if cur != "usd":
        raise StopRun(f"Stripe transaction {t.get('id')} is in {cur.upper()}. v0.1 handles USD only.")
    return {"id": t["id"], "date": datetime.fromtimestamp(t["created"], tz=timezone.utc).date(),
            "category": t.get("reporting_category") or t.get("type", ""),
            "amount": _cents(t["amount"]), "fee": _cents(t["fee"]), "net": _cents(t["net"]),
            "currency": cur, "description": t.get("description") or "", "source": "stripe-api"}


def pull_stripe(month_dir: Path, period: Period, manifest: Manifest, allow_test: bool = False,
                root: Path | None = None) -> list[str]:
    key = stripe_key(root, allow_test)
    start, nxt = _ts(period.start), _ts(period.shift(1).start)
    month = [_txn_row(t) for t in _stripe_list(key, "balance_transactions", {"created[gte]": start, "created[lt]": nxt})]
    after = [_txn_row(t) for t in _stripe_list(key, "balance_transactions", {"created[gte]": nxt})]
    bal = _stripe_get(key, "balance", {})
    total_now = sum((_cents(x["amount"]) for k in ("available", "pending") for x in bal.get(k, [])
                     if x.get("currency") == "usd"), Decimal(0))
    closing = total_now - sum((r["net"] for r in after), Decimal(0))
    opening = closing - sum((r["net"] for r in month), Decimal(0))
    inv_from = _ts(period.shift(-13).start)
    invoices = _stripe_list(key, "invoices", {"created[gte]": inv_from, "created[lt]": nxt,
                                              "expand[]": "data.lines.data.price"})
    inv_rows, line_rows = [], []
    for inv in invoices:
        if inv.get("status") not in ("open", "paid", "uncollectible"):
            continue
        if (inv.get("currency") or "usd").lower() != "usd":
            raise StopRun(f"Stripe invoice {inv.get('id')} is not in USD. v0.1 handles USD only.")
        st = inv.get("status_transitions") or {}
        total = _cents(inv.get("total", 0))
        ex_tax = _cents(inv["total_excluding_tax"]) if inv.get("total_excluding_tax") is not None else (
            total - _cents(inv.get("tax") or 0))
        inv_rows.append({"invoice_id": inv["id"], "customer": inv.get("customer") or "", "status": inv["status"],
                         "finalized_date": _date_or_blank(st.get("finalized_at")),
                         "paid_date": _date_or_blank(st.get("paid_at")),
                         "subtotal_ex_tax": ex_tax, "tax": total - ex_tax, "total": total})
        lines = (inv.get("lines") or {}).get("data", [])
        if (inv.get("lines") or {}).get("has_more"):
            lines = _stripe_list(key, f"invoices/{inv['id']}/lines", {})
        for ln in lines:
            disc = sum((_cents(x.get("amount", 0)) for x in ln.get("discount_amounts") or []), Decimal(0))
            per = ln.get("period") or {}
            is_sub = (ln.get("type") == "subscription" or bool(ln.get("subscription"))
                      or (ln.get("parent") or {}).get("type") == "subscription_item_details")
            price = ln.get("price") if isinstance(ln.get("price"), dict) else {}
            rec = price.get("recurring") or ln.get("plan") or {}
            line_rows.append({"invoice_id": inv["id"], "line_id": ln.get("id", ""),
                              "amount": _cents(ln.get("amount", 0)) - disc,
                              "period_start": _date_or_blank(per.get("start")),
                              "period_end": _date_or_blank(per.get("end")), "subscription": "yes" if is_sub else "no",
                              "interval": rec.get("interval") or "", "interval_count": rec.get("interval_count") or ""})
    # Everything fetched: write all files together.
    write_csv(month_dir / "inputs" / "stripe.csv", month, STRIPE_FIELDS)
    write_csv(month_dir / "inputs" / "stripe_invoices.csv", inv_rows, INVOICE_FIELDS)
    write_csv(month_dir / "inputs" / "stripe_invoice_lines.csv", line_rows, INVOICE_LINE_FIELDS)
    pulled = _now()
    manifest.record("stripe.csv", source="stripe-api", start=period.start.isoformat(), end=period.end.isoformat(),
                    timezone="UTC", rows=len(month), pulled_at=pulled)
    manifest.record("stripe_invoices.csv", source="stripe-api", rows=len(inv_rows), pulled_at=pulled,
                    created_from=period.shift(-13).start.isoformat())
    manifest.record("stripe_invoice_lines.csv", source="stripe-api", rows=len(line_rows), pulled_at=pulled)
    manifest.data["stripe_balance"] = {"opening": str(q2(opening)), "closing": str(q2(closing)), "source": "stripe-api",
                                       "how": "closing = balance now - net of transactions after month-end"}
    test_server = stripe_api() != STRIPE_API
    if test_server:
        manifest.data["stripe_balance"]["test_server"] = stripe_api()
    manifest.save()
    return ([f"TEST SERVER: this came from {stripe_api()}, not Stripe."] if test_server else []) + [
            f"stripe.csv ({len(month)} balance transactions, UTC month)",
            f"stripe_invoices.csv ({len(inv_rows)} invoices, {len(line_rows)} lines, last 13 months)",
            f"Stripe balance {opening} at the start of the month, {closing} at the end"]


def _date_or_blank(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat() if ts else ""


# Header aliases for Stripe CSV exports. Only UTC timestamps are accepted.
STRIPE_ALIASES = {
    "id": ["balance_transaction_id", "id"],
    "date": ["created_utc", "created (utc)"],
    "category": ["reporting_category", "type"],
    "amount": ["gross", "amount"],
    "fee": ["fee"],
    "net": ["net"],
    "currency": ["currency"],
    "description": ["description"],
}


def ingest_stripe_csv(month_dir: Path, period: Period, srcs: list[Path], opening: str, closing: str,
                      manifest: Manifest) -> str:
    rows, seen = [], set()
    for src in srcs:
        if not src.exists():
            raise StopRun(f"{src} does not exist.")
        header = [h.strip().lower() for h in csv_header(src)]
        col = {}
        for field, names in STRIPE_ALIASES.items():
            hit = next((n for n in names if n in header), None)
            if hit is None and field != "description":
                extra = (" Stripe exports must include the UTC timestamp column (created_utc)." if field == "date" else
                         " Use an export with fees and net included; a gross-only export is not enough.")
                raise StopRun(f"{src.name} has no {field} column (looked for: {', '.join(names)}).{extra}")
            col[field] = hit
        for i, r in enumerate(read_csv(src), 2):
            r = {(k or "").strip().lower(): v for k, v in r.items()}
            try:
                row = {"id": r[col["id"]].strip(), "date": parse_date(r[col["date"]]),
                       "category": r[col["category"]].strip().lower(), "amount": money(r[col["amount"]]),
                       "fee": money(r[col["fee"]]), "net": money(r[col["net"]]),
                       "currency": r[col["currency"]].strip().lower(),
                       "description": r.get(col["description"], "") if col["description"] else "", "source": "stripe-csv"}
            except (ValueError, KeyError) as e:
                raise StopRun(f"{src.name} row {i}: {e}. Nothing was ingested.") from e
            if not row["id"]:
                raise StopRun(f"{src.name} row {i} has no transaction id. Nothing was ingested.")
            if row["currency"] != "usd":
                raise StopRun(f"{src.name} row {i} is in {row['currency'].upper()}. v0.1 handles USD only.")
            if row["id"] in seen:
                continue   # the same transaction in two exports
            seen.add(row["id"])
            rows.append(row)
    try:
        o, c = money(opening), money(closing)
    except ValueError as e:
        raise StopRun(f"Stripe opening/closing balance is not a number: {e}.") from e
    write_csv(month_dir / "inputs" / "stripe.csv", rows, STRIPE_FIELDS)
    dates = [r["date"] for r in rows]
    manifest.record("stripe.csv", source="stripe-csv", files=[s.name for s in srcs], start=period.start.isoformat(),
                    end=period.end.isoformat(), timezone="UTC", first=min(dates).isoformat() if dates else "",
                    last=max(dates).isoformat() if dates else "", rows=len(rows))
    for name in ("stripe_invoices.csv", "stripe_invoice_lines.csv"):
        (month_dir / "inputs" / name).unlink(missing_ok=True)
        manifest.files.pop(name, None)
    manifest.data["stripe_balance"] = {"opening": str(q2(o)), "closing": str(q2(c)), "source": "typed",
                                       "how": "typed by a person from Stripe's Balance report"}
    manifest.save()
    return f"stripe.csv ({len(rows)} rows from {', '.join(s.name for s in srcs)}); balance {o} -> {c} typed"


# ---------------------------------------------------------------- bank and card statements

def ingest_statement(month_dir: Path, period: Period, cfg: dict, account: str, src: Path, ending_balance: str,
                     statement_end: str, opening_balance: str | None, statement_start: str | None,
                     manifest: Manifest) -> str:
    """One bank or card account. Balances come from the statement or the bank portal, typed by a
    person. They are never inferred from the transactions; the gate checks the rows add up to them."""
    accounts = _accounts(month_dir)
    aid, name, role = account_id_for(cfg, accounts, account, ("cash", "card"))
    fmt = None
    for k, v in (cfg.get("bank_formats") or {}).items():
        if norm(k) == norm(name):
            fmt = v
    if not fmt or not fmt.get("date_col") or not fmt.get("description_col") or not (
            fmt.get("amount_col") or (fmt.get("debit_col") and fmt.get("credit_col"))):
        raise StopRun(f'bank_formats["{name}"] in review-config.json does not say which columns hold the date, '
                      "description and amount (or debit and credit). Fill it in from the CSV header.")
    if not src.exists():
        raise StopRun(f"{src} does not exist.")
    try:
        end_bal = money(ending_balance)
        stmt_end = parse_date(statement_end)
    except ValueError as e:
        raise StopRun(f"{name}: {e}.") from e
    if not (period.start <= stmt_end <= period.end):
        raise StopRun(f"{name}: the statement must end inside {period.label} (on the last day, or mid-month for a "
                      f"card that closes mid-month). Got {stmt_end}.")

    prior = _prior_statement(month_dir, period, aid)
    if opening_balance is None and prior:
        open_bal, stmt_start, open_src = money(prior["ending_balance"]), parse_date(prior["statement_end"]), "last month's statement"
    elif opening_balance is not None:
        try:
            open_bal = money(opening_balance)
            stmt_start = parse_date(statement_start) if statement_start else period.prior.end
        except ValueError as e:
            raise StopRun(f"{name}: {e}.") from e
        open_src = "typed"
    else:
        raise StopRun(f"{name}: no opening balance. Type the statement's opening balance with --opening-balance "
                      "(and --statement-start for the day before the statement period begins, if it isn't the last "
                      "day of last month). Next month it is taken from this statement automatically.")
    if stmt_start >= stmt_end:
        raise StopRun(f"{name}: the statement start ({stmt_start}) must be before its end ({stmt_end}).")

    header = csv_header(src)
    needed = [fmt["date_col"], fmt["description_col"]] + (
        [fmt["amount_col"]] if fmt.get("amount_col") else [fmt["debit_col"], fmt["credit_col"]])
    if fmt.get("status_col"):
        needed.append(fmt["status_col"])
    missing = [h for h in needed if h not in header]
    if missing:
        raise StopRun(f"{src.name} has no column(s) {missing}. Its header is: {header}.")
    status_like = [h for h in header if re.fullmatch(r"(transaction )?status|state", h.strip().lower())]
    if status_like and not fmt.get("status_col"):
        raise StopRun(f"{src.name} has a {status_like[0]!r} column, so it may include pending or failed rows. Set "
                      f'bank_formats["{name}"].status_col to "{status_like[0]}" and posted_values to the values that '
                      "mean posted (e.g. [\"Sent\", \"Posted\"]).")
    posted = {norm(v) for v in fmt.get("posted_values") or []}
    if fmt.get("status_col") and not posted:
        raise StopRun(f'bank_formats["{name}"].posted_values is empty. List the status values that mean posted.')

    flip = -1 if fmt.get("flip_sign") is True else 1
    keep_to = max(stmt_end, period.end)
    rows, skipped = [], 0
    for i, r in enumerate(read_csv(src), 2):
        if fmt.get("status_col") and norm(r.get(fmt["status_col"], "")) not in posted:
            skipped += 1
            continue
        try:
            d = parse_date(r[fmt["date_col"]], fmt.get("date_format") or None)
            if fmt.get("amount_col"):
                amt = money(r[fmt["amount_col"]])
            else:
                dr, cr = (r[fmt["debit_col"]] or "").strip(), (r[fmt["credit_col"]] or "").strip()
                if dr and cr:
                    raise ValueError("both debit and credit are filled")
                amt = (money(cr) if cr else Decimal(0)) - (money(dr) if dr else Decimal(0))
        except ValueError as e:
            raise StopRun(f"{src.name} row {i}: {e}. Nothing was ingested.") from e
        if stmt_start < d <= keep_to:
            rows.append({"row": i, "date": d, "description": r[fmt["description_col"]], "amount": amt * flip})

    out = month_dir / "inputs" / "bank" / f"{aid}.csv"
    write_csv(out, rows, BANK_FIELDS)
    manifest.statements[aid] = {
        "account": name, "kind": role, "file": f"bank/{aid}.csv", "source_file": src.name,
        "opening_balance": str(open_bal), "opening_source": open_src, "statement_start": stmt_start.isoformat(),
        "ending_balance": str(end_bal), "statement_end": stmt_end.isoformat(),
        "rows_in_statement": sum(1 for r in rows if r["date"] <= stmt_end),
        "rows_after_statement": sum(1 for r in rows if r["date"] > stmt_end),
        "csv_last_date": max((r["date"] for r in rows), default=stmt_start).isoformat(),
        "skipped_not_posted": skipped}
    manifest.save()
    return (f"bank/{aid}.csv: {len(rows)} rows ({skipped} pending/failed skipped); "
            f"balance {open_bal} on {stmt_start} -> {end_bal} on {stmt_end}")


def _prior_statement(month_dir: Path, period: Period, aid: str) -> dict | None:
    p = month_dir.parent / period.prior.key / "inputs" / "manifest.json"
    if not p.exists():
        return None
    try:
        return (json.loads(p.read_text()).get("statements") or {}).get(aid)
    except json.JSONDecodeError:
        return None


def ingest_outstanding(month_dir: Path, period: Period, cfg: dict, account: str, src: Path,
                       manifest: Manifest) -> str:
    """Reconciling items at last month-end, from the last reconciliation. CSV columns: date, description,
    amount (book effect: deposit +, payment -), and optional side: book (in the books, not yet on the
    statement; the default) or bank (on the statement, not yet in the books)."""
    accounts = _accounts(month_dir)
    aid, name, _ = account_id_for(cfg, accounts, account, ("cash", "card"))
    if not src.exists():
        raise StopRun(f"{src} does not exist.")
    header = [h.lower() for h in csv_header(src)]
    if not {"date", "description", "amount"} <= set(header):
        raise StopRun(f"{src.name} needs columns date, description, amount. Its header is: {header}.")
    rows = []
    for i, r in enumerate(read_csv(src), 2):
        r = {(k or "").strip().lower(): v for k, v in r.items()}
        try:
            d = parse_date(r["date"])
            side = (r.get("side") or "book").strip().lower()
            if side not in ("book", "bank"):
                raise ValueError("side must be book or bank")
            rows.append({"account_id": aid, "side": side, "date": d, "description": r["description"],
                         "amount": money(r["amount"]), "source_ref": f"{src.name} row {i}"})
        except ValueError as e:
            raise StopRun(f"{src.name} row {i}: {e}. Nothing was ingested.") from e
        if d > period.prior.end:
            raise StopRun(f"{src.name} row {i} is dated {d}, after last month-end. Outstanding items must be from "
                          f"{period.prior.end} or earlier.")
    path = month_dir / "inputs" / "outstanding_prior.csv"
    existing = [r for r in (read_csv(path) if path.exists() else []) if r["account_id"] != aid]
    write_csv(path, existing + rows, OUTSTANDING_FIELDS)
    manifest.data.setdefault("outstanding_prior", {})[aid] = {"source": src.name, "rows": len(rows), "at": _now()}
    manifest.save()
    return f"{len(rows)} outstanding item(s) at {period.prior.end} recorded for {name}"


# ---------------------------------------------------------------- payroll and budget (optional)

def ingest_payroll(month_dir: Path, period: Period, cfg: dict, src: Path, manifest: Manifest) -> str:
    fmt = cfg.get("payroll_format") or {}
    for k in ("date_col", "gross_col", "employer_tax_col"):
        if not fmt.get(k):
            raise StopRun(f"payroll_format.{k} is empty in review-config.json. Fill it in from the payroll CSV header.")
    if bool(fmt.get("period_start_col")) != bool(fmt.get("period_end_col")):
        raise StopRun("payroll_format needs both period_start_col and period_end_col, or neither.")
    if not src.exists():
        raise StopRun(f"{src} does not exist.")
    header = csv_header(src)
    cols = [fmt[k] for k in ("date_col", "gross_col", "employer_tax_col", "period_start_col", "period_end_col") if fmt.get(k)]
    missing = [c for c in cols if c not in header]
    if missing:
        raise StopRun(f"{src.name} has no column(s) {missing}. Its header is: {header}.")
    window_end = add_days(period.end, 35)
    by_key: dict = {}
    for i, r in enumerate(read_csv(src), 2):
        try:
            d = parse_date(r[fmt["date_col"]], fmt.get("date_format") or None)
            g, t = money(r[fmt["gross_col"]]), money(r[fmt["employer_tax_col"]])
            ps = parse_date(r[fmt["period_start_col"]], fmt.get("date_format") or None) if fmt.get("period_start_col") else None
            pe = parse_date(r[fmt["period_end_col"]], fmt.get("date_format") or None) if fmt.get("period_end_col") else None
        except ValueError as e:
            raise StopRun(f"{src.name} row {i}: {e}. Nothing was ingested.") from e
        if ps and pe and ps > pe:
            raise StopRun(f"{src.name} row {i}: pay period starts after it ends.")
        if period.start <= d <= window_end:
            a = by_key.setdefault((d, ps, pe), [Decimal(0), Decimal(0)])
            a[0] += g
            a[1] += t
    rows = [{"check_date": d, "gross": v[0], "employer_taxes": v[1], "period_start": ps or "", "period_end": pe or ""}
            for (d, ps, pe), v in sorted(by_key.items(), key=lambda kv: kv[0][0])]
    write_csv(month_dir / "inputs" / "payroll.csv", rows, PAYROLL_FIELDS)
    manifest.record("payroll.csv", source="payroll-csv", file=src.name, start=period.start.isoformat(),
                    end=window_end.isoformat(), has_periods=bool(fmt.get("period_start_col")), rows=len(rows))
    return (f"payroll.csv ({len(rows)} pay runs dated {period.start} to {window_end}; totals only, "
            "no per-person data)")


_MONTH_COL = [
    (re.compile(r"^(\d{4})-(\d{2})$"), lambda m: (int(m[1]), int(m[2]))),
    (re.compile(r"^(\d{1,2})/(\d{4})$"), lambda m: (int(m[2]), int(m[1]))),
    (re.compile(r"^([A-Za-z]{3,9})\.?[ -]'?(\d{2}|\d{4})$"), None),
]


def _month_from_header(h: str) -> str | None:
    h = h.strip()
    for rx, fn in _MONTH_COL:
        m = rx.match(h)
        if not m:
            continue
        if fn:
            y, mo = fn(m)
        else:
            mo = next((i for i, n in enumerate(MONTH_NAMES, 1) if n.lower().startswith(m[1].lower()[:3])
                       and (len(m[1]) == 3 or n.lower().startswith(m[1].lower()))), None)
            if mo is None:
                return None
            y = int(m[2]) + (2000 if len(m[2]) == 2 else 0)
        if 1 <= mo <= 12 and 1900 <= y <= 2200:
            return f"{y:04d}-{mo:02d}"
    return None


def ingest_budget(month_dir: Path, period: Period, src: Path, manifest: Manifest) -> str:
    """Budget CSV, either long (account, month, amount) or wide (account + one column per month).
    Revenue and expenses are both positive numbers."""
    if not src.exists():
        raise StopRun(f"{src} does not exist.")
    header = csv_header(src)
    low = [h.lower() for h in header]
    if "account" not in low:
        raise StopRun(f"{src.name} needs an 'account' column. Its header is: {header}.")
    acol = header[low.index("account")]
    rows = []
    if {"month", "amount"} <= set(low):
        mcol, vcol = header[low.index("month")], header[low.index("amount")]
        for i, r in enumerate(read_csv(src), 2):
            month = _month_from_header(r[mcol]) or (r[mcol].strip()[:7] if re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", r[mcol].strip()) else None)
            if not month:
                raise StopRun(f"{src.name} row {i}: month {r[mcol]!r} isn't a month like 2026-09.")
            try:
                rows.append({"account": r[acol].strip(), "month": month, "amount": money(r[vcol])})
            except ValueError as e:
                raise StopRun(f"{src.name} row {i}: {e}. Nothing was ingested.") from e
    else:
        months = {h: _month_from_header(h) for h in header if h != acol}
        bad = [h for h, m in months.items() if m is None and h.strip().lower() not in ("total", "fy total", "")]
        seen_m = {}
        for h, m in months.items():
            if m and m in seen_m:
                raise StopRun(f"{src.name}: columns {seen_m[m]!r} and {h!r} are both {m}. Keep one.")
            if m:
                seen_m[m] = h
        if bad:
            raise StopRun(f"{src.name}: columns {bad} are not months (like 2026-09 or Sep 2026). Remove them or rename.")
        for i, r in enumerate(read_csv(src), 2):
            if not (r.get(acol) or "").strip():
                continue
            for h, m in months.items():
                if not m:
                    continue
                v = (r.get(h) or "").strip()
                if v == "":
                    continue
                try:
                    rows.append({"account": r[acol].strip(), "month": m, "amount": money(v)})
                except ValueError as e:
                    raise StopRun(f"{src.name} row {i}, column {h}: {e}. Nothing was ingested.") from e
    write_csv(month_dir / "inputs" / "budget.csv", [dict(r, account_id="") for r in rows],
              ["account_id", "account", "month", "amount"])
    manifest.record("budget.csv", source="budget-csv", file=src.name, rows=len(rows),
                    months=sorted({r["month"] for r in rows}))
    return f"budget.csv ({len(rows)} amounts, months {min(r['month'] for r in rows) if rows else '-'} to {max(r['month'] for r in rows) if rows else '-'})"


def attest(manifest: Manifest, what: str, who: str) -> str:
    """A person states something the data can't prove (a source truly had no activity, or there were
    no outstanding items). Without this, empty = missing."""
    if not who.strip():
        raise StopRun("Say who is confirming (--by NAME). This is only accepted on a person's word.")
    allowed = re.fullmatch(r"(stripe|payroll|payroll-coverage|bills|statement:.+|outstanding:.+|history:\d{4}-\d{2})", what)
    if not allowed:
        raise StopRun("--source must be stripe, payroll, payroll-coverage, bills, statement:<account>, "
                      "outstanding:<account> or history:YYYY-MM.")
    manifest.attestations[what] = {"by": who.strip(), "at": _now()}
    manifest.save()
    return f"recorded: {who.strip()} confirms {what} for {manifest.data['period']}"
