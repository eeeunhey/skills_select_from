import copy
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab import db, rubrics  # noqa: E402

ROOT = rubrics.ROOT
CASES = [f"T{i:02d}" for i in range(1, 11)]
SKILLS = ["webapp-testing", "frontend-design", "playwright", "pdf", "access", "theme-factory"]


def doc(case="T01"):
    return rubrics.load(ROOT / "rubrics" / f"{case}.yaml")[0]


@pytest.fixture
def conn():
    return db.connect(":memory:")


@pytest.mark.parametrize("case", CASES)
def test_shipped_rubrics_are_valid(case):
    d = doc(case)
    rubrics.check(d, SKILLS)  # raises on any problem, incl. leakage and unknown verifier ids
    reg = rubrics.known_verifiers()
    assert {r["verifier"] for r in d["requirements"]} == {k for k in reg if k.startswith(case + "-")}


def test_missing_verifier_caught():
    d = doc()
    del d["requirements"][0]["verifier"]
    assert any("missing verifier" in e for e in rubrics.validate(d))


def test_duplicate_ids_caught():
    d = doc()
    d["requirements"].append(copy.deepcopy(d["requirements"][0]) | {"verifier": "T01-X"})
    assert any("duplicate requirement id" in e for e in rubrics.validate(d))


def test_missing_mandatory_safety_caught():
    d = doc()
    d["requirements"] = [r for r in d["requirements"] if r["type"] != "safety"]
    assert "no mandatory safety requirement" in rubrics.validate(d)


@pytest.mark.parametrize("where,text", [
    ("goal", "Use webapp-testing to check the button"),
    ("prompt", "Run /playwright first."),
    ("prompt", "Use the frontend-design approach."),
    ("goal", "Pick a suitable skill for this."),
])
def test_skill_name_leakage_caught(where, text):
    d = doc()
    prompt = None
    if where == "goal":
        d["user_goal"] = text
    else:
        prompt = text
    assert any("leakage" in e for e in rubrics.validate(d, SKILLS, prompt))


def test_plain_words_are_not_leaks():
    # 'access' / 'pdf' are skill names somewhere, but as plain words they must not trip the check
    assert rubrics.validate(doc(), SKILLS, "Do not access the network. Export a pdf report.") == []


def test_tree_hash_is_deterministic_and_skips_node_modules(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"x\r\n")
    h1 = rubrics.tree_hash(tmp_path)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("1")
    assert rubrics.tree_hash(tmp_path) == h1
    (tmp_path / "a.txt").write_bytes(b"x\n")  # CRLF vs LF is the same content
    assert rubrics.tree_hash(tmp_path) == h1
    (tmp_path / "a.txt").write_text("y")
    assert rubrics.tree_hash(tmp_path) != h1


# version of the real rubric file under test (bumps when a frozen rubric gets a new version)
V = yaml.safe_load((ROOT / "rubrics" / "T01.yaml").read_text(encoding="utf-8"))["rubric_version"]


def _copy(tmp_path, case="T01"):
    p = tmp_path / f"{case}.yaml"
    p.write_text((ROOT / "rubrics" / f"{case}.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    return p


def test_register_creates_rubric_and_task(conn, tmp_path):
    out = rubrics.register(conn, _copy(tmp_path), "tester")
    task = conn.execute("SELECT * FROM tasks WHERE id='T01'").fetchone()
    assert task["fixture_commit"] == out["fixture_commit"] == f"sha256:{rubrics.tree_hash(ROOT / 'fixtures' / 'react-demo')}"
    assert task["rubric_version"] == V and task["split"] == "labeled"
    assert '"task_type": "button_flow"' in task["context_json"]
    assert "View my profile" in task["prompt"]
    assert rubrics.status(conn)[0]["approved_at"] is None


def test_approve_freezes_row(conn, tmp_path):
    path = _copy(tmp_path)
    rubrics.register(conn, path, "tester")
    rubrics.approve(conn, "T01", V, "human-reviewer")
    with pytest.raises(sqlite3.DatabaseError, match="frozen"):
        conn.execute("UPDATE rubrics SET yaml_text='x' WHERE id='T01'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM rubrics WHERE id='T01'")
    with pytest.raises(rubrics.RubricError, match="already approved"):
        rubrics.approve(conn, "T01", V, "again")


def test_freeze_check_detects_edit(conn, tmp_path):
    path = _copy(tmp_path)
    rubrics.register(conn, path, "tester")
    assert rubrics.freeze_check(conn, "T01", V, path) == "draft"
    rubrics.approve(conn, "T01", V, "human-reviewer")
    assert rubrics.freeze_check(conn, "T01", V, path) == "frozen-ok"
    path.write_text(path.read_text(encoding="utf-8").replace("weight: 1", "weight: 2", 1), encoding="utf-8")
    with pytest.raises(rubrics.RubricError, match="new rubric_version"):
        rubrics.freeze_check(conn, "T01", V, path)
    with pytest.raises(rubrics.RubricError, match="new rubric_version"):
        rubrics.register(conn, path, "tester")


def test_draft_can_be_reregistered_with_changes(conn, tmp_path):
    path = _copy(tmp_path)
    first = rubrics.register(conn, path, "tester")
    path.write_text(path.read_text(encoding="utf-8").replace("weight: 1", "weight: 2", 1), encoding="utf-8")
    assert rubrics.register(conn, path, "tester")["sha256"] != first["sha256"]


def test_declared_fixture_hash_must_match(conn, tmp_path):
    path = _copy(tmp_path)
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    d["project_fixture"]["tree_sha256"] = "0" * 64
    path.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    with pytest.raises(rubrics.RubricError, match="fixture tree hash"):
        rubrics.register(conn, path, "tester")
