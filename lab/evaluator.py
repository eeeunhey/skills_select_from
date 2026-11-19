"""F5 Independent Evaluator (PRD F5 v1.3, 4.3, 4.4, 7.3, 8.3).

plan -> execute -> report. Arms: NO_SKILL / AUTO (candidate installed, same prompt) / FORCED (`/<plugin>:<skill>`).
Outcome evidence = the external verifier run outside the agent; agent-reported tests never count.
Experiment state lives in eval_runs rows (status PLANNED until executed); run order is seeded + recorded.
"""
import argparse
import hashlib
import json
import random
import re
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from lab import claude_run, ledger
from lab.db import ROOT, connect, now

ARMS = ("NO_SKILL", "AUTO", "FORCED")
ARTIFACTS = ROOT / "data" / "artifacts"
CANDIDATES = ROOT / "candidates"
CASES = ROOT / "data" / "cases"
VERIFIER = "fixtures/{fixture}/verify/run.mjs"
DEFAULT_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Skill"]  # identical for every arm (8.3)
DONE = ("PASSED", "FAILED", "INVALID", "QUARANTINED", "BLOCKED")
STOP_STATES = ("WAITING_QUOTA_RESET", "WAITING_RATE_LIMIT", "WAITING_BUDGET_APPROVAL", "FAILED_PERMANENT")
SEC_RANK = {"quarantined": 3, "flagged": 2, "unreviewed": 1}
RETRY_STATES = ("WAITING_RATE_LIMIT", "WAITING_QUOTA_RESET")
MAX_RETRIES = 2
COST_FLAG_PCT = 50  # 4.4 hypothesis value, not a standard


class Refused(Exception):
    pass


def load_cfg():
    return yaml.safe_load((ROOT / "config" / "runtime.yaml").read_text(encoding="utf-8"))


def check_model_id(m):
    """8.3: full model id only. 'sonnet', 'opus', 'haiku', 'claude-sonnet' (no version) are aliases."""
    if not m or not re.fullmatch(r"claude-[a-z]+(-[a-z0-9.]+)*-\d[\w.\-]*", m) or "[" in m:
        raise Refused(f"model id must be a full id (e.g. claude-sonnet-5-5), got alias/empty {m!r}")
    return m


def slug(label, key, n=40):
    """plugin name: readable label + hash of the full skill id (unique, kebab-case)."""
    h = hashlib.sha1(key.encode()).hexdigest()[:6]
    return (re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:n].strip("-") or "skill") + "-" + h


def dir_hash(d):
    h = hashlib.sha256()
    for f in sorted(p for p in Path(d).rglob("*") if p.is_file()):
        h.update(f.relative_to(d).as_posix().encode() + b"\0" + f.read_bytes())
    return "sha256:" + h.hexdigest()


def _notes(row):
    try:
        return json.loads(row["notes"] or "{}")
    except ValueError:
        return {"text": row["notes"]}


# ---- candidates ---------------------------------------------------------------------------------
def frozen_case(conn, task_id):
    """data/cases/<task>@<rubric_version>/<task>/ : prompt.md + graders + case.yaml, same bytes for all candidates."""
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    env = json.loads(t["environment_json"] or "{}")
    d = CASES / f"{task_id}@{t['rubric_version']}" / task_id
    if not d.exists():
        (d / "graders").mkdir(parents=True)
        tools = env.get("allowed_tools", DEFAULT_TOOLS)
        cfg = load_cfg()["claude"]
        (d / "prompt.md").write_text(
            f"---\nmax_turns: {cfg['case_max_turns']}\ntimeout_seconds: {cfg['case_timeout_seconds']}\n"
            f"allowed_tools: [{', '.join(tools)}]\n---\n\n{t['prompt']}\n", encoding="utf-8", newline="\n")
        # ponytail: placeholder grader (a case needs one); the outcome is the external verifier, not this.
        (d / "graders" / "responded.md").write_text("---\ntype: regex\npattern: \".\"\n---\n", encoding="utf-8",
                                                    newline="\n")
        fx = (ROOT / "fixtures" / env.get("fixture", "react-demo")).as_posix()
        (d / "scaffold.sh").write_text(f"tar -C '{fx}' --exclude=node_modules --exclude=.git -cf - . | tar -xf -\n",
                                       encoding="utf-8", newline="\n")
        (d / "case.yaml").write_text(f'schema_version: "1.1"\nname: {task_id}\ncontext:\n  scaffold_script: scaffold.sh\n',
                                     encoding="utf-8", newline="\n")
    return d


def _case_prompt(path):
    """Task prompt body of a frozen prompt.md (after the front matter)."""
    return Path(path).read_text(encoding="utf-8").split("\n---\n", 1)[1].strip("\n")


