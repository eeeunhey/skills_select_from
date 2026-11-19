"""Daily SKILL.md collector: registered GitHub repos -> data/skills.json + daily report.

External SKILL.md text is untrusted DATA: only parsed with yaml.safe_load, never executed.
Token (optional) comes from env GITHUB_TOKEN (Actions secret), never written to disk.
"""
import datetime as dt
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SOURCES = DATA / "sources.json"
SKILLS = DATA / "skills.json"
RUNS = DATA / "runs.jsonl"
REPORTS = DATA / "reports"


def http_get(url, api=True):
    headers = {"User-Agent": "skill-selection-lab"}
    if api:
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        if os.environ.get("GITHUB_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        return r.read()


def parse_frontmatter(text):
    """Return dict from leading '---' YAML block; {} if missing/invalid."""
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        meta = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return {}
    return meta if isinstance(meta, dict) else {}


def _s(v):
    # ponytail: frontmatter values may be any YAML type; normalise to str/None for JSON + search
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)


def list_skill_files(repo):
    """Return (commit_sha, [(path, blob_sha)], warnings) via Git Trees API (2 API calls)."""
    head = json.loads(http_get(f"https://api.github.com/repos/{repo}/commits/HEAD"))  # default branch tip
    tree = json.loads(http_get(f"https://api.github.com/repos/{repo}/git/trees/{head['commit']['tree']['sha']}?recursive=1"))
    warnings = []
    if tree.get("truncated"):
        warnings.append(f"{repo}: git tree truncated, some SKILL.md may be missing")
    files = [(t["path"], t["sha"]) for t in tree["tree"]
             if t["type"] == "blob" and t["path"].split("/")[-1] == "SKILL.md"]
    return head["sha"], files, warnings


def build_record(repo, commit, path, blob_sha, text):
    meta = parse_frontmatter(text)
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    return {
        "skill_id": f"github:{repo}:{path}",
        "name": _s(meta.get("name")) or folder.split("/")[-1] or repo,
        "description": _s(meta.get("description")) or "",
        "source_url": f"https://github.com/{repo}/blob/{commit}/{path}",
        "repo": repo,
        "path": path,
        "author": repo.split("/")[0],
        "license": _s(meta.get("license")),
        "compatibility": _s(meta.get("compatibility")),
        "allowed_tools": _s(meta.get("allowed-tools")),
        "metadata": meta.get("metadata") if isinstance(meta.get("metadata"), dict) else None,
        "frontmatter_ok": bool(meta.get("name") and meta.get("description")),
        "blob_sha": blob_sha,
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def collect(sources, skills, now, fetch_list=list_skill_files, fetch_raw=None):
    """Mutates skills in place. Returns report dict. fetch_* injectable for tests."""
    fetch_raw = fetch_raw or (lambda repo, commit, path: http_get(
        f"https://raw.githubusercontent.com/{repo}/{commit}/{urllib.parse.quote(path)}", api=False).decode("utf-8", "replace"))
    report = {"run_at": now, "new": [], "changed": [], "removed": [], "warnings": [], "errors": [], "sources": {}}
    for src in sources:
        repo = src["repo"]
        try:
            commit, files, warns = fetch_list(repo)
        except (urllib.error.URLError, KeyError, ValueError, TimeoutError) as e:
            # ponytail: on repo failure we keep existing records untouched (no false "removed")
            report["errors"].append(f"{repo}: list failed: {e}")
            report["sources"][repo] = "error"
            continue
        report["warnings"] += warns
        seen = set()
        for path, blob_sha in files:
            sid = f"github:{repo}:{path}"
            seen.add(sid)
            old = skills.get(sid)
            if old and old["blob_sha"] == blob_sha and old.get("status") == "active":
                continue
            try:
                text = fetch_raw(repo, commit, path)
            except (urllib.error.URLError, TimeoutError) as e:
                report["errors"].append(f"{sid}: read failed: {e}")
                continue
            rec = build_record(repo, commit, path, blob_sha, text)
            if not rec["frontmatter_ok"]:
                report["warnings"].append(f"{sid}: frontmatter missing name/description")
            rec["first_seen"] = old["first_seen"] if old else now
            rec["changed_at"] = now
            rec["status"] = "active"
            rec["priority"] = src.get("priority", 1)
            skills[sid] = rec
            report["changed" if old else "new"].append(sid)
        for sid, rec in skills.items():
            if rec["repo"] == repo and rec.get("status") == "active" and sid not in seen:
                rec["status"] = "removed"
                rec["changed_at"] = now
                report["removed"].append(sid)
        report["sources"][repo] = f"ok ({len(files)} SKILL.md)"
    return report


def render_report(r, total_active):
    lines = [f"# Daily skill report — {r['run_at']}", "",
             f"active skills: {total_active} | new {len(r['new'])} | changed {len(r['changed'])} | removed {len(r['removed'])}", "",
             "## Sources"] + [f"- {k}: {v}" for k, v in r["sources"].items()]
    for key in ("new", "changed", "removed", "warnings", "errors"):
        lines += ["", f"## {key} ({len(r[key])})"] + [f"- {x}" for x in r[key]]
    return "\n".join(lines) + "\n"


def main():
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    sources = json.loads(SOURCES.read_text(encoding="utf-8"))["github_repos"]
    db = json.loads(SKILLS.read_text(encoding="utf-8")) if SKILLS.exists() else {"skills": {}}
    report = collect(sources, db["skills"], now)
    last = db.get("last_success")
    if last and dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(last) > dt.timedelta(hours=48):
        report["warnings"].append(f"last successful run was {last} (>48h): scheduled runs may have been skipped")
    db["updated_at"] = now
    db["last_success"] = now if not report["errors"] else db.get("last_success")
    SKILLS.write_text(json.dumps(db, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    REPORTS.mkdir(parents=True, exist_ok=True)
    active = sum(1 for s in db["skills"].values() if s.get("status") == "active")
    (REPORTS / f"{now[:10]}.md").write_text(render_report(report, active), encoding="utf-8")
    with RUNS.open("a", encoding="utf-8") as f:  # run log even when nothing changed
        f.write(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in report.items()} | {"errors": report["errors"]}, ensure_ascii=False) + "\n")
    print(render_report(report, active)[:2000])
    return 1 if report["errors"] and not any(v.startswith("ok") for v in report["sources"].values()) else 0


if __name__ == "__main__":
    sys.exit(main())
