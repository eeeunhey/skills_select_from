"""F8 ledger mapping (PRD 19.3 Claude rules, RES-10/11/17, OBS-02/06). Offline: hand-written stream-json lines
in the shape verified on Claude Code 2.1.327 (data/artifacts/smoke-claude-p.jsonl is a real one)."""
import json
from pathlib import Path

from lab import ledger
from lab.db import ROOT, connect

SID = "55815ac3-2426-40ee-a8d6-ad6e56429776"


def init(skills=("deep-research", "code-review"), plugins=(), model="claude-haiku-5-5"):
    return {"type": "system", "subtype": "init", "session_id": SID, "model": model, "tools": [], "mcp_servers": [],
            "claude_code_version": "2.1.327", "agents": ["claude", "Explore"], "skills": list(skills),
            "plugins": [{"name": "cc-plugin-telemetry", "path": "builtin"}, *plugins]}


def assistant(content, usage=None):
    return {"type": "assistant", "session_id": SID, "parent_tool_use_id": None,
            "message": {"id": "msg_1", "model": "claude-haiku-5-5", "role": "assistant", "content": content,
                        "usage": usage or {"input_tokens": 2, "cache_creation_input_tokens": 2844,
                                           "cache_read_input_tokens": 649, "output_tokens": 4}}}


def result(model_usage=None, cost=0.0123, subtype="success", is_error=False, status=None, text="OK", turns=1):
    r = {"type": "result", "subtype": subtype, "is_error": is_error, "api_error_status": status, "num_turns": turns,
         "result": text, "session_id": SID, "total_cost_usd": cost, "duration_ms": 996,
         "usage": {"input_tokens": 2, "cache_creation_input_tokens": 2844, "cache_read_input_tokens": 649,
                   "output_tokens": 34}, "permission_denials": []}
    if model_usage is not False:
        r["modelUsage"] = model_usage or {"claude-haiku-5-5": {
            "inputTokens": 180, "outputTokens": 420, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 2220,
            "costUSD": cost, "contextWindow": 1000000}}
    return r


def retry(n, error="overloaded", status=529, delay=1000):
    return {"type": "system", "subtype": "api_retry", "attempt": n, "max_retries": 10, "retry_delay_ms": delay,
            "error_status": status, "error": error, "session_id": SID, "uuid": f"u{n}"}


def lines(*msgs):
    return [json.dumps(m) for m in msgs]


CTX = {"auth_profile": "S", "model_id": "claude-haiku-5-5", "arm": "NO_SKILL"}


def test_usage_mapping_no_double_count():
    p = ledger.parse_stream(lines(init(), assistant([{"type": "text", "text": "OK"}]), result()))
    (row,) = ledger.calls_from_transcript(p, run_id="r1", ctx=CTX)
    assert (row["input_tokens"], row["cache_write_tokens"], row["cached_input_tokens"], row["output_tokens"]) == \
        (180, 2220, 0, 420)  # from result.modelUsage, not summed with per-message usage
    assert ledger.total_input(row) == 2400  # 19.4 example: 180 + 2220 + 0
    assert row["usage_source"] == "provider" and row["call_path"] == "claude_cli"
    assert row["estimated_cost"] == 0.0123 and row["session_id"] == SID and row["num_turns"] == 1


