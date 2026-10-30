"""Check that the words Claude wrote carry only numbers the checks computed.

1. The facts are recomputed from inputs/ (re-running the checks), so editing results.json, or the
   review's computed parts, can't make a number pass.
2. Every marked section is written; nothing outside the marked sections changed.
3. Every number between the markers matches a computed fact of the same unit (dollars, %, months,
   counts), at the precision written, with a sign or direction word that agrees with it.
4. Every reference in brackets exists in the review.
"""

from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from .outputs import MARKERS, PLACEHOLDER, normalized
from .util import _json_default
from .util import MONTH_NAMES, StopRun

WHITELIST = re.compile(
    r"401\s?\(k\)|403\s?\(b\)|83\s?\(b\)|\bForm\s+\d+[-\w]*|\b1099(-[A-Z]+)?\b|\bW-[2489]\b|\bASC\s?\d+(-\d+)*|"
    r"\bSection\s+\d+\w*|\bIRC\s?\d+|\bSOC\s?[12]\b|\bQ[1-4]\b(\s+\d{4})?|\bFY\s?'?\d{2,4}\b|\bH[12]\b|\bnet\s+\d{1,3}\b|"
    r"\bT\+\d\b|\b24/7\b|\bP&L\b|\btop\s+\d{1,2}\b|\b\d+-(day|week|month|year)s?\b|"
    r"\b\d{1,3}\s*[-–]\s*\d{1,3}\s+days?\b", re.I)
MONTHS = "|".join(MONTH_NAMES + [m[:3] for m in MONTH_NAMES] + ["Sept"])
DATES = re.compile(
    rf"\b\d{{4}}-\d{{2}}(-\d{{2}})?\b|\b\d{{1,2}}/\d{{1,2}}/\d{{2,4}}\b|\b\d{{1,2}}/\d{{4}}\b|"
    rf"\b({MONTHS})\.?\s+\d{{1,2}}\s*[-–]\s*\d{{1,2}}\b|"
    rf"\b({MONTHS})\.?\s+\d{{1,2}}(st|nd|rd|th)?(,\s*\d{{4}})?\b|\b({MONTHS})\.?\s+'?\d{{4}}\b|"
    rf"\b\d{{1,2}}(st|nd|rd|th)?\s+(of\s+)?({MONTHS})\b", re.I)
WORD_NUM = re.compile(r"\b(doubled|tripled|quadrupled|halved|twice as|three times|ten times)\b|"
                      r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|"
                      r"fifty|sixty|seventy|eighty|ninety|a|several|a few)\s+(hundred|thousand|million|billion)\b", re.I)
NUM = re.compile(
    r"(?P<neg>[-−])?(?P<paren>\()?(?P<cur>\$)?\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\)?"
    r"(?P<suf>\s?[kKmMbB]\b|\s?%|\s+percent(age points?)?\b|\s+(million|thousand|billion)\b|\s+months?\b|\s+pp\b)?")
SAFE_NOUNS = re.compile(r"^\s+(open\s+)?(high|medium|low\s+)?(items?|entr(y|ies)|lines?|drafts?|questions?|checks?|exceptions?)\b", re.I)
PLAN_WORDS = re.compile(r"\b(plan|term|contract|subscription|annual|policy|lease|prepaid|period|covers?|covering|paid for)\b", re.I)
UP = re.compile(r"\b(rose|grew|increased?|up|higher|gain(ed)?|profit(able)?|surplus|added|improved|ahead)\b", re.I)
DOWN = re.compile(r"\b(fell|dropped|decreased?|down|lower|loss|negative|declined?|cut|shortfall|deficit|"
                  r"worse|minus|behind|short)\b", re.I)
# Outflow words fit a negative figure ("burned $52k") and a positive outflow ("net burn of $88k") alike.
OUTFLOW = re.compile(r"\b(burn(s|ed|ing)?|used|spent|spend(ing)?|outflow)\b", re.I)
REF_ID = re.compile(r"^\s*([EAD]\d+|[TCKVG]\d)\s*$", re.I)


class Facts:
    def __init__(self, facts: list):
        self.by_unit = {"usd": [], "pct": [], "months": [], "count": []}
        for f in facts:
            try:
                v = Decimal(f["value"])
            except (InvalidOperation, KeyError, TypeError):
                continue
            signed = not f["label"].endswith(" amount")
            self.by_unit.setdefault(f["unit"], []).append((v, signed))

    def find(self, unit: str, value: Decimal, places: int, mult: Decimal):
        q = Decimal(1).scaleb(-places)
        return [(v, sg) for v, sg in self.by_unit.get(unit, [])
                if (abs(v) / mult).quantize(q, rounding=ROUND_HALF_UP) == value]


