"""Operational alerts (PRD 21.2). Delivery = stored rows shown by `runtime status` / dashboard / API.
Nothing is sent externally (that needs user approval, PRD 24).

ALERT-01 soft budget 80% | ALERT-02 hard limit | ALERT-03 quota exhausted | ALERT-04 crash recovery /
repeated failure | ALERT-05 05:00 window end | ALERT-06 token spike vs same-step median.
"""
import datetime as dt
import statistics

UTC = dt.timezone.utc


def raise_alert(conn, kind, job_id, message, fingerprint=None, cooldown_s=3600, now=None):
    """-> True if raised (new, or cooldown over), False if suppressed as a duplicate.
    No own transaction: safe inside the caller's `with tx(conn)`."""
    now = now or dt.datetime.now(UTC)
    fp = fingerprint or f"{kind}:{job_id}"
    row = conn.execute("SELECT created_at FROM alerts WHERE fingerprint=?", (fp,)).fetchone()
    if row and (now - dt.datetime.fromisoformat(row[0])).total_seconds() < cooldown_s:
        conn.execute("UPDATE alerts SET occurrences=occurrences+1 WHERE fingerprint=?", (fp,))
        return False
    conn.execute("INSERT INTO alerts (job_id, kind, fingerprint, message, created_at) VALUES (?,?,?,?,?)"
                 " ON CONFLICT(fingerprint) DO UPDATE SET job_id=excluded.job_id, message=excluded.message,"
                 " created_at=excluded.created_at, delivered_at=NULL, occurrences=occurrences+1",
                 (job_id, kind, fp, message[:1000], now.astimezone(UTC).isoformat()))
    return True


def pending(conn):
    return conn.execute("SELECT * FROM alerts WHERE delivered_at IS NULL ORDER BY created_at, alert_id").fetchall()


def mark_delivered(conn, alert_ids, now=None):
    at = (now or dt.datetime.now(UTC)).astimezone(UTC).isoformat()
    conn.executemany("UPDATE alerts SET delivered_at=? WHERE alert_id=?", [(at, i) for i in alert_ids])


TOKENS = ("COALESCE(input_tokens,0)+COALESCE(output_tokens,0)+COALESCE(cache_write_tokens,0)"
          "+COALESCE(cached_input_tokens,0)")


def check_token_spikes(conn, factor=3.0, min_calls=3, now=None):
    """ALERT-06: a call whose total tokens > factor x median of calls with the same step_id.
    Calls with unavailable usage are skipped (never counted as 0). -> list of flagged call_ids.
    ponytail: full scan of llm_calls; add a time filter when the ledger gets large."""
    rows = conn.execute(f"SELECT call_id, run_id, step_id, skill_id, model_id, num_turns, cache_write_tokens,"
                        f" {TOKENS} AS total FROM llm_calls WHERE step_id IS NOT NULL AND usage_source != 'unavailable'"
                        f" AND input_tokens IS NOT NULL").fetchall()
    by_step = {}
    for r in rows:
        by_step.setdefault(r["step_id"], []).append(r)
    flagged = []
    for step, calls in by_step.items():
        if len(calls) < min_calls:
            continue
        med = statistics.median(c["total"] for c in calls)
        for c in calls:
            if med > 0 and c["total"] > factor * med:
                flagged.append(c["call_id"])
                raise_alert(conn, "ALERT-06", c["run_id"],
                            f"step {step}: call {c['call_id']} used {c['total']} tokens = {c['total'] / med:.1f}x "
                            f"median {med:.0f}; candidates: num_turns={c['num_turns']}, cache_write="
                            f"{c['cache_write_tokens']}, skill={c['skill_id']}, model={c['model_id']}",
                            fingerprint=f"ALERT-06:{c['call_id']}", now=now)
    return flagged