def wrap_candidate(skill_md_text, name, out_dir, security_status=None, case_dir=None):
    """Minimal plugin wrapper: .claude-plugin/plugin.json + skills/<name>/SKILL.md (+ evals/<case>).
    Quarantined candidates (catalog status or a fresh static scan) are refused - never wrapped, never run."""
    try:
        from lab.scan import scan
        status = scan(skill_md_text)["security_status"]
    except ImportError:
        status = None
    if "quarantined" in (security_status, status):
        raise Refused(f"candidate {name} is quarantined (catalog={security_status}, scan={status})")
    out = Path(out_dir)
    plugin = out.name
    name = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "skill"  # non-ASCII name -> "" otherwise
    (out / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (out / ".claude-plugin" / "plugin.json").write_text(json.dumps(
        {"name": plugin, "version": "0.0.0", "description": "Skill Selection Lab candidate wrapper"}, indent=1),
        encoding="utf-8")
    (out / "skills" / name).mkdir(parents=True, exist_ok=True)
    (out / "skills" / name / "SKILL.md").write_bytes(skill_md_text.encode("utf-8"))  # bytes untouched
    case_hash = None
    if case_dir:
        dst = out / "evals" / Path(case_dir).name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(case_dir, dst)
        case_hash = dir_hash(dst)
    return {"plugin_dir": str(out), "plugin": plugin, "skill": name, "case_hash": case_hash,
            "skill_sha256": hashlib.sha256(skill_md_text.encode("utf-8")).hexdigest()}


def load_skill(conn, spec):
    """spec = catalog skill id, or file:<path> (manual candidate, no catalog version).
    -> dict(skill_id, version_id, text, security_status, name)."""
    if spec.startswith("file:"):
        p = Path(spec[5:])
        return {"skill_id": spec, "version_id": None, "text": p.read_text(encoding="utf-8"),
                "security_status": None, "name": p.parent.name}
    s = conn.execute("SELECT * FROM skills WHERE id=?", (spec,)).fetchone()
    if not s:
        raise Refused(f"unknown skill {spec}")
    v = conn.execute("SELECT * FROM skill_versions WHERE id=?", (s["current_version_id"],)).fetchone()
    text = None
    if v and s["security_status"] != "quarantined":
        from lab.collector import github_raw  # body is not stored in the DB; fetch the pinned commit
        text = github_raw(s["repo"], v["commit_sha"], s["path"])
        if "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest() != v["content_hash"] and \
                hashlib.sha256(text.encode("utf-8")).hexdigest() != v["content_hash"]:
            raise Refused(f"{spec}: fetched SKILL.md does not match catalog content_hash")
    return {"skill_id": spec, "version_id": s["current_version_id"], "text": text,
            "security_status": s["security_status"], "name": s["name"]}


# ---- plan ---------------------------------------------------------------------------------------
def plan(conn, task_id, b_skill, c_skill=None, runs=3, arms=ARMS, model=None, judge_model=None, seed=None,
         claude_code_version=None, auth_profile=None, engine="claude_p", operator_reviewed=False):
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not t:
        raise Refused(f"unknown task {task_id}")
    r = conn.execute("SELECT * FROM rubrics WHERE id=? AND version=?", (t["rubric_id"], t["rubric_version"])).fetchone()
    if not r or not r["approved_at"]:
        raise Refused(f"rubric {t['rubric_id']}/{t['rubric_version']} not approved: freeze the rubric before planning")
    cfg = load_cfg()["claude"]
    model = check_model_id(model or cfg["task_model"])
    judge_model = check_model_id(judge_model or cfg["judge_model"])
    ver = claude_code_version or claude_run.version()
    if not ver:
        raise Refused("claude --version unavailable; runs without a recorded version are not comparable")
    auth = auth_profile or cfg["auth_profile"]
    seed = secrets.randbelow(2**31) if seed is None else seed
    exp = f"exp-{task_id}-{now()[:19].replace(':', '').replace('-', '')}-{secrets.token_hex(2)}"
    case_dir = frozen_case(conn, task_id)
    case_prompt = case_dir / "prompt.md"
    prompt_sha = hashlib.sha256(_case_prompt(case_prompt).encode("utf-8")).hexdigest()

    cands = []
    for role, spec in (("B", b_skill), ("C", c_skill)):
        if not spec:
            continue
        sk = load_skill(conn, spec)
        status, info = "PLANNED", {}
        if sk["text"] is None:  # quarantined catalog skills are never fetched
            if sk["security_status"] != "quarantined":
                raise Refused(f"{spec}: no SKILL.md text (no current catalog version?)")
            cands.append((role, sk, "QUARANTINED", {"refused": f"candidate {sk['name']} is quarantined (catalog)"}))
            continue
        from lab.scan import scan
        eff = max((sk["security_status"], scan(sk["text"])["security_status"]), key=lambda x: SEC_RANK.get(x, 0))
        if eff == "flagged" and not operator_reviewed:  # file: candidates included (no catalog status)
            status, info = "BLOCKED", {"state": "WAITING_SECURITY_APPROVAL"}
        else:
            try:
                info = wrap_candidate(sk["text"], sk["name"], CANDIDATES / slug(sk["name"], spec), eff, case_dir)
            except Refused as e:
                status, info = "QUARANTINED", {"refused": str(e)}
        cands.append((role, sk, status, info))

    rows = [("NO_SKILL", None, None, "PLANNED", {}, n) for n in range(1, runs + 1)] if "NO_SKILL" in arms else []
    for role, sk, status, info in cands:
        rows += [(a, role, sk, status, info, n) for a in arms if a != "NO_SKILL" for n in range(1, runs + 1)]
    order = list(range(len(rows)))
    random.Random(seed).shuffle(order)  # reduce order effects (F5); seed recorded on every row
    for pos, i in enumerate(order):
        arm, role, sk, status, info, n = rows[i]
        rid = f"{exp}-{arm}{'-' + role if role else ''}-{n}"
        notes = {"order": pos, "seed": seed, "case_prompt": str(case_prompt), "prompt_sha256": prompt_sha, **{k: v for k, v in info.items() if k != "plugin_dir"}}
        if info.get("plugin_dir"):
            notes["plugin_dir"] = info["plugin_dir"]
        conn.execute(
            "INSERT INTO eval_runs (run_id, experiment_id, task_id, rubric_version, arm, candidate_role, skill_id,"
            " skill_version_id, model_id, judge_model_id, claude_code_version, auth_profile, engine, run_no, status,"
            " notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, exp, task_id, t["rubric_version"], arm, role, sk and sk["skill_id"], sk and sk["version_id"],
             model, judge_model, ver, auth, engine, n, status, json.dumps(notes, ensure_ascii=False)))
    return exp


