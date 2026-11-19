"""F8 token ledger for Claude Code runs (PRD 19.2-19.4, 20.5, RES-10/11/17/18).

Parses a `claude -p --output-format stream-json --verbose` transcript and maps it to llm_calls rows.
Field names verified against Claude Code 2.1.327 (see docs/claude-runtime.md).
"""
import json
import re

from lab.db import now

# Built-in skills / plugins that a project-only (`--setting-sources project`) run lists in its
# system/init message on Claude Code 2.1.327. Anything else that is not the candidate's
# `<plugin>:<skill>` is a leak from personal config (RES-18).
# ponytail: static allowlist; a new Claude Code version that adds built-ins makes runs INVALID
# (fails safe) until this set is refreshed from a NO_SKILL init message.
BUILTIN_SKILLS = {
    "deep-research", "design", "slides", "design-sync", "dataviz", "artifact-diagramming", "whiteboard",
    "artifact-capabilities", "update-config", "verify", "debug", "code-review", "simplify", "batch",
    "fewer-permission-prompts", "doctor", "loop", "schedule", "claude-api", "workflow-authoring", "run",
    "run-skill-generator", "plugin-authoring",
}
BUILTIN_AGENTS = {"claude", "claude-code-guide", "Explore", "general-purpose", "Plan", "web-fetch", "statusline-setup"}

_SECRET_KEY = re.compile(r"(api[_-]?key|secret|password|passwd|authorization|access[_-]?token|refresh[_-]?token"
                         r"|^token$|session[_-]?token|cookie|e-?mail)", re.I)
_SECRET_VAL = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
]


