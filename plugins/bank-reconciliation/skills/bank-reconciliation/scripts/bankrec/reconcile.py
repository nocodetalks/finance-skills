"""Reconcile every account brought in for the month: match, explain each difference, draft each fix.

Difference = book balance - statement balance. Every unmatched line becomes one item with an effect on that
difference (a book-only line adds its amount, a statement-only row subtracts its amount, a typo pair adds
book - bank). The opening gap that last period's uncleared items don't explain is one more item. These add up to
the difference by construction; the run asserts it and treats anything else as a bug.

Each item is one of:
  timing  - clears by itself next period (deposit in transit, outstanding check). No fix.
  error   - the books are wrong. A draft fix is written.
  decide  - the scripts can't tell. A person has to say what it is. No fix is guessed.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from .inputs import Manifest
from .matching import match_account
from .util import (ZERO, TRANSFER_WORDS, StopRun, add_days, days_between, fmt, money, norm, read_csv, read_json,
                   tokens)

DEFAULTS = {
    "window_days": 5,              # a deposit or payment this close to the statement end may still be in transit
    "outstanding_check_days": 90,  # an uncleared check older than this is stale
    "duplicate_days": 7,           # two book lines this close with the same payee and amount may be one booked twice
    "transfer_days": 3,            # a transfer shows on both statements within this many days
    "stripe_arrival_days": 5,      # a Stripe payout reaches the bank within this many days of being created
}
CHECK_TYPES = {"check", "bill payment (check)", "payroll check", "bill payment", "paycheck"}
TRANSFER_TYPES = {"transfer", "credit card payment"}
FEE_WORDS = ("fee", "service charge", "interest", "overdraft", "nsf", "maintenance", "dividend")


def _d(v) -> date:
    return date.fromisoformat(v) if isinstance(v, str) else v


def load(month_dir: Path, root: Path, prior_key: str) -> dict:
    m = Manifest(month_dir).data
    accounts = {}
    for name, a in m.get("accounts", {}).items():
        if not a.get("books") and not a.get("statement"):
            continue
        if not a.get("books"):
            raise StopRun(f"{name}: the statement is here but not the book side. Run add-books for it.")
        if not a.get("statement"):
            raise StopRun(f"{name}: the book side is here but not the statement. Run add-statement for it.")
        inp = month_dir / "inputs"
        books = [dict(r, date=_d(r["date"]), amount=money(r["amount"]), account=name)
                 for r in read_csv(inp / a["books"]["file"])]
        bank = [dict(r, row=int(r["row"]), date=_d(r["date"]), amount=money(r["amount"]), account=name,
                     source_ref=f"{a['statement']['source_file']} line {r.get('line') or r['row']}")
                for r in read_csv(inp / a["statement"]["file"])]
        prior, prior_src = _prior_items(month_dir, root, prior_key, name, a)
        opening_gap = money(a["books"]["opening"]) - money(a["statement"]["opening"])
        if prior is None:
            if opening_gap != 0:
                raise StopRun(
                    f"{name}: the books start at {fmt(money(a['books']['opening']))} and the statement at "
                    f"{fmt(money(a['statement']['opening']))} ({fmt(opening_gap)} apart), and there's no list of what "
                    f"was uncleared at {a['statement']['start']}. Add last period's uncleared items with "
                    f"add-outstanding --account \"{name}\" --file <csv> (columns date, description, amount, side), "
                    "taken from last period's reconciliation report. If there was no reconciliation last period, "
                    f"say so and run add-outstanding --account \"{name}\" --none --by \"<name>\": the gap is then "
                    "reported as an opening difference nobody has traced.")
            prior, prior_src = [], "none listed; the opening balances agree"
        else:
            prior, prior_src = _settle_carried(prior, prior_src, opening_gap)
        accounts[name] = {"name": name, "kind": a.get("kind", "bank"), "books": books, "bank": bank, "prior": prior,
                          "prior_source": prior_src, "meta": a,
                          "start": _d(a["statement"]["start"]), "end": _d(a["statement"]["end"]),
                          "book_open": money(a["books"]["opening"]), "book_close": money(a["books"]["closing"]),
                          "bank_open": money(a["statement"]["opening"]), "bank_close": money(a["statement"]["closing"])}
    if not accounts:
        raise StopRun("Nothing to reconcile yet. Add the book side (add-books) and the statement (add-statement) for "
                      "at least one account.")
    stripe = None
    if m.get("stripe"):
        stripe = [dict(r, date=_d(r["date"]), gross=money(r["gross"]), fee=money(r["fee"]), net=money(r["net"]))
                  for r in read_csv(month_dir / "inputs" / m["stripe"]["file"])]
    prior_drafts = {}
    pd = root / prior_key / "output" / "drafts.json"
    if pd.exists():
        prior_drafts = {d["id"]: d for d in (read_json(pd).get("drafts") or [])}
    return {"accounts": accounts, "stripe": stripe, "stripe_meta": m.get("stripe"), "prior_drafts": prior_drafts,
            "prior_key": prior_key}


def _prior_items(month_dir: Path, root: Path, prior_key: str, name: str, a: dict):
    """Last period's uncleared items: given explicitly, or carried from last month's run. None = unknown."""
    o = a.get("outstanding")
    if o and o.get("none"):
        return [], f"none (confirmed by {o['by']})"
    if o and o.get("file"):
        rows = read_csv(month_dir / "inputs" / o["file"])
        return [dict(r, date=_d(r["date"]), amount=money(r["amount"])) for r in rows], f"{o['source_file']}"
    carried = root / prior_key / "output" / "outstanding.csv"
    if carried.exists():
        rows = [r for r in read_csv(carried) if norm(r.get("account")) == norm(name)]
        return ([dict(r, date=_d(r["date"]), amount=money(r["amount"])) for r in rows],
                f"carried from the {prior_key} reconciliation")
    return None, ""


def _net(lines) -> Decimal:
    return (sum((l["amount"] for l in lines if l["side"] == "book"), ZERO)
            - sum((l["amount"] for l in lines if l["side"] == "bank"), ZERO))


def _settle_carried(prior: list, src: str, opening_gap: Decimal):
    """Carried items whose fixes were posted back into last period have left the opening gap. If the gap says every
    carried fix was posted, drop those items; otherwise keep them all (any rest shows as an opening difference)."""
    errs = [l for l in prior if l.get("kind") == "error"]
    if not errs or _net(prior) == opening_gap:
        return prior, src
    fixed = sum((money(l.get("fix_effect") or "0") for l in errs), ZERO)
    if _net(prior) + fixed == opening_gap:
        return [l for l in prior if l.get("kind") != "error"], src + "; last period's drafted fixes are in the books"
    return prior, src


def _as_prior_book(name, lines):
    return [{"line_id": f"P{i + 1}", "txn_id": l.get("txn_id", ""), "txn_type": l.get("txn_type") or "uncleared",
             "date": l["date"], "name": l.get("description", ""), "memo": "", "doc_num": l.get("doc_num", ""),
             "amount": l["amount"], "split_account": "", "prior": True, "account": name,
             "source_ref": l.get("source_ref", ""), "kind": l.get("kind", ""), "draft": l.get("draft", ""),
             "fix_effect": money(l.get("fix_effect") or "0"), "title": l.get("title", "")}
            for i, l in enumerate(lines) if l["side"] == "book"]


def _as_prior_bank(name, lines):
    return [{"row": f"P{i + 1}", "date": l["date"], "description": l.get("description", ""), "amount": l["amount"],
             "prior": True, "account": name, "source_ref": l.get("source_ref", ""), "kind": l.get("kind", ""),
             "draft": l.get("draft", ""), "fix_effect": money(l.get("fix_effect") or "0"), "title": l.get("title", "")}
            for i, l in enumerate(lines) if l["side"] == "bank"]


# ---------- references ----------

def book_ref(b: dict) -> str:
    if b.get("prior"):
        return f"uncleared {b['date']} ({b.get('source_ref') or 'last period'})"
    t = b.get("txn_type") or "Line"
    rid = f" {b['txn_id']}" if b.get("txn_id") else ""
    doc = f" #{b['doc_num']}" if b.get("doc_num") else ""
    return f"{t}{rid}{doc} on {b['date']}"


def bank_ref(acct: dict, s: dict) -> str:
    if s.get("prior"):
        return f"statement {s['date']} ({s.get('source_ref') or 'last period'})"
    return s["source_ref"]


def who(b: dict) -> str:
    return b.get("name") or b.get("memo") or b.get("txn_type") or "no payee"


def is_check(b: dict) -> bool:
    """A check waits to clear. A refund or expense with a document number is not a check."""
    t = norm(b.get("txn_type"))
    return t in CHECK_TYPES or (t in ("", "uncleared") and (b.get("doc_num") or "").strip().isdigit())


def is_refund(b: dict) -> bool:
    return "refund" in norm(b.get("txn_type")) or "refund" in norm(b.get("memo"))


def stripe_like(*texts) -> bool:
    return any("stripe" in norm(t) for t in texts)


# ---------- the run ----------

class Ctx:
    def __init__(self, data: dict, cfg: dict):
        self.data, self.cfg = data, cfg
        self.items = []
        self.drafts = []
        self.notes = defaultdict(list)

    def item(self, acct, kind, cause, title, effect, refs, why, draft=None, **extra):
        it = {"id": f"I{len(self.items) + 1}", "account": acct["name"], "kind": kind, "cause": cause, "title": title,
              "effect": effect, "refs": refs, "why": why, "draft": draft}
        it.update(extra)
        self.items.append(it)
        return it

    def draft(self, status, action, title, record, change, effects, evidence):
        d = {"id": f"D{len(self.drafts) + 1}", "status": status, "action": action, "title": title, "record": record,
             "change": change, "effects": {k: v for k, v in effects.items() if v != 0}, "evidence": evidence}
        self.drafts.append(d)
        return d["id"]


def _record(b: dict) -> dict:
    return {k: b.get(k) for k in ("account", "txn_id", "txn_type", "date", "name", "memo", "doc_num", "amount",
                                  "split_account")}


def run(data: dict, cfg: dict) -> dict:
    cfg = {**DEFAULTS, **(cfg or {})}
    c = Ctx(data, cfg)
    accts = data["accounts"]
    w = cfg["window_days"]
    for a in accts.values():
        pb, pk = _as_prior_book(a["name"], a["prior"]), _as_prior_bank(a["name"], a["prior"])
        a["prior_book"], a["prior_bank"] = pb, pk
        a["match"] = match_account(a["books"], pb, a["bank"] + pk, w, add_days(a["end"], -w))
    claimed_bank, claimed_book = set(), set()
    _cross_account(c, claimed_bank, claimed_book)
    for a in accts.values():
        _carried_errors(c, a, claimed_bank, claimed_book)
        _typos(c, a)
        _book_only(c, a, claimed_book)
        _bank_only(c, a, claimed_bank)
        _opening(c, a)
    return _bridge(c)


def _other_accounts(c, a):
    return [o for o in c.data["accounts"].values() if o["name"] != a["name"]]


def _cross_account(c: Ctx, claimed_bank: set, claimed_book: set) -> None:
    """Money that moved between two of the accounts here, or hit one account while the books put it in another."""
    td = c.cfg["transfer_days"]
    accts = c.data["accounts"]
    for a in accts.values():
        for s in a["match"].bank_left:
            if s.get("prior"):
                continue
            # 1. A transfer in from another account, booked there as an expense/deposit (or a transfer to the wrong place)
            for o in _other_accounts(c, a):
                hit = next((b for b in o["books"] if b["amount"] == -s["amount"]
                            and days_between(b["date"], s["date"]) <= td
                            and (o["name"], b["line_id"]) not in claimed_book
                            and (norm(b.get("txn_type")) not in TRANSFER_TYPES
                                 or (b.get("split_account") and norm(b["split_account"]) != norm(a["name"])))), None)
                if not hit:
                    continue
                matched_there = hit["line_id"] in {x["line_id"] for x, _, _ in o["match"].pairs}
                words = TRANSFER_WORDS.search(" ".join([s["description"], hit.get("memo", ""), hit.get("name", "")]))
                if not (matched_there or words):
                    continue
                claimed_bank.add((a["name"], s["row"]))
                claimed_book.add((o["name"], hit["line_id"]))
                typ = hit.get("txn_type") or "a transaction"
                to = f" to {hit['split_account']}" if hit.get("split_account") else ""
                status = "ready" if matched_there else "confirm first"
                frm, dst = (o["name"], a["name"]) if s["amount"] > 0 else (a["name"], o["name"])
                did = c.draft(status, "change-type", f"Make {typ} {hit.get('txn_id') or ''} a transfer".replace("  ", " "),
                              _record(hit),
                              f"Change {book_ref(hit)} ({fmt(hit['amount'])}, {o['name']}) from {typ}{to} into a "
                              f"Transfer of {fmt(abs(s['amount']))} from {frm} to {dst} dated {hit['date']}. "
                              f"{o['name']}'s balance doesn't change; {a['name']}'s changes by {fmt(s['amount'], True)}.",
                              {a["name"]: s["amount"]},
                              [bank_ref(a, s), f"{book_ref(hit)} in {o['name']}"]
                              + ([f"also on {o['name']}'s statement"] if matched_there else []))
                c.item(a, "error", "transfer-booked-wrong",
                       f"Transfer from {o['name']} not in the books here: booked in {o['name']} as {typ}{to}",
                       -s["amount"], [bank_ref(a, s), f"{book_ref(hit)} in {o['name']}"],
                       f"\"{s['description']}\" {fmt(s['amount'], True)} on {s['date']}. {o['name']} has {book_ref(hit)} "
                       f"for {fmt(hit['amount'])}, booked as {typ}{to} instead of a transfer to {a['name']}"
                       + (", and it's on that account's statement too" if matched_there else "") + ".",
                       did, date=s["date"], amount=s["amount"], bank=s)
                break
            if (a["name"], s["row"]) in claimed_bank:
                continue
            # 2. The same money in another account's books, never on that account's statement: booked to the wrong account
            for o in _other_accounts(c, a):
                hit = next((b for b in o["match"].book_left if not b.get("prior") and b["amount"] == s["amount"]
                            and days_between(b["date"], s["date"]) <= c.cfg["window_days"]
                            and (o["name"], b["line_id"]) not in claimed_book), None)
                if not hit:
                    continue
                claimed_bank.add((a["name"], s["row"]))
                claimed_book.add((o["name"], hit["line_id"]))
                did = c.draft("confirm first", "move", f"Move {hit.get('txn_type') or 'line'} {hit.get('txn_id') or ''} to {a['name']}",
                              _record(hit),
                              f"Change the bank account on {book_ref(hit)} ({who(hit)}, {fmt(hit['amount'])}) from "
                              f"{o['name']} to {a['name']}: it cleared {a['name']}, not {o['name']}.",
                              {o["name"]: -hit["amount"], a["name"]: hit["amount"]},
                              [bank_ref(a, s), f"{book_ref(hit)} in {o['name']}"])
                c.item(a, "error", "wrong-bank-account",
                       f"{fmt(s['amount'])} cleared here but is booked in {o['name']}", -s["amount"],
                       [bank_ref(a, s), f"{book_ref(hit)} in {o['name']}"],
                       f"\"{s['description']}\" on {s['date']} matches {book_ref(hit)} ({who(hit)}), which is in "
                       f"{o['name']}'s books but not on its statement.", did, date=s["date"], amount=s["amount"], bank=s)
                c.item(o, "error", "wrong-bank-account",
                       f"{who(hit)} {fmt(hit['amount'])} is booked here but cleared {a['name']}", hit["amount"],
                       [book_ref(hit), bank_ref(a, s)],
                       f"Not on this statement; {a['name']}'s statement has it on {s['date']} (\"{s['description']}\").",
                       did, date=hit["date"], amount=hit["amount"], book=hit)
                break


def _carried_errors(c: Ctx, a: dict, claimed_bank: set, claimed_book: set) -> None:
    """Last period's book errors that are still open: their fix wasn't posted."""
    pk = c.data.get("prior_key", "last period")
    lines = [("book", b) for b in a["match"].book_left if b.get("prior") and b.get("kind") == "error"] + \
            [("bank", s) for s in a["match"].bank_left if s.get("prior") and s.get("kind") == "error"]
    for side, ln in lines:
        (claimed_book if side == "book" else claimed_bank).add((a["name"], ln["line_id"] if side == "book" else ln["row"]))
        old = c.data.get("prior_drafts", {}).get(ln.get("draft") or "")
        did = None
        if ln["fix_effect"] != 0:
            did = c.draft(old["status"] if old else "confirm first", old["action"] if old else "carried",
                          f"Still to post: {pk} fix {ln.get('draft') or ''}".strip(), old.get("record") if old else None,
                          (old["change"] if old else f"Fix the {pk} item {ln.get('source_ref')}.")
                          + f" (Drafted in the {pk} reconciliation as {ln.get('draft') or 'a fix'}; not in the books yet.)",
                          {a["name"]: ln["fix_effect"]}, [ln.get("source_ref") or "", f"{pk} drafts.json"])
        desc = who(ln) if side == "book" else ln["description"]
        title = ln.get("title") or f"{desc} {fmt(ln['amount'])}"
        c.item(a, "error", "carried-error", f"Still open from {pk}: {title}",
               ln["amount"] if side == "book" else -ln["amount"], [ln.get("source_ref") or ""],
               f"Found in the {pk} reconciliation and its fix ({ln.get('draft') or 'none'}) isn't in the books yet.",
               did, date=ln["date"], amount=ln["amount"], **{side: ln})


def _typos(c: Ctx, a: dict) -> None:
    for b, s, kind in a["match"].typos:
        delta = s["amount"] - b["amount"]
        nine = kind == "digits swapped" and (abs(delta) * 100) % 9 == 0
        did = c.draft("confirm first", "edit", f"Correct the amount on {b.get('txn_type') or 'line'} {b.get('txn_id') or ''}".strip(),
                      _record(b),
                      f"Change {book_ref(b)} ({who(b)}) from {fmt(b['amount'])} to {fmt(s['amount'])}, the amount on the "
                      "statement, once the deposit slip, check or invoice confirms it."
                      + (" Anything this payment was applied to (an invoice or bill) needs the same correction."
                         if norm(b.get("txn_type")) in ("payment", "bill payment", "bill payment (check)", "receive payment") else ""),
                      {a["name"]: delta}, [book_ref(b), bank_ref(a, s)])
        c.item(a, "error", "amount-typo",
               f"{who(b)}: books {fmt(b['amount'])}, statement {fmt(s['amount'])} ({kind})", b["amount"] - s["amount"],
               [book_ref(b), bank_ref(a, s)],
               f"Same day (±2), \"{s['description']}\". The {fmt(abs(delta))} gap"
               + (" divides by 9, the usual sign of two digits typed the wrong way round." if nine else ".")
               + " It's treated as one transaction typed wrong; a person confirms that from the source document.",
               did, date=b["date"], amount=b["amount"], book=b, bank=s)


def _stripe_payout(c: Ctx, amount: Decimal, lo: date, hi: date, used: set):
    for p in sorted(c.data["stripe"] or [], key=lambda p: p["date"]):
        if p["category"] == "payout" and -p["net"] == amount and lo <= p["date"] <= hi and p["id"] not in used:
            return p
    return None


def _book_only(c: Ctx, a: dict, claimed: set) -> None:
    w, end = c.cfg["window_days"], a["end"]
    left = [b for b in a["match"].book_left if (a["name"], b["line_id"]) not in claimed]
    done = set()
    stripe = c.data["stripe"]
    clearing = (c.data.get("stripe_meta") or {}).get("clearing_account") or "your Stripe clearing account"

    # Duplicates: the same payee, type and amount twice within a few days, with at least one copy not on the statement.
    groups = defaultdict(list)
    for b in a["books"]:
        groups[(norm(who(b)), norm(b.get("txn_type")), b["amount"])].append(b)
    left_ids = {b["line_id"] for b in left}
    for (_, _, amt), lines in groups.items():
        lines.sort(key=lambda b: (b["date"], b["line_id"]))
        cluster = [x for x in lines if any(x is not y and days_between(x["date"], y["date"]) <= c.cfg["duplicate_days"]
                                           for y in lines)]
        un = [x for x in cluster if x["line_id"] in left_ids]
        if len(cluster) < 2 or not un:
            continue
        on_stmt = [x for x in cluster if x["line_id"] not in left_ids]
        if stripe is not None and all(is_refund(x) for x in un):
            k = sum(1 for r in stripe if r["category"] == "refund" and r["net"] == amt
                    and any(days_between(r["date"], x["date"]) <= w for x in cluster))
            real, dupes = un[:k], un[k:]
            for x in dupes:
                did = c.draft("ready", "delete", f"Delete duplicate {x.get('txn_type') or 'line'} {x.get('txn_id') or ''}".strip(), _record(x),
                              f"Delete {book_ref(x)} ({who(x)}, {fmt(x['amount'])}): Stripe refunded {fmt(abs(amt))} "
                              f"{k} time{'s' if k != 1 else ''} and the books have it {len(cluster)} times.",
                              {a["name"]: -x["amount"]}, [book_ref(x), "stripe.csv refunds"])
                c.item(a, "error", "duplicate", f"{who(x)} {fmt(x['amount'])} booked twice", x["amount"],
                       [book_ref(y) for y in cluster] + ["stripe.csv"],
                       f"Stripe shows {k} refund{'s' if k != 1 else ''} of {fmt(abs(amt))}; the books have "
                       f"{len(cluster)}. This copy is the extra one.", did, date=x["date"], amount=x["amount"], book=x)
                done.add(x["line_id"])
            for x in real:
                did = c.draft("confirm first", "move", f"Move refund {x.get('txn_id') or ''} off {a['name']}".strip(), _record(x),
                              f"{book_ref(x)} ({fmt(x['amount'])}) was paid out of the Stripe balance, not {a['name']}. "
                              f"Change its payment account to {clearing}. If Stripe payouts are booked net to revenue "
                              "(no clearing account), the refund is already inside a payout and this entry should be "
                              "removed instead. Your accountant decides which.",
                              {a["name"]: -x["amount"]}, [book_ref(x), "stripe.csv refunds"])
                c.item(a, "error", "paid-from-stripe", f"{who(x)} refund {fmt(x['amount'])} came out of Stripe, not this account",
                       x["amount"], [book_ref(x), "stripe.csv"],
                       "Stripe has this refund, and Stripe refunds come out of the Stripe balance, so it never "
                       "reaches the bank statement. It isn't an outstanding check and won't clear.",
                       did, date=x["date"], amount=x["amount"], book=x)
                done.add(x["line_id"])
            continue
        # Without Stripe proof: one copy on the statement and one not is a strong sign; both off it is a question.
        for x in (un if on_stmt else un[1:]):
            status = "confirm first"
            did = c.draft(status, "delete", f"Delete possible duplicate {x.get('txn_type') or 'line'} {x.get('txn_id') or ''}".strip(), _record(x),
                          f"Delete {book_ref(x)} ({who(x)}, {fmt(x['amount'])}) if it's the same payment as "
                          f"{', '.join(book_ref(y) for y in cluster if y is not x)}. Check the source (Stripe, the vendor, "
                          "the payroll provider) first.", {a["name"]: -x["amount"]}, [book_ref(y) for y in cluster])
            c.item(a, "error" if on_stmt else "decide", "possible-duplicate",
                   f"{who(x)} {fmt(x['amount'])} may be booked twice", x["amount"], [book_ref(y) for y in cluster],
                   (f"Only one copy is on the statement ({len(on_stmt)} matched); this one isn't." if on_stmt else
                    f"{len(cluster)} copies within {c.cfg['duplicate_days']} days and none is on the statement. "
                    + ("Add the Stripe export to settle whether they're one refund or two." if any(is_refund(y) or stripe_like(who(y)) for y in cluster)
                       else "Check the source to see whether it was paid once or twice.")),
                   did, date=x["date"], amount=x["amount"], book=x)
            done.add(x["line_id"])

    used_payouts = set()
    for b in sorted(left, key=lambda b: b["date"]):
        if b["line_id"] in done:
            continue
        age = (end - b["date"]).days
        ref = [book_ref(b)]
        if b.get("prior"):
            if is_check(b) and age <= c.cfg["outstanding_check_days"] or age <= w:
                c.item(a, "timing", "outstanding-carried", f"{who(b)} {fmt(b['amount'])} from {b['date']} still uncleared",
                       b["amount"], ref, f"Carried from last period, {age} days old.", date=b["date"], amount=b["amount"], book=b)
            else:
                c.item(a, "decide", "stale", f"{who(b)} {fmt(b['amount'])} uncleared for {age} days", b["amount"], ref,
                       "Carried from last period and still not on the statement. Old uncleared items are usually "
                       "voided checks, payments made another way, or entries booked twice. A person decides whether it "
                       "will ever clear.", date=b["date"], amount=b["amount"], book=b)
            continue
        if stripe is not None and is_refund(b) and stripe_like(who(b), b.get("memo")):
            hit = any(r["category"] == "refund" and r["net"] == b["amount"] and days_between(r["date"], b["date"]) <= w
                      for r in stripe)
            if hit:
                did = c.draft("confirm first", "move", f"Move refund {b.get('txn_id') or ''} off {a['name']}".strip(), _record(b),
                              f"{book_ref(b)} ({fmt(b['amount'])}) was paid out of the Stripe balance. Change its payment "
                              f"account to {clearing}, or remove it if payouts are booked net to revenue.",
                              {a["name"]: -b["amount"]}, [book_ref(b), "stripe.csv refunds"])
                c.item(a, "error", "paid-from-stripe", f"{who(b)} refund {fmt(b['amount'])} came out of Stripe, not this account",
                       b["amount"], ref + ["stripe.csv"], "Stripe has this refund; it never reaches the bank.",
                       did, date=b["date"], amount=b["amount"], book=b)
                continue
        if b["amount"] > 0 and age <= w:
            if stripe is not None and stripe_like(who(b), b.get("memo")):
                p = _stripe_payout(c, b["amount"], add_days(b["date"], -w), end, used_payouts)
                if p:
                    used_payouts.add(p["id"])
                    c.item(a, "timing", "deposit-in-transit", f"Stripe payout {fmt(b['amount'])} in transit",
                           b["amount"], ref + [f"stripe.csv {p['id']}"],
                           f"Stripe created payout {p['id']} on {p['date']}; it reaches the bank after {end}.",
                           date=b["date"], amount=b["amount"], book=b)
                else:
                    c.item(a, "decide", "no-such-payout", f"{fmt(b['amount'])} Stripe deposit has no Stripe payout behind it",
                           b["amount"], ref + ["stripe.csv"],
                           f"Booked on {b['date']} but Stripe has no payout of {fmt(b['amount'])}. It isn't money in "
                           "transit. It may be another payout booked with the wrong amount or date.",
                           date=b["date"], amount=b["amount"], book=b)
                continue
            c.item(a, "timing", "deposit-in-transit", f"{who(b)} {fmt(b['amount'])} deposit in transit", b["amount"], ref,
                   f"Booked {b['date']}, {age} days before the statement ended. Expect it early next period; if it "
                   "doesn't arrive, it's an error.", date=b["date"], amount=b["amount"], book=b)
            continue
        if b["amount"] < 0 and (age <= w or (is_check(b) and age <= c.cfg["outstanding_check_days"])):
            c.item(a, "timing", "outstanding-payment",
                   f"{who(b)} {fmt(b['amount'])} {'check' if is_check(b) else 'payment'} not cleared yet", b["amount"], ref,
                   f"Issued {b['date']} ({age} days before the statement ended).", date=b["date"], amount=b["amount"], book=b)
            continue
        why = (f"{b.get('txn_type') or 'Line'} for {who(b)} on {b['date']}, {age} days before the statement ended, never "
               "reached the statement. It isn't a check, so it shouldn't be waiting to clear. It may have been paid from "
               "another account")
        why += " or out of the Stripe balance" if stripe_like(who(b), b.get("memo")) or is_refund(b) else ""
        why += ", booked twice, or never paid. Check before changing anything."
        if stripe is None and (is_refund(b) or stripe_like(who(b), b.get("memo"))):
            why += " Adding the Stripe export would settle it."
        c.item(a, "decide", "book-not-on-statement", f"{who(b)} {fmt(b['amount'])} is in the books but not on the statement",
               b["amount"], ref, why, date=b["date"], amount=b["amount"], book=b)


def _template(a: dict, s: dict):
    """The book line this statement row most looks like: same words, same direction. Same amount first, then lines
    that are on the statement (so known to be booked right), then the latest."""
    tk = tokens(s["description"])
    on_stmt = {b["line_id"] for b, _, _ in a["match"].pairs}
    cands = [b for b in a["books"] if (b["amount"] > 0) == (s["amount"] > 0) and tk & tokens(b.get("name"), b.get("memo"))]
    if not cands:
        return None
    cands.sort(key=lambda b: (b["amount"] == s["amount"], b["line_id"] in on_stmt, b["date"]), reverse=True)
    return cands[0]


def _bank_only(c: Ctx, a: dict, claimed: set) -> None:
    used_payouts = set()
    for s in sorted(a["match"].bank_left, key=lambda s: s["date"]):
        if (a["name"], s["row"]) in claimed:
            continue
        ref = [bank_ref(a, s)]
        desc = s["description"]
        if s.get("prior"):
            did = c.draft("confirm first", "create", f"Record {desc or 'last period item'}", None,
                          f"Record {fmt(s['amount'])} \"{desc}\" dated {s['date']} in {a['name']}. It has been on the "
                          "statement since last period. Check it isn't booked under another account or date first.",
                          {a["name"]: s["amount"]}, ref)
            c.item(a, "error", "statement-still-not-in-books", f"\"{desc}\" {fmt(s['amount'], True)} from last period still not in the books",
                   -s["amount"], ref, f"On the statement {s['date']}, carried from last period.", did,
                   date=s["date"], amount=s["amount"], bank=s)
            continue
        if c.data["stripe"] is not None and s["amount"] > 0 and stripe_like(desc):
            p = _stripe_payout(c, s["amount"], add_days(s["date"], -c.cfg["stripe_arrival_days"]), s["date"], used_payouts)
            if p:
                used_payouts.add(p["id"])
                t = _template(a, s)
                like = f" the same way as {book_ref(t)}" if t else ""
                did = c.draft("ready", "create", f"Record Stripe payout {p['id']}", _record(t) if t else None,
                              f"Record a deposit of {fmt(s['amount'])} on {s['date']} in {a['name']} for Stripe payout "
                              f"{p['id']}{like}.", {a["name"]: s["amount"]}, ref + [f"stripe.csv {p['id']}"])
                c.item(a, "error", "missing-deposit", f"Stripe payout {fmt(s['amount'])} on {s['date']} not in the books",
                       -s["amount"], ref + [f"stripe.csv {p['id']}"],
                       f"Stripe created payout {p['id']} on {p['date']}, and it reached the bank on {s['date']}. "
                       "Nothing in the books matches it.", did, date=s["date"], amount=s["amount"], bank=s)
                continue
        t = _template(a, s)
        fee = any(wd in norm(desc) for wd in FEE_WORDS)
        if t:
            same = t["amount"] == s["amount"]
            change = (f"Record {fmt(s['amount'])} \"{desc}\" on {s['date']} in {a['name']}, set up like {book_ref(t)} "
                      f"({who(t)}" + (f", {t['split_account']}" if t.get("split_account") else "") + ")."
                      + (" Same payee and amount, so it's likely the next one in the series" if same else "")
                      + (". " if same else " ") + "Check it isn't already booked under another account or date first.")
        else:
            change = (f"Record {fmt(s['amount'])} \"{desc}\" on {s['date']} in {a['name']}. Nothing in the books "
                      "looks like it, so ask which account it belongs to"
                      + (" (bank fees and interest have their own accounts)" if fee else "") + ".")
        did = c.draft("confirm first", "create", f"Record {desc or 'statement item'} {s['date']}", _record(t) if t else None,
                      change, {a["name"]: s["amount"]}, ref + ([book_ref(t)] if t else []))
        if t and t["amount"] == s["amount"]:
            why = (f"\"{desc}\" {fmt(s['amount'], True)} on {s['date']}. The books have {book_ref(t)} for the same "
                   f"payee and amount, and nothing for this one.")
        else:
            why = f"\"{desc}\" {fmt(s['amount'], True)} on {s['date']}. Nothing in the books matches it."
        c.item(a, "error", "missing-entry", f"\"{desc}\" {fmt(s['amount'], True)} on {s['date']} isn't in the books",
               -s["amount"], ref, why, did, date=s["date"], amount=s["amount"], bank=s)


def _opening(c: Ctx, a: dict) -> None:
    gap = a["book_open"] - a["bank_open"]
    carried = sum((x["amount"] for x in a["prior_book"]), ZERO) - sum((x["amount"] for x in a["prior_bank"]), ZERO)
    rest = gap - carried
    if rest == 0:
        return
    none = (a["meta"].get("outstanding") or {}).get("none")
    c.item(a, "decide", "opening-difference",
           f"The opening balances differ by {fmt(rest)} that nothing explains",
           rest, [f"books at {add_days(a['start'], -1)}", "statement opening balance"],
           (f"Books start at {fmt(a['book_open'])}, the statement at {fmt(a['bank_open'])}. "
            + ("Nothing was listed as uncleared at the start. " if none else
               f"Last period's uncleared items account for {fmt(carried)}. ")
            + "Usual causes: a transaction in an earlier, already-reconciled period was added, changed or deleted, or "
              "last period's uncleared list was incomplete. Look at changes to earlier periods in this account."),
           date=add_days(a["start"], -1), amount=rest)


def _bridge(c: Ctx) -> dict:
    order = list(c.data["accounts"])
    kinds = ["error", "decide", "timing"]
    c.items.sort(key=lambda i: (order.index(i["account"]), kinds.index(i["kind"]), str(i.get("date")), i["title"]))
    for n, i in enumerate(c.items, start=1):
        i["id"] = f"I{n}"
    renum = {}
    for i in c.items:
        if i.get("draft") and i["draft"] not in renum:
            renum[i["draft"]] = f"D{len(renum) + 1}"
    for d in c.drafts:
        renum.setdefault(d["id"], f"D{len(renum) + 1}")
    for i in c.items:
        i["draft"] = renum.get(i["draft"]) if i.get("draft") else None
    for d in c.drafts:
        d["id"] = renum[d["id"]]
    c.drafts.sort(key=lambda d: int(d["id"][1:]))
    out = []
    for a in c.data["accounts"].values():
        its = [i for i in c.items if i["account"] == a["name"]]
        diff = a["book_close"] - a["bank_close"]
        explained = sum((i["effect"] for i in its), ZERO)
        if explained != diff:
            raise RuntimeError(f"{a['name']}: items add to {explained} but the difference is {diff}")
        timing = [i for i in its if i["kind"] == "timing"]
        errors = [i for i in its if i["kind"] == "error"]
        decide = [i for i in its if i["kind"] == "decide"]
        after = a["book_close"] + sum((v for d in c.drafts for k, v in d["effects"].items() if k == a["name"]), ZERO)
        adj_bank = a["bank_close"] + sum((i["effect"] for i in timing), ZERO)
        if not its:
            status = "reconciled"
        elif not errors and not decide:
            status = "reconciled (timing items only)"
        elif decide:
            status = "not reconciled: items need a decision"
        else:
            status = "not reconciled: book fixes needed"
        m = a["match"]
        out.append({
            "account": a["name"], "kind": a["kind"], "start": a["start"], "end": a["end"],
            "book_opening": a["book_open"], "book_closing": a["book_close"],
            "statement_opening": a["bank_open"], "statement_closing": a["bank_close"],
            "difference": diff, "status": status,
            "timing_total": sum((i["effect"] for i in timing), ZERO),
            "error_total": sum((i["effect"] for i in errors), ZERO),
            "decide_total": sum((i["effect"] for i in decide), ZERO),
            "adjusted_bank": adj_bank, "books_after_drafts": after,
            "left_after_drafts": after - adj_bank,
            "matched": len(m.pairs), "matched_late": len(m.late), "matched_groups": sum(1 for p in m.pairs if p[2] == "group"),
            "book_lines": len(a["books"]), "statement_rows": len(a["bank"]),
            "prior_source": a["prior_source"],
            "books_source": a["meta"]["books"]["source"], "books_file": a["meta"]["books"]["source_file"],
            "statement_file": a["meta"]["statement"]["source_file"],
            "has_split_account": a["meta"]["books"].get("has_split_account", False),
        })
    outstanding = []
    for i in c.items:
        d = next((x for x in c.drafts if x["id"] == i.get("draft")), None)
        fix = (d or {}).get("effects", {}).get(i["account"], ZERO)
        for side in ("book", "bank"):
            ln = i.get(side)
            if ln is None:
                continue
            outstanding.append({"account": i["account"], "side": side, "date": ln["date"],
                                "description": who(ln) if side == "book" else ln["description"],
                                "amount": ln["amount"], "txn_id": ln.get("txn_id", ""), "txn_type": ln.get("txn_type", ""),
                                "doc_num": ln.get("doc_num", ""), "kind": i["kind"], "item": i["id"], "title": i["title"],
                                "draft": i.get("draft") or "", "fix_effect": fix,
                                "source_ref": ln.get("source_ref") or book_ref(ln)})
            fix = ZERO
    return {"accounts": out, "items": c.items, "drafts": c.drafts, "outstanding": outstanding,
            "stripe": c.data.get("stripe_meta"), "thresholds": c.cfg}
