"""Ground-truth rubrics (PRD 2.2, F0): validate, register, approve (human), freeze-check.

CLI:
  python -m lab.rubrics register rubrics/*.yaml --author <name> [--skill-names names.txt]
  python -m lab.rubrics approve T01 rubric-v1 --by <name>        # HUMAN action; the DB trigger then freezes the row
  python -m lab.rubrics status
  python -m lab.rubrics check rubrics/*.yaml                     # freeze_check every file
  python -m lab.rubrics fixture-hash [fixtures/react-demo]
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import yaml

from lab import db

ROOT = db.ROOT
PENDING = "PENDING_FREEZE"
REQ_TYPES = {"functional", "regression", "safety", "human_quality"}
VERIFIER_KINDS = {"external_test", "log_policy", "builtin_grader", "human"}  # = eval_checks.verifier vocabulary
REQUIRED_KEYS = ("case_id", "user_goal", "project_fixture", "environment", "requirements", "forbidden", "rubric_version")
TREE_EXCLUDE = {"node_modules", "dist", "test-results", "playwright-report", ".vite"}


class RubricError(ValueError):
    pass


def text_sha(text):
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def tree_hash(path):
    """Deterministic sha256 over (relative posix path, sha256(content with CRLF->LF)), node_modules etc. excluded."""
    path = Path(path)
    h = hashlib.sha256()
    entries = []
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in TREE_EXCLUDE]
        for f in filenames:
            p = Path(dirpath) / f
            entries.append((p.relative_to(path).as_posix(), hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()))
    for rel, digest in sorted(entries):
        h.update(f"{rel}\0{digest}\n".encode())
    return h.hexdigest()


def load(path):
    text = Path(path).read_text(encoding="utf-8")
    return yaml.safe_load(text), text


def known_verifiers(fixture=ROOT / "fixtures" / "react-demo"):
    """{test_id: kind} from the fixture's verifier registry."""
    reg = json.loads((Path(fixture) / "verify" / "cases.json").read_text(encoding="utf-8"))
    return {f"{case}-{rid}": kind for case, reqs in reg.items() if not case.startswith("_") for rid, kind in reqs.items()}


def _leaks(text, skill_names):
    """Skill names found in text. Hyphenated names match as whole tokens; single words only as /name or 'name skill'
    (plain words like 'access' or 'pdf' are too common to ban outright). The word 'skill' itself is always a leak."""
    found = []
    if re.search(r"\bskills?\b", text, re.I):
        found.append("skill")
    for name in skill_names:
        n = re.escape(name.strip().lower())
        if not n:
            continue
        if "-" in name or "_" in name:
            pat = rf"(?<![\w-]){n}(?![\w-])"
        else:
            pat = rf"(?:^|\s)/{n}(?![\w-])|(?<![\w-]){n}\s+skill"
        if re.search(pat, text, re.I):
            found.append(name)
    return found


def validate(doc, skill_names=(), prompt_text=None, verifiers=None):
    """Return a list of problems (empty = valid)."""
    if not isinstance(doc, dict):
        return ["rubric is not a mapping"]
    errs = [f"missing key: {k}" for k in REQUIRED_KEYS if k not in doc]
    reqs = doc.get("requirements") or []
    if not reqs:
        errs.append("no requirements")
    ids, tests = set(), set()
    for i, r in enumerate(reqs):
        rid = r.get("id")
        where = f"requirement {rid or '#' + str(i)}"
        if not rid:
            errs.append(f"{where}: missing id")
        elif rid in ids:
            errs.append(f"duplicate requirement id: {rid}")
        ids.add(rid)
        if r.get("type") not in REQ_TYPES:
            errs.append(f"{where}: type must be one of {sorted(REQ_TYPES)}")
        if not str(r.get("condition") or "").strip():
            errs.append(f"{where}: missing condition")
        v = r.get("verifier")
        if not v or not isinstance(v, str):
            errs.append(f"{where}: missing verifier (test id)")
        else:
            if v in tests:
                errs.append(f"{where}: verifier {v} used by more than one requirement")
            tests.add(v)
            if verifiers is not None and v not in verifiers:
                errs.append(f"{where}: verifier {v} not in verifier registry")
        if r.get("verifier_kind") not in VERIFIER_KINDS:
            errs.append(f"{where}: verifier_kind must be one of {sorted(VERIFIER_KINDS)}")
    if not any(r.get("type") == "safety" and r.get("mandatory") is True for r in reqs):
        errs.append("no mandatory safety requirement")
    if not isinstance(doc.get("approved_before_selection", False), bool):
        errs.append("approved_before_selection must be a boolean")
    graded = [str(doc.get("user_goal", ""))] + [str(r.get("condition", "")) for r in reqs] + [str(f) for f in doc.get("forbidden") or []]
    if prompt_text:
        graded.append(prompt_text)
    for leak in sorted({x for t in graded for x in _leaks(t, skill_names)}):
        errs.append(f"skill-name leakage: {leak!r} appears in goal/conditions/forbidden/prompt")
    return errs


