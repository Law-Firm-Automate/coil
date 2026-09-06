"""Collect audit/results/*.json into the rows the Google Sheet wants.

Prints JSON: {"rows": [[...]], "summary": {...}, "by_group": [[...]]}. One row per tool.
"""
import json
import glob
import os
from collections import Counter, defaultdict

ORDER = {"fail": 0, "partial": 1, "not testable": 2, "pass": 3}
SEV = {"high": 0, "medium": 1, "low": 2, "": 3}
HEADERS = ["Group", "Tool", "Route", "Result", "Severity", "What was checked", "Notes", "Repro"]


def load():
    rows = []
    for path in sorted(glob.glob(os.path.join(os.path.dirname(__file__), "results", "*.json"))):
        with open(path) as fh:
            try:
                data = json.load(fh)
            except json.JSONDecodeError as e:
                print(f"# BAD JSON in {path}: {e}")
                continue
        slice_name = os.path.basename(path).replace(".json", "")
        for r in data:
            r["_slice"] = slice_name
            rows.append(r)
    return rows


def norm(r):
    res = (r.get("result") or "").strip().lower()
    if res in ("not_testable", "untested", "not tested"):
        res = "not testable"
    return res


def main():
    rows = load()
    rows.sort(key=lambda r: (ORDER.get(norm(r), 4), SEV.get((r.get("severity") or "").lower(), 3),
                             r.get("group", ""), r.get("tool", "")))
    out = [[r.get("group", ""), r.get("tool", ""), r.get("route", ""), norm(r),
            (r.get("severity") or "").lower(), r.get("checked", ""), r.get("notes", ""), r.get("repro", "")]
           for r in rows]
    counts = Counter(norm(r) for r in rows)
    sev = Counter((r.get("severity") or "").lower() for r in rows if norm(r) in ("fail", "partial"))
    by_group = defaultdict(Counter)
    for r in rows:
        by_group[r.get("group", "")][norm(r)] += 1
    group_rows = [["Group", "Pass", "Partial", "Fail", "Not testable", "Total"]]
    for g in sorted(by_group, key=lambda g: (-by_group[g]["fail"], -by_group[g]["partial"], g)):
        c = by_group[g]
        group_rows.append([g, c["pass"], c["partial"], c["fail"], c["not testable"], sum(c.values())])
    print(json.dumps({
        "headers": HEADERS,
        "rows": out,
        "summary": {"total": len(rows), **{k: counts.get(k, 0) for k in ("pass", "partial", "fail", "not testable")},
                    "high": sev.get("high", 0), "medium": sev.get("medium", 0), "low": sev.get("low", 0)},
        "by_group": group_rows,
    }, indent=None))


if __name__ == "__main__":
    main()
