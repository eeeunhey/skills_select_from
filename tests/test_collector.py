"""Collector tests (offline, fake fetchers). PRD F1, 5.2, GT-05, RES-01."""
import json
import urllib.error

import pytest

from lab import db
from lab.collector import collect, export, git_blob_sha

NOW = ["2026-11-01T00:00:00+00:00", "2026-11-02T00:00:00+00:00", "2026-11-03T00:00:00+00:00", "2026-11-04T00:00:00+00:00"]
SRC = {"github_repos": [{"repo": "o/a", "claude_native": True}, {"repo": "o/b", "claude_native": False}], "manual": []}


def skill(name, desc="does things"):
    return f"---\nname: {name}\ndescription: {desc}\n---\nbody of {name}\n"


class Fake:
    def __init__(self):
        self.files = {"o/a": {}, "o/b": {}}  # repo -> {path: text}
        self.fail = {}  # repo -> exception for tree
        self.raw_calls = []

    def tree(self, repo):
        if repo in self.fail:
            raise self.fail[repo]
        f = self.files[repo]
        return "c" + str(len(self.raw_calls)), {p: (git_blob_sha(t), len(t)) for p, t in f.items()}, False

    def raw(self, repo, ref, path):
        self.raw_calls.append((repo, path))
        return self.files[repo][path]


def run(conn, fake, i):
    return collect(conn, SRC, NOW[i], fetch_tree=fake.tree, fetch_raw=fake.raw, sleep=lambda s: None)


def row(conn, sid):
    return conn.execute("SELECT * FROM skills WHERE id=?", (sid,)).fetchone()


@pytest.fixture
def conn():
    return db.connect(":memory:")


def test_new_changed_unchanged_removed_readded(conn):
    f = Fake()
    f.files["o/a"] = {"x/SKILL.md": skill("x"), "y/SKILL.md": skill("y")}
    r = run(conn, f, 0)
    assert r["counts"]["new"] == 2
    assert row(conn, "github:o/a:x/SKILL.md")["compatibility_status"] == "unverified"
    assert conn.execute("SELECT count(*) FROM skills_fts").fetchone()[0] == 2

    f.raw_calls.clear()
    r = run(conn, f, 1)
    assert r["counts"]["unchanged"] == 2 and f.raw_calls == []  # no re-read when blob sha unchanged
    assert row(conn, "github:o/a:x/SKILL.md")["last_seen"] == NOW[1]
    assert row(conn, "github:o/a:x/SKILL.md")["last_changed"] == NOW[0]

    f.files["o/a"]["x/SKILL.md"] = skill("x", "new desc")
    del f.files["o/a"]["y/SKILL.md"]
    r = run(conn, f, 2)
    assert r["counts"]["changed"] == 1 and r["counts"]["removed"] == 1
    assert row(conn, "github:o/a:x/SKILL.md")["description"] == "new desc"
    assert row(conn, "github:o/a:y/SKILL.md")["status"] == "removed"
    assert conn.execute("SELECT count(*) FROM skill_versions WHERE skill_id='github:o/a:x/SKILL.md'").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM skills_fts").fetchone()[0] == 1

    f.files["o/a"]["y/SKILL.md"] = skill("y")
    r = run(conn, f, 3)
    assert r["counts"]["readded"] == 1 and row(conn, "github:o/a:y/SKILL.md")["status"] == "active"
    assert conn.execute("SELECT count(*) FROM skill_versions WHERE skill_id='github:o/a:y/SKILL.md'").fetchone()[0] == 1


def test_moved(conn):
    f = Fake()
    f.files["o/a"] = {"old/SKILL.md": skill("m")}
    run(conn, f, 0)
    f.files["o/a"] = {"new/SKILL.md": skill("m")}
    r = run(conn, f, 1)
    old = row(conn, "github:o/a:old/SKILL.md")
    assert old["status"] == "moved" and old["moved_to"] == "github:o/a:new/SKILL.md"
    assert row(conn, "github:o/a:new/SKILL.md")["duplicate_of"] is None
    assert r["counts"]["moved"] == 1 and r["counts"]["removed"] == 0 and r["counts"]["duplicates"] == 0


def test_duplicates_and_non_claude_note(conn):
    f = Fake()
    f.files["o/a"] = {"d/SKILL.md": skill("d")}
    run(conn, f, 0)
    f.files["o/b"] = {"copy/SKILL.md": skill("d"), "similar/SKILL.md": skill("d") + "extra\n"}
    r = run(conn, f, 1)
    assert r["counts"]["duplicates"] == 1
    assert row(conn, "github:o/b:copy/SKILL.md")["duplicate_of"] == "github:o/a:d/SKILL.md"
    meta = json.loads(conn.execute("SELECT metadata_json FROM skill_versions WHERE skill_id='github:o/b:similar/SKILL.md'").fetchone()[0])
    assert "github:o/a:d/SKILL.md" in meta["near_duplicates"]
    assert meta["compatibility_note"].startswith("non-Claude source")
    assert meta["source_url"].startswith("https://github.com/o/b/blob/")


