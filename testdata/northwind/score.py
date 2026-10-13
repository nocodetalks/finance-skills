"""Score a month-end review of the Northwind sandbox against the answer key.

  python3 testdata/northwind/score.py <work folder>/2026-09/output/results.json

Prints which of the 15 planted mistakes were caught, by which finding, and every other open finding (extras), so
nothing the review said is left unexplained.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

KEY = json.loads((Path(__file__).resolve().parent / "answer_key.json").read_text())


def findings(res: dict) -> list[dict]:
    """Every finding: open and acknowledged leads, plus those grouped under them ('also seen by')."""
    out = []
    for e in res["exceptions"] + res.get("acknowledged", []):
        out.append({"id": e["id"], "check": e["check"], "title": e["title"], "detail": e.get("detail", ""), "lead": e})
        out += [{"id": e["id"], "check": a["check"], "title": a["title"], "detail": a.get("detail", ""), "lead": e}
                for a in e.get("also", [])]
    return out


def matches(f: dict, rule: dict) -> bool:
    if rule.get("check") and f["check"] != rule["check"]:
        return False
    title, detail = f["title"].lower(), str(f["detail"]).lower()
    return (all(w.lower() in title for w in rule.get("words", []))
            and all(w.lower() in detail for w in rule.get("detail", [])))


def score(res: dict) -> tuple[str, int]:
    fs = findings(res)
    used, lines, caught, expected_miss_ok = set(), [], 0, 0
    for m in KEY["mistakes"]:
        hits = []
        for rule in m["find"]:
            hit = next((f for f in fs if matches(f, rule)), None)
            hits.append(hit)
            if hit:
                used.add(hit["lead"]["id"])
        found = all(hits)
        if m.get("expect") == "missed":
            ok = not any(hits)
            expected_miss_ok += ok
            status = "missed, as expected" if ok else "flagged (unexpected)"
        else:
            caught += found
            status = "CAUGHT" if found else ("PARTLY" if any(hits) else "MISSED")
        by = "; ".join(f"{h['id']} {h['check']}: {h['title']}" for h in hits if h) or "-"
        lines.append(f"{m['n']:>2}. {status:22} {m['mistake']}\n      by {by}"
                     + (f"\n      note: {m['live_note']}" if m.get("live_note") and status != "CAUGHT" else ""))
    extras = [e for e in res["exceptions"] if e["id"] not in used]
    total = len([m for m in KEY["mistakes"] if m.get("expect") != "missed"])
    head = (f"Caught {caught} of {total} planted mistakes"
            + (", plus the expected miss (#15)" if expected_miss_ok else "") + ".")
    out = [head, ""] + lines + ["", f"Other open findings ({len(extras)}), each needs an explanation:"]
    out += [f"  {e['id']} [{e['severity']}] {e['check']}: {e['title']}" for e in extras] or ["  none"]
    return "\n".join(out), caught


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__)
        return 2
    text, _ = score(json.loads(Path(argv[0]).read_text()))
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