def known_refs(results: dict) -> set:
    refs = set()
    for e in results.get("exceptions", []) + results.get("acknowledged", []):
        refs.add(e["id"].lower())
        refs |= {r.lower() for r in e.get("refs", [])}
    refs |= {x["id"].lower() for x in results.get("drafts", [])}
    refs |= {c["id"].lower() for c in results.get("checks", [])}
    for row in (results.get("flux") or {}).get("rows", []):
        refs |= {f"v1 {row['account']}".lower(), f"p&l {row['account']}".lower()}
        for dv in row.get("drivers", []):
            refs |= {r.lower() for r in dv.get("refs", [])}
    for row in (results.get("balance_sheet") or {}).get("rows", []):
        refs |= {f"tb {row['account']}".lower(), f"v2 {row['account']}".lower()}
        refs |= {t["ref"].lower() for t in row.get("top_lines", [])}
    return refs


REF_LIKE = re.compile(r"^(GL|TB|P&L|V[1-4]|bill|stripe|history|bank/)", re.I)


def _strip_refs(text: str, refs: set, bad: list) -> str:
    """Known references are removed. Unknown reference-looking parts are reported. Anything else inside
    brackets is checked like ordinary text."""
    def repl(m):
        parts = [p.strip() for p in re.split(r"[;,]", m.group(1)) if p.strip()]
        keep = []
        for p in parts:
            if p.lower() in refs:
                continue
            if REF_ID.match(p) or REF_LIKE.match(p):
                bad.append(f"[{p}] is not a reference in this review")
                if REF_ID.match(p) or re.match(r"^(GL|bill|stripe|bank/)", p, re.I):
                    continue
            keep.append(p)
        return " " + " ".join(keep) + " "
    text = re.sub(r"\[([^\]]*)\]", repl, text)
    return re.sub(r"`([^`]*)`", repl, text)


def check_text(text: str, facts: Facts, refs: set | None = None, allow_durations: bool = False) -> list[str]:
    bad = []
    for m in WORD_NUM.finditer(text):
        bad.append(f'"{m.group(0)}": write the computed figure instead of a number word')
    clean = _strip_refs(text, refs or set(), bad)
    clean = WHITELIST.sub(" ", clean)
    clean = DATES.sub(" ", clean)
    clean = re.sub(r"(?m)^\s*(\d{1,2})[.)]\s", " ", clean)
    for s in re.split(r"(?<=[.!?])\s+|\n", clean):
        for m in NUM.finditer(s):
            raw = m.group(0).strip()
            num = m.group("num")
            suf = (m.group("suf") or "").strip().lower()
            try:
                value = Decimal(num.replace(",", ""))
            except InvalidOperation:
                continue
            places = len(num.split(".")[1]) if "." in num else 0
            neg = bool(m.group("neg")) or (bool(m.group("paren")) and m.group(0).rstrip().endswith(")"))
            after = s[m.end():m.end() + 30]
            mult = {"k": Decimal(1000), "thousand": Decimal(1000), "m": Decimal(1000000), "million": Decimal(1000000),
                    "b": Decimal(10) ** 9, "billion": Decimal(10) ** 9}.get(suf, Decimal(1))
            bare = not (m.group("cur") or suf or "," in num or "." in num)
            if bare and 1990 <= value <= 2100:
                continue                              # a year
            if "%" in suf or "percent" in suf or suf == "pp":
                unit = "pct"
            elif suf.startswith("month"):
                unit = "months"
                if "runway" not in s.lower() and (allow_durations or PLAN_WORDS.search(s)) and value <= 36:
                    continue                          # a plan term, not a metric
            elif m.group("cur") or mult != 1 or "," in num or "." in num:
                unit = "usd"
            else:
                if value <= 100 and SAFE_NOUNS.match(after):
                    continue                          # counting items in this review
                unit = "count"
            sig = len(num.replace(",", "").replace(".", "").lstrip("0"))
            if mult != 1 and sig < (3 if mult == 1000 else 2):
                bad.append(f"{raw}: too rounded to check; give more digits (e.g. $83.9k, $2.9 million)")
                continue
            hits = facts.find(unit, value, places, mult)
            if unit == "count" and not hits:
                hits = facts.find("usd", value, places, mult)
            if not hits:
                bad.append(f"{raw} is not a number the checks computed")
                continue
            if not all(sg for _, sg in hits):
                continue                              # matches a magnitude (an item's amount): no sign to check
            window = _clause(s, m.start(), m.end())
            up, down, outflow = bool(UP.search(window)), bool(DOWN.search(window)), bool(OUTFLOW.search(window))
            if all(v < 0 for v, _ in hits):
                if not neg and not down and not outflow:
                    bad.append(f"{raw}: the computed figure is negative; write it with a minus sign or say loss/down/fell")
                elif not neg and up:
                    bad.append(f"{raw}: the computed figure is negative, but the sentence reads as an increase or profit")
            elif all(v > 0 for v, _ in hits):
                if neg:
                    bad.append(f"{raw}: the computed figure is positive")
                elif down and not up and unit != "months":
                    bad.append(f"{raw}: the computed figure is positive, but the sentence reads as a decrease or loss")
    return bad


