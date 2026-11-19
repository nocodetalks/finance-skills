"""Command line: bankrec.py <command> --root <work folder> --month YYYY-MM.

Exit codes: 0 done · 2 stopped (missing or wrong input; read the message) · 3 error (a bug or unexpected data;
use nothing from the run).
"""

from __future__ import annotations

import argparse
import shutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, inputs, reconcile, report
from .util import Period, StopRun, read_json


def _ctx(a):
    root = Path(a.root).expanduser().resolve()
    period = Period(a.month)
    month_dir = root / period.key
    (month_dir / "inputs").mkdir(parents=True, exist_ok=True)
    return root, period, month_dir


def _cols(a, keys):
    return {k: getattr(a, f"{k}_col", None) for k in keys}


def _dates(a, period):
    return (a.start or period.start.isoformat(), a.end or period.end.isoformat())


def cmd_add_books(a):
    root, period, month_dir = _ctx(a)
    s, e = _dates(a, period)
    print(inputs.add_books(month_dir, a.account, Path(a.file).expanduser(), a.opening, a.closing, s, e, a.kind,
                           a.source, _cols(a, inputs.BOOK_ALIASES), a.no_activity))


def cmd_add_statement(a):
    root, period, month_dir = _ctx(a)
    s, e = _dates(a, period)
    print(inputs.add_statement(month_dir, a.account, Path(a.file).expanduser(), a.opening, a.closing, s, e,
                               a.flip_sign, a.date_format, _cols(a, ["date", "description", "amount", "debit",
                                                                     "credit", "status"]),
                               a.posted or [], a.trim, a.no_activity))


def cmd_add_stripe(a):
    root, period, month_dir = _ctx(a)
    print(inputs.add_stripe(month_dir, Path(a.file).expanduser(), a.clearing_account))


def cmd_add_outstanding(a):
    root, period, month_dir = _ctx(a)
    if bool(a.file) == bool(a.none):
        raise StopRun("Give either --file <csv> or --none --by \"<name>\".")
    if a.none and not (a.by or "").strip():
        raise StopRun("--none needs --by \"<name of the person who confirmed it>\".")
    print(inputs.add_outstanding(month_dir, a.account, Path(a.file).expanduser() if a.file else None,
                                 a.by if a.none else None))


def _cfg(root: Path) -> dict:
    p = root / "bankrec-config.json"
    if not p.exists():
        return {}
    cfg = read_json(p)
    bad = [k for k in cfg if k not in reconcile.DEFAULTS]
    if bad:
        raise StopRun(f"bankrec-config.json has keys this doesn't know: {', '.join(bad)}. "
                      f"Allowed: {', '.join(reconcile.DEFAULTS)}.")
    for k, v in cfg.items():
        if not isinstance(v, int) or isinstance(v, bool) or v < 0 or v > 365:
            raise StopRun(f"bankrec-config.json: {k} must be a whole number of days from 0 to 365, got {v!r}.")
    return cfg


def cmd_run(a):
    root, period, month_dir = _ctx(a)
    out = month_dir / "output"
    data = reconcile.load(month_dir, root, period.prior.key)
    res = reconcile.run(data, _cfg(root))
    if out.exists():
        prev = month_dir / "output.previous"
        shutil.rmtree(prev, ignore_errors=True)
        out.rename(prev)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    files = report.write_all(out, period.label, res, stamp)
    print(report.summary(out))
    print("Files:\n  " + "\n  ".join(str(f) for f in files))


def cmd_summary(a):
    root, period, month_dir = _ctx(a)
    print(report.summary(month_dir / "output"), end="")


