"""A stand-in for Stripe's API with Northwind's made-up data, on this computer only (level-4 test of `pull-stripe`).

  python3 testdata/northwind/fake_stripe.py --port 8765
  MER_STRIPE_API_BASE=http://127.0.0.1:8765 mer.py pull-stripe --root <work folder> --month 2026-09

It answers the four calls `pull-stripe` makes, shaped like Stripe's API version 2024-06-20:
- /v1/balance_transactions: September's rows are the same as the level-4 CSV export. October adds ten charges and
  a payout, so the closing balance has to be worked back from today's balance.
- /v1/balance: today's balance.
- /v1/invoices and /v1/invoices/{id}/lines: 20 monthly $2,500 subscriptions, Sep 2025 to Oct 2026.

Like Stripe, lists are newest first and paged with `starting_after`. Pages here are 10 long, so paging is exercised.
Requests need a restricted key (rk_...), as the skill sends.
Without a Stripe-Version header, expanding `data.lines.data.price` fails, as it does on accounts whose default API
version is newer than 2024-06-20.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
import fixture_northwind as fx  # noqa: E402

PAGE = 10
FEE, GROSS = 7400, 250000           # cents: $74 fee on a $2,500 charge, as in the fixture


def ts(d: date, hour: int = 14) -> int:
    return int(datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc).timestamp())


def cents(v) -> int:
    return int((v * 100).to_integral_value())


def balance_transactions() -> list[dict]:
    rows = []
    for r in fx.base_extras()["stripe"]:
        rows.append({"id": r["id"], "object": "balance_transaction", "amount": cents(r["amount"]), "fee": cents(r["fee"]),
                     "net": cents(r["net"]), "currency": "usd", "created": ts(r["date"]), "description": r["description"],
                     "reporting_category": r["category"], "type": r["category"], "status": "available"})
    for k in range(1, 11):          # October, after month-end
        rows.append({"id": f"txn_oc{k:02d}", "object": "balance_transaction", "amount": GROSS, "fee": FEE,
                     "net": GROSS - FEE, "currency": "usd", "created": ts(date(2026, 10, k)),
                     "description": f"Subscription renewal #{k}", "reporting_category": "charge", "type": "charge",
                     "status": "pending"})
    rows.append({"id": "po_05", "object": "balance_transaction", "amount": -5 * (GROSS - FEE), "fee": 0,
                 "net": -5 * (GROSS - FEE), "currency": "usd", "created": ts(date(2026, 10, 6)),
                 "description": "STRIPE PAYOUT", "reporting_category": "payout", "type": "payout", "status": "available"})
    return rows


OPENING_SEP = 200000                # $2,000.00 on Aug 31, as in statements.md


def balance() -> dict:
    now = OPENING_SEP + sum(t["net"] for t in balance_transactions())
    pending = sum(t["net"] for t in balance_transactions() if t["status"] == "pending")
    return {"object": "balance", "livemode": True,
            "available": [{"amount": now - pending, "currency": "usd", "source_types": {"card": now - pending}}],
            "pending": [{"amount": pending, "currency": "usd", "source_types": {"card": pending}}]}


def add_month(d: date) -> date:
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return date(y, m, min(d.day, 28))


def invoices() -> list[dict]:
    out, m = [], date(2025, 9, 1)
    while m <= date(2026, 10, 1):
        for c in range(1, 21):
            day = date(m.year, m.month, c)
            if day > date(2026, 10, 10):
                continue
            iid = f"in_nw{m:%y%m}{c:02d}"
            out.append({
                "id": iid, "object": "invoice", "customer": f"cus_NW{c:02d}", "status": "paid", "currency": "usd",
                "created": ts(day, 13), "total": GROSS, "subtotal": GROSS, "total_excluding_tax": GROSS, "tax": None,
                "status_transitions": {"finalized_at": ts(day, 13), "paid_at": ts(day, 14)},
                "lines": {"object": "list", "has_more": False, "url": f"/v1/invoices/{iid}/lines", "data": [{
                    "id": f"il_{iid[3:]}", "object": "line_item", "amount": GROSS, "currency": "usd",
                    "type": "subscription", "subscription": f"sub_NW{c:02d}", "discount_amounts": [],
                    "period": {"start": ts(day, 13), "end": ts(add_month(day), 13)},
                    "price": {"id": "price_nw_monthly", "object": "price", "unit_amount": GROSS,
                              "recurring": {"interval": "month", "interval_count": 1}}}]}})
        m = add_month(m)
    return out


class Handler(BaseHTTPRequestHandler):
    calls: list[str] = []

    def reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def error(self, code: int, msg: str) -> None:
        self.reply(code, {"error": {"type": "invalid_request_error", "message": msg}})

    def log_message(self, fmt, *args):
        sys.stderr.write(f"fake-stripe {self.command} {self.path}\n")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer rk_"):
            return self.error(401, "Invalid API Key provided.")
        expand = q.get("expand[]", [])
        if "data.lines.data.price" in expand and not self.headers.get("Stripe-Version"):
            return self.error(400, "This property cannot be expanded (data.lines.data.price).")
        path = url.path.rstrip("/")
        if path == "/v1/balance":
            return self.reply(200, balance())
        if path == "/v1/balance_transactions":
            items = balance_transactions()
        elif path == "/v1/invoices":
            items = invoices()
            if "data.lines.data.price" not in expand:
                for i in items:
                    for ln in i["lines"]["data"]:
                        ln["price"] = ln["price"]["id"]
        elif path.startswith("/v1/invoices/") and path.endswith("/lines"):
            inv = next((i for i in invoices() if i["id"] == path.split("/")[3]), None)
            if not inv:
                return self.error(404, "No such invoice.")
            items = inv["lines"]["data"]
        else:
            return self.error(404, f"Unrecognized request URL (GET: {path}).")
        if "created[gte]" in q:
            items = [i for i in items if i.get("created", 0) >= int(q["created[gte]"][0])]
        if "created[lt]" in q:
            items = [i for i in items if i.get("created", 0) < int(q["created[lt]"][0])]
        items.sort(key=lambda i: (i.get("created", 0), i["id"]), reverse=True)
        if "starting_after" in q:
            ids = [i["id"] for i in items]
            after = q["starting_after"][0]
            items = items[ids.index(after) + 1:] if after in ids else []
        limit = min(int(q.get("limit", ["10"])[0]), PAGE)
        self.reply(200, {"object": "list", "url": path, "has_more": len(items) > limit, "data": items[:limit]})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"Fake Stripe for Northwind on http://127.0.0.1:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