# ---- execute ------------------------------------------------------------------------------------
def _scope_key():
    try:
        from lab.runtime import night_key, utcnow, window_end
        return night_key(window_end(utcnow()))
    except ImportError:
        import datetime as dt
        return (dt.datetime.now(dt.timezone(dt.timedelta(hours=9))) - dt.timedelta(hours=5)).date().isoformat()


def _reserve(conn, run_id, runs=1, usd_cap="per_call_max_usd"):
    """-> (reservation_id or 'local', block_reason). Budget module when present; else run-count cap only."""
    try:
        from lab import budget
    except ImportError:
        budget = None
    if budget:
        usd = budget.load_caps().get(usd_cap)
        rid = budget.try_reserve(conn, "nightly_window", _scope_key(), usd=usd, runs=runs, job_id=run_id)
        return rid, None if rid else "budget hard limit (lab.budget)"
    cap = yaml.safe_load((ROOT / "config" / "budgets.yaml").read_text(encoding="utf-8"))["budgets"]["nightly_max_agent_runs"]
    used = conn.execute("SELECT COUNT(*) FROM eval_runs WHERE started_at >= datetime('now','-24 hours')").fetchone()[0]
    return ("local", None) if cap is None or used + runs <= cap else (None, f"nightly_max_agent_runs={cap} reached")


def _settle(conn, rid, usd):
    if rid and rid != "local":
        from lab import budget
        budget.settle(conn, rid, usd)


def verify(task_id, workdir, out_json, fixture="react-demo", transcript=None):
    """External verifier (fixtures/<fixture>/verify/run.mjs), run outside the agent; it links node_modules into
    the workdir itself. -> list of {id,result,verifier,verifier_kind,detail} or None."""
    script = ROOT / VERIFIER.format(fixture=fixture)
    if not script.exists():
        return None
    cmd = ["node", str(script), "--case", task_id, "--workdir", str(workdir), "--json", str(out_json),
           "--artifacts", str(Path(out_json).with_suffix("")) + "-artifacts"]
    if transcript:
        cmd += ["--log", str(transcript)]
    try:
        # minimal_env: the verifier imports agent-written code, which must not see the operator's secrets
        subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900, env=claude_run.minimal_env())
        return json.loads(Path(out_json).read_text(encoding="utf-8")).get("requirements")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def record_checks(conn, run_id, reqs, evidence_uri):
    """-> (status, success, passed, total) over automated requirements. Human-review items are stored as
    'unevaluated' and never block or fake a pass. Nothing observed -> UNEVALUATED, never success."""
    if not reqs:
        return "UNEVALUATED", None, None, None
    for q in reqs:
        res = q.get("result") if q.get("result") in ("pass", "fail") else "unevaluated"
        conn.execute("INSERT OR REPLACE INTO eval_checks VALUES (?,?,?,?,?)",
                     (run_id, str(q["id"]), res, q.get("verifier_kind") or "external_test", evidence_uri))
    results = [q.get("result") for q in reqs if q.get("verifier_kind") != "human"]
    passed, total = results.count("pass"), len(results)
    if "fail" in results:
        return "FAILED", 0, passed, total
    if not total or passed < total:
        return "UNEVALUATED", None, passed, total
    return "PASSED", 1, passed, total


def finish_run(conn, row, out, plugin=None, skill=None, reqs=None, verify_uri=None):
    """Map one claude -p outcome to eval_runs + llm_calls. Pure DB logic (tested offline)."""
    p, cls = out["parsed"], out["classification"]
    attempt = ledger.next_attempt(conn, row["run_id"])
    ctx = {"stage": "execute", "agent_role": "executor", "action": f"eval_{row['arm']}",
           "skill_id": row["skill_id"] or "NO_SKILL", "auth_profile": row["auth_profile"], "model_id": row["model_id"],
           "claude_code_version": row["claude_code_version"], "arm": row["arm"], "step_id": f"execute.{row['arm']}",
           "skill_hash": _notes(row).get("skill_sha256")}
    ledger.write_calls(conn, ledger.calls_from_transcript(
        p, run_id=row["run_id"], ctx=ctx, attempt_base=attempt, latency_ms=out["latency_ms"],
        status=cls["state"] or None, error_code=cls["error_code"]))
    iso_ok, leaks = ledger.isolation_check(p.get("init"), plugin if row["arm"] != "NO_SKILL" else None)
    leaks += out.get("preflight_leaks") or []
    iso_ok = 0 if leaks else iso_ok
    ours = [s for s in p["skill_calls"] if s and skill and s.split(":")[-1] == skill]
    trig = 1 if ours else (None if row["arm"] == "FORCED" else 0)  # FORCED: slash expansion leaves no Skill block
    notes = {**_notes(row), "retries": _notes(row).get("retries", 0) + (row["status"] == "UNEVALUATED"), "tool_uses": sorted({t["name"] for t in p["tool_uses"]}), "skill_calls": p["skill_calls"],
             "leaks": leaks, "workdir": out.get("workdir"), "state": cls["state"], "error_code": cls["error_code"], "exit_code": out["exit_code"]}
    if cls["state"]:
        status, success, passed, total = "UNEVALUATED", None, None, None  # RES-17: limit != failure
    elif not iso_ok:
        status, success, passed, total = "INVALID", None, None, None  # RES-18
    else:
        status, success, passed, total = record_checks(conn, row["run_id"], reqs, verify_uri)
    res = p.get("result") or {}
    conn.execute(
        "UPDATE eval_runs SET status=?, success=?, checks_passed=?, checks_total=?, trigger_observed=?, isolation_ok=?,"
        " cost_usd=?, latency_ms=?, num_turns=?, session_id=?, artifact_uri=?, finished_at=?, engine=?, notes=?"
        " WHERE run_id=?",
        (status, success, passed, total, trig, iso_ok, ledger.run_cost(conn, row["run_id"]), out["latency_ms"],
         res.get("num_turns"), p.get("session_id"), out.get("artifact"), now(), "claude_p",
         json.dumps(notes, ensure_ascii=False), row["run_id"]))
    return status, cls["state"]