def test_failed_repo_keeps_data(conn):
    f = Fake()
    f.files["o/a"] = {"k/SKILL.md": skill("k")}
    run(conn, f, 0)
    f.fail["o/a"] = urllib.error.HTTPError("u", 403, "rate limited", {}, None)
    r = run(conn, f, 1)
    assert row(conn, "github:o/a:k/SKILL.md")["status"] == "active"
    assert r["counts"]["removed"] == 0 and any("o/a" in e for e in r["errors"])
    run_row = conn.execute("SELECT * FROM collection_runs ORDER BY id DESC").fetchone()
    assert "o/a" in run_row["errors_json"] and run_row["finished_at"]


def test_transient_5xx_retried(conn):
    f = Fake()
    f.files["o/a"] = {"k/SKILL.md": skill("k")}
    real, n, sleeps = f.tree, [0], []

    def flaky(repo):
        n[0] += 1
        if n[0] == 1:
            raise urllib.error.HTTPError("u", 502, "bad gateway", {}, None)
        return real(repo)
    r = collect(conn, SRC, NOW[0], fetch_tree=flaky, fetch_raw=f.raw, sleep=sleeps.append)
    assert r["counts"]["new"] == 1 and sleeps == [1]


def test_broken_yaml_recorded(conn):
    f = Fake()
    f.files["o/a"] = {"bad/SKILL.md": "---\nname: [unclosed\ndescription: x\n---\nbody\n"}
    r = run(conn, f, 0)
    s = json.loads(conn.execute("SELECT scan_json FROM skill_versions").fetchone()[0])
    assert s["frontmatter_parse_ok"] is False and row(conn, "github:o/a:bad/SKILL.md")["name"] == "bad"
    assert any("frontmatter" in w for w in r["warnings"])


def test_plugin_distribution(conn):
    f = Fake()
    f.files["o/a"] = {"plugins/p1/.claude-plugin/plugin.json": "{}", "plugins/p1/skills/s/SKILL.md": skill("s"),
                      "other/skills/t/SKILL.md": skill("t"), ".claude-plugin/plugin.json": "{}",
                      "skills/r/SKILL.md": skill("r")}
    run(conn, f, 0)
    assert tuple(row(conn, "github:o/a:plugins/p1/skills/s/SKILL.md")[k] for k in ("distribution", "plugin_name")) == ("plugin", "p1")
    assert row(conn, "github:o/a:other/skills/t/SKILL.md")["distribution"] == "standalone"  # no manifest
    assert tuple(row(conn, "github:o/a:skills/r/SKILL.md")[k] for k in ("distribution", "plugin_name")) == ("plugin", "a")


def test_oversize_skipped(conn):
    f = Fake()
    f.files["o/a"] = {"big/SKILL.md": skill("big") + "x" * (600 * 1024)}
    r = run(conn, f, 0)
    assert r["counts"]["new"] == 0 and any("abnormal size" in w for w in r["warnings"])


def test_crash_mid_run_no_duplicate_versions(conn):
    """RES-01: an exception mid-run, then a re-run -> each (skill, blob) stored once."""
    f = Fake()
    f.files["o/a"] = {f"s{i}/SKILL.md": skill(f"s{i}") for i in range(4)}
    real = f.raw

    def crash(repo, ref, path):
        if path == "s2/SKILL.md":
            raise RuntimeError("worker killed")
        return real(repo, ref, path)
    with pytest.raises(RuntimeError):
        collect(conn, SRC, NOW[0], fetch_tree=f.tree, fetch_raw=crash, sleep=lambda s: None)
    run(conn, f, 0)
    run(conn, f, 1)
    assert conn.execute("SELECT count(*) FROM skill_versions").fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM skills WHERE status='active'").fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM skills_fts").fetchone()[0] == 4


def test_manual_link_and_export(conn):
    f = Fake()
    f.files["o/m"] = {"x/SKILL.md": skill("manual-one")}
    src = {"github_repos": [], "manual": [{"url": "https://github.com/o/m/blob/main/x/SKILL.md"}]}
    collect(conn, src, NOW[0], fetch_tree=f.tree, fetch_raw=f.raw, sleep=lambda s: None)
    r = collect(conn, src, NOW[1], fetch_tree=f.tree, fetch_raw=f.raw, sleep=lambda s: None)
    assert r["counts"]["unchanged"] == 1
    out = export(conn)
    assert out["skills"][0]["name"] == "manual-one" and out["skills"][0]["scan"]["security_status"] == "unreviewed"
