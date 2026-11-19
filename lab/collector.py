"""Daily SKILL.md collector (PRD F1, 5.2): GitHub repos in config/sources.yaml -> SQLite catalog.

Per repo: 2 API calls (commits/HEAD + git/trees?recursive=1); SKILL.md content via raw.githubusercontent
(not API-metered), only when the blob sha changed. A failed repo never marks its skills removed.
External text is untrusted DATA (yaml.safe_load + regex only). GITHUB_TOKEN is read from env, never stored.
No LLM calls here (OBS-12): github_api_calls is infra usage only.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from lab import db
from lab.scan import parse_frontmatter, scan

ANALYSIS_VERSION = "scan-v1"
MAX_BYTES = 512 * 1024
NON_CLAUDE_NOTE = "non-Claude source; unverified until executed in Claude Code"
API_CALLS = [0]  # ponytail: process-wide counter; collect() reports the delta per run


class RateLimited(Exception):
    pass


def http_get(url, api=True):
    headers = {"User-Agent": "skill-selection-lab"}
    if api:
        API_CALLS[0] += 1
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        if os.environ.get("GITHUB_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        return r.read()


def github_tree(repo):
    """-> (commit_sha, {path: (blob_sha, size)}, truncated)"""
    head = json.loads(http_get(f"https://api.github.com/repos/{repo}/commits/HEAD"))
    tree = json.loads(http_get(f"https://api.github.com/repos/{repo}/git/trees/{head['commit']['tree']['sha']}?recursive=1"))
    blobs = {t["path"]: (t["sha"], t.get("size", 0)) for t in tree["tree"] if t["type"] == "blob"}
    return head["sha"], blobs, bool(tree.get("truncated"))


def github_raw(repo, ref, path):
    return http_get(f"https://raw.githubusercontent.com/{repo}/{ref}/{urllib.parse.quote(path)}", api=False).decode("utf-8", "replace")


def retry(fn, sleep, tries=4):
    """5xx / network errors: up to 3 retries with backoff. 403/429: RateLimited, no retry."""
    for i in range(tries):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                raise RateLimited(f"HTTP {e.code} (rate limit / forbidden)") from e
            if e.code < 500 or i == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if i == tries - 1:
                raise
        sleep(2 ** i)


def git_blob_sha(text):
    b = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest()


def _s(v):
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)


def _under(paths, folder):
    if not folder:
        return list(paths)
    return [p[len(folder) + 1:] for p in paths if p.startswith(folder + "/")]


def locate(path, blobs, repo):
    """-> (distribution, plugin_name, plugin_root|None) for a SKILL.md path."""
    parts = path.split("/")
    if len(parts) >= 3 and parts[-3] == "skills":
        root = "/".join(parts[:-3])
        if (root + "/" if root else "") + ".claude-plugin/plugin.json" in blobs:
            return "plugin", parts[-4] if len(parts) >= 4 else repo.split("/")[-1], root
    return "standalone", None, None


def _hints(meta):
    m = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    return " ".join(_s(x) for x in (meta.get("when_to_use"), meta.get("compatibility"), meta.get("tags"), m.get("tags")) if x)


def _write_skill(conn, src, repo, commit, path, blob_sha, text, blobs, now, counts):
    """One skill = one transaction (RES-01: a crash loses at most the skill in flight, never duplicates)."""
    sid = f"github:{repo}:{path}"
    meta, _ = parse_frontmatter(text)
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    dist, plugin, proot = locate(path, blobs, repo)
    result = scan(text, _under(blobs, folder), _under(blobs, proot) if proot is not None else ())
    name = _s(meta.get("name")) or folder.split("/")[-1] or repo
    desc = _s(meta.get("description")) or ""
    chash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    old = conn.execute("SELECT * FROM skills WHERE id=?", (sid,)).fetchone()
    first_seen = old["first_seen"] if old else now
    with db.tx(conn):
        near = [r[0] for r in conn.execute(
            "SELECT id FROM skills WHERE lower(name)=lower(?) AND description=? AND repo<>? AND status='active'",
            (name, desc, repo))]
        metadata = {"frontmatter": json.loads(json.dumps(meta, default=str)),
                    "source_url": f"https://github.com/{repo}/blob/{commit}/{path}",
                    "distribution": dist, "plugin_name": plugin, "bytes": len(text.encode("utf-8")),
                    "near_duplicates": near}
        if not src["claude_native"]:
            metadata["compatibility_note"] = NON_CLAUDE_NOTE
        v = conn.execute("SELECT id FROM skill_versions WHERE skill_id=? AND blob_sha=?", (sid, blob_sha)).fetchone()
        if old is None:
            conn.execute("INSERT INTO skills (id, source_id, repo, path, name, distribution, first_seen, last_seen, last_changed)"
                         " VALUES (?,?,?,?,?,?,?,?,?)", (sid, src["id"], repo, path, name, dist, now, now, now))
        if v:  # reverted to an earlier blob: reuse that version (append-only)
            vid = v[0]
        else:
            vid = conn.execute(
                "INSERT INTO skill_versions (skill_id, commit_sha, blob_sha, content_hash, fetched_at, metadata_json,"
                " scan_json, analysis_version) VALUES (?,?,?,?,?,?,?,?)",
                (sid, commit, blob_sha, chash, now, json.dumps(metadata, ensure_ascii=False),
                 json.dumps(result), ANALYSIS_VERSION)).lastrowid
        dup = conn.execute(
            "SELECT s.id FROM skills s JOIN skill_versions v ON v.id=s.current_version_id"
            " WHERE v.content_hash=? AND s.id<>? AND s.status='active' AND s.first_seen<=?"
            " ORDER BY s.first_seen, s.id LIMIT 1", (chash, sid, first_seen)).fetchone()
        conn.execute(
            "UPDATE skills SET name=?, description=?, license=?, distribution=?, plugin_name=?, status='active',"
            " moved_to=NULL, duplicate_of=?, security_status=?, current_version_id=?, last_seen=?, last_changed=?"
            " WHERE id=?", (name, desc, _s(meta.get("license")), dist, plugin, dup[0] if dup else None,
                            result["security_status"], vid, now, now, sid))
        conn.execute("DELETE FROM skills_fts WHERE skill_id=?", (sid,))
        conn.execute("INSERT INTO skills_fts (skill_id, name, description, path, hints) VALUES (?,?,?,?,?)",
                     (sid, name, desc, path, _hints(meta)))
    counts["changed" if old and old["status"] == "active" else "readded" if old else "new"] += 1
    counts["duplicates"] += bool(dup)
    return result


def _ingest(conn, src, repo, commit, blobs, get_text, now, counts, report, sleep):
    """Process one repo's SKILL.md files. -> set of seen skill ids. Raises RateLimited to stop the repo."""
    seen = set()
    for path, (blob_sha, size) in sorted(blobs.items()):
        if path.rsplit("/", 1)[-1] != "SKILL.md":
            continue
        sid = f"github:{repo}:{path}"
        seen.add(sid)
        cur = conn.execute("SELECT s.status, v.blob_sha FROM skills s LEFT JOIN skill_versions v"
                           " ON v.id=s.current_version_id WHERE s.id=?", (sid,)).fetchone()
        if cur and cur["blob_sha"] == blob_sha and cur["status"] == "active":
            conn.execute("UPDATE skills SET last_seen=? WHERE id=?", (now, sid))
            counts["unchanged"] += 1
            continue
        if size > MAX_BYTES:
            report["warnings"].append(f"{sid}: {size} bytes > {MAX_BYTES} (abnormal size), skipped")
            continue
        try:
            text = retry(lambda: get_text(path), sleep)
        except RateLimited:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            report["errors"].append(f"{sid}: read failed: {e}")
            continue
        r = _write_skill(conn, src, repo, commit, path, blob_sha, text, blobs, now, counts)
        if not r["frontmatter_parse_ok"]:
            report["warnings"].append(f"{sid}: frontmatter missing or unparseable (Claude loads body only)")
    return seen