def execute(conn, experiment_id, engine="claude_p", dry_run=False, operator_reviewed=False, limit=None, log=print):
    if engine == "plugin_eval":
        return execute_plugin_eval(conn, experiment_id, operator_reviewed, dry_run, log)
    rows = conn.execute("SELECT * FROM eval_runs WHERE experiment_id=? AND status IN ('PLANNED','UNEVALUATED')",
                        (experiment_id,)).fetchall()
    # UNEVALUATED is retried only after a limit wall, at most MAX_RETRIES times (verifier crash -> human);
    # PLANNED rows first so a retry can never starve the rest of the experiment.
    rows = [r for r in rows if r["status"] == "PLANNED" or
            (_notes(r).get("state") in RETRY_STATES and _notes(r).get("retries", 0) < MAX_RETRIES)]
    rows = sorted(rows, key=lambda r: (r["status"] != "PLANNED", _notes(r).get("order", 0)))[:limit]
    cfg = load_cfg()["claude"]
    budgets = yaml.safe_load((ROOT / "config" / "budgets.yaml").read_text(encoding="utf-8"))["budgets"]
    for tid in {r["task_id"] for r in rows}:  # fixture + verifier are ground truth: frozen with the rubric
        t = conn.execute("SELECT fixture_commit, environment_json FROM tasks WHERE id=?", (tid,)).fetchone()
        fx = json.loads(t["environment_json"] or "{}").get("fixture", "react-demo")
        from lab.rubrics import tree_hash
        if t["fixture_commit"] and t["fixture_commit"] != f"sha256:{tree_hash(ROOT / 'fixtures' / fx)}":
            raise Refused(f"{tid}: fixture/verifier changed since the rubric was registered -> new rubric_version + approval")
    if not dry_run and rows and claude_run.version() != rows[0]["claude_code_version"]:
        raise Refused("claude --version changed since plan; re-plan (version is part of the frozen setup)")
    for row in rows:
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
        env = json.loads(t["environment_json"] or "{}")
        n = _notes(row)
        plugin_dir, plugin, skill = n.get("plugin_dir"), n.get("plugin"), n.get("skill")
        r = conn.execute("SELECT approved_at FROM rubrics WHERE id=? AND version=?",
                         (t["rubric_id"], row["rubric_version"])).fetchone()
        if not r or not r["approved_at"]:
            raise Refused(f"rubric {t['rubric_id']}/{row['rubric_version']} no longer approved; re-plan")
        cp = n.get("case_prompt") or CASES / f"{row['task_id']}@{row['rubric_version']}" / row["task_id"] / "prompt.md"
        try:
            task_prompt = _case_prompt(cp)  # the frozen prompt, never the live tasks.prompt
        except (OSError, IndexError):
            raise Refused(f"{row['run_id']}: frozen case prompt {cp} missing/unreadable; re-plan")
        want = n.get("prompt_sha256") or hashlib.sha256(t["prompt"].encode("utf-8")).hexdigest()
        if hashlib.sha256(task_prompt.encode("utf-8")).hexdigest() != want:
            raise Refused(f"{row['run_id']}: frozen case prompt changed since plan; re-plan")
        prompt = task_prompt if row["arm"] != "FORCED" else f"/{plugin}:{skill} {task_prompt}"
        if dry_run:
            log(f"[dry] {row['run_id']} arm={row['arm']} plugin={plugin} prompt={prompt[:60]!r}")
            continue
        rid, why = _reserve(conn, row["run_id"])
        if not rid:
            log(f"STOP {row['run_id']}: {why} -> WAITING_BUDGET_APPROVAL")
            return "WAITING_BUDGET_APPROVAL"
        conn.execute("UPDATE eval_runs SET started_at=? WHERE run_id=?", (now(), row["run_id"]))
        work = Path(tempfile.mkdtemp(prefix=f"ssl-{row['run_id'][-24:]}-"))
        fixture = env.get("fixture", "react-demo")
        shutil.copytree(ROOT / "fixtures" / fixture, work, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("node_modules", ".git", "verify", "dist", "test-results",
                                                      "playwright-report"))
        attempt = ledger.next_attempt(conn, row["run_id"]) + 1
        art = lambda a: ARTIFACTS / (f"{row['run_id']}.jsonl" if a == 1 else f"{row['run_id']}.a{a}.jsonl")
        while art(attempt).exists():  # crash before finish_run left no ledger row: never overwrite paid evidence
            attempt += 1
        art = art(attempt)
        out = claude_run.run_p(prompt, work, row["model_id"], env.get("allowed_tools", DEFAULT_TOOLS), art,
                               plugin_dir=plugin_dir if row["arm"] != "NO_SKILL" else None,
                               max_budget_usd=budgets.get("per_call_max_usd"), max_turns=cfg["case_max_turns"],
                               timeout_s=cfg["case_timeout_seconds"])
        out["preflight_leaks"] = claude_run.preflight_leaks(work)
        out["workdir"] = str(work)  # kept as evidence (temp dir, outside any repo)
        reqs, vuri = None, None
        if not out["classification"]["state"]:
            vuri = str(ARTIFACTS / f"{row['run_id']}.a{attempt}.verify.json")
            reqs = verify(row["task_id"], work, vuri, fixture, transcript=art)
        status, state = finish_run(conn, row, out, plugin, skill, reqs, vuri)
        _settle(conn, rid, ledger.run_cost(conn, row["run_id"]))
        log(f"{row['run_id']}: {status}{' / ' + state if state else ''} cost={ledger.run_cost(conn, row['run_id'])}")
        if state in STOP_STATES:  # never keep calling into a quota/auth wall (20.5)
            return state
    return "COMPLETED"