def _prompt(doc):
    pf = doc.get("prompt_file")
    return (ROOT / pf).read_text(encoding="utf-8") if pf and (ROOT / pf).exists() else None


def check(doc, skill_names=()):
    errs = validate(doc, skill_names, _prompt(doc), known_verifiers())
    if doc.get("prompt_file") and _prompt(doc) is None:
        errs.append(f"prompt_file not found: {doc['prompt_file']}")
    if errs:
        raise RubricError(f"{doc.get('case_id', '?')}: " + "; ".join(errs))


def _fixture_sha(doc):
    fx = doc["project_fixture"]
    actual = tree_hash(ROOT / fx["path"])
    declared = fx.get("tree_sha256", PENDING)
    if declared not in (PENDING, actual):
        raise RubricError(f"{doc['case_id']}: fixture tree hash {actual} != declared {declared}")
    return actual


def register(conn, path, author, skill_names=()):
    """Insert/refresh a DRAFT rubric + its task row. Approved rubrics are never rewritten."""
    doc, text = load(path)
    check(doc, skill_names)
    cid, ver, sha = doc["case_id"], doc["rubric_version"], text_sha(text)
    row = conn.execute("SELECT sha256, approved_at FROM rubrics WHERE id=? AND version=?", (cid, ver)).fetchone()
    if row and row["approved_at"]:
        if row["sha256"] != sha:
            raise RubricError(f"{cid} {ver} is approved and frozen; file changed -> create a new rubric_version")
        return {"case_id": cid, "version": ver, "sha256": sha, "status": "already approved; unchanged"}
    fixture = _fixture_sha(doc)
    with db.tx(conn):
        if not row:
            conn.execute("INSERT INTO rubrics (id, version, yaml_text, sha256, author, created_at) VALUES (?,?,?,?,?,?)",
                         (cid, ver, text, sha, author, db.now()))
        elif row["sha256"] != sha:
            conn.execute("UPDATE rubrics SET yaml_text=?, sha256=?, author=?, created_at=? WHERE id=? AND version=?",
                         (text, sha, author, db.now(), cid, ver))
        conn.execute(
            "INSERT INTO tasks (id, prompt, context_json, environment_json, fixture_commit, rubric_id, rubric_version, split)"
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET prompt=excluded.prompt, context_json=excluded.context_json,"
            " environment_json=excluded.environment_json, fixture_commit=excluded.fixture_commit, rubric_id=excluded.rubric_id,"
            " rubric_version=excluded.rubric_version, split=excluded.split",
            (cid, _prompt(doc) or doc["user_goal"],
             json.dumps({"task_type": doc.get("task_type"), "stack": doc.get("stack", [])}),
             json.dumps(doc["environment"]), f"sha256:{fixture}", cid, ver, doc.get("split", "labeled")))
    return {"case_id": cid, "version": ver, "sha256": sha, "fixture_commit": f"sha256:{fixture}"}


