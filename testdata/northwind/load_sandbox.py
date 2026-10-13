"""Load the Northwind test company into an Intuit SANDBOX company (level-4 testing of month-end-review).

This is test tooling, never part of the skill: it writes to QuickBooks, and it refuses anything but a sandbox
connection (Intuit's development keys can't reach real companies anyway). It loads the transactions from
tests/fixture_northwind.py, so the live company plants the same 15 mistakes as the unit tests.

  python3 testdata/northwind/load_sandbox.py wipe  --profile sandbox-loader   # delete every transaction
  python3 testdata/northwind/load_sandbox.py load  --profile sandbox-loader   # accounts, names, transactions, tidy
  python3 testdata/northwind/load_sandbox.py tidy  --profile sandbox-loader   # clear sample leftovers again
  python3 testdata/northwind/load_sandbox.py check --profile sandbox-loader   # QuickBooks' Sep 30 TB vs the fixture's
  python3 testdata/northwind/load_sandbox.py files --out <folder>             # statements, Stripe, payroll, budget

Connect the profile first with `mer.py connect --profile sandbox-loader --environment sandbox`.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal as D
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "tests"))

import fixture_northwind as fx  # noqa: E402  (also puts the skill's scripts/ on sys.path)
from mer import qbo  # noqa: E402
from mer.util import StopRun  # noqa: E402

COMPANY_NAME = "Northwind SaaS Inc"
TAG = "northwind-loader"
WIPE_ORDER = ["Deposit", "BillPayment", "Payment", "RefundReceipt", "SalesReceipt", "CreditMemo", "Invoice",
              "Estimate", "VendorCredit", "Bill", "PurchaseOrder", "Purchase", "JournalEntry", "Transfer",
              "TimeActivity"]
CUSTOMER_TYPES = {"Deposit", "Invoice", "Payment", "Refund Receipt"}
ITEMS = {"20": "SaaS Subscription", "22": "Implementation Services"}     # income account -> service item
LATE_BILLS = {   # bills.csv rows the GL can't carry: doc numbers, and the October bill for September usage
    ("Acme Design", "2026-09-03"): "AD-0903", ("Acme Design", "2026-09-06"): "AD-0903",
    ("Cooley LLP", "2026-09-28"): "C-118"}


class Sandbox(qbo.Client):
    """The skill's read-only client plus the writes a test loader needs, locked to sandbox companies."""

    def __init__(self, profile: str):
        super().__init__(profile)
        if self.creds["environment"] != "sandbox" or self.base != qbo.API_BASE["sandbox"]:
            raise StopRun(f"Profile '{profile}' is not a sandbox connection. This loader only writes to sandboxes.")

    def post(self, entity: str, body: dict, **params) -> dict:
        if self.token is None:
            self._refresh()
        params = dict(params, minorversion=qbo.MINOR_VERSION)
        url = f"{self.base}/v3/company/{self.realm}/{entity.lower()}?{urllib.parse.urlencode(params)}"
        assert url.startswith(qbo.API_BASE["sandbox"])
        for attempt in range(4):
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
                "Authorization": f"Bearer {self.token}", "Accept": "application/json",
                "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:600]
                e.close()
                if e.code == 429 and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise qbo.QBOError(f"{entity} {e.code}: {detail}", e.code) from e

    def all(self, entity: str, where: str = "") -> list[dict]:
        return self.query(f"SELECT * FROM {entity}{' WHERE ' + where if where else ''}")

    def every(self, entity: str, where: str = "") -> list[dict]:
        """Active and inactive (a plain query returns active names, accounts and items only)."""
        w = f"{where} AND " if where else ""
        return self.all(entity, w + "Active = true") + self.all(entity, w + "Active = false")

    def set_active(self, entity: str, obj: dict, active: bool) -> dict:
        return self.post(entity, {"Id": obj["Id"], "SyncToken": obj["SyncToken"], "sparse": True,
                                  "Name": obj["Name"].replace(" (deleted)", ""), "Active": active})[entity]

    def delete(self, entity: str, obj: dict) -> None:
        self.post(entity, {"Id": obj["Id"], "SyncToken": obj["SyncToken"]}, operation="delete")


# ---------------------------------------------------------------- wipe