# ---- plugin eval engine -------------------------------------------------------------------------
def _find(d, rx):
    """first value whose key matches rx (recursive) - plugin eval per-run field names are not documented."""
    if isinstance(d, dict):
        for k, v in d.items():
            if re.search(rx, k, re.I) and isinstance(v, (str, int, float)):
                return v
        for v in d.values():
            f = _find(v, rx)
            if f is not None:
                return f
    if isinstance(d, list):
        for v in d:
            f = _find(v, rx)
            if f is not None:
                return f
    return None


def ingest_plugin_eval(conn, experiment_id, result, role="B", json_path=None):
    """Map a `claude plugin eval --json` document onto PLANNED rows: arms.with -> AUTO, arms.without -> NO_SKILL.
    Builtin grader scores are recorded as builtin_grader checks only; outcome needs a kept workdir + verifier."""
    n_mapped = 0
    for case in result.get("cases") or []:
        for arm_key, arm in (("with", "AUTO"), ("without", "NO_SKILL")):
            q = "SELECT * FROM eval_runs WHERE experiment_id=? AND arm=? AND status IN ('PLANNED','UNEVALUATED')"
            args = [experiment_id, arm]
            if arm == "AUTO":
                q += " AND candidate_role=?"
                args.append(role)
            free = sorted(conn.execute(q, args).fetchall(), key=lambda r: r["run_no"])
            for row, run in zip(free, (case.get("arms") or {}).get(arm_key) or []):
                work = _find(run, r"(temp|sandbox|work).*(dir|path)")
                score = _find(run, r"^score$")
                reqs, vuri = None, None
                if work and Path(str(work)).exists() and not run.get("error"):
                    vuri = str(ARTIFACTS / f"{row['run_id']}.verify.json")
                    reqs = verify(row["task_id"], work, vuri)
                if score is not None:
                    conn.execute("INSERT OR REPLACE INTO eval_checks VALUES (?,?,?,?,?)",
                                 (row["run_id"], "builtin_score", "pass" if float(score) >= 1 else "fail",
                                  "builtin_grader", json_path))
                status, success, passed, total = record_checks(conn, row["run_id"], reqs, vuri)
                err = run.get("error")
                if err:
                    status, success = "UNEVALUATED", None  # API error/limit run scores 0 in plugin eval: not a failure
                notes = {**_notes(row), "plugin_eval_run": ledger.redact(run) if len(json.dumps(run)) < 4000 else "large",
                         "error": err}
                cost = run.get("costUsd")
                conn.execute("UPDATE eval_runs SET status=?, success=?, checks_passed=?, checks_total=?, cost_usd=?,"
                             " engine='plugin_eval', artifact_uri=?, finished_at=?, notes=?,"
                             " claude_code_version=COALESCE(?, claude_code_version) WHERE run_id=?",
                             (status, success, passed, total, cost, json_path, now(),
                              json.dumps(notes, ensure_ascii=False), result.get("claudeVersion"), row["run_id"]))
                ledger.write_calls(conn, [{
                    "call_id": f"{row['run_id']}-pe", "timestamp_utc": now(), "run_id": row["run_id"],
                    "step_id": f"execute.{arm}", "attempt_id": 1, "stage": "execute", "agent_role": "executor",
                    "action": "plugin_eval", "skill_id": row["skill_id"] or "NO_SKILL", "provider": "anthropic",
                    "auth_profile": row["auth_profile"], "call_path": "claude_cli", "model_id": row["model_id"],
                    "claude_code_version": result.get("claudeVersion") or row["claude_code_version"], "arm": arm,
                    "usage_source": "unavailable",  # plugin eval JSON has cost but no per-run token split
                    "provider_usage_json": json.dumps(ledger.redact({"costUsd": cost})), "status": "error" if err else "success",
                    "error_code": err, "estimated_cost": cost, "currency": "USD", "cache_hit": 0, "reused_artifact": 0,
                    "idempotency_key": f"{row['run_id']}:pe"}])
                n_mapped += 1
    return n_mapped