def _upsert_source(conn, sid, url, typ, priority, native):
    conn.execute("INSERT INTO sources (id, url, type, priority, claude_native) VALUES (?,?,?,?,?)"
                 " ON CONFLICT(id) DO UPDATE SET url=excluded.url, priority=excluded.priority,"
                 " claude_native=excluded.claude_native", (sid, url, typ, priority, int(native)))


def collect(conn, sources, now, fetch_tree=github_tree, fetch_raw=github_raw, sleep=time.sleep):
    """sources: parsed config/sources.yaml. Returns report dict (also stored in collection_runs)."""
    calls0 = API_CALLS[0]
    run_id = conn.execute("INSERT INTO collection_runs (started_at) VALUES (?)", (now,)).lastrowid
    counts = dict.fromkeys(("new", "changed", "readded", "removed", "moved", "duplicates", "unchanged"), 0)
    report = {"run_at": now, "sources": {}, "warnings": [], "errors": [], "removed": [], "moved": []}
    ok_repos = []
    seen = set()
    for s in sources.get("github_repos") or []:
        repo = s["repo"]
        src = {"id": f"github:{repo}", "claude_native": bool(s.get("claude_native"))}
        _upsert_source(conn, src["id"], f"https://github.com/{repo}", "github_repo", s.get("priority", 1), src["claude_native"])
        last = conn.execute("SELECT last_success_at FROM sources WHERE id=?", (src["id"],)).fetchone()[0]
        if last and dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(last) > dt.timedelta(hours=48):
            report["warnings"].append(f"{repo}: last success {last} is older than 48h (scheduled runs skipped?)")
        info = {"last_success_at": last}
        report["sources"][repo] = info
        try:
            commit, blobs, truncated = retry(lambda: fetch_tree(repo), sleep)
            if truncated:
                report["warnings"].append(f"{repo}: git tree truncated, some SKILL.md may be missing (scope incomplete)")
            info.update(commit=commit, skill_files=sum(p.rsplit("/", 1)[-1] == "SKILL.md" for p in blobs), truncated=truncated)
            seen |= _ingest(conn, src, repo, commit, blobs, lambda p: fetch_raw(repo, commit, p), now, counts, report, sleep)
        except (RateLimited, urllib.error.URLError, TimeoutError, ConnectionError, KeyError, ValueError) as e:
            report["errors"].append(f"{repo}: {type(e).__name__}: {e} (existing records kept)")
            info["status"] = "error"
            continue
        info.update(status="ok", last_success_at=now)
        conn.execute("UPDATE sources SET scan_cursor=?, last_success_at=? WHERE id=?", (commit, now, src["id"]))
        if not truncated:  # an incomplete listing must never mark existing skills removed/moved
            ok_repos.append(repo)

    for m in sources.get("manual") or []:  # {url: https://github.com/<o>/<r>/blob/<ref>/<path>/SKILL.md}
        url = m["url"]
        hit = re.match(r"https://github\.com/([^/]+/[^/]+)/blob/([^/]+)/(.+/SKILL\.md|SKILL\.md)$", url)
        if not hit:
            report["errors"].append(f"manual {url}: not a github blob URL to a SKILL.md")
            continue
        repo, ref, path = hit.groups()
        src = {"id": f"manual:{repo}/{path}", "claude_native": bool(m.get("claude_native"))}
        _upsert_source(conn, src["id"], url, "manual", m.get("priority", 4), src["claude_native"])
        try:
            text = retry(lambda: fetch_raw(repo, ref, path), sleep)
            # ponytail: no tree for manual links -> scan sees SKILL.md only (no scripts/hooks/.mcp.json check)
            _ingest(conn, src, repo, ref, {path: (git_blob_sha(text), len(text.encode("utf-8")))},
                    lambda p: text, now, counts, report, sleep)
        except (RateLimited, urllib.error.URLError, TimeoutError, ConnectionError) as e:
            report["errors"].append(f"manual {url}: {e}")
            continue
        conn.execute("UPDATE sources SET scan_cursor=?, last_success_at=? WHERE id=?", (ref, now, src["id"]))
        report["sources"][url] = {"status": "ok", "commit": ref, "skill_files": 1, "last_success_at": now}

    # Removed / moved: only for repos whose tree was read successfully this run.
    for repo in ok_repos:
        gone = conn.execute("SELECT s.id, v.content_hash FROM skills s LEFT JOIN skill_versions v ON v.id=s.current_version_id"
                            " WHERE s.repo=? AND s.status='active' AND s.source_id=?", (repo, f"github:{repo}")).fetchall()
        for sid, chash in gone:
            if sid in seen:
                continue
            to = conn.execute(
                "SELECT s.id FROM skills s JOIN skill_versions v ON v.id=s.current_version_id"
                " WHERE v.content_hash=? AND s.first_seen=? AND s.id<>? AND s.status='active' LIMIT 1",
                (chash, now, sid)).fetchone()
            with db.tx(conn):
                if to:
                    conn.execute("UPDATE skills SET status='moved', moved_to=?, last_changed=? WHERE id=?", (to[0], now, sid))
                    # the new path is a move, not a duplicate of the old one
                    counts["duplicates"] -= conn.execute("UPDATE skills SET duplicate_of=NULL WHERE id=? AND duplicate_of=?",
                                                         (to[0], sid)).rowcount
                    report["moved"].append(f"{sid} -> {to[0]}")
                else:
                    conn.execute("UPDATE skills SET status='removed', last_changed=? WHERE id=?", (now, sid))
                    report["removed"].append(sid)
                conn.execute("DELETE FROM skills_fts WHERE skill_id=?", (sid,))
            counts["moved" if to else "removed"] += 1

    report["counts"] = counts
    report["github_api_calls"] = API_CALLS[0] - calls0
    conn.execute("UPDATE collection_runs SET finished_at=?, counts_json=?, errors_json=?, github_api_calls=? WHERE id=?",
                 (db.now(), json.dumps(counts), json.dumps({"errors": report["errors"], "warnings": report["warnings"]},
                                                           ensure_ascii=False), report["github_api_calls"], run_id))
    return report


