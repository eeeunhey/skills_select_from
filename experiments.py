"""Skill Selection Lab - A/B/C experiment log and report (spec section 05).

Records runs the user (or a later runner) produced; never runs agents or skill scripts itself.
Arms: A = no skill, B = selected top-1 skill, C = runner-up skill.
"""
import argparse
import csv
import datetime
import html
import json
import statistics
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
CSV_PATH = DATA / "experiments.csv"
PLANS_PATH = DATA / "experiment_plans.jsonl"
DECISIONS_PATH = DATA / "decisions.jsonl"
SKILLS_PATH = DATA / "skills.json"

COLUMNS = ["experiment_id", "date", "task_type", "task", "arm", "skill_id", "model", "run",
           "passed", "total", "errors", "cost_usd", "latency_s", "quality", "failure_code", "notes"]
TASK_TYPES = {"app_ui", "button_flow", "code_review"}
FAILURE_CODES = {
    "R1": "검색 누락 (good skill not found as candidate)",
    "R2": "잘못된 순위 (better skill ranked lower)",
    "R3": "환경 불일치",
    "R4": "과잉 사용 (no-skill equal or better)",
    "R5": "지침 미준수 (selected but not read/followed)",
    "R6": "조합 충돌",
}


def validate(row):
    """Return list of error strings for one row (dict of str)."""
    errs = []
    arm, sid = row.get("arm", ""), row.get("skill_id", "").strip()
    if arm not in ("A", "B", "C"):
        errs.append(f"arm must be A/B/C, got {arm!r}")
    elif arm == "A" and sid:
        errs.append("arm A (no skill) must have empty skill_id")
    elif arm in ("B", "C") and not sid:
        errs.append(f"arm {arm} needs a skill_id")
    if row.get("task_type") not in TASK_TYPES:
        errs.append(f"task_type must be one of {sorted(TASK_TYPES)}")
    if not row.get("experiment_id"):
        errs.append("experiment_id required")
    try:
        passed, total = int(row.get("passed", "")), int(row.get("total", ""))
        if total <= 0 or passed < 0 or passed > total:
            errs.append(f"need 0 <= passed <= total and total > 0 (got {passed}/{total})")
    except ValueError:
        errs.append("passed/total must be integers")
    fc = row.get("failure_code", "")
    if fc and fc not in FAILURE_CODES:
        errs.append(f"failure_code must be empty or R1..R6, got {fc!r}")
    q = row.get("quality", "")
    if q and q not in {"1", "2", "3", "4", "5"}:
        errs.append(f"quality must be empty or 1-5, got {q!r}")
    for k in ("errors", "cost_usd", "latency_s", "run"):
        if row.get(k):
            try:
                float(row[k])
            except ValueError:
                errs.append(f"{k} must be a number")
    return errs


def append_row(row, path=CSV_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in COLUMNS})


def load_rows(path=CSV_PATH):
    path = Path(path)
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def known_skills():
    try:
        return json.loads(SKILLS_PATH.read_text(encoding="utf-8")).get("skills", {})
    except (OSError, ValueError):
        return {}


def _mean(xs):
    return statistics.mean(xs) if xs else None


def _num(rows, key):
    return [float(r[key]) for r in rows if r.get(key, "") != ""]