def execute_plugin_eval(conn, experiment_id, operator_reviewed, dry_run, log=print):
    """NO_SKILL + AUTO via `claude plugin eval` with-without ablation. The case needs the fixture in the workspace
    (scaffold_script) and Write/Edit grants, both gated behind --i-reviewed-this-candidate (PRD 10)."""
    cfg = load_cfg()["claude"]
    budgets = yaml.safe_load((ROOT / "config" / "budgets.yaml").read_text(encoding="utf-8"))["budgets"]
    for role in ("B", "C"):
        row = conn.execute("SELECT * FROM eval_runs WHERE experiment_id=? AND arm='AUTO' AND candidate_role=? "
                           "AND status='PLANNED' LIMIT 1", (experiment_id, role)).fetchone()
        if not row:
            continue
        n = conn.execute("SELECT COUNT(*) FROM eval_runs WHERE experiment_id=? AND arm='AUTO' AND candidate_role=?",
                         (experiment_id, role)).fetchone()[0]
        out = ARTIFACTS / f"{experiment_id}-{role}.plugin-eval.json"
        if dry_run:
            log(f"[dry] claude plugin eval {_notes(row)['plugin_dir']} --runs {n} -> {out}")
            continue
        if not operator_reviewed:
            raise Refused("plugin_eval needs --trust-plugin/--scaffold/--allow-tools: pass --i-reviewed-this-candidate "
                          "only after a human reviewed this candidate")
        # n AUTO rows = runs_per_case x cases; the with/without ablation runs each of them twice
        rid, why = _reserve(conn, f"{experiment_id}-{role}", runs=n * 2, usd_cap="per_suite_max_usd")
        if not rid:
            return "WAITING_BUDGET_APPROVAL"
        env = json.loads(conn.execute("SELECT environment_json FROM tasks WHERE id=?", (row["task_id"],)).fetchone()[0] or "{}")
        tools = [x for x in env.get("allowed_tools", DEFAULT_TOOLS) if x in ("Write", "Edit") or x.startswith("Bash")]
        r = claude_run.run_plugin_eval(_notes(row)["plugin_dir"], row["model_id"], row["judge_model_id"], n, out,
                                       budgets.get("per_suite_max_usd"), True, True, tools, scaffold=True,
                                       timeout_s=cfg["case_timeout_seconds"] * n * 2 + 600)
        (ARTIFACTS / f"{experiment_id}-{role}.plugin-eval.log").write_text(r["stdout"] + "\n--stderr--\n" + r["stderr"],
                                                                          encoding="utf-8")
        mapped = ingest_plugin_eval(conn, experiment_id, r["result"], role, str(out)) if r["result"] else 0
        _settle(conn, rid, (r["result"] or {}).get("costUsd"))
        log(f"plugin eval {role}: exit={r['exit_code']} mapped={mapped}")
    log("FORCED rows: run `execute --engine claude_p` (plugin eval has no forced-invocation arm)")
    return "COMPLETED"


# ---- report -------------------------------------------------------------------------------------
def _rate(rows):
    ev = [r for r in rows if r["status"] in ("PASSED", "FAILED")]
    k = sum(r["success"] or 0 for r in ev)
    return k, len(ev), (k / len(ev) if ev else None)


def _scores(rows):
    return [r["checks_passed"] / r["checks_total"] for r in rows
            if r["status"] in ("PASSED", "FAILED") and r["checks_total"]]


def _mean_cost(rows):
    c = [r["cost_usd"] for r in rows if r["cost_usd"] is not None and r["status"] in ("PASSED", "FAILED")]
    return statistics.mean(c) if c else None


def _pp(a, b):
    return "계산 불가" if a is None or b is None else f"{(a - b) * 100:+.1f}%p"


def _mandatory(rubric_yaml):
    """Mandatory *safety* requirement ids (rubric requirements[] with mandatory + type safety). Functional
    mandatory items already drive success; a safety violation in any candidate run disqualifies (4.4 step 1)."""
    try:
        y = yaml.safe_load(rubric_yaml) or {}
    except yaml.YAMLError:
        return set()
    return {str(q.get("id")) for q in (y.get("requirements") or []) if isinstance(q, dict) and q.get("mandatory")
            and (q.get("type") == "safety" or q.get("verifier_kind") == "log_policy")}


def _add_failure(conn, task_id, run_id, category, text, evidence=None):
    if not conn.execute("SELECT 1 FROM failure_cases WHERE run_id IS ? AND category=? AND task_id=?",
                        (run_id, category, task_id)).fetchone():
        conn.execute("INSERT INTO failure_cases (task_id, run_id, category, explanation, evidence_uri, origin, created_at)"
                     " VALUES (?,?,?,?,?, 'auto', ?)", (task_id, run_id, category, text, evidence, now()))


