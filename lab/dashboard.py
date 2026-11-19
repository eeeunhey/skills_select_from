"""Static ops dashboard (PRD 21.1 screen E + 7.1 screen A + 7.4 screen D): one self-contained HTML file,
inline CSS, no JS. Controls (pause/resume/cancel) live in the token-gated API, not here.

python -m lab.dashboard --out reports/dashboard.html
"""
import argparse
import datetime as dt
import html
import json
import sys
from pathlib import Path

from lab import alerts, budget
from lab.db import ROOT, connect
from lab.runtime import KST, _resume_hint, iso, load_runtime, night_key, parse, redact, utcnow, window_end

EST = '<span class="est">추정</span>'

USAGE_SQL = ("SELECT COUNT(*) calls, SUM(input_tokens) input, SUM(output_tokens) output,"
             " SUM(cache_write_tokens) cache_write, SUM(cached_input_tokens) cache_read,"
             " SUM(usage_source='unavailable') unavailable_calls, SUM(estimated_cost) est_cost,"
             " SUM(CASE WHEN status!='success' THEN estimated_cost END) failed_cost,"
             " SUM(status!='success') failed_calls FROM llm_calls WHERE timestamp_utc>=? AND timestamp_utc<?")


def usage_summary(conn, since, until):
    """Totals for llm_calls in [since, until). Token sums skip unavailable usage (never 0-filled)."""
    return dict(conn.execute(USAGE_SQL, (iso(since), iso(until))).fetchone())


def kst(s):
    return parse(s).astimezone(KST).strftime("%m-%d %H:%M KST") if s else "-"