def summarize(rows):
    """Aggregate rows -> {"experiments": {...}, "by_failure": {...}, "by_task_type": {...}, "by_skill": {...}}."""
    groups = {}
    for r in rows:
        groups.setdefault(r["experiment_id"], []).append(r)

    experiments, by_failure, by_task_type, skill_deltas = {}, {}, {}, {}
    for eid, ers in groups.items():
        arms = {}
        for arm in ("A", "B", "C"):
            ar = [r for r in ers if r["arm"] == arm]
            if not ar:
                continue
            rates = [int(r["passed"]) / int(r["total"]) for r in ar]
            arms[arm] = {
                "n": len(ar),
                "skill_id": ar[0]["skill_id"],
                "pass_rate": _mean(rates),
                # stdev needs >=2 runs; single run -> None (shown as "-")
                "pass_stdev": statistics.stdev(rates) if len(rates) > 1 else None,
                "errors": _mean(_num(ar, "errors")),
                "cost_usd": _mean(_num(ar, "cost_usd")),
                "latency_s": _mean(_num(ar, "latency_s")),
                "quality": _mean(_num(ar, "quality")),  # human subjective, kept apart from pass rate
            }
        recorded = sorted({r["failure_code"] for r in ers if r.get("failure_code")})
        suggested = []
        delta_ba = delta_bc = None
        if "A" in arms and "B" in arms:
            delta_ba = arms["B"]["pass_rate"] - arms["A"]["pass_rate"]
            if delta_ba <= 0:
                suggested.append("R4")
            skill_deltas.setdefault(arms["B"]["skill_id"], []).append(delta_ba)
        if "B" in arms and "C" in arms:
            delta_bc = arms["B"]["pass_rate"] - arms["C"]["pass_rate"]
            if delta_bc < 0:
                suggested.append("R2")
        task_type = ers[0]["task_type"]
        experiments[eid] = {"task_type": task_type, "task": ers[0]["task"], "model": ers[0]["model"],
                            "arms": arms, "delta_b_vs_a": delta_ba, "delta_b_vs_c": delta_bc,
                            "recorded_codes": recorded, "suggested_codes": suggested}
        for code in recorded:
            by_failure.setdefault(code, {"recorded": 0, "suggested": 0})["recorded"] += 1
        for code in suggested:
            by_failure.setdefault(code, {"recorded": 0, "suggested": 0})["suggested"] += 1
        t = by_task_type.setdefault(task_type, {"experiments": 0, "deltas": []})
        t["experiments"] += 1
        if delta_ba is not None:
            t["deltas"].append(delta_ba)

    by_skill = {s: {"experiments": len(d), "mean_delta_vs_a": _mean(d)} for s, d in skill_deltas.items()}
    for t in by_task_type.values():
        t["mean_delta_vs_a"] = _mean(t.pop("deltas"))
    return {"experiments": experiments, "by_failure": dict(sorted(by_failure.items())),
            "by_task_type": by_task_type, "by_skill": by_skill}


def _f(x, fmt="{:.2f}"):
    return "-" if x is None else fmt.format(x)


def _pct(x):
    return "-" if x is None else f"{x * 100:.0f}%"


def _sign(x):
    return "-" if x is None else f"{x * 100:+.0f}pp"


def _tables(s):
    """Yield (title, headers, rows) tables shared by md and html renderers."""
    arm_rows, verdict_rows = [], []
    for eid, e in s["experiments"].items():
        for arm, a in e["arms"].items():
            arm_rows.append([eid, arm, a["skill_id"] or "(none)", a["n"], _pct(a["pass_rate"]),
                             _pct(a["pass_stdev"]), _f(a["errors"]), _f(a["cost_usd"], "{:.4f}"),
                             _f(a["latency_s"], "{:.1f}"), _f(a["quality"])])
        b_better_c = "-" if e["delta_b_vs_c"] is None else ("yes" if e["delta_b_vs_c"] > 0 else "no")
        verdict_rows.append([eid, e["task_type"], e["model"], _sign(e["delta_b_vs_a"]), b_better_c,
                             ", ".join(e["recorded_codes"]) or "-",
                             ", ".join(e["suggested_codes"]) or "-"])
    yield ("Per experiment & arm", ["experiment", "arm", "skill", "n", "pass rate", "stdev",
                                    "errors", "cost $", "latency s", "quality (human)"], arm_rows)
    yield ("Verdicts", ["experiment", "task type", "model", "B vs A", "top-1 > runner-up",
                        "recorded codes", "suggested codes (auto)"], verdict_rows)
    yield ("Failure codes", ["code", "meaning", "recorded", "suggested"],
           [[c, FAILURE_CODES[c], v["recorded"], v["suggested"]] for c, v in s["by_failure"].items()])
    yield ("By task type", ["task type", "experiments", "mean B vs A"],
           [[t, v["experiments"], _sign(v["mean_delta_vs_a"])] for t, v in s["by_task_type"].items()])
    yield ("Per-skill evidence (arm B)", ["skill_id", "experiments", "mean delta vs A"],
           [[k, v["experiments"], _sign(v["mean_delta_vs_a"])] for k, v in s["by_skill"].items()])


