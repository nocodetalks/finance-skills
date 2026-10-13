"""QuickBooks Online: read-only pull straight to files.

Data never passes through the model: this module calls the QuickBooks Accounting
API and writes canonical CSVs. The only HTTP verbs used are GET (reports and
queries) and one POST, to Intuit's OAuth token endpoint, to swap a sign-in code
or a refresh token for an access token. Nothing here can create, change or
delete anything in QuickBooks.

Credentials: ~/.config/finance-skills/quickbooks-<profile>.json (mode 600), written
by `mer.py connect`. See references/quickbooks-setup.md.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from .util import (MONTH_NAMES, Period, StopRun, add_days, money_or_zero,
                   norm, parse_date, write_csv)

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
API_BASE = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
MINOR_VERSION = "75"
GL_COLUMNS = "tx_date,txn_type,doc_num,name,memo,split_acc,subt_nat_amount"

ACCOUNT_FIELDS = ["account_id", "name", "full_name", "account_type", "account_subtype", "classification", "active"]
TB_FIELDS = ["account_id", "account", "debit", "credit"]
GL_FIELDS = ["line_id", "txn_id", "txn_type", "date", "account_id", "account", "name", "memo", "doc_num", "amount"]
BILL_FIELDS = ["bill_id", "line_no", "vendor", "txn_date", "created_date", "doc_num", "account", "amount", "description"]
OPEN_BILL_FIELDS = ["bill_id", "vendor", "txn_date", "doc_num", "amount", "balance"]
AGING_FIELDS = ["kind", "as_of", "total", "over_90"]
BUDGET_FIELDS = ["account_id", "account", "month", "amount"]
PNL_MONTH_FIELDS = ["account_id", "account", "month", "amount"]
PNL_TOTAL_FIELDS = ["account_id", "account", "amount"]
CREDIT_FIELDS = ["kind", "id", "customer", "date", "amount"]

QBO_FILES = ["accounts.csv", "tb_current.csv", "tb_prior.csv", "gl.csv", "gl_history.csv", "pnl_monthly.csv",
             "pnl_ytd.csv", "aging.csv", "bills.csv", "open_bills.csv", "ar_credits.csv", "budget.csv"]


class QBOError(StopRun):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------- credentials

def config_home() -> Path:
    return Path(os.environ.get("MER_CONFIG_HOME", Path.home() / ".config" / "finance-skills"))


def cred_path(profile: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", profile or ""):
        raise StopRun("Profile names may use letters, numbers, - and _ only.")
    return config_home() / f"quickbooks-{profile}.json"


def check_private(p: Path) -> None:
    mode = p.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise StopRun(f"{p} can be read by other users on this computer. Run `chmod 600 {p}` and try again.")


def load_creds(profile: str) -> dict:
    p = cred_path(profile)
    if not p.exists():
        raise StopRun(
            f"QuickBooks is not connected for profile '{profile}' (no {p}).\n"
            "Connect it with `mer.py connect` (see references/quickbooks-setup.md), then run this step again."
        )
    check_private(p)
    try:
        creds = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise StopRun(f"{p} is not valid JSON ({e.msg}). Delete it and run `mer.py connect` again.") from e
    missing = [k for k in ("client_id", "client_secret", "refresh_token", "realm_id", "environment") if not creds.get(k)]
    if missing:
        raise StopRun(f"{p} is missing: {', '.join(missing)}. Run `mer.py connect` again.")
    if creds["environment"] not in API_BASE:
        raise StopRun(f"{p}: environment must be 'sandbox' or 'production'.")
    return creds


def write_private(p: Path, data: dict) -> None:
    """Write JSON readable by this user only (mode 600), replacing the file in one step."""
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def save_creds(profile: str, creds: dict) -> None:
    write_private(cred_path(profile), creds)


class TokenError(Exception):
    """Intuit's token endpoint said no. `error` is its OAuth error code (invalid_grant, invalid_client, ...)."""

    def __init__(self, status: int, error: str):
        super().__init__(f"{status} {error}".strip())
        self.status, self.error = status, error