def wipe(sb: Sandbox) -> None:
    """Delete every transaction. Linked ones (a payment inside a deposit) go in a later round."""
    for rnd in range(1, 6):
        found, failed = 0, []
        for ent in WIPE_ORDER:
            objs = sb.all(ent)
            found += len(objs)
            gone = 0
            for o in objs:
                try:
                    sb.delete(ent, o)
                    gone += 1
                except qbo.QBOError as e:
                    failed.append(f"{ent} {o['Id']}: {str(e)[:200]}")
            if objs:
                print(f"  round {rnd}: {ent:14} {gone} of {len(objs)} deleted")
        if not found:
            print("No transactions left.")
            return
        if failed and rnd == 5:
            print("Could not delete:\n  " + "\n  ".join(failed))


# ---------------------------------------------------------------- load

def fixture_transactions() -> list[dict]:
    b = fx.Book()
    fx.base_history(b)
    fx.base_september(b)
    txns: dict[str, dict] = {}
    for l in b.lines:
        t = txns.setdefault(l["txn_id"], {"type": l["txn_type"], "date": l["date"], "name": l["name"],
                                          "memo": l["memo"], "doc": l["doc_num"], "entries": []})
        t["entries"].append((l["account_id"], l["amount"]))
    out = sorted(txns.values(), key=lambda t: t["date"])          # stable: keeps the fixture's order within a day
    out.append({"type": "Bill", "date": fx.date(2026, 10, 3), "name": "Amazon Web Services",
                "memo": "AWS usage - September 2026", "doc": "AWS-0926", "entries": [("33", D("8200")), ("6", D("-8200"))]})
    for t in out:
        if t["type"] == "Bill" and (t["name"], t["date"].isoformat()) in LATE_BILLS:
            t["doc"] = LATE_BILLS[(t["name"], t["date"].isoformat())]
    return out