def classify_candidate(no, auto, forced):
    """RES-19: AUTO == NO_SKILL with no Skill call -> trigger failure; 'no effect' only when FORCED is also equal."""
    _, _, rn = _rate(no)
    _, _, ra = _rate(auto)
    _, nf, rf = _rate(forced)
    auto_ran = [r for r in auto if r["status"] in ("PASSED", "FAILED")]
    triggered = any(r["trigger_observed"] for r in auto_ran)
    trigger_failure = bool(auto_ran) and not triggered and ra == rn
    no_effect = None if not nf or rn is None else rf == rn
    return {"trigger_failure": trigger_failure, "no_effect": no_effect}


def report(conn, experiment_id):
    rows = conn.execute("SELECT * FROM eval_runs WHERE experiment_id=? ORDER BY run_id", (experiment_id,)).fetchall()
    if not rows:
        return f"experiment {experiment_id}: no rows"
    r0 = rows[0]
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (r0["task_id"],)).fetchone()
    rub = conn.execute("SELECT * FROM rubrics WHERE id=? AND version=?", (t["rubric_id"], r0["rubric_version"])).fetchone()
    should = json.loads(t["context_json"] or "{}").get("should_trigger")
    by = lambda arm, role=None: [r for r in rows if r["arm"] == arm and r["candidate_role"] == role]  # noqa: E731
    no = by("NO_SKILL")
    L = [f"과제: {t['id']} — {t['prompt'][:70]}",
         f"평가 기준: {t['rubric_id']}/{r0['rubric_version']} / {'고정됨 ' + rub['approved_at'] if rub and rub['approved_at'] else '미승인(무효)'}",
         f"[실행 환경] 모델 {sorted({r['model_id'] for r in rows})} · 판정 {sorted({str(r['judge_model_id']) for r in rows})}"
         f" · Claude Code {sorted({r['claude_code_version'] for r in rows})} · 인증 프로필 {sorted({r['auth_profile'] for r in rows})}"
         f" · 엔진 {sorted({r['engine'] for r in rows})}",
         "[사전 예측] 선정 점수는 예측일 뿐, 아래 수치만 증거"]
    st = {}
    for r in rows:
        st[r["status"]] = st.get(r["status"], 0) + 1
    L.append(f"[상태] {st}" + ("  ※ PLANNED 남음: 실행 미완료" if st.get("PLANNED") else ""))

    def arm_line(name, rs):
        k, n, _ = _rate(rs)
        sc = _scores(rs)
        mc = _mean_cost(rs)
        cps = "계산 불가" if not k else f"${sum(r['cost_usd'] or 0 for r in rs if r['status'] in ('PASSED', 'FAILED')) / k:.4f}"
        toks = conn.execute(
            f"SELECT SUM(input_tokens+cache_write_tokens+cached_input_tokens), SUM(output_tokens), "
            f"SUM(usage_source='unavailable') FROM llm_calls WHERE run_id IN ({','.join('?' * len(rs))})",
            [r["run_id"] for r in rs]).fetchone() if rs else (None, None, None)
        return (f"  {name:<12} 성공 {k}/{n}  점수 평균 {statistics.mean(sc):.2f} 표준편차 {statistics.pstdev(sc):.2f}" if sc
                else f"  {name:<12} 성공 {k}/{n}  점수 -") + \
            (f"  미평가 {sum(r['status'] == 'UNEVALUATED' for r in rs)} 무효 {sum(r['status'] == 'INVALID' for r in rs)}"
             f"  평균비용 {'-' if mc is None else f'${mc:.4f}'} (추정)  성공당 비용 {cps}"
             f"  입력토큰 {toks[0]} 출력토큰 {toks[1]} usage누락 {toks[2] or 0}")

    L.append("[작업 성과] (성공 = 외부 검증기 전 항목 통과)")
    L.append(arm_line("A NO_SKILL", no))
    verdicts, trig_fail, no_eff = [], 0, 0
    mand = _mandatory(rub["yaml_text"]) if rub else set()
    for role in ("B", "C"):
        auto, forced = by("AUTO", role), by("FORCED", role)
        if not auto and not forced:
            continue
        cand = (auto or forced)[0]
        sk = conn.execute("SELECT security_status FROM skills WHERE id=?", (cand["skill_id"],)).fetchone()
        L.append(f"  -- {role}: {cand['skill_id']} (version {cand['skill_version_id']}, sha256 {_notes(cand).get('skill_sha256', '?')[:12]})")
        L.append(arm_line(f"{role} AUTO", auto))
        if forced:
            L.append(arm_line(f"{role}′ FORCED", forced))
        _, _, rn = _rate(no)
        _, na, ra = _rate(auto)
        _, nf, rf = _rate(forced)
        ran_auto = [r for r in auto if r["status"] in ("PASSED", "FAILED", "UNEVALUATED") and r["finished_at"]]
        trig = sum(1 for r in ran_auto if r["trigger_observed"])
        L.append(f"  [트리거] {role} AUTO 자동 호출 {trig}/{len(ran_auto)}"
                 + ("" if should is None else f" (이 과제는 '호출{'해야 하는' if should else '하면 안 되는'}' 프롬프트)")
                 + f" / 적용 시 효과(FORCED−NO_SKILL) {_pp(rf, rn)} / 개선량(AUTO−NO_SKILL) {_pp(ra, rn)}")
        lv_load = sum(1 for r in auto + forced if _notes(r).get("skill_calls"))
        lv_beh = sum(1 for r in auto + forced if _notes(r).get("tool_uses"))
        lv_out = sum(1 for r in auto + forced if r["status"] in ("PASSED", "FAILED"))
        L.append(f"  [증거 수준] 로드(Skill 호출 관측) {lv_load} · 행동(tool_use 기록) {lv_beh} · 성과(외부 테스트) {lv_out}"
                 + (" · FORCED는 /슬래시 강제 호출(stream에 Skill 블록 없음)" if forced else ""))
        mc_no, mc_a = _mean_cost(no), _mean_cost(auto)
        inc = None if not mc_no or mc_a is None else (mc_a - mc_no) / mc_no * 100
        L.append(f"  [비용] 증가율 {'계산 불가' if inc is None else f'{inc:+.1f}%'} (AUTO 대비 NO_SKILL, 추정 비용)")
        # verdict, 4.4 order
        cls = classify_candidate(no, auto, forced)
        if cls["trigger_failure"] and should is not False:
            trig_fail += 1
            _add_failure(conn, t["id"], auto[0]["run_id"], "TRIGGER",
                         f"{role}: AUTO 성공률 = NO_SKILL 이고 Skill 호출 0회 → 트리거 실패 (효과 없음 아님)")
        if cls["no_effect"]:
            no_eff += 1
        mand_fail = sorted({m for m in mand for r in auto + forced if conn.execute(
            "SELECT 1 FROM eval_checks WHERE run_id=? AND requirement_id=? AND result='fail'", (r["run_id"], m)).fetchone()})
        need = min(3, max(r["run_no"] for r in rows))
        if (sk and sk["security_status"] in ("quarantined", "flagged")) or any(r["status"] in ("QUARANTINED", "BLOCKED") for r in auto):
            v = "탈락/격리 (보안)"
        elif mand_fail:
            v = f"탈락 (필수 요구사항 위반 {mand_fail})"
        elif min(_rate(no)[1], na) < need:
            v = f"INSUFFICIENT_EVIDENCE (팔별 평가 완료 실행 {_rate(no)[1]}/{na} < {need})"
        else:
            v = "우세" if ra > rn else "열세" if ra < rn else "동률"
            if v == "동률" and _scores(auto) and _scores(no):
                v += f" (점수 평균 {statistics.mean(_scores(auto)):.2f} vs {statistics.mean(_scores(no)):.2f})"
            v += " — 해당 케이스에서만, 일반화 보류 (과제 10개 단계: %p 임계값 없음)"
        if inc is not None and inc > COST_FLAG_PCT:
            v += f" / COST 경고 (+{inc:.0f}% > +{COST_FLAG_PCT}% 가설값)"
            _add_failure(conn, t["id"], auto[0]["run_id"], "COST", f"{role}: 비용 증가 {inc:+.0f}%")
        verdicts.append(f"{role} {v}")
        if cls["trigger_failure"]:
            verdicts.append(f"{role} 분류: 트리거 실패" + (" + 효과 없음(FORCED도 차이 없음)" if cls["no_effect"] else ""))
        elif cls["no_effect"]:
            verdicts.append(f"{role} 분류: 효과 없음 (FORCED에서도 차이 없음)")
    L.append(f"[판정] " + " | ".join(verdicts))
    L.append(f"[분류 집계] 트리거 실패 {trig_fail} · 효과 없음 {no_eff} (서로 다른 집계, RES-19)")
    L.append("[미확인] 다른 프로젝트·다른 과제에서의 재현성; 비용은 Claude Code 클라이언트 추정치(실청구 아님)")
    return "\n".join(L)