NOTE = ("Pass rate comes from automated tests with ground truth; quality is a separate human rating. "
        "Suggested codes are inferred from numbers only (B<=A -> R4, C>B -> R2) and are not human-confirmed.")


def render_md(s):
    out = ["# Skill experiments report", "", f"_{NOTE}_", ""]
    for title, headers, rows in _tables(s):
        out += [f"## {title}", ""]
        if not rows:
            out += ["_(no data)_", ""]
            continue
        out.append("| " + " | ".join(headers) + " |")
        out.append("|" + "---|" * len(headers))
        out += ["| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |" for r in rows]
        out.append("")
    return "\n".join(out)


CSS = """
:root{--bg:#fff;--fg:#1b1b1b;--muted:#666;--line:#ddd;--a:#999;--b:#2f6fdb;--c:#d08a1e}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#e8e8e8;--muted:#999;--line:#333;--a:#777;--b:#6fa0ff;--c:#f0b050}}
body{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:16px}
table{border-collapse:collapse;width:100%;margin:8px 0 24px;display:block;overflow-x:auto}
th,td{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left;white-space:nowrap}
th{color:var(--muted);font-weight:600}.note{color:var(--muted)}
.bar{display:inline-block;height:10px;vertical-align:middle;margin-right:6px;border-radius:2px}
.A{background:var(--a)}.B{background:var(--b)}.C{background:var(--c)}
"""


def render_html(s):
    e = html.escape
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
             f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Skill Experiments</title><style>{CSS}</style></head><body>"
             f"<h1>Skill experiments</h1><p class='note'>{e(NOTE)}</p>"]
    bars = []
    for eid, ex in s["experiments"].items():
        bars.append(f"<h3>{e(eid)} <span class='note'>{e(ex['task_type'])}: {e(ex['task'])}</span></h3>")
        for arm, a in ex["arms"].items():
            w = round((a["pass_rate"] or 0) * 300)
            bars.append(f"<div>{arm} <span class='bar {arm}' style='width:{w}px'></span>"
                        f"{_pct(a['pass_rate'])} &plusmn;{_pct(a['pass_stdev'])} (n={a['n']})</div>")
    parts.append("<h2>Pass rate by arm</h2>" + ("".join(bars) or "<p class='note'>(no data)</p>"))
    for title, headers, rows in _tables(s):
        parts.append(f"<h2>{e(title)}</h2>")
        if not rows:
            parts.append("<p class='note'>(no data)</p>")
            continue
        parts.append("<table><tr>" + "".join(f"<th>{e(h)}</th>" for h in headers) + "</tr>")
        parts += ["<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>" for r in rows]
        parts.append("</table>")
    parts.append("</body></html>")
    return "".join(parts)


def cmd_add(a):
    row = {k: (getattr(a, k, None) or "") for k in COLUMNS}
    row["date"] = a.date or datetime.date.today().isoformat()
    row["run"] = str(a.run)
    errs = validate(row)
    if errs:
        sys.exit("invalid row:\n  " + "\n  ".join(errs))
    if row["skill_id"] and row["skill_id"] not in known_skills():
        print(f"warning: skill_id not in {SKILLS_PATH.name}: {row['skill_id']}", file=sys.stderr)
    append_row(row, a.csv)
    print(f"appended {row['experiment_id']} arm {row['arm']} run {row['run']} -> {a.csv}")