class Loader:
    def __init__(self, sb: Sandbox):
        self.sb = sb
        self.acc: dict[str, str] = {}        # fixture account id -> QuickBooks id
        self.types: dict[str, str] = {}      # fixture account id -> AccountType
        self.names: dict[str, tuple] = {}    # display name -> (id, "Customer"|"Vendor")
        self.items: dict[str, str] = {}      # fixture income account id -> item id
        self.open_bills: list[dict] = []
        self.invoices: list[dict] = []
        self.keep: set[str] = set()          # Northwind accounts, parents included

    def ref(self, aid):
        return {"value": self.acc[aid]}

    def ensure_accounts(self, used: set[str]) -> None:
        existing = {a["FullyQualifiedName"].replace(" (deleted)", "").lower(): a for a in self.sb.every("Account")}
        parents = {}
        for aid, full, atype, sub, _ in fx.ACCOUNTS:
            if aid not in used:
                continue
            parts = full.split(":")
            for depth in range(len(parts)):
                name = ":".join(parts[:depth + 1])
                a = existing.get(name.lower())
                if a and a["AccountType"].replace(" ", "") != atype.replace(" ", ""):
                    raise StopRun(f"Account '{name}' exists as {a['AccountType']}, the fixture needs {atype}.")
                if not a:
                    body = {"Name": parts[depth], "AccountType": atype, "AccountSubType": sub}
                    if depth:
                        body.update(SubAccount=True, ParentRef={"value": parents[":".join(parts[:depth])]})
                    try:
                        a = self.sb.post("Account", body)["Account"]
                    except qbo.QBOError:
                        body.pop("AccountSubType")
                        a = self.sb.post("Account", body)["Account"]
                    print(f"  created account {name} ({a.get('AccountSubType')})")
                    existing[name.lower()] = a
                elif not a.get("Active", True):
                    a = self.sb.set_active("Account", a, True)
                parents[name] = a["Id"]
                self.keep.add(a["Id"])
            self.acc[aid] = parents[full]
            self.types[aid] = atype

    def ensure_names(self, txns: list[dict]) -> None:
        want: dict[str, str] = {}
        for t in txns:
            if t["name"]:
                want.setdefault(t["name"], "Customer" if t["type"] in CUSTOMER_TYPES else "Vendor")
        want.setdefault("Stripe customer", "Customer")
        have = {c["DisplayName"]: (c["Id"], "Customer") for c in self.sb.all("Customer")}
        have.update({v["DisplayName"]: (v["Id"], "Vendor") for v in self.sb.all("Vendor")})
        for name, kind in want.items():
            if name in have:
                if have[name][1] != kind:
                    raise StopRun(f"'{name}' exists as a {have[name][1]}, the fixture needs a {kind}.")
            else:
                obj = self.sb.post(kind, {"DisplayName": name})[kind]
                have[name] = (obj["Id"], kind)
                print(f"  created {kind.lower()} {name}")
            self.names[name] = have[name]

    def ensure_items(self) -> None:
        have = {i["Name"]: i for i in self.sb.all("Item")}
        for aid, name in ITEMS.items():
            i = have.get(name) or self.sb.post("Item", {"Name": name, "Type": "Service",
                                                        "IncomeAccountRef": self.ref(aid)})["Item"]
            self.items[aid] = i["Id"]

    def entity(self, name: str):
        return self.names[name] if name else None

    # one builder per fixture transaction type -------------------------------------------------

    def build(self, t: dict) -> tuple[str, dict]:
        e, memo, amt = t["entries"], t["memo"], lambda a: str(abs(a))
        base = {"TxnDate": t["date"].isoformat(), "PrivateNote": f"{memo} [{TAG}]".strip()}
        if t["doc"]:
            base["DocNumber"] = t["doc"]
        typ = t["type"]
        if typ == "Journal Entry":
            lines = []
            for aid, a in e:
                det = {"PostingType": "Debit" if a > 0 else "Credit", "AccountRef": self.ref(aid)}
                who = self.entity(t["name"]) or (self.names["Stripe customer"] if self.types[aid] == "Accounts Receivable" else None)
                if self.types[aid] == "Accounts Receivable" and who[1] != "Customer":
                    who = self.names["Stripe customer"]
                if who:
                    det["Entity"] = {"Type": who[1], "EntityRef": {"value": who[0]}}
                lines.append({"Amount": amt(a), "DetailType": "JournalEntryLineDetail", "Description": memo,
                              "JournalEntryLineDetail": det})
            return "JournalEntry", dict(base, Line=lines)
        if typ == "Deposit":
            cash = next(aid for aid, a in e if self.types[aid] == "Bank" and a > 0)
            who = self.entity(t["name"])
            lines = []
            for aid, a in e:
                if aid == cash:
                    continue
                det = {"AccountRef": self.ref(aid)}
                if who:
                    det["Entity"] = {"value": who[0], "type": who[1].upper()}
                lines.append({"Amount": str(-a), "DetailType": "DepositLineDetail", "Description": memo,
                              "DepositLineDetail": det})
            return "Deposit", dict(base, DepositToAccountRef=self.ref(cash), Line=lines)
        if typ in ("Check", "Expense", "Payroll Check"):
            pay = next(aid for aid, a in e if a < 0)
            kind = "CreditCard" if self.types[pay] == "Credit Card" else ("Check" if "Check" in typ else "Cash")
            body = dict(base, PaymentType=kind, AccountRef=self.ref(pay), Line=[
                {"Amount": amt(a), "DetailType": "AccountBasedExpenseLineDetail", "Description": memo,
                 "AccountBasedExpenseLineDetail": {"AccountRef": self.ref(aid)}} for aid, a in e if a > 0])
            if t["name"]:
                body["EntityRef"] = {"value": self.entity(t["name"])[0], "type": "Vendor"}
            return "Purchase", body
        if typ == "Bill":
            return "Bill", dict(base, VendorRef={"value": self.entity(t["name"])[0]}, Line=[
                {"Amount": amt(a), "DetailType": "AccountBasedExpenseLineDetail", "Description": memo,
                 "AccountBasedExpenseLineDetail": {"AccountRef": self.ref(aid)}}
                for aid, a in e if self.types[aid] != "Accounts Payable"])
        if typ == "Bill Payment (Check)":
            total = next(a for aid, a in e if self.types[aid] == "Accounts Payable")
            bill = next(b for b in self.open_bills if b["vendor"] == t["name"] and b["amount"] == total
                        and b["date"] <= t["date"])
            self.open_bills.remove(bill)
            bank = next(aid for aid, a in e if a < 0)
            return "BillPayment", dict(base, VendorRef={"value": self.entity(t["name"])[0]}, PayType="Check",
                                       CheckPayment={"BankAccountRef": self.ref(bank)}, TotalAmt=str(total),
                                       Line=[{"Amount": str(total), "LinkedTxn": [{"TxnId": bill["id"], "TxnType": "Bill"}]}])
        if typ == "Credit Card Payment":
            src = next(aid for aid, a in e if a < 0)
            dst = next(aid for aid, a in e if a > 0)
            return "Transfer", {"TxnDate": base["TxnDate"], "PrivateNote": base["PrivateNote"],
                                "FromAccountRef": self.ref(src), "ToAccountRef": self.ref(dst),
                                "Amount": amt(e[0][1])}
        if typ == "Invoice":
            inc = next(aid for aid, a in e if a < 0)
            return "Invoice", dict(base, CustomerRef={"value": self.entity(t["name"])[0]}, Line=[
                {"Amount": amt(e[0][1]), "DetailType": "SalesItemLineDetail", "Description": memo,
                 "SalesItemLineDetail": {"ItemRef": {"value": self.items[inc]}}}])
        if typ == "Payment":
            total = next(-a for aid, a in e if self.types[aid] == "Accounts Receivable")
            inv = next(i for i in self.invoices if i["customer"] == t["name"])
            bank = next(aid for aid, a in e if a > 0)
            return "Payment", dict(base, CustomerRef={"value": self.entity(t["name"])[0]}, TotalAmt=str(total),
                                   DepositToAccountRef=self.ref(bank),
                                   Line=[{"Amount": str(total), "LinkedTxn": [{"TxnId": inv["id"], "TxnType": "Invoice"}]}])
        if typ == "Refund Receipt":
            inc = next(aid for aid, a in e if a > 0)
            bank = next(aid for aid, a in e if a < 0)
            return "RefundReceipt", dict(base, CustomerRef={"value": self.entity(t["name"])[0]},
                                         DepositToAccountRef=self.ref(bank), Line=[
                {"Amount": amt(e[0][1]), "DetailType": "SalesItemLineDetail", "Description": memo,
                 "SalesItemLineDetail": {"ItemRef": {"value": self.items[inc]}}}])
        raise StopRun(f"No loader for fixture transaction type {typ!r}.")

    def load(self, txns: list[dict]) -> None:
        for t in txns:
            entity, body = self.build(t)
            params = {"include": "allowduplicatedocnum"} if entity == "Bill" else {}
            obj = self.sb.post(entity, body, **params)[entity]
            if entity == "Bill":
                self.open_bills.append({"id": obj["Id"], "vendor": t["name"], "date": t["date"],
                                        "amount": next(a for aid, a in t["entries"] if a > 0)})
            if entity == "Invoice":
                self.invoices.append({"id": obj["Id"], "customer": t["name"]})
            print(f"  {t['date']} {t['type']:22} {t['name'] or '-':22} {t['memo'][:34]}")