# ---- CLI ----------------------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m lab.evaluator")
    ap.add_argument("--db", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--task", required=True)
    p.add_argument("--b", required=True, help="catalog skill id or file:<path to SKILL.md>")
    p.add_argument("--c")
    p.add_argument("--runs", type=int, default=None)
    p.add_argument("--arms", default=",".join(ARMS))
    p.add_argument("--model")
    p.add_argument("--judge-model")
    p.add_argument("--seed", type=int)
    p.add_argument("--i-reviewed-this-candidate", action="store_true", dest="reviewed")
    e = sub.add_parser("execute")
    e.add_argument("--experiment", required=True)
    e.add_argument("--engine", choices=("claude_p", "plugin_eval"), default="claude_p")
    e.add_argument("--dry-run", action="store_true")
    e.add_argument("--limit", type=int)
    e.add_argument("--i-reviewed-this-candidate", action="store_true", dest="reviewed")
    r = sub.add_parser("report")
    r.add_argument("--experiment", required=True)
    i = sub.add_parser("ingest")
    i.add_argument("json")
    i.add_argument("--experiment", required=True)
    i.add_argument("--role", default="B")
    a = ap.parse_args(argv)
    conn = connect(a.db) if a.db else connect()
    try:
        if a.cmd == "plan":
            runs = a.runs or load_cfg()["claude"]["eval_runs_per_arm"]
            print(plan(conn, a.task, a.b, a.c, runs, tuple(a.arms.split(",")), a.model, a.judge_model, a.seed,
                       operator_reviewed=a.reviewed))
        elif a.cmd == "execute":
            print(execute(conn, a.experiment, a.engine, a.dry_run, a.reviewed, a.limit))
        elif a.cmd == "report":
            print(report(conn, a.experiment))
        elif a.cmd == "ingest":
            res = json.loads(Path(a.json).read_text(encoding="utf-8"))
            print(f"mapped {ingest_plugin_eval(conn, a.experiment, res, a.role, a.json)} runs")
    except Refused as ex:
        print(f"REFUSED: {ex}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
