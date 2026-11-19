"""Local REST API (PRD 22.3), stdlib only. Binds 127.0.0.1 by default.

GET  /jobs  /jobs/{id}  /jobs/{id}/usage  /jobs/{id}/events  /reports/nightly?date=YYYY-MM-DD  /dashboard
POST /jobs {"experiment_id", "approved_by"}   POST /jobs/{id}/pause|resume|cancel|approve-budget {"by"}
POST needs env LAB_API_TOKEN set and header `Authorization: Bearer <token>`; unset -> every POST is 403.
Audit: runtime transitions already write job_events with `by`.

python -m lab.api --port 8765
"""
import argparse
import datetime as dt
import hmac
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lab import dashboard, runtime
from lab.db import connect
from lab.runtime import KST, redact

ACTIONS = {"pause": runtime.pause, "resume": runtime.resume, "cancel": runtime.cancel,
           "approve-budget": runtime.approve_budget}


def _job(conn, jid):
    j = conn.execute("SELECT * FROM jobs WHERE job_id=?", (jid,)).fetchone()
    if j is None:
        return None
    steps = [dict(r) for r in conn.execute("SELECT step_id, seq, status, attempt_count, result_ref FROM job_steps"
                                           " WHERE job_id=? ORDER BY seq", (jid,))]
    cp = conn.execute("SELECT step_id, cursor, created_at FROM checkpoints WHERE job_id=? ORDER BY created_at DESC,"
                      " rowid DESC LIMIT 1", (jid,)).fetchone()
    return dict(j, payload=json.loads(j["payload_json"]), steps=steps, last_checkpoint=dict(cp) if cp else None,
                done=sum(s["status"] == "DONE" for s in steps), total=len(steps),
                resumes=runtime._resume_hint(j, runtime.load_runtime()))


def _usage(conn, jid):
    """llm_calls of a job: run_id = job_id, or an eval run of the job's experiment."""
    j = conn.execute("SELECT payload_json FROM jobs WHERE job_id=?", (jid,)).fetchone()
    exp = json.loads(j[0]).get("experiment_id")
    rows = conn.execute(
        "SELECT step_id, model_id, skill_id, attempt_id, status, COUNT(*) calls, SUM(input_tokens) input,"
        " SUM(output_tokens) output, SUM(cache_write_tokens) cache_write, SUM(cached_input_tokens) cache_read,"
        " SUM(usage_source='unavailable') unavailable_calls, SUM(estimated_cost) estimated_cost FROM llm_calls"
        " WHERE run_id=? OR run_id IN (SELECT run_id FROM eval_runs WHERE experiment_id=?)"
        " GROUP BY step_id, model_id, skill_id, attempt_id, status ORDER BY step_id, attempt_id", (jid, exp))
    return {"job_id": jid, "cost_note": "estimated_cost is a client-side estimate, not billing",
            "rows": [dict(r) for r in rows]}


def _nightly(conn, date):
    """Night `date` (KST) = window ending date+1 05:00 KST."""
    end = dt.datetime.combine(dt.date.fromisoformat(date) + dt.timedelta(days=1), dt.time(5), KST)
    jobs = conn.execute("SELECT job_id, status FROM jobs WHERE window_end_at=?", (runtime.iso(end),)).fetchall()
    return {"date": date, "window_end_kst": end.isoformat(),
            "completed": [r[0] for r in jobs if r[1] == "COMPLETED"],
            "incomplete": [{"job_id": r[0], "status": r[1]} for r in jobs if r[1] != "COMPLETED"],
            "usage_estimated": dashboard.usage_summary(conn, end - dt.timedelta(days=1), end),
            "budget": runtime.budget.remaining(conn, date),
            "alerts": [dict(a) for a in conn.execute("SELECT kind, job_id, message, created_at FROM alerts WHERE"
                                                     " created_at>=? AND created_at<?",
                                                     (runtime.iso(end - dt.timedelta(days=1)), runtime.iso(end)))]}


