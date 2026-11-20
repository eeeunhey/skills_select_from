"""Cross-task summary (PRD 4.4 stage 1, '시스템 검증'): per-task 우세/동률/열세 counts, no %p thresholds.

Uses the latest experiment per task with a human-approved rubric. Every run is n=1 per arm at this stage, so the
table is a system check, not a performance claim (4.5).
"""
import argparse
import sys

from lab import db

ARMS = ("NO_SKILL", "AUTO", "FORCED")


def latest_experiments(conn):
    rows = conn.execute(
        "SELECT e.task_id, e.experiment_id, max(e.started_at) AS t FROM eval_runs e"
        " JOIN rubrics r ON r.id = e.task_id AND r.version = e.rubric_version AND r.approved_at IS NOT NULL"
        " WHERE e.status NOT IN ('PLANNED') GROUP BY e.task_id, e.experiment_id ORDER BY e.task_id, t").fetchall()
    out = {}
    for r in rows:
        out[r["task_id"]] = r["experiment_id"]          # later experiment for the same task wins
    return out


def arm_stats(conn, exp):
    s = {}
    for arm in ARMS:
        rs = conn.execute("SELECT status, success, checks_passed, checks_total, cost_usd, trigger_observed, skill_id,"
                          " rubric_version FROM eval_runs WHERE experiment_id=? AND arm=? AND candidate_role IS NOT 'C'",
                          (exp, arm)).fetchall()
        ev = [r for r in rs if r["success"] is not None]
        s[arm] = {"n": len(ev), "ok": sum(r["success"] for r in ev), "status": sorted({r["status"] for r in rs}),
                  "score": (sum(r["checks_passed"] or 0 for r in ev) / sum(r["checks_total"] or 1 for r in ev)) if ev else None,
                  "cost": sum(r["cost_usd"] or 0 for r in rs) if rs else None,
                  "trigger": [r["trigger_observed"] for r in rs if arm == "AUTO"],
                  "skill": next((r["skill_id"] for r in rs if r["skill_id"]), None),
                  "rubric": next((r["rubric_version"] for r in rs), None)}
    return s


def compare(a, b):
    if a["score"] is None or b["score"] is None:
        return "미평가"
    return "우세" if a["score"] > b["score"] else "열세" if a["score"] < b["score"] else "동률"


def summarize(conn):
    rows, tally = [], {"AUTO": {}, "FORCED": {}}
    for task, exp in latest_experiments(conn).items():
        s = arm_stats(conn, exp)
        auto, forced = compare(s["AUTO"], s["NO_SKILL"]), compare(s["FORCED"], s["NO_SKILL"])
        tally["AUTO"][auto] = tally["AUTO"].get(auto, 0) + 1
        tally["FORCED"][forced] = tally["FORCED"].get(forced, 0) + 1
        trig = s["AUTO"]["trigger"]
        rows.append({"task": task, "exp": exp, "rubric": s["NO_SKILL"]["rubric"], "skill": s["AUTO"]["skill"], "s": s,
                     "auto": auto, "forced": forced,
                     "trigger": f"{sum(1 for x in trig if x)}/{len(trig)}" if trig else "-"})
    return rows, tally


def fmt(st):
    if st["score"] is None:
        return f"미평가 {st['status']}"
    return f"{st['ok']}/{st['n']} ({st['score']:.2f})"


def render(rows, tally):
    out = ["# 10-task system check (PRD 4.4 stage 1)", "",
           "> 팔당 n=1. 수치 기준 없이 과제별 우세/동률/열세 건수만 보고 (4.4). B = 선정기 차순위 스킬(선정기는 대부분 NO_SKILL 권장). "
           "비용은 Claude Code 추정치(프로필 S).", "",
           "| task | rubric | B skill | NO_SKILL | AUTO | FORCED | AUTO 트리거 | AUTO vs NO | FORCED vs NO | 비용 N/A/F ($) |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        s = r["s"]
        cost = "/".join(f"{s[a]['cost']:.4f}" if s[a]["cost"] is not None else "-" for a in ARMS)
        out.append(f"| {r['task']} | {r['rubric']} | {(r['skill'] or '-').split(':')[-1].replace('/SKILL.md', '')} | "
                   f"{fmt(s['NO_SKILL'])} | {fmt(s['AUTO'])} | {fmt(s['FORCED'])} | {r['trigger']} | {r['auto']} | {r['forced']} | {cost} |")
    out += ["", "| 비교 | " + " | ".join(("우세", "동률", "열세", "미평가")) + " |", "|---|---|---|---|---|"]
    for k in ("AUTO", "FORCED"):
        out.append(f"| {k} vs NO_SKILL | " + " | ".join(str(tally[k].get(x, 0)) for x in ("우세", "동률", "열세", "미평가")) + " |")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m lab.summary", description=__doc__)
    ap.add_argument("--db", default=str(db.DB_PATH))
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    text = render(*summarize(db.connect(a.db)))
    if a.out:
        open(a.out, "w", encoding="utf-8").write(text)
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
