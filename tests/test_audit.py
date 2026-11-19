"""Selection audit: failure classification + negation analyzer (offline, fake catalog)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab import audit, db, finder, selector  # noqa: E402


def add(conn, repo, path, name, desc):
    sid = f"github:{repo}:{path}"
    conn.execute("INSERT OR IGNORE INTO sources (id, url, type, claude_native) VALUES (?,?, 'github_repo', 1)",
                 (f"github:{repo}", f"https://github.com/{repo}"))
    conn.execute("INSERT INTO skills (id, source_id, repo, path, name, description, distribution, license, first_seen,"
                 " last_seen, last_changed) VALUES (?,?,?,?,?,?, 'standalone', 'MIT', 't','t','t')",
                 (sid, f"github:{repo}", repo, path, name, desc))
    conn.execute("INSERT INTO skill_versions (skill_id, commit_sha, blob_sha, content_hash, fetched_at, metadata_json,"
                 " scan_json, analysis_version) VALUES (?, 'c', ?, 'h', 't', '{}', ?, 'scan-v1')",
                 (sid, sid, json.dumps({"frontmatter_parse_ok": True, "spec_fields_only": True})))
    conn.execute("INSERT INTO skills_fts (skill_id, name, description, path, hints) VALUES (?,?,?,?, '')",
                 (sid, name, desc, path))


def test_classification_and_metrics(tmp_path, monkeypatch):
    conn = db.connect(":memory:")
    add(conn, "o/r", "web/SKILL.md", "webapp-testing", "Test web apps in a real browser with Playwright: click buttons")
    add(conn, "o/r", "xl/SKILL.md", "xlsx", "Create Excel spreadsheet files")
    tasks = [
        {"task_id": "A", "query": "브라우저 버튼 클릭 테스트", "relevant_skills": [{"repo": "o/r", "path": "web/SKILL.md"}]},
        {"task_id": "B", "query": "파일 이름 바꾸기", "relevant_skills": [], "acceptable_no_skill": True},
        {"task_id": "C", "query": "weather forecast", "relevant_skills": [{"repo": "o/r", "path": "nope/SKILL.md"}]},
    ]
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(json.dumps(t, ensure_ascii=False) for t in tasks), encoding="utf-8")
    monkeypatch.setitem(audit.SPLITS, "labeled", f)
    res = audit.run(conn, selector.POLICY_V1, "labeled")
    by = {r["task_id"]: r for r in res["rows"]}
    assert by["A"]["correct"] and by["A"]["top1"].endswith("web/SKILL.md")
    # known sel-v1 weakness: one generic concept ("파일"->file) matches xlsx with fit 1.0 -> overconfident
    assert by["B"]["failure"][0] == "OVERCONFIDENT" and by["B"]["top1"].endswith("xl/SKILL.md")
    assert by["C"]["failure"][0] == "RETRIEVAL"
    assert res["summary"]["no_skill_justified"] == 0.0 and res["summary"]["failures"]["RETRIEVAL"] == 1
    audit.record(conn, res)
    assert conn.execute("SELECT origin FROM failure_cases").fetchone()[0] == "auto"   # suggestion, not confirmed


def test_negation_excludes_terms():
    t = finder.analyze("Azure 계정 없이 React 앱 테스트, without dotnet", negation=True)
    terms = [c["term"] for c in t["concepts"]]
    assert "azure" not in terms and "dotnet" not in terms and "react" in terms
    assert "azure" in [c["term"] for c in finder.analyze("Azure 계정 없이 React 앱")["concepts"]]  # v1 unchanged
    assert "node" in [c["term"] for c in finder.analyze("react node", negation=True)["concepts"]]   # \b: 'no' in node