def _clause(s: str, start: int, end: int) -> str:
    """The clause around a number: from the previous , ; : or 'but' to the next one."""
    left = s[:start]
    cut = max(left.rfind(";"), left.rfind(","), left.rfind(":"), left.lower().rfind(" but "))
    right = s[end:]
    stops = [i for i in (right.find(";"), right.find(","), right.find(":"), right.lower().find(" but ")) if i >= 0]
    return left[cut + 1:] + s[start:end] + right[:min(stops) if stops else len(right)]


def _sections(text: str, name: str) -> list[tuple[str, str | None]]:
    out = []
    for s, e in MARKERS[name]:
        if s not in text or e not in text:
            out.append((s, None))
        else:
            out.append((s, text.split(s, 1)[1].split(e, 1)[0]))
    return out


def _rebuild(month_dir: Path, stored: dict):
    """Re-run the gate and checks from inputs/ and rebuild results.json and both documents, or None if the inputs no
    longer pass the gate. Nothing stored next to the outputs is trusted: a hash file there can be rewritten as easily
    as the outputs it protects."""
    from . import config
    from .checks import run_all
    from .data import load
    from .gate import run_gate
    from .outputs import build_results, render, render_update
    from .util import Period
    root, period = month_dir.parent, Period(month_dir.name)
    d = load(month_dir, period, config.load(root))
    if run_gate(d):
        return None
    results, exc, ctx = run_all(d, root)
    res = build_results(d, results, exc, ctx)
    res["meta"]["generated_at"] = (stored.get("meta") or {}).get("generated_at", "")   # the one value that varies
    docs = {"review.md": render(d, res, results), "investor-update.md": render_update(d, res)}
    return json.loads(json.dumps(res, default=_json_default)), docs


def verify(month_dir: Path) -> list[str]:
    out = month_dir / "output"
    problems = []
    if not (out / "results.json").exists():
        raise StopRun("No review to verify. Run `mer.py run` first.")
    results = json.loads((out / "results.json").read_text())
    rebuilt = _rebuild(month_dir, results)
    if rebuilt is None:
        problems.append("The inputs no longer pass the gate. Re-run the review.")
        fresh_res, docs = {"facts": []}, {}
    else:
        fresh_res, docs = rebuilt
        if fresh_res != results:
            problems.append("results.json doesn't match what the inputs produce now (it was edited, or the inputs or "
                            "acknowledgements changed). Re-run the review.")
    fresh = fresh_res["facts"]
    facts, refs = Facts(fresh), known_refs(results)
    for name in ("review.md", "investor-update.md"):
        p = out / name
        if not p.exists():
            problems.append(f"{name} is missing. Re-run.")
            continue
        text = p.read_text(encoding="utf-8")
        if name in docs and normalized(text, name) != normalized(docs[name], name):
            problems.append(f"{name}: text outside the marked sections was changed. Only write between the markers.")
        for marker, body in _sections(text, name):
            label = marker.replace("<!-- ", "").replace(":start -->", "")
            if body is None:
                problems.append(f"{name}: the {label} markers are missing.")
                continue
            if PLACEHOLDER in body or not body.strip():
                problems.append(f"{name}: the {label} section has not been written.")
                continue
            for b in check_text(body, facts, refs, allow_durations=(label == "questions")):
                problems.append(f"{name} {label}: {b}")
    return problems