def cmd_status(a):
    root, period, month_dir = _ctx(a)
    m = inputs.Manifest(month_dir).data
    if not m.get("accounts") and not m.get("stripe"):
        print(f"Nothing added for {period.key} yet.")
        return
    for name, acc in m.get("accounts", {}).items():
        parts = [f"books: {acc['books']['lines']} lines" if acc.get("books") else "books: MISSING",
                 f"statement: {acc['statement']['rows']} rows" if acc.get("statement") else "statement: MISSING"]
        o = acc.get("outstanding")
        parts.append("uncleared at start: " + ("none (confirmed)" if o and o.get("none") else
                                               f"{o['rows']} items" if o else "from last run, or not needed"))
        print(f"{name} ({acc.get('kind', 'bank')}): " + "; ".join(parts))
    if m.get("stripe"):
        print(f"Stripe: {m['stripe']['rows']} rows")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bankrec.py", description="Read-only bank reconciliation for QuickBooks.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--root", default="./bank-rec", help="work folder (default ./bank-rec)")
        sp.add_argument("--month", required=True, help="YYYY-MM: the month the statement closes in")

    def period(sp):
        sp.add_argument("--start", help="statement start date YYYY-MM-DD (default: the 1st)")
        sp.add_argument("--end", help="statement end date YYYY-MM-DD (default: the last day of the month)")

    b = sub.add_parser("add-books", help="the account's lines from QuickBooks, with its balances")
    common(b); period(b)
    b.add_argument("--account", required=True)
    b.add_argument("--file", required=True)
    b.add_argument("--opening", required=True, help="QuickBooks balance the day before --start")
    b.add_argument("--closing", required=True, help="QuickBooks balance on --end")
    b.add_argument("--kind", default="bank", choices=["bank", "card"])
    b.add_argument("--source", required=True, help="where the lines came from (connector and report, or export)")
    b.add_argument("--no-activity", action="store_true")
    for k in inputs.BOOK_ALIASES:
        b.add_argument(f"--{k.replace('_', '-')}-col", dest=f"{k}_col")
    b.set_defaults(fn=cmd_add_books)

    s = sub.add_parser("add-statement", help="the bank or card statement export, with its balances")
    common(s); period(s)
    s.add_argument("--account", required=True)
    s.add_argument("--file", required=True)
    s.add_argument("--opening", required=True, help="opening balance printed on the statement")
    s.add_argument("--closing", required=True, help="closing balance printed on the statement")
    s.add_argument("--flip-sign", action="store_true")
    s.add_argument("--date-format", help="e.g. %%d/%%m/%%Y for day-first dates")
    s.add_argument("--posted", action="append", help="status value meaning posted (repeatable)")
    s.add_argument("--trim", action="store_true", help="drop rows outside the statement period")
    s.add_argument("--no-activity", action="store_true")
    for k in ("date", "description", "amount", "debit", "credit", "status"):
        s.add_argument(f"--{k}-col", dest=f"{k}_col")
    s.set_defaults(fn=cmd_add_statement)

    st = sub.add_parser("add-stripe", help="optional: Stripe balance transactions export")
    common(st)
    st.add_argument("--file", required=True)
    st.add_argument("--clearing-account", help="name of the Stripe clearing account in QuickBooks, if there is one")
    st.set_defaults(fn=cmd_add_stripe)

    o = sub.add_parser("add-outstanding", help="what was uncleared at the start (first run only)")
    common(o)
    o.add_argument("--account", required=True)
    o.add_argument("--file")
    o.add_argument("--none", action="store_true")
    o.add_argument("--by")
    o.set_defaults(fn=cmd_add_outstanding)

    for name, fn, h in (("run", cmd_run, "reconcile everything added for the month"),
                        ("summary", cmd_summary, "print the last run's summary"),
                        ("status", cmd_status, "what's been added so far")):
        x = sub.add_parser(name, help=h)
        common(x)
        x.set_defaults(fn=fn)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        a.fn(a)
        return 0
    except StopRun as e:
        print(f"STOPPED: {e}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        print("ERROR: something unexpected happened. Use nothing from this run, and report the error above.",
              file=sys.stderr)
        return 3
