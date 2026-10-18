import os

import pytest

from skill_validator.db import (
    AgentRun,
    BudgetApproval,
    Checkpoint,
    FileRecord,
    ReviewItem,
    ReviewSession,
    ReviewTask,
)
from skill_validator.storage import PathViolation, normalize_relative


@pytest.mark.parametrize("bad", ["../x", "a/../../x", "/etc/passwd", "C:/Windows/x", "..\\x", "", "./."])
def test_unsafe_relative_paths_are_rejected(bad):
    with pytest.raises(PathViolation):
        normalize_relative(bad)


def test_relative_path_is_normalized():
    assert normalize_relative("scripts\\send.py") == "scripts/send.py"
    assert normalize_relative("./SKILL.md") == "SKILL.md"


def test_resolve_stays_inside_area(storage, user):
    p = storage.resolve("source", user.id, "s1", "scripts/a.py")
    assert storage.area_dir("source", user.id, "s1").resolve() in p.parents
    with pytest.raises(PathViolation):
        storage.resolve("source", user.id, "s1", "../s2/a.py")
    with pytest.raises(PathViolation):
        storage.resolve("secret_area", user.id, "s1", "a.py")


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs extra privileges on Windows")
def test_symlink_is_rejected(storage, user, tmp_path):
    base = storage.area_dir("source", user.id, "s1")
    base.mkdir(parents=True)
    (tmp_path / "outside.txt").write_text("x")
    (base / "link.txt").symlink_to(tmp_path / "outside.txt")
    with pytest.raises(PathViolation):
        storage.resolve("source", user.id, "s1", "link.txt")


def test_areas_are_separated_per_user_and_review(storage):
    a = storage.area_dir("source", "u1", "s1")
    b = storage.area_dir("source", "u2", "s1")
    c = storage.area_dir("candidate", "u1", "s1")
    assert len({a, b, c}) == 3


def test_delete_review_removes_all_areas(storage, user):
    for area in ("source", "candidate", "delivery"):
        p = storage.resolve(area, user.id, "s1", "f.txt")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    storage.delete_review(user.id, "s1")
    for area in ("source", "candidate", "delivery"):
        assert not storage.area_dir(area, user.id, "s1").exists()


def test_schema_round_trip(db, user):
    s = ReviewSession(user_id=user.id, budget_cost_ceiling=6.0)
    db.add(s)
    db.flush()
    t = ReviewTask(session_id=s.id, stage="analyze", task_type="analyze_group", target_ref={"paths": ["SKILL.md"]})
    db.add(t)
    db.flush()
    db.add_all(
        [
            FileRecord(session_id=s.id, area="source", relative_path="SKILL.md", type=".md", size=1, hash="h"),
            ReviewItem(
                session_id=s.id,
                task_id=t.id,
                title="t",
                locations=[{"path": "SKILL.md", "start": 1, "end": 2, "role": "main"}],
                evidence="e",
                relevance="relevant",
                safety_status="ok",
                defect_type="none",
                severity="info",
                certainty="high",
                reason="r",
                recommendation="keep",
            ),
            Checkpoint(
                session_id=s.id,
                stage="analyze",
                completed_task_ids=[],
                pending_task_ids=[t.id],
                source_hash="h",
                decisions_version=0,
                spent_tokens_total=0,
                spent_cost_total=0.0,
                remaining_tokens=10,
                remaining_cost=6.0,
            ),
            BudgetApproval(
                session_id=s.id,
                previous_ceiling={"cost_usd": 6.0},
                new_ceiling={"cost_usd": 8.0},
                spent_at_approval={"cost_usd": 5.9},
                approved_by=user.id,
            ),
            AgentRun(session_id=s.id, task_id=t.id, config_version="v", model_id="mock"),
        ]
    )
    db.commit()
    again = db.get(ReviewSession, s.id)
    assert again.items[0].locations[0]["path"] == "SKILL.md"
    assert again.tasks[0].target_ref == {"paths": ["SKILL.md"]}