def test_missing_usage_is_unavailable_not_zero():  # RES-10
    for res in (result(model_usage=False), result(model_usage={"m": {"inputTokens": 0, "outputTokens": 0,
                "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0, "costUSD": 0}}, cost=0)):
        (row,) = ledger.calls_from_transcript(ledger.parse_stream(lines(init(), res)), run_id="r", ctx=CTX)
        assert row["usage_source"] == "unavailable"
        assert row["input_tokens"] is None and row["output_tokens"] is None
        assert row["estimated_cost"] == (res["total_cost_usd"] or None)  # reported estimate kept, 0 never invented
        assert ledger.total_input(row) is None
    # crash: no result line at all, plus a truncated last line
    p = ledger.parse_stream(lines(init()) + ['{"type":"assist'])
    (row,) = ledger.calls_from_transcript(p, run_id="r", ctx=CTX)
    assert row["usage_source"] == "unavailable" and p["bad_lines"] == 1


def test_three_retries_recorded_and_total_includes_failed_attempts():  # RES-11, OBS-06
    conn = connect(":memory:")
    p = ledger.parse_stream(lines(init(), retry(1), retry(2), retry(3), result(cost=0.05)))
    rows = ledger.write_calls(conn, ledger.calls_from_transcript(p, run_id="r3", ctx=CTX))
    got = conn.execute("SELECT attempt_id, status, usage_source FROM llm_calls WHERE run_id='r3' ORDER BY attempt_id").fetchall()
    assert [tuple(g) for g in got] == [(1, "retried", "unavailable"), (2, "retried", "unavailable"),
                                       (3, "retried", "unavailable"), (4, "success", "provider")]
    assert ledger.run_cost(conn, "r3") == 0.05  # result total already covers the failed attempts
    ledger.write_calls(conn, rows)  # resume re-writes same keys -> ignored (no double count)
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 4
    # a second process attempt for the same run continues numbering
    rows2 = ledger.calls_from_transcript(ledger.parse_stream(lines(init(), result(cost=0.01))), run_id="r3", ctx=CTX,
                                         attempt_base=ledger.next_attempt(conn, "r3"))
    ledger.write_calls(conn, rows2)
    assert rows2[0]["attempt_id"] == 5 and abs(ledger.run_cost(conn, "r3") - 0.06) < 1e-9


def test_error_classification():
    def c(*msgs, exit_code=1):
        return ledger.classify(ledger.parse_stream(lines(*msgs)), exit_code)

    plan_limit = result(is_error=True, text="Claude usage limit reached. Your limit resets at 5am", cost=0,
                        model_usage=False)
    assert c(init(), plan_limit)["state"] == "WAITING_QUOTA_RESET"  # RES-17
    rej = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1795105800}}
    r = c(init(), rej, result(is_error=True, text="x", model_usage=False))
    assert r["state"] == "WAITING_QUOTA_RESET" and r["reset_at"] == 1795105800
    assert c(init(), retry(1, "rate_limit", 429, 30000), result(is_error=True, status=429))["state"] == "WAITING_RATE_LIMIT"
    assert c(init(), retry(1, "rate_limit", 429, None), result(is_error=True, status=429))["state"] == "WAITING_QUOTA_RESET"
    assert c(init(), retry(1, "billing_error", 402), result(is_error=True, status=402))["state"] == "WAITING_QUOTA_RESET"
    assert c(init(), result(is_error=True, status=401))["state"] == "FAILED_PERMANENT"
    assert c(init(), result(is_error=True, status=403))["state"] == "FAILED_PERMANENT"
    assert c(init(), result(is_error=True, status=529))["state"] == "transient"
    assert c(init(), result(subtype="error_max_budget_usd", is_error=True))["state"] == "WAITING_BUDGET_APPROVAL"
    assert ledger.classify(ledger.parse_stream(lines(init())), 143)["state"] == "transient"  # SIGTERM -> rerun step
    assert c(init(), result(), exit_code=0)["state"] is None
    assert c(init(), result(subtype="error_max_turns", is_error=True))["state"] is None  # agent ran: still gradable
    # runtime-facing string API
    assert ledger.classify_error(ledger.parse_stream(lines(init(), plan_limit)), 1) == "WAITING_QUOTA_RESET"


def test_secrets_redacted():
    leak = retry(1)
    leak["detail"] = "key sk-ant-api03-AAAABBBBCCCCDDDD and Bearer abcdefghijkl for me@example.com"
    leak["api_key"] = "sk-whatever-1234567890"
    p = ledger.parse_stream(lines(init(), leak, result()))
    blob = json.dumps(ledger.calls_from_transcript(p, run_id="r", ctx=CTX))
    for s in ("sk-ant-api03", "abcdefghijkl", "me@example.com", "sk-whatever"):
        assert s not in blob
    assert "inputTokens" in blob  # usage keys containing 'token' survive


def test_isolation_leak_detection():  # RES-18
    ok, _ = ledger.isolation_check(init(skills=("deep-research", "demo:say-pong"),
                                        plugins=({"name": "demo", "path": "/tmp/demo"},)), "demo")
    assert ok == 1
    ok, leaks = ledger.isolation_check(init(skills=("deep-research", "my-personal-skill")), None)
    assert ok == 0 and leaks == ["skill:my-personal-skill"]
    ok, leaks = ledger.isolation_check(init(plugins=({"name": "ponytail", "path": "C:/Users/x/.claude/plugins/p"},)), None)
    assert ok == 0 and leaks == ["plugin:ponytail"]
    assert ledger.isolation_check(None)[0] == 0


def test_skill_tool_use_detected_once():
    tu = {"type": "tool_use", "id": "toolu_1", "name": "Skill", "input": {"skill": "demo:say-pong"}}
    p = ledger.parse_stream(lines(init(), assistant([tu]), assistant([tu]),
                                  assistant([{"type": "tool_use", "id": "toolu_2", "name": "Edit", "input": {}}]),
                                  result()))
    assert p["skill_calls"] == ["demo:say-pong"] and [t["name"] for t in p["tool_uses"]] == ["Edit"]


def test_real_smoke_transcript_parses():
    f = ROOT / "data" / "artifacts" / "smoke-claude-p.jsonl"
    if not f.exists():
        return
    p = ledger.read_transcript(f)
    (row,) = ledger.calls_from_transcript(p, run_id="smoke", ctx=CTX)
    assert row["usage_source"] == "provider" and row["input_tokens"] is not None
    assert ledger.classify(p, 0)["state"] is None and ledger.isolation_check(p["init"])[0] == 1