def token_request(client_id: str, client_secret: str, form: dict) -> dict:
    """The module's one POST: Intuit's OAuth token endpoint (a sign-in code or a refresh token in, tokens out)."""
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(form).encode(), method="POST", headers={
        "Authorization": f"Basic {auth}", "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            tok = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            err = str(json.loads(e.read().decode(errors="replace")).get("error", ""))
        except (ValueError, AttributeError):
            err = ""
        finally:
            e.close()
        raise TokenError(e.code, err) from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise StopRun(f"Could not reach Intuit's sign-in service ({getattr(e, 'reason', e)}). Nothing was changed.") from e
    except json.JSONDecodeError as e:
        raise StopRun("Intuit's sign-in service returned something that isn't JSON. Nothing was changed.") from e
    if not tok.get("access_token") or not tok.get("refresh_token"):
        raise StopRun("Intuit's sign-in service returned no tokens. Nothing was changed.")
    return tok


class Client:
    def __init__(self, profile: str, creds: dict | None = None):
        self.profile = profile
        self.creds = creds or load_creds(profile)
        self.base = API_BASE[self.creds["environment"]]
        self.realm = self.creds["realm_id"]
        self.token = None

    def _refresh(self) -> None:
        try:
            tok = token_request(self.creds["client_id"], self.creds["client_secret"],
                                {"grant_type": "refresh_token", "refresh_token": self.creds["refresh_token"]})
        except TokenError as e:
            raise StopRun(
                f"QuickBooks refused the saved connection ({e}). The connection has probably expired (about 100 "
                "days unused), been disconnected in QuickBooks, or another tool used it. Run `mer.py connect` again."
            ) from e
        self.token = tok["access_token"]
        if tok.get("refresh_token") and tok["refresh_token"] != self.creds["refresh_token"]:
            self.creds["refresh_token"] = tok["refresh_token"]
            save_creds(self.profile, self.creds)

    def get(self, path: str, params: dict) -> dict:
        if self.token is None:
            self._refresh()
        params = dict(params, minorversion=MINOR_VERSION)
        url = f"{self.base}/v3/company/{self.realm}/{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, method="GET", headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise QBOError(f"QuickBooks returned {e.code} for {path}: {detail}", e.code) from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise StopRun(f"Could not reach QuickBooks ({getattr(e, 'reason', e)}). Nothing was saved.") from e
        except json.JSONDecodeError as e:
            raise StopRun(f"QuickBooks returned something that isn't JSON for {path}. Nothing was saved.") from e

    def report(self, name: str, **params) -> dict:
        data = self.get(f"reports/{name}", {k: v for k, v in params.items() if v is not None})
        if "Header" not in data or "Rows" not in data or "Columns" not in data:
            raise StopRun(f"QuickBooks {name} report came back without Header/Columns/Rows. Nothing was saved.")
        for opt in (data["Header"].get("Option") or []):
            if opt.get("Name") == "NoReportData" and str(opt.get("Value")).lower() == "true":
                data["_no_data"] = True
        return data

    def general_ledger(self, start: date, end: date, basis: str) -> dict:
        try:
            return self.report("GeneralLedger", start_date=start.isoformat(), end_date=end.isoformat(),
                               accounting_method=basis, columns=GL_COLUMNS)
        except QBOError as e:
            if e.code == 400:   # column keys rejected: fall back to the report's default columns
                return self.report("GeneralLedger", start_date=start.isoformat(), end_date=end.isoformat(),
                                   accounting_method=basis)
            raise

    def query(self, q: str) -> list[dict]:
        out, start = [], 1
        while True:
            data = self.get("query", {"query": f"{q} STARTPOSITION {start} MAXRESULTS 1000"})
            resp = data.get("QueryResponse", {})
            batch = next((v for k, v in resp.items() if isinstance(v, list)), [])
            out.extend(batch)
            if len(batch) < 1000:
                return out
            start += 1000


# ---------------------------------------------------------------- report parsing

def _colkey(col: dict) -> str:
    for m in col.get("MetaData", []) or []:
        if m.get("Name") == "ColKey":
            return m.get("Value")
    return col.get("ColType") or col.get("ColTitle") or ""


def _colmeta(col: dict, name: str):
    for m in col.get("MetaData", []) or []:
        if m.get("Name") == name:
            return m.get("Value")
    return None


def flatten(report: dict) -> tuple[list[str], list[dict]]:
    """Flatten a QBO report into rows with their section path.

    Returns (column keys, rows). Each row: {kind, path: [(name, id)], cells: [{value, id}]}.
    """
    cols = report.get("Columns", {}).get("Column", [])
    keys = [_colkey(c) for c in cols]
    out: list[dict] = []

    def walk(rows: dict, path: list):
        for row in rows.get("Row", []) or []:
            if "Header" in row or "Rows" in row or row.get("type") == "Section":
                hdr = row.get("Header", {}).get("ColData", [])
                here = path + [(hdr[0].get("value", ""), hdr[0].get("id"))] if hdr else path
                if hdr and len(hdr) > 1:
                    # A parent account's own amounts sit on its heading row (P&L, trial balance).
                    out.append({"kind": "Header", "path": path, "cells": hdr})
                if "Rows" in row:
                    walk(row["Rows"], here)
                if "Summary" in row:
                    out.append({"kind": "Summary", "path": here, "cells": row["Summary"].get("ColData", [])})
            elif "ColData" in row:
                out.append({"kind": "Data", "path": path, "cells": row["ColData"]})

    walk(report.get("Rows", {}), [])
    return keys, out


def _need(keys: list[str], key: str, report: str) -> int:
    if key not in keys:
        raise StopRun(f"QuickBooks {report} report has no '{key}' column (columns: {', '.join(keys)}). "
                      "Nothing was saved; this needs a fix in the skill, not a guess.")
    return keys.index(key)


def parse_trial_balance(report: dict) -> list[dict]:
    cols = [c.get("ColTitle", "").strip().lower() for c in report["Columns"]["Column"]]
    if "debit" not in cols or "credit" not in cols:
        raise StopRun(f"Trial Balance report has no Debit/Credit columns ({cols}). Nothing was saved.")
    di, ci = cols.index("debit"), cols.index("credit")
    _, rows = flatten(report)
    out = []
    for r in rows:
        if r["kind"] not in ("Data", "Header"):
            continue
        c = r["cells"]
        name, aid = c[0].get("value", ""), c[0].get("id")
        if not aid or len(c) <= max(di, ci):
            continue
        out.append({"account_id": aid, "account": name,
                    "debit": money_or_zero(c[di].get("value")),
                    "credit": money_or_zero(c[ci].get("value"))})
    return out


def parse_general_ledger(report: dict, accounts: dict[str, dict], tag: str = "") -> list[dict]:
    """GL detail -> one row per line, amounts made debit-positive.

    QuickBooks shows GL amounts in each account's natural direction (an increase is
    positive). Asset and expense accounts are debit-natural; the rest are flipped.
    The gate proves this: every transaction must sum to zero (G4), balance-sheet
    movement must equal the trial balance, and P&L must equal the P&L report.
    """
    keys, rows = flatten(report)
    i_date = _need(keys, "tx_date", "General Ledger")
    i_type = _need(keys, "txn_type", "General Ledger")
    i_amt = _need(keys, "subt_nat_amount", "General Ledger")
    i_name = keys.index("name") if "name" in keys else None
    i_memo = keys.index("memo") if "memo" in keys else None
    i_doc = keys.index("doc_num") if "doc_num" in keys else None
    by_full = {}
    for k, a in accounts.items():
        by_full.setdefault(norm(a.get("full_name", "")), k)
    out, seq = [], {}
    for r in rows:
        if r["kind"] != "Data":
            continue
        c = r["cells"]
        raw_date = (c[i_date].get("value") or "").strip()
        if raw_date == "" or raw_date.lower().startswith("beginning balance"):
            continue
        natural = money_or_zero(c[i_amt].get("value"))
        names = [n for n, _ in r["path"]]
        aid = r["path"][-1][1] if r["path"] else None       # the nearest heading only, never a parent's id
        if not aid and names:
            aid = by_full.get(norm(":".join(names)))           # parent accounts come back without an id
        if not aid:
            if natural == 0:
                continue   # QuickBooks' zero-amount system lines ("Not Specified", linking credits to charges)
            raise StopRun(f"General Ledger line dated {raw_date} for {natural} sits under "
                          f"\"{' > '.join(names) or 'no heading'}\", which isn't an account in the chart of accounts. "
                          "Nothing was saved.")
        if aid not in accounts:
            raise StopRun(f"General Ledger account id {aid} ({names[-1] if names else '?'}) is not in the chart of "
                          "accounts. Pull again.")
        debit_natural = accounts[aid]["classification"] in ("Asset", "Expense")
        amount = natural if debit_natural else -natural
        txn_id = c[i_type].get("id") or ""
        if not txn_id:
            raise StopRun(f"General Ledger line dated {raw_date} has no transaction id. Nothing was saved.")
        k = (txn_id, aid)
        seq[k] = seq.get(k, 0) + 1
        out.append({
            "line_id": f"{tag}{txn_id}-{aid}-{seq[k]}",
            "txn_id": txn_id, "txn_type": c[i_type].get("value", ""),
            "date": parse_date(raw_date), "account_id": aid, "account": accounts[aid]["full_name"],
            "name": c[i_name].get("value", "") if i_name is not None else "",
            "memo": c[i_memo].get("value", "") if i_memo is not None else "",
            "doc_num": c[i_doc].get("value", "") if i_doc is not None else "",
            "amount": amount,
        })
    return out


def _month_of_column(col: dict) -> str | None:
    sd = _colmeta(col, "StartDate")
    if sd:
        return sd[:7]
    title = (col.get("ColTitle") or "").strip()
    m = re.fullmatch(r"([A-Za-z]{3,9})\.? (\d{4})", title)
    if m:
        for i, name in enumerate(MONTH_NAMES, 1):
            if name.lower().startswith(m.group(1).lower()[:3]):
                return f"{m.group(2)}-{i:02d}"
    return None


def parse_pnl_monthly(report: dict) -> list[dict]:
    cols = report["Columns"]["Column"]
    months = [(_month_of_column(c), i) for i, c in enumerate(cols)]
    months = [(m, i) for m, i in months if m]
    if not months:
        raise StopRun("Profit and Loss by month came back without month columns. Nothing was saved.")
    _, rows = flatten(report)
    out = []
    for r in rows:
        if r["kind"] not in ("Data", "Header"):
            continue
        c = r["cells"]
        aid = c[0].get("id")
        if not aid:
            continue
        for m, i in months:
            if i >= len(c):
                continue
            v = money_or_zero(c[i].get("value"))
            if v != 0:
                out.append({"account_id": aid, "account": c[0].get("value", ""), "month": m, "amount": v})
    return out


def parse_pnl_total(report: dict) -> list[dict]:
    _, rows = flatten(report)
    out = []
    for r in rows:
        if r["kind"] not in ("Data", "Header"):
            continue
        c = r["cells"]
        if c and c[0].get("id") and len(c) > 1:
            out.append({"account_id": c[0]["id"], "account": c[0].get("value", ""),
                        "amount": money_or_zero(c[-1].get("value"))})
    return out


def parse_aging(report: dict, kind: str, as_of: str) -> dict:
    cols = [(c.get("ColTitle") or "").strip().lower() for c in report["Columns"]["Column"]]
    _, rows = flatten(report)
    totals = [r for r in rows if r["cells"] and (r["cells"][0].get("value", "").strip().upper() == "TOTAL")]
    if not totals:
        if report.get("_no_data"):
            return {"kind": kind, "as_of": as_of, "total": money_or_zero("0"), "over_90": money_or_zero("0")}
        raise StopRun(f"{kind.upper()} aging report has no TOTAL row. Nothing was saved.")
    cells = totals[-1]["cells"]
    over = [i for i, t in enumerate(cols) if re.search(r"91|over 90|> ?90", t)]
    over_90 = sum((money_or_zero(cells[i].get("value")) for i in over if i < len(cells)), money_or_zero("0"))
    return {"kind": kind, "as_of": as_of, "total": money_or_zero(cells[-1].get("value")), "over_90": over_90}


def parse_accounts(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for a in items:
        if a["Id"] in seen:
            continue
        seen.add(a["Id"])
        out.append({
            "account_id": a["Id"], "name": a.get("Name", ""), "full_name": a.get("FullyQualifiedName", a.get("Name", "")),
            "account_type": a.get("AccountType", ""), "account_subtype": a.get("AccountSubType", ""),
            "classification": a.get("Classification", ""), "active": str(a.get("Active", True)).lower(),
        })
    return out


def parse_bills(items: list[dict]) -> list[dict]:
    out = []
    for b in items:
        created = (b.get("MetaData") or {}).get("CreateTime", "")
        for n, line in enumerate(b.get("Line", []) or [], 1):
            det = line.get("AccountBasedExpenseLineDetail") or {}
            item = line.get("ItemBasedExpenseLineDetail") or {}
            acct = (det.get("AccountRef") or {}).get("name") or (item.get("ItemRef") or {}).get("name", "")
            out.append({
                "bill_id": b["Id"], "line_no": n, "vendor": (b.get("VendorRef") or {}).get("name", ""),
                "txn_date": b.get("TxnDate", ""), "created_date": parse_date(created).isoformat() if created else "",
                "doc_num": b.get("DocNumber", ""), "account": acct,
                "amount": money_or_zero(line.get("Amount")),
                "description": " ".join(x for x in (line.get("Description"), b.get("PrivateNote")) if x),
            })
    return out


def parse_open_bills(items: list[dict]) -> list[dict]:
    return [{"bill_id": b["Id"], "vendor": (b.get("VendorRef") or {}).get("name", ""), "txn_date": b.get("TxnDate", ""),
             "doc_num": b.get("DocNumber", ""), "amount": money_or_zero(b.get("TotalAmt")),
             "balance": money_or_zero(b.get("Balance"))} for b in items if money_or_zero(b.get("Balance")) > 0]


def parse_budget(items: list[dict], period: Period, accounts: dict[str, dict], name: str | None) -> list[dict] | None:
    cands = [b for b in items if str(b.get("Active", True)).lower() == "true"
             and b.get("BudgetType", "ProfitAndLoss") == "ProfitAndLoss"
             and b.get("StartDate", "") <= period.end.isoformat() <= b.get("EndDate", "9999")]
    if name:
        cands = [b for b in cands if b.get("Name") == name]
    if not cands:
        return None
    if len(cands) > 1:
        raise StopRun("More than one active budget covers this month: "
                      + ", ".join(b.get("Name", "?") for b in cands)
                      + '. Set "budget_name" in review-config.json to choose one.')
    sums: dict[tuple, object] = {}
    for d in cands[0].get("BudgetDetail", []) or []:
        aid = (d.get("AccountRef") or {}).get("value")
        month = (d.get("BudgetDate") or "")[:7]
        if not aid or not month:
            continue
        sums[(aid, month)] = sums.get((aid, month), 0) + money_or_zero(d.get("Amount"))
    return [{"account_id": a, "account": accounts.get(a, {}).get("full_name", a), "month": m, "amount": v}
            for (a, m), v in sorted(sums.items())]


def parse_preferences(items: list[dict]) -> dict:
    if not items:
        return {}
    p = items[0]
    out = {}
    fy = ((p.get("AccountingInfoPrefs") or {}).get("FirstMonthOfFiscalYear") or "").strip()
    for i, name in enumerate(MONTH_NAMES, 1):
        if fy.lower() == name.lower():
            out["fiscal_year_start_month"] = i
    basis = ((p.get("ReportPrefs") or {}).get("ReportBasis") or "").strip()
    if basis in ("Accrual", "Cash"):
        out["basis"] = basis
    return out


def fy_start(p: Period, fy_month: int) -> date:
    y = p.year if p.month >= fy_month else p.year - 1
    return date(y, fy_month, 1)


# ---------------------------------------------------------------- pull

def pull(month_dir: Path, period: Period, cfg: dict | None, profile: str, manifest) -> list[str]:
    """Pull everything QuickBooks provides for the month, all or nothing."""
    c = Client(profile)
    inputs = month_dir / "inputs"
    tmp = inputs / ".pull-tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    pulled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest.data["qbo_pull"] = {"complete": False, "started_at": pulled_at, "profile": profile}
    manifest.save()
    meta, saved = {}, []

    prefs = parse_preferences(c.query("SELECT * FROM Preferences"))
    basis = (cfg or {}).get("basis") or prefs.get("basis") or "Accrual"
    fy_month = (cfg or {}).get("fiscal_year_start_month") or prefs.get("fiscal_year_start_month") or 1
    history_months = int(((cfg or {}).get("thresholds") or {}).get("history_months", 6))

    accounts = parse_accounts(c.query("SELECT * FROM Account WHERE Active = true")
                              + c.query("SELECT * FROM Account WHERE Active = false"))
    write_csv(tmp / "accounts.csv", accounts, ACCOUNT_FIELDS)
    meta["accounts.csv"] = {"rows": len(accounts)}
    acc = {a["account_id"]: a for a in accounts}
    saved.append(f"accounts.csv ({len(accounts)} accounts)")

    for fname, p in (("tb_current.csv", period), ("tb_prior.csv", period.prior)):
        rep = c.report("TrialBalance", start_date=fy_start(p, fy_month).isoformat(), end_date=p.end.isoformat(),
                       accounting_method=basis)
        rows = parse_trial_balance(rep)
        write_csv(tmp / fname, rows, TB_FIELDS)
        meta[fname] = {"as_of": p.end.isoformat(), "report_end": rep["Header"].get("EndPeriod", ""), "rows": len(rows)}
        saved.append(f"{fname} (as of {p.end})")

    rep = c.general_ledger(period.start, period.end, basis)
    gl = parse_general_ledger(rep, acc)
    write_csv(tmp / "gl.csv", gl, GL_FIELDS)
    meta["gl.csv"] = {"start": period.start.isoformat(), "end": period.end.isoformat(),
                      "report_start": rep["Header"].get("StartPeriod", ""), "report_end": rep["Header"].get("EndPeriod", ""),
                      "rows": len(gl)}
    saved.append(f"gl.csv ({len(gl)} lines)")

    hist, months = [], []
    for n in range(history_months, 0, -1):
        p = period.shift(-n)
        rep = c.general_ledger(p.start, p.end, basis)
        hist.extend(parse_general_ledger(rep, acc, tag=f"h{p.key}-"))
        months.append(p.key)
    write_csv(tmp / "gl_history.csv", hist, GL_FIELDS)
    meta["gl_history.csv"] = {"months": months, "rows": len(hist)}
    saved.append(f"gl_history.csv ({len(hist)} lines, {months[0] if months else '-'} to {months[-1] if months else '-'})")

    first = period.shift(-history_months) if history_months else period
    rep = c.report("ProfitAndLoss", start_date=first.start.isoformat(), end_date=period.end.isoformat(),
                   accounting_method=basis, summarize_column_by="Month")
    pm = parse_pnl_monthly(rep)
    write_csv(tmp / "pnl_monthly.csv", pm, PNL_MONTH_FIELDS)
    meta["pnl_monthly.csv"] = {"start": first.start.isoformat(), "end": period.end.isoformat(), "rows": len(pm)}

    ys = fy_start(period, fy_month)
    rep = c.report("ProfitAndLoss", start_date=ys.isoformat(), end_date=period.end.isoformat(),
                   accounting_method=basis, summarize_column_by="Total")
    py = parse_pnl_total(rep)
    write_csv(tmp / "pnl_ytd.csv", py, PNL_TOTAL_FIELDS)
    meta["pnl_ytd.csv"] = {"start": ys.isoformat(), "end": period.end.isoformat(), "rows": len(py)}
    saved.append(f"P&L by month and year to date (fiscal year from {ys})")

    aging = [
        parse_aging(c.report("AgedReceivables", report_date=period.end.isoformat(), aging_method="Report_Date"),
                    "ar", period.end.isoformat()),
        parse_aging(c.report("AgedPayables", report_date=period.end.isoformat(), aging_method="Report_Date"),
                    "ap", period.end.isoformat()),
    ]
    write_csv(tmp / "aging.csv", aging, AGING_FIELDS)
    meta["aging.csv"] = {"as_of": period.end.isoformat(), "rows": 2}
    saved.append("aging.csv (AR and AP totals, over-90 buckets)")

    to = add_days(period.end, 20)
    bills = parse_bills(c.query(f"SELECT * FROM Bill WHERE TxnDate >= '{period.start}' AND TxnDate <= '{to}'"))
    write_csv(tmp / "bills.csv", bills, BILL_FIELDS)
    meta["bills.csv"] = {"start": period.start.isoformat(), "end": to.isoformat(), "rows": len(bills)}
    ob = parse_open_bills(c.query("SELECT * FROM Bill WHERE Balance > '0'"))
    write_csv(tmp / "open_bills.csv", ob, OPEN_BILL_FIELDS)
    meta["open_bills.csv"] = {"as_of_pull": pulled_at, "rows": len(ob)}
    saved.append(f"bills.csv ({len(bills)} bill lines), open_bills.csv ({len(ob)} open)")

    since = add_days(period.start, -365)
    credits = [{"kind": "unapplied_payment", "id": x["Id"], "customer": (x.get("CustomerRef") or {}).get("name", ""),
                "date": x.get("TxnDate", ""), "amount": money_or_zero(x.get("UnappliedAmt"))}
               for x in c.query(f"SELECT * FROM Payment WHERE TxnDate >= '{since}' AND TxnDate <= '{period.end}'")
               if money_or_zero(x.get("UnappliedAmt")) > 0]
    credits += [{"kind": "credit_memo", "id": x["Id"], "customer": (x.get("CustomerRef") or {}).get("name", ""),
                 "date": x.get("TxnDate", ""), "amount": money_or_zero(x.get("RemainingCredit"))}
                for x in c.query(f"SELECT * FROM CreditMemo WHERE TxnDate >= '{since}' AND TxnDate <= '{period.end}'")
                if money_or_zero(x.get("RemainingCredit")) > 0]
    write_csv(tmp / "ar_credits.csv", credits, CREDIT_FIELDS)
    meta["ar_credits.csv"] = {"as_of_pull": pulled_at, "rows": len(credits)}

    budget = parse_budget(c.query("SELECT * FROM Budget"), period, acc, (cfg or {}).get("budget_name"))
    if budget:
        write_csv(tmp / "budget.csv", budget, BUDGET_FIELDS)
        meta["budget.csv"] = {"rows": len(budget)}
        saved.append(f"budget.csv ({len(budget)} lines)")
    else:
        saved.append("no QuickBooks budget covers this month")

    # All pulled: swap in. Remove earlier QuickBooks files this pull did not produce.
    for name in QBO_FILES:
        old = inputs / name
        if old.exists() and manifest.files.get(name, {}).get("source") == "quickbooks" and name not in meta:
            old.unlink()
            manifest.files.pop(name, None)
    for name, m in meta.items():
        (tmp / name).replace(inputs / name)
        manifest.files[name] = dict(m, source="quickbooks", pulled_at=pulled_at)
    shutil.rmtree(tmp)
    manifest.data["qbo_prefs"] = prefs
    manifest.data["qbo_pull"] = {"complete": True, "started_at": pulled_at, "profile": profile, "basis": basis,
                                 "fiscal_year_start_month": fy_month, "history_months": history_months}
    manifest.save()
    return saved
