"""Small shared helpers: money, dates, periods, CSV/JSON I/O."""

from __future__ import annotations

import calendar
import csv
import json
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ZERO = Decimal("0")
CENT = Decimal("0.01")


class StopRun(Exception):
    """The run must stop. The message is shown to the person as-is."""


# ---------- money ----------

_MONEY = re.compile(r"^-?(\d+(\.\d*)?|\.\d+)$")


def money(value) -> Decimal:
    """Parse a money value strictly. Raises ValueError on anything that isn't a plain finite number.

    Accepts: 1234.56, -1234.56, 1,234.56, $1,234.56, -$1,234.56, (1,234.56), 1234.56-
    Rejects: empty, NaN, Infinity, "12 34", "(-5)", "1e5", text. Empty is NOT zero.
    """
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"not a finite number: {value!r}")
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    if value is None:
        raise ValueError("empty amount")
    s = str(value).strip()
    if s == "":
        raise ValueError("empty amount")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1].strip()
        if s.startswith("-"):
            raise ValueError(f"not a number: {value!r}")
    if s.endswith("-") and not s.startswith("-"):
        neg, s = True, s[:-1]
    if s.startswith("-$"):
        neg, s = not neg, s[2:]
    elif s.startswith("$"):
        s = s[1:]
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s):
        s = s.replace(",", "")
    if not _MONEY.match(s):
        raise ValueError(f"not a number: {value!r}")
    d = Decimal(s)
    return -d if neg else d


def q2(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def fmt(d, sign: bool = False) -> str:
    """$1,234.56 / -$1,234.56 (or +$ with sign=True). None -> ''."""
    if d is None:
        return ""
    d = q2(Decimal(d))
    s = f"${abs(d):,.2f}"
    if d < 0:
        return "-" + s
    return ("+" + s) if sign and d > 0 else s


# ---------- dates ----------

DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%Y/%m/%d")


def parse_date(s, fmt_hint: str | None = None) -> date:
    s = str(s or "").strip()
    if not s:
        raise ValueError("empty date")
    if fmt_hint:
        try:
            return datetime.strptime(s, fmt_hint).date()
        except ValueError as e:
            raise ValueError(f"date {s!r} doesn't match format {fmt_hint!r}") from e
    head = s.split("T")[0].split(" ")[0]
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(head, f).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {s!r}")


def iso_date(s: str, what: str) -> date:
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
    except ValueError as e:
        raise StopRun(f"{what} must be a date like 2026-09-30, got {s!r}.") from e


class Period:
    """A calendar month, e.g. Period('2026-09'). Names the work folder; statements carry their own dates."""

    def __init__(self, ym: str):
        m = re.fullmatch(r"(\d{4})-(\d{2})", ym or "")
        if not m or not (1900 <= int(m.group(1)) <= 2200) or not (1 <= int(m.group(2)) <= 12):
            raise StopRun(f"Month must look like 2026-09, got {ym!r}.")
        self.year, self.month = int(m.group(1)), int(m.group(2))
        self.key = ym
        self.start = date(self.year, self.month, 1)
        self.end = date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])

    @property
    def prior(self) -> "Period":
        m = self.year * 12 + (self.month - 1) - 1
        return Period(f"{m // 12:04d}-{m % 12 + 1:02d}")

    @property
    def label(self) -> str:
        return self.start.strftime("%B %Y")


def days_between(a: date, b: date) -> int:
    return abs((a - b).days)


def add_days(d: date, n: int) -> date:
    return d + timedelta(days=n)


# ---------- text ----------

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "account"


STOP = {"the", "and", "for", "from", "to", "inc", "llc", "ltd", "co", "corp", "payment", "deposit", "debit", "credit",
        "ach", "wire", "online", "pos", "card", "purchase", "pmt", "trf", "xfer", "with", "via", "bank", "in", "out",
        "sent", "received", "transaction"}


def tokens(*texts) -> set:
    out = set()
    for t in texts:
        out |= {w for w in re.findall(r"[a-z]{3,}", (t or "").lower()) if w not in STOP}
    return out


TRANSFER_WORDS = re.compile(r"\b(transfer|xfer|trf|sweep|move money|to savings|from savings|to checking|"
                            r"from checking|to treasury|from treasury)\b", re.I)


# ---------- I/O ----------

# A cell starting with one of these runs as a formula in Excel or Google Sheets. Text cells are written with a
# leading ' so they show as text; read_csv removes it.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_PLAIN_NUMBER = re.compile(r"[-+]?(\d[\d,]*)?\.?\d+%?")


def _unescape(v):
    return v[1:] if isinstance(v, str) and len(v) > 1 and v[0] == "'" and v[1] in _FORMULA_START else v


def read_csv(path: Path) -> list[dict]:
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return [{(k or "").strip(): _unescape(v) for k, v in r.items()} for r in csv.DictReader(f)]
    except UnicodeDecodeError as e:
        raise StopRun(f"{path.name} is not a UTF-8 CSV file. Re-save it as CSV (UTF-8).") from e


def csv_header(path: Path) -> list[str]:
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return [h.strip() for h in next(csv.reader(f), [])]
    except UnicodeDecodeError as e:
        raise StopRun(f"{path.name} is not a UTF-8 CSV file. Re-save it as CSV (UTF-8).") from e


def _cell(v):
    if isinstance(v, Decimal):
        return str(q2(v))
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (list, tuple, set)):
        v = "; ".join(str(x) for x in v)
    if isinstance(v, str) and v.startswith(_FORMULA_START) and not _PLAIN_NUMBER.fullmatch(v):
        return "'" + v
    return "" if v is None else v


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: _cell(r.get(k)) for k in fields})


def read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise StopRun(f"{path} is not valid JSON ({e.msg} at line {e.lineno}). Fix or re-create it.") from e


def _json_default(v):
    if isinstance(v, Decimal):
        return str(q2(v))
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, set):
        return sorted(v)
    raise TypeError(type(v))


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=_json_default)
        f.write("\n")