def tidy(sb: Sandbox, keep: set[str]) -> None:
    """Rename the company, and make the sample company's leftovers inactive so `init` doesn't propose them.

    The wipe can't remove two kinds of sample entries:
    - Starting inventory: an "Inventory Starting Value" dated when Intuit made the sandbox. The API can neither
      delete nor move it, so it stays in that month. Making the items inactive zeroes it on today's date.
    - Sales-tax payments: there's no US API for them, and QuickBooks won't delete them by hand either ("created by
      the system"). Each one is cancelled with a journal entry on the same day.
    """
    info = sb.get(f"companyinfo/{sb.realm}", {})["CompanyInfo"]
    if info.get("CompanyName") != COMPANY_NAME:
        try:
            sb.post("CompanyInfo", {"Id": info["Id"], "SyncToken": info["SyncToken"], "sparse": True,
                                    "CompanyName": COMPANY_NAME})
            print(f"  company renamed to {COMPANY_NAME}")
        except qbo.QBOError as e:
            print(f"  could not rename the company ({e.code}); rename it in QuickBooks settings")
    accts = {a["Id"]: {"classification": a["Classification"], "full_name": a["FullyQualifiedName"]}
             for a in sb.every("Account")}
    rep = sb.general_ledger(fx.date(2025, 1, 1), fx.date.today(), "Accrual")
    gl = qbo.parse_general_ledger(rep, accts)
    lines = [l for l in gl if l["txn_type"] == "Sales Tax Payment"]
    done = {l["date"] for l in gl if l["txn_type"] == "Journal Entry"
            and l["memo"].startswith("Cancel Intuit sample sales-tax payment")}
    by_txn: dict = {}
    for l in lines:
        by_txn.setdefault((l["txn_id"], l["date"]), []).append(l)
    for (tid, dt), ls in by_txn.items():
        memo = f"Cancel Intuit sample sales-tax payment {tid}"
        if dt in done or sum(l["amount"] for l in ls) != 0:
            continue
        sb.post("JournalEntry", {"TxnDate": dt.isoformat(), "PrivateNote": f"{memo} [{TAG}]", "Line": [
            {"Amount": str(abs(l["amount"])), "DetailType": "JournalEntryLineDetail", "Description": memo,
             "JournalEntryLineDetail": {"PostingType": "Credit" if l["amount"] > 0 else "Debit",
                                        "AccountRef": {"value": l["account_id"]}}} for l in ls]})
        print(f"  cancelled sample sales-tax payment {tid} ({dt}) with a journal entry")
    for i in sb.all("Item", "Type = 'Inventory'"):
        try:
            sb.set_active("Item", i, False)
            print(f"  made inventory item {i['Name']} inactive")
        except qbo.QBOError as e:
            print(f"  inventory item {i['Name']} stays active ({str(e)[150:300]})")
    kept = []
    for a in sb.all("Account", "Active = true"):
        if a["Id"] in keep:
            continue
        if a.get("CurrentBalance"):
            kept.append(f"{a['FullyQualifiedName']} ({a['CurrentBalance']})")
            continue
        try:
            sb.set_active("Account", a, False)
        except qbo.QBOError:
            kept.append(a["FullyQualifiedName"])       # system accounts can't be made inactive
    print("  sample accounts still active: " + (", ".join(kept) or "none"))