def render_report(conn, r):
    c = r["counts"]
    status = dict(conn.execute("SELECT status, count(*) FROM skills GROUP BY status").fetchall())
    sec = dict(conn.execute("SELECT security_status, count(*) FROM skills WHERE status='active' GROUP BY 1").fetchall())
    lines = [f"# Collect report — {r['run_at']}", "",
             "LLM calls: 0 (collection never calls an LLM). GitHub API calls: %d (infra usage, OBS-12)." % r["github_api_calls"], "",
             "| counts | " + " | ".join(c) + " |", "|---" * (len(c) + 1) + "|", "| | " + " | ".join(str(v) for v in c.values()) + " |", "",
             f"Catalog: {status}. Active security_status: {sec}.", "", "## Sources (scan scope)"]
    for k, i in r["sources"].items():
        lines.append(f"- {k}: {i.get('status')} | commit {i.get('commit', '-')} | SKILL.md files {i.get('skill_files', '-')}"
                     f" | truncated {i.get('truncated', False)} | last success {i.get('last_success_at')}")
    lines.append("- scope: only the repos/links listed above (not all of GitHub; PRD F1 note)")
    for key in ("removed", "moved", "warnings", "errors"):
        lines += ["", f"## {key} ({len(r[key])})"] + [f"- {x}" for x in r[key]]
    return "\n".join(lines) + "\n"