def redact(obj):
    """Recursively drop secret-looking keys and mask secret-looking values (RES-12)."""
    if isinstance(obj, dict):
        return {k: "[REDACTED]" if _SECRET_KEY.search(str(k)) else redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        for rx in _SECRET_VAL:
            obj = rx.sub("[REDACTED]", obj)
    return obj


def parse_stream(lines):
    """stream-json lines (str or dict) -> summary dict. Tolerates junk/partial lines (crash)."""
    p = {"init": None, "result": None, "retries": [], "rate_limits": [], "skill_calls": [], "tool_uses": [],
         "session_id": None, "permission_denied": 0, "bad_lines": 0}
    seen_tool_ids = set()
    for line in lines:
        if isinstance(line, str):
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except ValueError:
                p["bad_lines"] += 1
                continue
        else:
            m = line
        t, st = m.get("type"), m.get("subtype")
        p["session_id"] = p["session_id"] or m.get("session_id")
        if t == "system" and st == "init":
            p["init"] = m
        elif t == "system" and st == "api_retry":
            p["retries"].append(m)
        elif t == "system" and st == "permission_denied":
            p["permission_denied"] += 1
        elif t == "rate_limit_event":
            p["rate_limits"].append(m.get("rate_limit_info") or {})
        elif t == "result":
            p["result"] = m
        elif t == "assistant":
            for b in (m.get("message") or {}).get("content") or []:
                if b.get("type") != "tool_use" or b.get("id") in seen_tool_ids:
                    continue  # same message id repeats with parallel tool calls (19.3 rule 3)
                seen_tool_ids.add(b.get("id"))
                if b.get("name") == "Skill":
                    p["skill_calls"].append((b.get("input") or {}).get("skill"))
                else:
                    p["tool_uses"].append({"name": b.get("name"), "input": b.get("input")})
    return p


def read_transcript(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return parse_stream(f)


def isolation_check(init, candidate_plugin=None):
    """-> (ok: 0/1, leaks: list). No init message => cannot prove isolation => 0."""
    if not init:
        return 0, ["no system/init message"]
    leaks = []
    for pl in init.get("plugins") or []:
        if pl.get("path") != "builtin" and pl.get("name") != candidate_plugin:
            leaks.append(f"plugin:{pl.get('name')}")
    for s in init.get("skills") or []:
        if s in BUILTIN_SKILLS or (candidate_plugin and s.startswith(candidate_plugin + ":")):
            continue
        leaks.append(f"skill:{s}")
    for a in init.get("agents") or []:
        if a not in BUILTIN_AGENTS and not (candidate_plugin and a.startswith(candidate_plugin + ":")):
            leaks.append(f"agent:{a}")
    for srv in init.get("mcp_servers") or []:
        leaks.append(f"mcp:{srv.get('name')}")
    return (0 if leaks else 1), leaks


# ---- error classification (PRD 20.5 Claude mapping). Strings match lab.runtime._normalize. -----
_PLAN_LIMIT = re.compile(r"(usage limit|limit reached|out of (extra )?usage|weekly limit|session limit|"
                         r"credit balance is too low|resets? (at|in))", re.I)
_SPEND_LIMIT = re.compile(r"(spend(ing)? limit|workspace .*limit)", re.I)


def classify(parsed=None, exit_code=None, stderr=""):
    """-> dict(state, error_code, retry_after_s, reset_at, attempts_used) ; state None = no error.

    states: WAITING_RATE_LIMIT | WAITING_QUOTA_RESET | WAITING_BUDGET_APPROVAL | FAILED_PERMANENT | transient
    """
    parsed = parsed or parse_stream([])
    res, retries = parsed.get("result"), parsed.get("retries") or []
    out = {"state": None, "error_code": None, "retry_after_s": None, "reset_at": None,
           "attempts_used": len(retries) + 1}
    if exit_code == 143:  # SIGTERM: the turn in flight has no result -> rerun from last committed step
        return {**out, "state": "transient", "error_code": "exit_143"}
    failed = res is None or res.get("is_error") or (res.get("subtype") or "success") != "success" or exit_code not in (0, None)
    if not failed:
        return out
    status = (res or {}).get("api_error_status")
    last = retries[-1] if retries else {}
    err = last.get("error")
    text = " ".join([str((res or {}).get("result") or ""), " ".join(map(str, (res or {}).get("errors") or [])), stderr or ""])
    rejected = [r for r in parsed.get("rate_limits") or [] if r.get("status") not in (None, "allowed", "allowed_warning")]
    sub = (res or {}).get("subtype")

    def done(state, code, **kw):
        return {**out, "state": state, "error_code": code, **kw}

    if sub == "error_max_budget_usd":
        return done("WAITING_BUDGET_APPROVAL", sub)
    if status in (401, 403) or err in ("authentication_failed", "oauth_org_not_allowed", "account_on_hold"):
        return done("FAILED_PERMANENT", err or f"http_{status}")
    if status == 402 or err in ("billing_error", "cloud_credential_error"):
        return done("WAITING_QUOTA_RESET", err or "http_402")
    if rejected or _PLAN_LIMIT.search(text):  # profile S plan limit (RES-17): never switch to pay-as-you-go
        reset = (rejected[-1].get("resetsAt") if rejected else None)
        return done("WAITING_QUOTA_RESET", "plan_usage_limit", reset_at=reset)
    if status == 400 and _SPEND_LIMIT.search(text):
        return done("WAITING_BUDGET_APPROVAL", "http_400_spend_limit")
    if status == 429 or err == "rate_limit":
        delay = last.get("retry_delay_ms")
        if delay:  # retry-after known
            return done("WAITING_RATE_LIMIT", "rate_limit", retry_after_s=delay / 1000)
        return done("WAITING_QUOTA_RESET", "rate_limit_no_retry_after")
    if status in (500, 502, 503, 504, 529) or err in ("overloaded", "server_error") or last.get("no_response"):
        return done("transient", err or f"http_{status}")
    if sub in ("error_max_turns", "error_during_execution"):
        return {**out, "error_code": sub}  # agent ran and stopped; outcome is still gradable
    if res is None:
        return done("transient", "no_result")  # crash: no result message
    return done("FAILED_PERMANENT", err or sub or "unknown")


def classify_error(e=None, exit_code=None, stderr=""):
    """Runtime classifier: exception (with .parsed/.exit_code) or parsed dict -> state string or None."""
    if isinstance(e, BaseException):
        return classify(getattr(e, "parsed", None), getattr(e, "exit_code", exit_code), getattr(e, "stderr", stderr))["state"]
    return classify(e, exit_code, stderr)["state"]


# ---- llm_calls mapping --------------------------------------------------------------------------
def _usage_rows(res):
    """result.modelUsage -> [(model, usage dict)] or [] when unavailable (missing or crash zeros, RES-10)."""
    mu = (res or {}).get("modelUsage") or {}
    rows = []
    for model, u in mu.items():
        keys = ("inputTokens", "cacheCreationInputTokens", "cacheReadInputTokens", "outputTokens")
        if any(u.get(k) is None for k in keys):
            continue
        rows.append((model, u))
    if rows and all(sum(u[k] for k in ("inputTokens", "cacheCreationInputTokens", "cacheReadInputTokens",
                                       "outputTokens")) == 0 for _, u in rows):
        return []  # 19.3 rule 5: crash right after start reports zeros -> unavailable
    return rows


def total_input(row):
    """19.3 rule 1: uncached + cache write + cache read (each counted once). None if unavailable."""
    parts = (row.get("input_tokens"), row.get("cache_write_tokens"), row.get("cached_input_tokens"))
    return None if any(v is None for v in parts) else sum(parts)


def calls_from_transcript(parsed, *, run_id, ctx, attempt_base=0, latency_ms=None, status=None, error_code=None):
    """-> list of llm_calls row dicts. One row per api_retry (no usage) + one per model in modelUsage.

    ctx: stage, agent_role, action, skill_id, skill_hash, auth_profile, model_id, claude_code_version, arm,
    step_id, checkpoint_id, project_id.
    """
    res = parsed.get("result")
    ts = now()
    skill_called = 1 if parsed.get("skill_calls") else 0
    base = {
        "timestamp_utc": ts, "run_id": run_id, "step_id": ctx.get("step_id"), "stage": ctx.get("stage", "execute"),
        "agent_role": ctx.get("agent_role", "executor"), "action": ctx.get("action"),
        "skill_id": ctx.get("skill_id") or "NO_SKILL", "skill_hash": ctx.get("skill_hash"),
        "project_id": ctx.get("project_id"), "provider": "anthropic", "auth_profile": ctx.get("auth_profile"),
        "call_path": "claude_cli", "session_id": parsed.get("session_id"),
        "claude_code_version": ctx.get("claude_code_version") or (parsed.get("init") or {}).get("claude_code_version"),
        "arm": ctx.get("arm"), "skill_tool_called": skill_called, "num_turns": (res or {}).get("num_turns"),
        "reasoning_tokens": None, "prompt_parts_estimate_json": None, "price_version": None, "currency": "USD",
        "cache_hit": 0, "reused_artifact": 0, "checkpoint_id": ctx.get("checkpoint_id"), "request_id": None,
    }
    rows = []
    attempt = attempt_base
    for r in parsed.get("retries") or []:  # RES-11: every failed attempt is its own ledger line
        attempt += 1
        rows.append({**base, "attempt_id": attempt, "model_id": ctx.get("model_id"), "usage_source": "unavailable",
                     "input_tokens": None, "cache_write_tokens": None, "cached_input_tokens": None,
                     "output_tokens": None, "provider_usage_json": json.dumps(redact(r), ensure_ascii=False),
                     "latency_ms": r.get("retry_delay_ms"), "status": "retried",
                     "error_code": r.get("error") or (f"http_{r['error_status']}" if r.get("error_status") else None),
                     "estimated_cost": None})
    attempt += 1
    final_status = status or ("success" if res and not res.get("is_error") else "error")
    raw = redact({k: (res or {}).get(k) for k in ("subtype", "is_error", "api_error_status", "num_turns",
                                                  "total_cost_usd", "usage", "modelUsage", "duration_ms",
                                                  "duration_api_ms", "stop_reason", "terminal_reason")})
    usage = _usage_rows(res)
    if not usage:
        cost = (res or {}).get("total_cost_usd")
        rows.append({**base, "attempt_id": attempt, "model_id": ctx.get("model_id"), "usage_source": "unavailable",
                     "input_tokens": None, "cache_write_tokens": None, "cached_input_tokens": None,
                     "output_tokens": None, "provider_usage_json": json.dumps(raw, ensure_ascii=False),
                     "latency_ms": latency_ms, "status": final_status, "error_code": error_code,
                     "estimated_cost": cost if cost else None})
    for model, u in usage:
        rows.append({**base, "attempt_id": attempt, "model_id": model, "usage_source": "provider",
                     "input_tokens": u["inputTokens"], "cache_write_tokens": u["cacheCreationInputTokens"],
                     "cached_input_tokens": u["cacheReadInputTokens"], "output_tokens": u["outputTokens"],
                     "provider_usage_json": json.dumps(raw, ensure_ascii=False), "latency_ms": latency_ms,
                     "status": final_status, "error_code": error_code,
                     "estimated_cost": u.get("costUSD")})  # client estimate (rule 4), not billing
    for r in rows:
        suffix = "" if r["status"] == "retried" or len(usage) <= 1 else ":" + r["model_id"]
        r["idempotency_key"] = f"{run_id}:{r['attempt_id']}{suffix}"
        r["call_id"] = f"{run_id}-a{r['attempt_id']}{suffix.replace(':', '-')}"
    return rows


COLS = ["call_id", "timestamp_utc", "run_id", "step_id", "attempt_id", "stage", "agent_role", "action", "skill_id",
        "skill_hash", "project_id", "provider", "auth_profile", "call_path", "model_id", "request_id", "session_id",
        "claude_code_version", "arm", "skill_tool_called", "num_turns", "usage_source", "input_tokens",
        "cache_write_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "provider_usage_json",
        "prompt_parts_estimate_json", "latency_ms", "status", "error_code", "estimated_cost", "price_version",
        "currency", "cache_hit", "reused_artifact", "idempotency_key", "checkpoint_id"]


def write_calls(conn, rows):
    """Insert ledger rows; a repeated idempotency_key is ignored (no double count on resume)."""
    for r in rows:
        conn.execute(f"INSERT OR IGNORE INTO llm_calls ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})",
                     [r.get(c) for c in COLS])
    return rows


def next_attempt(conn, run_id):
    return conn.execute("SELECT COALESCE(MAX(attempt_id),0) FROM llm_calls WHERE run_id=?", (run_id,)).fetchone()[0]


def run_cost(conn, run_id):
    """Sum of estimated cost over every attempt of a run (failed attempts included, OBS-06). None if all unknown."""
    return conn.execute("SELECT SUM(estimated_cost) FROM llm_calls WHERE run_id=?", (run_id,)).fetchone()[0]


def main(argv=None):
    """python -m lab.ledger <transcript.jsonl> [--db PATH] [--run-id ID]: map + print (and store) ledger rows."""
    import argparse
    from lab.db import connect
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript")
    ap.add_argument("--db")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--auth-profile", default="S")
    a = ap.parse_args(argv)
    p = read_transcript(a.transcript)
    rid = a.run_id or f"ingest-{p.get('session_id')}"
    rows = calls_from_transcript(p, run_id=rid, ctx={"auth_profile": a.auth_profile, "agent_role": "smoke",
                                                     "model_id": (p.get("init") or {}).get("model")})
    if a.db:
        write_calls(connect(a.db), rows)
    for r in rows:
        print(json.dumps({k: r[k] for k in COLS if k != "provider_usage_json"}, ensure_ascii=False))
        print("total_input_tokens =", total_input(r), "| classify:", classify(p)["state"])


if __name__ == "__main__":
    main()
