"""Small shared helpers: money, dates, periods, CSV/JSON I/O."""

from __future__ import annotations

import calendar
import csv
import hashlib
import json
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

ZERO = Decimal("0")
CENT = Decimal("0.01")


class StopRun(Exception):
    """The run must stop. The message is shown to the user as-is."""


# ---------- money ----------

_MONEY = re.compile(r"^-?(\d+(\.\d*)?|\.\d+)$")   # QuickBooks writes zero as ".00"


def money(value) -> Decimal:
    """Parse a money value strictly. Raises ValueError on anything that isn't a plain finite number.

    Accepts: 1234.56, -1234.56, 1,234.56, $1,234.56, (1,234.56), 1234.56-
    Rejects: empty, NaN, Infinity, "12 34", "(-5)", "1e5", text.
    An empty string is NOT zero: callers decide what empty means.
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


def money_or_zero(value) -> Decimal:
    """For report cells where QuickBooks leaves zero cells blank."""
    if value is None or str(value).strip() == "":
        return ZERO
    return money(value)


def q2(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def fmt(d, sign: bool = False) -> str:
    """$1,234.56 / -$1,234.56 (or +$ with sign=True). None -> '' ."""
    if d is None:
        return ""
    d = q2(Decimal(d))
    s = f"${abs(d):,.2f}"
    if d < 0:
        return "-" + s
    return ("+" + s) if sign and d > 0 else s


def pct(part: Decimal, base: Decimal):
    """Percent change relative to base; None when base is zero."""
    if base == 0:
        return None
    return (part / abs(base) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def to_bool(v, name: str) -> bool:
    if v is True or v is False:
        return v
    raise StopRun(f'"{name}" in review-config.json must be true or false (without quotes), got {v!r}.')


# ---------- dates and periods ----------

def parse_date(s, fmt_hint: str | None = None) -> date:
    s = str(s).strip()
    if not s:
        raise ValueError("empty date")
    if fmt_hint:
        try:
            return datetime.strptime(s, fmt_hint).date()
        except ValueError as e:
            raise ValueError(f"date {s!r} doesn't match format {fmt_hint!r}") from e
    for f in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError as e:
        raise ValueError(f"unrecognised date: {s!r}") from e


MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
               "October", "November", "December"]


class Period:
    """A calendar month, e.g. Period('2026-09')."""

    def __init__(self, ym: str):
        m = re.fullmatch(r"(\d{4})-(\d{2})", ym or "")
        if not m or not (1900 <= int(m.group(1)) <= 2200) or not (1 <= int(m.group(2)) <= 12):
            raise StopRun(f"Month must look like 2026-09, got {ym!r}.")
        self.year, self.month = int(m.group(1)), int(m.group(2))
        self.key = ym
        self.start = date(self.year, self.month, 1)
        self.end = date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])

    def shift(self, n: int) -> "Period":
        m = self.year * 12 + (self.month - 1) + n
        return Period(f"{m // 12:04d}-{m % 12 + 1:02d}")

    @property
    def prior(self) -> "Period":
        return self.shift(-1)

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end

    @property
    def label(self) -> str:
        return f"{MONTH_NAMES[self.month - 1]} {self.year}"

    def mentioned_in(self, text: str) -> bool:
        """Does free text name this month? 'September', 'Sept 2026', '09/2026', '2026-09'.

        A bare month name counts only if no other year is attached to it, and 'May' always
        needs a year (the English word 'may' is too common)."""
        t = text or ""
        full = MONTH_NAMES[self.month - 1]
        abbrevs = {full, full[:3]} | ({"Sept"} if self.month == 9 else set())
        y, yy = str(self.year), str(self.year)[2:]
        numeric = [rf"\b0?{self.month}/{y}\b", rf"\b{y}-{self.month:02d}\b", rf"\b0?{self.month}/{yy}\b"]
        if any(re.search(p, t) for p in numeric):
            return True
        for a in abbrevs:
            pat = rf"\b{a}\b\.?(?:\s+\d{{1,2}}(?:st|nd|rd|th)?\b)?(?:\s*[-,]?\s*(\d{{4}}|['’]\d{{2}})\b)?"
            for m in re.finditer(pat, t, re.I):
                if a.lower() == "may" and m.group(0)[0].islower():
                    continue
                year = m.group(1)
                if year is None:
                    if a.lower() == "may":
                        continue
                    return True
                if year.lstrip("'’") in (y, yy):
                    return True
        return False

    def __repr__(self):
        return f"Period({self.key})"


def days_between(a: date, b: date) -> int:
    return abs((a - b).days)


def add_days(d: date, n: int) -> date:
    return d + timedelta(days=n)


def overlap_days(a0: date, a1: date, b0: date, b1: date) -> int:
    lo, hi = max(a0, b0), min(a1, b1)
    return max(0, (hi - lo).days + 1)


# ---------- I/O ----------

def read_csv(path: Path) -> list[dict]:
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except UnicodeDecodeError as e:
        raise StopRun(f"{path.name} is not a UTF-8 CSV file.") from e


def csv_header(path: Path) -> list[str]:
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return [h.strip() for h in next(csv.reader(f), [])]
    except UnicodeDecodeError as e:
        raise StopRun(f"{path.name} is not a UTF-8 CSV file. Re-save it as CSV (UTF-8).") from e


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: _cell(r.get(k)) for k in fields})


def _cell(v):
    if isinstance(v, Decimal):
        return str(q2(v))
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (list, tuple, set)):
        return "; ".join(str(x) for x in v)
    return "" if v is None else v


def read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise StopRun(f"{path} is not valid JSON ({e.msg} at line {e.lineno}). Fix or re-create it.") from e


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=_json_default)
        f.write("\n")


def _json_default(v):
    if isinstance(v, Decimal):
        return str(q2(v))
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, set):
        return sorted(v)
    raise TypeError(type(v))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def slug(s: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return out or "account"


def norm(s: str) -> str:
    """Normalise a name for matching (vendor names, account names)."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def memo_key(s: str) -> str:
    """Memo with dates, month names and numbers removed, so 'Depreciation Sep 2026' == 'Depreciation Aug 2026'."""
    t = norm(s)
    t = re.sub(r"\b(" + "|".join(m.lower() for m in MONTH_NAMES) + r"|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b", " ", t)
    t = re.sub(r"[\d/.,$-]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()