def _table(headers, rows):
    if not rows:
        return '<p class="muted">없음 (no rows)</p>'
    h = "".join(f"<th>{html.escape(str(x))}</th>" for x in headers)
    b = "".join("<tr>" + "".join(f"<td>{c if isinstance(c, Raw) else html.escape('-' if c is None else str(c))}</td>"
                                 for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>"


class Raw(str):
    """Pre-escaped cell."""


def _usd(x):
    return "-" if x is None else f"${x:.4f}"


def render_html(conn, now=None, cfg=None, caps=None):
    now = now or utcnow()
    cfg = cfg or load_runtime()
    end = window_end(now, cfg["runtime"].get("work_until_local", "05:00"))
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
    tiles = [("실행 중", counts.get("RUNNING", 0)), ("할당량 대기", counts.get("WAITING_QUOTA_RESET", 0)),
             ("예산 대기", counts.get("WAITING_BUDGET_APPROVAL", 0)),
             ("05:00 마감", counts.get("PAUSED_WINDOW_CLOSED", 0)), ("완료", counts.get("COMPLETED", 0))]
    remaining_jobs = sum(n for s, n in counts.items() if s not in ("COMPLETED", "FAILED_PERMANENT", "CANCELLED"))
    s = []
    s.append('<section class="tiles">' + "".join(f'<div class="tile"><b>{n}</b><span>{html.escape(t)}</span></div>'
                                                 for t, n in tiles) + "</section>")
    s.append(f"<p>야간 창 종료 <b>{end:%Y-%m-%d %H:%M} KST</b> · 남은 작업 <b>{remaining_jobs}</b> · "
             f"생성 {now.astimezone(KST):%Y-%m-%d %H:%M KST}</p>")

    rows = []
    for j in conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50").fetchall():
        done, total = conn.execute("SELECT SUM(status='DONE'), COUNT(*) FROM job_steps WHERE job_id=?",
                                   (j["job_id"],)).fetchone()
        cp = conn.execute("SELECT step_id, created_at FROM checkpoints WHERE job_id=? ORDER BY created_at DESC,"
                          " rowid DESC LIMIT 1", (j["job_id"],)).fetchone()
        rows.append((j["job_id"], j["status"], f"{done or 0}/{total}",
                     f"{cp['step_id']} @ {kst(cp['created_at'])}" if cp else "-", kst(j["next_eligible_at"]),
                     _resume_hint(j, cfg)))
    s.append("<h2>진행률</h2>" + _table(["job", "status", "steps", "last checkpoint", "next eligible", "resumes"], rows))

    rem = budget.remaining(conn, night_key(end), caps)
    win = usage_summary(conn, end - dt.timedelta(days=1), end)
    tot = usage_summary(conn, dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc), now + dt.timedelta(days=3650))
    urow = lambda label, u: (label, u["calls"], u["input"], u["output"], u["cache_write"], u["cache_read"],
                             u["unavailable_calls"], Raw(f"{_usd(u['est_cost'])} {EST}"),
                             Raw(f"{_usd(u['failed_cost'])} {EST}"))
    s.append("<h2>사용량</h2>" + _table(
        ["범위", "calls", "input", "output", "cache write", "cache read", "usage 미확인 calls", "비용", "실패 시도 비용"],
        [urow("이번 야간 창 (전 05:00~05:00 KST)", win), urow("전체", tot)]))
    s.append(f"<p>야간 예산 {night_key(end)}: 사용 {_usd(rem['spent_usd'])} {EST}, runs {rem['spent_runs']} · 잔액 "
             f"{'USD 상한 없음' if rem['remaining_usd'] is None else _usd(rem['remaining_usd'])}, "
             f"runs {'-' if rem['remaining_runs'] is None else rem['remaining_runs']}</p>")

    top = conn.execute(f"SELECT step_id, skill_id, model_id, COUNT(*) n, SUM({alerts.TOKENS}) tokens,"
                       " SUM(estimated_cost) cost FROM llm_calls WHERE input_tokens IS NOT NULL"
                       " GROUP BY step_id, skill_id, model_id ORDER BY tokens DESC LIMIT 10").fetchall()
    s.append("<h2>토큰 소비 TOP 10 (단계·스킬·모델)</h2>" + _table(
        ["step", "skill", "model", "calls", "tokens", "비용 (추정)"],
        [(r["step_id"], r["skill_id"], r["model_id"], r["n"], r["tokens"], _usd(r["cost"])) for r in top]))

    srcs = conn.execute("SELECT id, enabled, last_success_at FROM sources ORDER BY id").fetchall()
    run = conn.execute("SELECT * FROM collection_runs ORDER BY id DESC LIMIT 1").fetchone()
    s.append("<h2>수집 상태</h2>" + _table(["source", "enabled", "last success"],
                                       [(r["id"], r["enabled"], kst(r["last_success_at"])) for r in srcs]))
    if run:
        c = json.loads(run["counts_json"] or "{}")
        s.append(f"<p>최근 수집 #{run['id']} {kst(run['started_at'])} → {kst(run['finished_at'])} · "
                 + html.escape(", ".join(f"{k}={v}" for k, v in c.items()) or "counts 없음")
                 + f" · GitHub API calls {run['github_api_calls']}</p>")
    for col, title in (("review_status", "검토 상태"), ("security_status", "보안 상태")):
        dist = conn.execute(f"SELECT {col}, COUNT(*) FROM skills WHERE status='active' GROUP BY {col} ORDER BY 2 DESC")
        s.append(f"<h3>{title}</h3>" + _table([col, "skills"], [tuple(r) for r in dist.fetchall()]))

    fc = conn.execute("SELECT category, COUNT(*), SUM(origin='human') FROM failure_cases GROUP BY category"
                      " ORDER BY 2 DESC").fetchall()
    s.append("<h2>실패 사례 (범주별)</h2>" + _table(["category", "cases", "human-confirmed"], [tuple(r) for r in fc]))

    al = conn.execute("SELECT * FROM alerts ORDER BY created_at DESC, alert_id DESC LIMIT 30").fetchall()
    s.append("<h2>알림</h2>" + _table(["kind", "job", "at", "repeats", "delivered", "message"],
                                     [(a["kind"], a["job_id"], kst(a["created_at"]), a["occurrences"],
                                       kst(a["delivered_at"]), redact(a["message"])) for a in al]))
    return PAGE.replace("{BODY}", "\n".join(s))


PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lab Night Ops</title>
<style>
:root{--bg:#fafaf9;--fg:#1c1917;--muted:#78716c;--line:#e7e5e4;--tile:#fff;--accent:#b45309}
@media (prefers-color-scheme:dark){:root{--bg:#1c1917;--fg:#f5f5f4;--muted:#a8a29e;--line:#44403c;--tile:#292524;--accent:#f59e0b}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif;max-width:1200px}
h1{font-size:20px;margin:0 0 12px}h2{font-size:16px;margin:24px 0 8px}h3{font-size:14px;margin:12px 0 4px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px}
.tile{background:var(--tile);border:1px solid var(--line);border-radius:6px;padding:10px}
.tile b{display:block;font-size:22px}.tile span{color:var(--muted)}
table{border-collapse:collapse;width:100%;display:block;overflow-x:auto}
th,td{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600}.muted{color:var(--muted)}
.est{font-size:11px;color:var(--accent);border:1px solid var(--accent);border-radius:3px;padding:0 3px}
</style></head><body><h1>야간 작업 관제판</h1>
{BODY}
<p class="muted">비용은 클라이언트 추정치(total_cost_usd)이며 청구 금액이 아니다. 정적 스냅샷: 새로 고치려면 다시 생성.</p>
</body></html>
"""


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m lab.dashboard")
    p.add_argument("--db", default=None)
    p.add_argument("--out", default=str(ROOT / "reports" / "dashboard.html"))
    a = p.parse_args(argv)
    conn = connect(a.db) if a.db else connect()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(conn), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    sys.exit(main())