def make_server(db_path, host="127.0.0.1", port=8765, token=None):
    """token=None -> env LAB_API_TOKEN. Each request opens its own connection (sqlite is per-thread)."""
    token = token if token is not None else os.environ.get("LAB_API_TOKEN")

    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else \
                json.dumps(redact(body), ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):  # keep stderr quiet; the audit trail is job_events
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            conn = connect(db_path)
            try:
                if parts == ["jobs"]:
                    return self._send(200, [dict(r) for r in conn.execute(
                        "SELECT j.job_id, j.kind, j.status, j.next_eligible_at, j.lease_owner, j.updated_at,"
                        " (SELECT COUNT(*) FROM job_steps s WHERE s.job_id=j.job_id AND s.status='DONE') done,"
                        " (SELECT COUNT(*) FROM job_steps s WHERE s.job_id=j.job_id) total"
                        " FROM jobs j ORDER BY j.created_at")])
                if parts == ["dashboard"]:
                    return self._send(200, dashboard.render_html(conn), "text/html; charset=utf-8")
                if parts == ["reports", "nightly"]:
                    date = urllib.parse.parse_qs(u.query).get("date", [None])[0]
                    try:
                        return self._send(200, _nightly(conn, date))
                    except (TypeError, ValueError):
                        return self._send(400, {"error": "date=YYYY-MM-DD required"})
                if len(parts) in (2, 3) and parts[0] == "jobs":
                    job = _job(conn, parts[1])
                    if job is None:
                        return self._send(404, {"error": "no such job"})
                    if len(parts) == 2:
                        return self._send(200, job)
                    if parts[2] == "usage":
                        return self._send(200, _usage(conn, parts[1]))
                    if parts[2] == "events":
                        return self._send(200, [dict(r, payload=json.loads(r["payload_redacted"] or "null")
                                                     if (r["payload_redacted"] or "").startswith(("{", "[")) else
                                                     r["payload_redacted"]) for r in conn.execute(
                            "SELECT * FROM job_events WHERE job_id=? ORDER BY event_id", (parts[1],))])
                self._send(404, {"error": "not found"})
            finally:
                conn.close()

        def do_POST(self):
            auth = self.headers.get("Authorization", "")
            if not token or not hmac.compare_digest(auth.encode(), f"Bearer {token}".encode()):
                return self._send(403, {"error": "POST disabled (LAB_API_TOKEN unset) or bad token"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self._send(400, {"error": "JSON body required"})
            parts = [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]
            conn = connect(db_path)
            try:
                if parts == ["jobs"]:
                    if not body.get("approved_by") or not body.get("experiment_id"):
                        return self._send(400, {"error": "experiment_id and approved_by required"})
                    jid = runtime.enqueue_experiment(conn, body["experiment_id"], body["approved_by"])
                    return self._send(201, {"job_id": jid})
                if len(parts) == 3 and parts[0] == "jobs" and parts[2] in ACTIONS:
                    if not body.get("by"):
                        return self._send(400, {"error": '"by" required'})
                    ACTIONS[parts[2]](conn, parts[1], f"api:{body['by']}")
                    return self._send(200, {"job_id": parts[1], "action": parts[2], "by": body["by"]})
                self._send(404, {"error": "not found"})
            except ValueError as e:
                self._send(404 if str(e).startswith("no job") else 409, {"error": str(e)})
            finally:
                conn.close()

    return ThreadingHTTPServer((host, port), H)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m lab.api")
    p.add_argument("--db", default=None)
    p.add_argument("--host", default="127.0.0.1", help="keep 127.0.0.1 unless you add real auth/TLS")
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args(argv)
    srv = make_server(a.db or runtime.ROOT / "data" / "lab.db", a.host, a.port)
    print(f"http://{a.host}:{srv.server_port}  (POST {'enabled' if os.environ.get('LAB_API_TOKEN') else 'disabled'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