def approve(conn, case_id, version, approver):
    """HUMAN action. After this the DB trigger rejects any UPDATE/DELETE of the row."""
    row = conn.execute("SELECT approved_at FROM rubrics WHERE id=? AND version=?", (case_id, version)).fetchone()
    if not row:
        raise RubricError(f"{case_id} {version} not registered")
    if row["approved_at"]:
        raise RubricError(f"{case_id} {version} already approved")
    task = conn.execute("SELECT fixture_commit FROM tasks WHERE id=? AND rubric_version=?", (case_id, version)).fetchone()
    doc = yaml.safe_load(conn.execute("SELECT yaml_text FROM rubrics WHERE id=? AND version=?", (case_id, version)).fetchone()[0])
    if task and task["fixture_commit"] != f"sha256:{tree_hash(ROOT / doc['project_fixture']['path'])}":
        raise RubricError(f"{case_id}: fixture changed since registration; re-register before approving")
    conn.execute("UPDATE rubrics SET approved_by=?, approved_at=? WHERE id=? AND version=?", (approver, db.now(), case_id, version))


def freeze_check(conn, case_id, version, path):
    """'draft' | 'frozen-ok'; raises RubricError if an approved rubric's file (or fixture) changed."""
    row = conn.execute("SELECT sha256, approved_at, yaml_text FROM rubrics WHERE id=? AND version=?", (case_id, version)).fetchone()
    if not row:
        raise RubricError(f"{case_id} {version} not registered")
    if not row["approved_at"]:
        return "draft"
    if text_sha(Path(path).read_text(encoding="utf-8")) != row["sha256"]:
        raise RubricError(f"{case_id} {version}: file changed after approval -> create a new rubric_version")
    task = conn.execute("SELECT fixture_commit FROM tasks WHERE id=?", (case_id,)).fetchone()
    fx = yaml.safe_load(row["yaml_text"])["project_fixture"]["path"]
    if task and task["fixture_commit"] != f"sha256:{tree_hash(ROOT / fx)}":
        raise RubricError(f"{case_id} {version}: fixture tree changed after approval -> create a new rubric_version")
    return "frozen-ok"


def status(conn):
    return [dict(r) for r in conn.execute(
        "SELECT r.id, r.version, substr(r.sha256,1,12) AS sha, r.author, r.approved_by, r.approved_at, t.split, t.fixture_commit"
        " FROM rubrics r LEFT JOIN tasks t ON t.id = r.id ORDER BY r.id, r.version")]


def _skill_names(conn, extra_file):
    names = {r[0] for r in conn.execute("SELECT DISTINCT name FROM skills")}
    if extra_file:
        names |= {l.strip() for l in Path(extra_file).read_text(encoding="utf-8").splitlines() if l.strip()}
    return sorted(names)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m lab.rubrics")
    ap.add_argument("--db", default=str(db.DB_PATH))
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register"); r.add_argument("paths", nargs="+"); r.add_argument("--author", required=True); r.add_argument("--skill-names")
    a = sub.add_parser("approve"); a.add_argument("case_id"); a.add_argument("version"); a.add_argument("--by", required=True)
    sub.add_parser("status")
    c = sub.add_parser("check"); c.add_argument("paths", nargs="+")
    f = sub.add_parser("fixture-hash"); f.add_argument("path", nargs="?", default=str(ROOT / "fixtures" / "react-demo"))
    args = ap.parse_args(argv)
    if args.cmd == "fixture-hash":
        print(tree_hash(args.path))
        return 0
    conn = db.connect(args.db)
    rc = 0
    try:
        if args.cmd == "register":
            names = _skill_names(conn, args.skill_names)
            for p in args.paths:
                try:
                    print(json.dumps(register(conn, p, args.author, names)))
                except RubricError as e:
                    print(f"ERROR {p}: {e}", file=sys.stderr)
                    rc = 1
        elif args.cmd == "approve":
            approve(conn, args.case_id, args.version, args.by)
            print(f"approved {args.case_id} {args.version} by {args.by} (frozen)")
        elif args.cmd == "status":
            for row in status(conn):
                print(f"{row['id']} {row['version']} sha={row['sha']} split={row['split']} "
                      f"{'APPROVED by ' + row['approved_by'] + ' at ' + row['approved_at'] if row['approved_at'] else 'PENDING human approval'}")
        elif args.cmd == "check":
            for p in args.paths:
                doc, _ = load(p)
                try:
                    print(f"{doc['case_id']}: {freeze_check(conn, doc['case_id'], doc['rubric_version'], p)}")
                except RubricError as e:
                    print(f"ERROR {e}", file=sys.stderr)
                    rc = 1
    except RubricError as e:
        print(f"ERROR {e}", file=sys.stderr)
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
