"""Command line: mer.py <command> --root <folder> --month YYYY-MM (connect takes no month).

Exit codes: 0 done · 1 verify found problems · 2 stopped (missing or wrong input) · 3 error (a bug or
unexpected data; nothing from this run should be used).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import traceback
from pathlib import Path

from . import __version__, config, connect, inputs, qbo
from .checks import run_all
from .data import load
from .gate import gate_report, run_gate
from .outputs import PLACEHOLDER, write_all
from .util import Period, StopRun, read_csv, write_json
from .verify import verify


def _ctx(a):
    root = Path(a.root).expanduser().resolve()
    period = Period(a.month)
    month_dir = root / period.key
    (month_dir / "inputs").mkdir(parents=True, exist_ok=True)
    return root, period, month_dir


def cmd_connect(a):
    if a.check:
        print(connect.check(a.profile))
    elif a.finish:
        print(connect.finish(a.profile, a.finish))
    else:
        print(connect.start(Path(a.root).expanduser().resolve(), a.profile, a.environment, a.env_file,
                            a.redirect_uri, a.replace, not a.no_open))
    return 0


def cmd_pull_qbo(a):
    root, period, month_dir = _ctx(a)
    cfg = config.load(root) if config.config_path(root).exists() else None
    saved = qbo.pull(month_dir, period, cfg, a.profile, inputs.Manifest(month_dir, period))
    print("Pulled from QuickBooks (read-only):\n  " + "\n  ".join(saved))


def cmd_init(a):
    root, period, month_dir = _ctx(a)
    if config.config_path(root).exists():
        raise StopRun(f"{config.config_path(root)} already exists. Edit it instead; init never overwrites a config.")
    acc = month_dir / "inputs" / "accounts.csv"
    if not acc.exists():
        raise StopRun(f"No chart of accounts at {acc}. Run `mer.py pull-qbo` first.")
    prefs = inputs.Manifest(month_dir, period).data.get("qbo_prefs") or {}
    cfg = config.propose(read_csv(acc), a.company, prefs)
    p = config.save(root, cfg)
    print(f"Proposed config written to {p}.")
    print(f"Basis {cfg['basis']}, fiscal year starts in month {cfg['fiscal_year_start_month']} (from QuickBooks preferences).")
    print("Proposed account roles (a person must check these):")
    for role, names in cfg["roles"].items():
        print(f"  {role:19} {', '.join(names) if names else '(none found)'}")
    print("Nothing runs until a person has checked them and you run `mer.py confirm-roles --by <their name>`.")


def cmd_confirm_roles(a):
    root, _, _ = _ctx(a)
    raw = config.confirm(root, a.by)
    print(f"Roles confirmed by {a.by.strip()}. If the roles change later, they must be confirmed again.")
    for role, names in (raw.get("roles") or {}).items():
        if names:
            print(f"  {role:19} {', '.join(names)}")


def cmd_pull_stripe(a):
    root, period, month_dir = _ctx(a)
    print("\n".join(inputs.pull_stripe(month_dir, period, inputs.Manifest(month_dir, period), a.allow_test_mode, root)))


def cmd_ingest_stripe(a):
    _, period, month_dir = _ctx(a)
    print(inputs.ingest_stripe_csv(month_dir, period, [Path(f).expanduser() for f in a.file], a.opening_balance,
                                   a.closing_balance, inputs.Manifest(month_dir, period)))


def cmd_ingest_statement(a):
    root, period, month_dir = _ctx(a)
    print(inputs.ingest_statement(month_dir, period, config.load(root), a.account, Path(a.file).expanduser(),
                                  a.ending_balance, a.statement_end, a.opening_balance, a.statement_start,
                                  inputs.Manifest(month_dir, period)))


def cmd_ingest_outstanding(a):
    root, period, month_dir = _ctx(a)
    print(inputs.ingest_outstanding(month_dir, period, config.load(root), a.account, Path(a.file).expanduser(),
                                    inputs.Manifest(month_dir, period)))


def cmd_ingest_payroll(a):
    root, period, month_dir = _ctx(a)
    print(inputs.ingest_payroll(month_dir, period, config.load(root), Path(a.file).expanduser(),
                                inputs.Manifest(month_dir, period)))


def cmd_ingest_budget(a):
    _, period, month_dir = _ctx(a)
    print(inputs.ingest_budget(month_dir, period, Path(a.file).expanduser(), inputs.Manifest(month_dir, period)))


def cmd_attest(a):
    root, period, month_dir = _ctx(a)
    src = a.source
    for prefix in ("statement:", "outstanding:"):
        if src.startswith(prefix):
            accounts = read_csv(month_dir / "inputs" / "accounts.csv") if (month_dir / "inputs" / "accounts.csv").exists() else []
            if not accounts:
                raise StopRun("Pull QuickBooks first so the account can be identified.")
            aid, _, _ = config.account_id_for(config.load(root), accounts, src[len(prefix):], ("cash", "card"))
            src = prefix + aid
    print(inputs.attest(inputs.Manifest(month_dir, period), src, a.by))


def cmd_acknowledge(a):
    root, period, month_dir = _ctx(a)
    rp = month_dir / "output" / "results.json"
    if not rp.exists():
        raise StopRun("No review for this month. Run `mer.py run` first.")
    res = json.loads(rp.read_text())
    e = next((x for x in res["exceptions"] if x["id"] == a.id), None)
    if not e:
        raise StopRun(f"{a.id} is not an open item in this month's review.")
    if e.get("pin"):
        raise StopRun(f"{a.id} is an integrity item (something doesn't tie). It can't be acknowledged, only fixed.")
    if not a.by.strip() or not a.reason.strip():
        raise StopRun("Say who is acknowledging (--by) and why (--reason).")
    until = a.until or period.shift(int(config.load(root)["thresholds"]["ack_months"]) - 1).key
    if Period(until).key < period.key:
        raise StopRun(f"--until {until} is before {period.key}; the acknowledgement would never apply.")
    from .checks.post import load_acks
    acks = [x for x in load_acks(root) if x.get("fingerprint") != e["fingerprint"]]
    from datetime import datetime, timezone
    acks.append({"fingerprint": e["fingerprint"], "title": e["title"], "by": a.by.strip(), "reason": a.reason.strip(),
                 "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "from": period.key, "until": until,
                 "amount": str(e["amount"])})
    write_json(root / "acknowledged.json", acks)
    print(f"Acknowledged {a.id} ({e['title']}) until {until}. It stays listed but no longer counts as open. Re-run to update.")


def _written(out: Path) -> bool:
    from .outputs import MARKERS
    for name, pairs in MARKERS.items():
        p = out / name
        if not p.exists():
            continue
        t = p.read_text(encoding="utf-8")
        for s_, e_ in pairs:
            if s_ in t and e_ in t:
                body = t.split(s_, 1)[1].split(e_, 1)[0]
                if body.strip() and PLACEHOLDER not in body:
                    return True
    return False


def _retire_output(month_dir: Path) -> None:
    """Keep the last good review in output.previous/. A stop or crash report is simply removed."""
    out, prev = month_dir / "output", month_dir / "output.previous"
    if not out.exists():
        return
    if (out / "review.md").exists():
        if prev.exists():
            shutil.rmtree(prev)
        out.rename(prev)
    else:
        shutil.rmtree(out)


def _gate(a, root, period, month_dir):
    cfg = config.load(root)
    d = load(month_dir, period, cfg)
    fails = run_gate(d)
    if fails:
        out = month_dir / "output"
        out.mkdir(parents=True, exist_ok=True)
        report = gate_report(d, fails)
        (out / "gate-report.md").write_text(report, encoding="utf-8")
        print(report)
        print(f"(saved to {out / 'gate-report.md'})")
    return d, not fails


def cmd_gate(a):
    root, period, month_dir = _ctx(a)
    cfg = config.load(root)
    fails = run_gate(load(month_dir, period, cfg))
    if fails:
        print(gate_report(load(month_dir, period, cfg), fails))
        return 2
    print("Gate passed: every required input is here, for the right dates, the right shape, adds up, and isn't empty.")
    return 0


def cmd_run(a):
    root, period, month_dir = _ctx(a)
    out = month_dir / "output"
    if _written(out) and not a.force:
        raise StopRun("This month's review already has commentary written. Re-running replaces it (the old review is "
                      "kept in output.previous/). Pass --force to re-run.")
    _retire_output(month_dir)
    try:
        try:
            d, ok = _gate(a, root, period, month_dir)
        except StopRun as e:
            out.mkdir(parents=True, exist_ok=True)
            (out / "gate-report.md").write_text(f"# Stopped\n\nThe review did not run. Nothing was checked or written.\n\n"
                                                f"{e}\n", encoding="utf-8")
            raise
        if not ok:
            return 2
        results, exc, ctx = run_all(d, root)
        tmp = month_dir / "output.tmp"
        if tmp.exists():
            shutil.rmtree(tmp)
        res = write_all(d, tmp, results, exc, ctx)
        tmp.rename(out)
    except StopRun:
        raise
    except Exception:
        out.mkdir(parents=True, exist_ok=True)
        (out / "error-report.md").write_text("# Error\n\nThe review crashed, so nothing from this run exists. This is a "
                                             "bug or unexpected data, not a problem with the books.\n\n```\n"
                                             + traceback.format_exc() + "```\n", encoding="utf-8")
        raise
    c = res["counts"]
    print(f"{d.cfg.get('company', '')} {d.period.label}: {c['checks_run']} checks run, {c['checks_pass']} pass, "
          f"{c['checks_flag']} need you, {c['checks_not_run']} not run. {c['open_items']} open item(s) "
          f"({c['open_high']} high){', ' + str(c['acknowledged']) + ' acknowledged' if c['acknowledged'] else ''}.")
    for e in res["exceptions"][:12]:
        print(f"  {e['id']} [{e['severity']}] {e['title']}")
    print(f"Review: {out / 'review.md'}")
    print("Next: write the commentary, questions and investor-update notes between the markers, then `mer.py verify`.")
    return 0


def cmd_summary(a):
    _, _, month_dir = _ctx(a)
    rp = month_dir / "output" / "results.json"
    if not rp.exists():
        raise StopRun("No review for this month. Run `mer.py run` first.")
    from .outputs import summary
    print(summary(json.loads(rp.read_text())))
    return 0


def cmd_verify(a):
    _, _, month_dir = _ctx(a)
    problems = verify(month_dir)
    if problems:
        print("Verify failed. Fix these using only numbers from results.json (never by editing results.json):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("Verify passed: every number in the commentary, questions and investor notes comes from the checks, and "
          "nothing else in the review was changed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mer.py", description=f"month-end-review {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **extra):
        p = sub.add_parser(name)
        p.add_argument("--root", default="month-end", help="folder holding review-config.json and one folder per month")
        p.add_argument("--month", required=True, help="YYYY-MM")
        for flag, kw in extra.items():
            p.add_argument(f"--{flag.replace('_', '-')}", **kw)
        p.set_defaults(fn=fn)

    cp = sub.add_parser("connect", help="connect QuickBooks with your own Intuit keys from <root>/.env")
    cp.add_argument("--root", default="month-end", help="work folder; its .env holds the Intuit keys")
    cp.add_argument("--profile", default="default", help="one profile per QuickBooks company")
    cp.add_argument("--environment", choices=sorted(qbo.API_BASE), help="production (real company) or sandbox")
    cp.add_argument("--finish", metavar="ADDRESS", help="the address the browser landed on after Connect")
    cp.add_argument("--check", action="store_true", help="confirm the saved connection works (shows no keys)")
    cp.add_argument("--env-file", help="a different .env file holding the keys")
    cp.add_argument("--redirect-uri", default=connect.DEFAULT_REDIRECT)
    cp.add_argument("--replace", action="store_true", help="replace a different company saved under this profile")
    cp.add_argument("--no-open", action="store_true", help="print the sign-in link instead of opening the browser")
    cp.set_defaults(fn=cmd_connect)
    add("pull-qbo", cmd_pull_qbo, profile={"default": "default"})
    add("init", cmd_init, company={"required": True})
    add("confirm-roles", cmd_confirm_roles, by={"required": True})
    add("pull-stripe", cmd_pull_stripe, allow_test_mode={"action": "store_true"})
    add("ingest-stripe", cmd_ingest_stripe, file={"required": True, "action": "append"},
        opening_balance={"required": True}, closing_balance={"required": True})
    add("ingest-statement", cmd_ingest_statement, account={"required": True}, file={"required": True},
        ending_balance={"required": True}, statement_end={"required": True}, opening_balance={"default": None},
        statement_start={"default": None})
    add("ingest-outstanding", cmd_ingest_outstanding, account={"required": True}, file={"required": True})
    add("ingest-payroll", cmd_ingest_payroll, file={"required": True})
    add("ingest-budget", cmd_ingest_budget, file={"required": True})
    add("attest", cmd_attest, source={"required": True}, by={"required": True})
    add("acknowledge", cmd_acknowledge, id={"required": True}, by={"required": True}, reason={"required": True},
        until={"default": None})
    add("gate", cmd_gate)
    add("run", cmd_run, force={"action": "store_true"})
    add("verify", cmd_verify)
    add("summary", cmd_summary)

    a = ap.parse_args(argv)
    try:
        rc = a.fn(a)
        return rc if isinstance(rc, int) else 0
    except StopRun as e:
        print(f"STOPPED: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"ERROR (exit 3): {type(e).__name__}: {e}\nThis is a bug or unexpected data. Nothing from this run "
              "should be used.", file=sys.stderr)
        return 3