def check(sb: Sandbox) -> int:
    """QuickBooks' own trial balance at Sep 30 and Aug 31 against the fixture's, by account name."""
    root = Path(tempfile.mkdtemp())
    try:
        inp = fx.build(root) / "inputs"
        bad = 0
        for fname, end in (("tb_prior.csv", "2026-08-31"), ("tb_current.csv", "2026-09-30")):
            with open(inp / fname, newline="") as f:
                want = {r["account"]: D(r["debit"] or 0) - D(r["credit"] or 0) for r in csv.DictReader(f)}
            rep = sb.report("TrialBalance", start_date="2026-01-01", end_date=end, accounting_method="Accrual")
            names = {a["Id"]: a["FullyQualifiedName"] for a in sb.all("Account")}
            got = {}
            for r in qbo.parse_trial_balance(rep):
                got[names.get(r["account_id"], r["account"])] = r["debit"] - r["credit"]
            ours = {n.lower() for _, n, *_ in fx.ACCOUNTS}
            for name in sorted(set(want) | set(got)):
                w, g = want.get(name, D(0)), got.get(name, D(0))
                if w == g:
                    continue
                if name.replace(" (deleted)", "").lower() in ours:
                    bad += 1
                    print(f"  {end} {name:34} fixture {w:>12} QuickBooks {g:>12}")
                else:
                    print(f"  {end} sample leftover: {name} {g}")
        print("Northwind's accounts match the fixture." if not bad else f"{bad} difference(s) on Northwind's accounts.")
        return 1 if bad else 0
    finally:
        shutil.rmtree(root)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["wipe", "load", "tidy", "check", "files"])
    ap.add_argument("--profile", default="sandbox-loader")
    ap.add_argument("--out", help="folder for the statement, Stripe, payroll and budget files")
    a = ap.parse_args(argv)
    try:
        if a.command == "files":
            from make_files import write_files
            print(write_files(Path(a.out or "northwind-files").expanduser().resolve()))
            return 0
        sb = Sandbox(a.profile)
        info = sb.get(f"companyinfo/{sb.realm}", {})["CompanyInfo"]
        print(f"Sandbox company: {info.get('CompanyName')} (ID {sb.realm})")
        if a.command == "wipe":
            wipe(sb)
        elif a.command == "load":
            if any(sb.all(ent) for ent in ("Deposit", "Bill", "Purchase", "JournalEntry", "Invoice")):
                raise StopRun("The company still has transactions. Run `wipe` first, so the load starts clean.")
            txns = fixture_transactions()
            used = {aid for t in txns for aid, _ in t["entries"]}
            ld = Loader(sb)
            ld.ensure_accounts(used)
            ld.ensure_names(txns)
            ld.ensure_items()
            ld.load(txns)
            print(f"Loaded {len(txns)} transactions.")
            tidy(sb, ld.keep)
            return check(sb)
        elif a.command == "tidy":
            ld = Loader(sb)
            ld.ensure_accounts({aid for t in fixture_transactions() for aid, _ in t["entries"]})
            tidy(sb, ld.keep)
            return check(sb)
        elif a.command == "check":
            return check(sb)
        return 0
    except StopRun as e:
        print(f"STOPPED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
