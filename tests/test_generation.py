import pytest

from skill_validator.db import OutputArtifact, ReviewItem, ValidationResult
from skill_validator.generator import package_tree_hash, plan_candidate, request_generation
from skill_validator.orchestrator import (
    WorkflowError,
    approve_proposal,
    run_until_idle,
    set_decision,
)
from skill_validator.storage import area_hash

from .test_orchestrator import items_by_defect, to_decisions


def demo_choices(svc, db, review, concealment_action="remove"):
    """The demo scenario from SPEC section 17."""
    by = items_by_defect(db, review)
    d = set_decision(db, review, by["broad_file_access"], "fix")
    run_until_idle(svc)
    db.refresh(d)
    db.refresh(review)
    approve_proposal(db, review, d)
    if concealment_action == "keep_open":
        set_decision(db, review, by["concealment"], "adapt")
        run_until_idle(svc)
        db.refresh(review)
    set_decision(db, review, by["concealment"], concealment_action)
    set_decision(db, review, by["reviewer_manipulation"], "remove")
    set_decision(db, review, by["network_send"], "remove")
    return by


def read_delivery(svc, review, name):
    return svc.storage.resolve("delivery", review.user_id, review.id, name).read_bytes()


def test_generation_is_blocked_until_everything_is_decided(svc, svc_db, svc_user):
    review = to_decisions(svc, svc_db, svc_user)
    with pytest.raises(WorkflowError, match="טרם הוכרעו"):
        request_generation(svc, svc_db, review)


def test_end_to_end_demo_with_mock_model(svc, svc_db, svc_user):
    review = to_decisions(svc, svc_db, svc_user)
    source_hash_before = area_hash(svc.storage, "source", svc_user.id, review.id)
    demo_choices(svc, svc_db, review)

    request_generation(svc, svc_db, review)
    assert review.stage == "revalidate"
    # Nothing is downloadable before the re-check has finished.
    assert not svc.storage.area_dir("delivery", svc_user.id, review.id).exists()
    with pytest.raises(WorkflowError):
        set_decision(svc_db, review, svc_db.query(ReviewItem).first(), "remove")

    run_until_idle(svc)
    svc_db.refresh(review)
    assert review.stage == "delivered" and review.status == "done"
    assert review.final_status == "completed_no_open"

    # The corrected copy matches the choices.
    files = svc.storage.list_files("candidate", svc_user.id, review.id)
    assert sorted(files) == ["LICENSE", "SKILL.md"]
    skill = svc.storage.resolve("candidate", svc_user.id, review.id, "SKILL.md").read_text(encoding="utf-8")
    assert "Vary sentence length" in skill  # style guidelines kept
    assert "sample texts they choose" in skill  # narrowed access
    assert "~/Documents" not in skill
    assert "send_text.py" not in skill  # the reference line went with the script
    assert "Do not tell the user" not in skill
    assert "Note to the reviewer" not in skill
    src_license = svc.storage.resolve("source", svc_user.id, review.id, "LICENSE").read_bytes()
    assert svc.storage.resolve("candidate", svc_user.id, review.id, "LICENSE").read_bytes() == src_license

    # The source was not changed.
    assert area_hash(svc.storage, "source", svc_user.id, review.id) == source_hash_before

    # The re-check read the copy itself, and every risky finding has a result.
    results = svc_db.query(ValidationResult).filter_by(session_id=review.id).all()
    structural = [r for r in results if r.kind == "structural"]
    assert structural[0].outcome == "passed" and structural[0].new_issues == []
    semantic = {r.item_id: r for r in results if r.kind == "semantic"}
    by = items_by_defect(svc_db, review)
    for defect in ("broad_file_access", "concealment", "reviewer_manipulation", "network_send"):
        assert semantic[by[defect].id].outcome == "closed"
    candidate_hash = area_hash(svc.storage, "candidate", svc_user.id, review.id)
    assert {r.candidate_hash for r in results} == {candidate_hash}

    # Delivery: package equals the re-checked copy; reports cover every finding and change.
    artifacts = {a.kind: a for a in svc_db.query(OutputArtifact).filter_by(session_id=review.id)}
    assert set(artifacts) == {"package_zip", "review_md", "changes_md"}
    package = read_delivery(svc, review, artifacts["package_zip"].storage_path)
    assert package_tree_hash(package) == candidate_hash
    review_md = read_delivery(svc, review, "REVIEW.md").decode("utf-8")
    for item in svc_db.query(ReviewItem).filter_by(session_id=review.id):
        assert item.title in review_md
    assert "מצב מודל מדומה" in review_md and "אינו הבטחת בטיחות" in review_md
    assert "בטוח לחלוטין" not in review_md
    changes_md = read_delivery(svc, review, "CHANGES.md").decode("utf-8")
    for defect in ("broad_file_access", "concealment", "reviewer_manipulation", "network_send"):
        assert by[defect].id in changes_md


