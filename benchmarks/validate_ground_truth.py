"""Sanity check of the ground truth (rubric validation only; the evaluated agent never sees this).

For each case: grade an untouched BASE copy and a REFERENCE-solution copy with the external verifier.
Expected:
  BASE      functional -> fail (the defect exists); regression / S2 -> pass; S1 (log_policy), human -> unevaluated
  REFERENCE every functional/regression/S2 -> pass;                         S1, human -> unevaluated

Usage: python benchmarks/validate_ground_truth.py [T01 T03 ...]  -> prints matrix, writes benchmarks/ground-truth-matrix.json
Exit code 1 if any cell differs from expectation.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "react-demo"
RUN = FIXTURE / "verify" / "run.mjs"
REFS = ROOT / "benchmarks" / "reference-solutions"


def expected(req, variant):
    if req["verifier_kind"] in ("log_policy", "human"):
        return "unevaluated"
    if variant == "base" and req["type"] == "functional":
        return "fail"
    return "pass"


def grade(case, workdir, artifacts):
    out = artifacts / "result.json"
    subprocess.run(["node", str(RUN), "--case", case, "--workdir", str(workdir), "--artifacts", str(artifacts), "--json", str(out)],
                   check=True, capture_output=True, text=True, encoding="utf-8")
    return {r["id"]: r for r in json.loads(out.read_text(encoding="utf-8"))["requirements"]}


def main(cases):
    matrix, bad = {}, []
    with tempfile.TemporaryDirectory(prefix="gt-") as tmp:
        tmp = Path(tmp)
        for case in cases:
            rubric = yaml.safe_load((ROOT / "rubrics" / f"{case}.yaml").read_text(encoding="utf-8"))
            matrix[case] = {}
            for variant in ("base", "reference"):
                work = tmp / f"{case}-{variant}"
                subprocess.run(["node", str(RUN), "prepare", "--dest", str(work)], check=True, capture_output=True)
                if variant == "reference":
                    shutil.copytree(REFS / case / "files", work, dirs_exist_ok=True)
                res = grade(case, work, tmp / f"{case}-{variant}-art")
                for req in rubric["requirements"]:
                    got = res[req["id"]]["result"]
                    want = expected(req, variant)
                    matrix[case].setdefault(req["id"], {"type": req["type"]})[variant] = got
                    if got != want:
                        bad.append(f"{case} {variant} {req['id']}: got {got}, want {want}\n    {res[req['id']]['detail'][:400]}")
            row = "  ".join(f"{rid}[{v['type'][:4]}] base={v['base']}/ref={v['reference']}" for rid, v in matrix[case].items())
            print(f"{case}: {row}", flush=True)
    (ROOT / "benchmarks" / "ground-truth-matrix.json").write_text(json.dumps(matrix, indent=2), encoding="utf-8")
    print("\nMISMATCHES:\n" + "\n".join(bad) if bad else "\nall cells as expected")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or [f"T{i:02d}" for i in range(1, 11)]))
