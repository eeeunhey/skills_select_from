"""Selection Audit (PRD F6, 4.1-4.2, M4): score selector policies against labeled/holdout tasks.

Labels are drafts (label_status=draft-unreviewed) -> every number here is an internal preliminary result.
Tune policies on the labeled split only; look at holdout to compare, never to tune (4.5).
"""
import argparse
import json
import sys
from pathlib import Path

from lab import db, finder, selector

ROOT = Path(__file__).resolve().parents[1]
SPLITS = {"labeled": ROOT / "benchmarks/labeled-tasks.jsonl", "holdout": ROOT / "benchmarks/holdout-tasks.jsonl"}
NO = selector.NO_SKILL


def load(split):
    return [json.loads(line) for line in SPLITS[split].read_text(encoding="utf-8").splitlines() if line.strip()]


def sid(s):
    return f"github:{s['repo']}:{s['path']}"


def audit_task(conn, t, policy, k=20):
    task = finder.analyze(t["query"], stack=t.get("stack"), tools=t.get("tools"), **policy.get("analyzer", {}))
    rel = {sid(s) for s in t.get("relevant_skills", [])}
    neg = {sid(s) for s in t.get("hard_negatives", [])}
    found = [h["id"] for h in finder.find(conn, task, top_k=k)["hits"]]
    # full detailed ranking (not just the 3 shown, which include NO_SKILL) so "top-3" really means 3 skills
    res = selector.recommend(conn, task, policy={**policy, "max_output": policy["max_detailed"] + 1},
                             rubric_approved=True)  # audit context, not an experiment
    ranked = [r["skill_id"] for r in res["ranked"]]
    skills3 = [r for r in ranked if r != NO][:3]
    top1 = ranked[0]
    no_ok = t.get("acceptable_no_skill", False)
    gated = {e["skill_id"]: e for e in res["excluded"] if e["skill_id"] in rel and e["status"] != "제외"}
    correct = (top1 in rel) or (top1 == NO and (no_ok or not rel))
    fail = None
    if not correct:
        if top1 in neg:
            fail = ("ENVIRONMENT", f"hard negative {top1} ranked 1st (environment mismatch not caught)")
        elif rel and not rel & set(found):
            fail = ("RETRIEVAL", f"none of {len(rel)} relevant skills in top-{k} search results")
        elif rel and set(gated) == rel & set(found):
            fail = ("ENVIRONMENT", "relevant skills retrieved but all blocked by gates: " +
                    "; ".join(f"{g['name']}={g['status']}" for g in gated.values()))
        elif not rel:
            fail = ("OVERCONFIDENT", f"no skill needed, but recommended {top1} ({res['verdict']})")
        else:
            fail = ("RANKING", f"relevant skill retrieved but rank 1 = {top1}")
    return {"task_id": t["task_id"], "gt_ref": t.get("gt_ref"), "query": t["query"], "top1": top1,
            "verdict": res["verdict"], "correct": correct,
            "recall_at_k": (len(rel & set(found)) / len(rel)) if rel else None,
            "precision_at_3": ((len(rel & set(skills3)) / len(skills3)) if skills3 else 0.0) if rel else None,
            "top3_hit": bool(rel & set(skills3)) if rel else None,
            "no_skill_case": not rel, "failure": fail}


def summarize(rows):
    def mean(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 3) if xs else None
    no_cases = [r for r in rows if r["no_skill_case"]]
    return {"n": len(rows), "top1_accuracy": mean([float(r["correct"]) for r in rows]),
            "recall_at_20": mean([r["recall_at_k"] for r in rows]),
            "precision_at_3": mean([r["precision_at_3"] for r in rows]),
            "top3_inclusion": mean([None if r["top3_hit"] is None else float(r["top3_hit"]) for r in rows]),
            "no_skill_justified": mean([float(r["top1"] == NO) for r in no_cases]),
            "failures": {c: sum(1 for r in rows if r["failure"] and r["failure"][0] == c)
                         for c in ("RETRIEVAL", "RANKING", "OVERCONFIDENT", "ENVIRONMENT")}}


def run(conn, policy, split):
    rows = [audit_task(conn, t, policy) for t in load(split)]
    return {"policy": policy["policy_version"], "split": split, "summary": summarize(rows), "rows": rows}


def record(conn, result):
    """Write auto-suggested failure_cases (origin=auto: a hypothesis, not a confirmed cause)."""
    with db.tx(conn):
        for r in result["rows"]:
            if r["failure"]:
                conn.execute("INSERT INTO failure_cases (task_id, category, explanation, proposed_fix, origin, created_at)"
                             " VALUES (?,?,?,?, 'auto', ?)",
                             (r["task_id"], r["failure"][0], f"[{result['policy']}/{result['split']}] {r['failure'][1]}",
                              None, db.now()))


def render(results):
    out = ["# Selection audit", "",
           "> 라벨은 draft-unreviewed. 내부 예비 실험이며 일반화 결론 아님 (PRD 4.5). 정책 조정은 labeled로만, holdout은 비교용.", ""]
    keys = ["n", "top1_accuracy", "top3_inclusion", "recall_at_20", "precision_at_3", "no_skill_justified"]
    out += ["| policy | split | " + " | ".join(keys) + " | failures |", "|" + "---|" * (len(keys) + 3)]
    for r in results:
        s = r["summary"]
        out.append(f"| {r['policy']} | {r['split']} | " + " | ".join(str(s[k]) for k in keys) +
                   " | " + ", ".join(f"{k}:{v}" for k, v in s["failures"].items() if v) + " |")
    for r in results:
        out += ["", f"## {r['policy']} / {r['split']}", "", "| task | GT | top1 | ok | failure |", "|---|---|---|---|---|"]
        for x in r["rows"]:
            f = f"{x['failure'][0]}: {x['failure'][1]}" if x["failure"] else ""
            out.append(f"| {x['task_id']} | {x['gt_ref']} | {x['top1'].split(':')[-1].replace('/SKILL.md', '')} | "
                       f"{'O' if x['correct'] else 'X'} | {f} |")
    return "\n".join(out) + "\n"


POLICIES = {p["policy_version"]: p for p in (selector.POLICY_V1, selector.POLICY_V2)}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m lab.audit", description=__doc__)
    ap.add_argument("--db", default=str(db.DB_PATH))
    ap.add_argument("--policy", action="append", choices=sorted(POLICIES), help="repeat to compare (default: all)")
    ap.add_argument("--split", action="append", choices=sorted(SPLITS), help="default: labeled + holdout")
    ap.add_argument("--record", action="store_true", help="write failures of the LAST policy to failure_cases")
    ap.add_argument("--out", help="markdown report path")
    a = ap.parse_args(argv)
    conn = db.connect(a.db)
    results = [run(conn, POLICIES[p], s) for p in (a.policy or sorted(POLICIES)) for s in (a.split or ["labeled", "holdout"])]
    text = render(results)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8")
    if a.record:
        for r in results:
            if r["policy"] == results[-1]["policy"]:
                record(conn, r)
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