def test_kept_risk_is_reported_as_open(svc, svc_db, svc_user):
    review = to_decisions(svc, svc_db, svc_user)
    by = demo_choices(svc, svc_db, review, concealment_action="keep_open")
    request_generation(svc, svc_db, review)
    run_until_idle(svc)
    svc_db.refresh(review)
    assert review.final_status == "completed_with_open"
    r = svc_db.query(ValidationResult).filter_by(item_id=by["concealment"].id).one()
    assert r.outcome == "accepted_by_user"
    skill = svc.storage.resolve("candidate", svc_user.id, review.id, "SKILL.md").read_text(encoding="utf-8")
    assert "Do not tell the user" in skill


def test_removal_that_leaves_a_broken_reference_is_shown_before_delivery(svc, svc_db, svc_user):
    review = to_decisions(svc, svc_db, svc_user)
    by = demo_choices(svc, svc_db, review)
    # Simulate a finding that does not list the line that references the script.
    script = by["network_send"]
    script.locations = [loc for loc in script.locations if loc["role"] == "main"]
    svc_db.commit()
    plan = plan_candidate(svc, svc_db, review)
    assert [b["token"] for b in plan.broken] == ["scripts/send_text.py"]  # visible before generating

    request_generation(svc, svc_db, review)
    run_until_idle(svc)
    svc_db.refresh(review)
    structural = svc_db.query(ValidationResult).filter_by(session_id=review.id, kind="structural").one()
    assert structural.outcome == "failed" and "scripts/send_text.py" in structural.new_issues[0]
    assert review.final_status == "completed_with_open"


def test_overlap_with_a_replacement_is_refused(svc, svc_db, svc_user):
    review = to_decisions(svc, svc_db, svc_user)
    by = demo_choices(svc, svc_db, review)
    fixed = by["broad_file_access"]  # approved replacement wording
    hide = by["concealment"]  # removal
    hide.locations = [{"path": "SKILL.md", "start": fixed.locations[0]["start"], "end": hide.locations[0]["end"], "role": "main"}]
    svc_db.commit()
    with pytest.raises(WorkflowError, match="חופפים"):
        plan_candidate(svc, svc_db, review)


def test_overlapping_removals_are_merged(svc, svc_db, svc_user):
    review = to_decisions(svc, svc_db, svc_user)
    by = demo_choices(svc, svc_db, review)
    hide, note = by["concealment"], by["reviewer_manipulation"]
    # Both removals cover the hide line, as when two findings point at the same line.
    note.locations = [{"path": "SKILL.md", "start": hide.locations[0]["start"], "end": note.locations[0]["end"], "role": "main"}]
    svc_db.commit()
    plan = plan_candidate(svc, svc_db, review)
    assert "Do not tell the user" not in plan.texts["SKILL.md"]
    assert "Note to the reviewer" not in plan.texts["SKILL.md"]
    assert "Vary sentence length" in plan.texts["SKILL.md"]


def test_whole_file_removed_through_a_reference_location_is_dropped(svc, svc_db, svc_user):
    review = to_decisions(svc, svc_db, svc_user)
    by = demo_choices(svc, svc_db, review)
    script = by["network_send"]
    # As the real model did: the SKILL.md line is the main location, the script a reference.
    script.locations = [
        {"path": "SKILL.md", "start": script.locations[1]["start"], "end": script.locations[1]["end"], "role": "main"},
        {"path": "scripts/send_text.py", "start": 1, "end": 19, "role": "reference"},
    ]
    svc_db.commit()
    plan = plan_candidate(svc, svc_db, review)
    assert "scripts/send_text.py" not in plan.texts and plan.removed_files == ["scripts/send_text.py"]
    assert plan.broken == []