def cmd_plan(a):
    b, c, query = a.b, a.c, None
    if a.from_decision is not None:
        lines = DECISIONS_PATH.read_text(encoding="utf-8").splitlines() if DECISIONS_PATH.exists() else []
        if not 0 <= a.from_decision < len(lines):
            sys.exit(f"decision line {a.from_decision} not found in {DECISIONS_PATH}")
        d = json.loads(lines[a.from_decision])
        query = d.get("query")
        ranked = [x if isinstance(x, str) else x.get("skill_id") or x.get("id") for x in d.get("ranked") or []]
        b = b or (ranked[0] if ranked else None)
        c = c or (ranked[1] if len(ranked) > 1 else None)
    if not b:
        sys.exit("need --b <skill_id> (decision had no 'ranked' list)")
    skills = known_skills()
    for sid in filter(None, (b, c)):
        if sid not in skills:
            print(f"warning: skill_id not in {SKILLS_PATH.name}: {sid}", file=sys.stderr)
    eid = f"exp-{datetime.date.today():%Y%m%d}-{uuid.uuid4().hex[:6]}"
    arms = {"A": None, "B": b}
    if c:
        arms["C"] = c
    plan = {"experiment_id": eid, "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "task_type": a.task_type, "task": a.task, "query": query, "model": a.model,
            "runs_per_arm": a.runs, "arms": arms,
            "keep_identical": ["model: " + a.model, "task prompt / goal text (verbatim)",
                               "input data & repo snapshot", "tools available", "environment (OS, versions)",
                               f"time limit: {a.time_limit}", "test suite with ground truth"],
            "only_difference": "skill loaded (A none, B top-1, C runner-up)",
            "record_with": f"python experiments.py add --experiment-id {eid} --task-type {a.task_type} "
                           f"--task \"...\" --arm B --skill-id ... --model {a.model} --run 1 "
                           f"--passed N --total M"}
    PLANS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PLANS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(plan, ensure_ascii=False) + "\n")
    print(json.dumps(plan, ensure_ascii=False, indent=2))


def cmd_report(a):
    s = summarize(load_rows(a.csv))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    md = render_md(s)
    out.write_text(md, encoding="utf-8")
    out.with_suffix(".html").write_text(render_html(s), encoding="utf-8")
    print(md)
    print(f"\nwrote {out} and {out.with_suffix('.html')}", file=sys.stderr)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    ad = sub.add_parser("add", help="append one run row")
    ad.add_argument("--csv", default=str(CSV_PATH))
    ad.add_argument("--experiment-id", required=True)
    ad.add_argument("--date")
    ad.add_argument("--task-type", required=True, choices=sorted(TASK_TYPES))
    ad.add_argument("--task", required=True)
    ad.add_argument("--arm", required=True)
    ad.add_argument("--skill-id", default="")
    ad.add_argument("--model", required=True)
    ad.add_argument("--run", type=int, default=1)
    ad.add_argument("--passed", required=True)
    ad.add_argument("--total", required=True)
    for k in ("errors", "cost-usd", "latency-s", "quality", "failure-code", "notes"):
        ad.add_argument(f"--{k}", default="")
    ad.set_defaults(fn=cmd_add)

    pl = sub.add_parser("plan", help="write an A/B/C experiment plan")
    pl.add_argument("--task-type", required=True, choices=sorted(TASK_TYPES))
    pl.add_argument("--task", required=True)
    pl.add_argument("--b")
    pl.add_argument("--c")
    pl.add_argument("--model", required=True)
    pl.add_argument("--runs", type=int, default=3)
    pl.add_argument("--time-limit", default="30m")
    pl.add_argument("--from-decision", type=int, help="decision # printed by recommend.py --save (0-based line)")
    pl.set_defaults(fn=cmd_plan)

    rp = sub.add_parser("report", help="write markdown + html report")
    rp.add_argument("--csv", default=str(CSV_PATH))
    rp.add_argument("--out", default=str(DATA / "reports" / "experiments.md"))
    rp.set_defaults(fn=cmd_report)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