def export(conn):
    """JSON catalog export (PRD 5.4): sources + every skill with its current version's metadata and scan."""
    rows = conn.execute("SELECT s.*, v.commit_sha, v.blob_sha, v.content_hash, v.metadata_json, v.scan_json, v.analysis_version"
                        " FROM skills s LEFT JOIN skill_versions v ON v.id=s.current_version_id ORDER BY s.id").fetchall()
    skills = []
    for r in rows:
        d = {k: r[k] for k in r.keys() if k not in ("metadata_json", "scan_json")}
        d["metadata"], d["scan"] = json.loads(r["metadata_json"] or "{}"), json.loads(r["scan_json"] or "{}")
        d["source_url"] = d["metadata"].get("source_url")
        skills.append(d)
    return {"generated_at": db.now(), "sources": [dict(r) for r in conn.execute("SELECT * FROM sources ORDER BY id")],
            "skills": skills}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m lab.collector", description="Collect SKILL.md from config/sources.yaml")
    ap.add_argument("--db", default=str(db.DB_PATH))
    ap.add_argument("--sources", default=str(db.ROOT / "config" / "sources.yaml"))
    ap.add_argument("--export", help="write JSON catalog, e.g. data/skills.json")
    ap.add_argument("--reports", default=str(db.ROOT / "data" / "reports"))
    a = ap.parse_args(argv)
    conn = db.connect(a.db)
    sources = yaml.safe_load(Path(a.sources).read_text(encoding="utf-8")) or {}
    r = collect(conn, sources, db.now())
    text = render_report(conn, r)
    Path(a.reports).mkdir(parents=True, exist_ok=True)
    (Path(a.reports) / f"collect-{r['run_at'][:10]}.md").write_text(text, encoding="utf-8")
    if a.export:
        Path(a.export).parent.mkdir(parents=True, exist_ok=True)
        Path(a.export).write_text(json.dumps(export(conn), ensure_ascii=False, indent=1), encoding="utf-8")
    conn.close()  # checkpoints WAL into lab.db so the committed file is complete
    print(text[:3000])
    return 1 if r["sources"] and all(i.get("status") != "ok" for i in r["sources"].values()) else 0


if __name__ == "__main__":
    sys.exit(main())
