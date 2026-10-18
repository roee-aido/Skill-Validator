import os
import stat

import pytest

from skill_validator.agent import ModelFailure
from skill_validator.db import AgentRun, BudgetApproval, FileRecord, ReviewItem, ReviewSession, ReviewTask, UserDecision
from skill_validator.orchestrator import (
    WorkflowError,
    allowed_actions,
    approve_ceiling,
    approve_proposal,
    confirm_purpose,
    current_decision,
    reset_decision,
    resume,
    run_task,
    run_until_idle,
    set_decision,
    start_review,
    undecided_items,
)

from .conftest import DEMO_ZIP

BAD_ZIP = DEMO_ZIP.parent / "bad-archive.zip"


def upload_demo(svc, db, user, usage="לשכתב טקסט כך שיישמע טבעי"):
    return start_review(svc, db, user, [(DEMO_ZIP.name, DEMO_ZIP.read_bytes())], usage, True)


def to_decisions(svc, db, user):
    review = upload_demo(svc, db, user)
    run_until_idle(svc)
    db.refresh(review)
    confirm_purpose(svc, db, review, review.purpose)
    run_until_idle(svc)
    db.refresh(review)
    return review


def items_by_defect(db, review):
    return {i.defect_type: i for i in db.query(ReviewItem).filter_by(session_id=review.id)}


def test_disclosure_is_required(svc, svc_db, svc_user):
    with pytest.raises(WorkflowError):
        start_review(svc, svc_db, svc_user, [(DEMO_ZIP.name, DEMO_ZIP.read_bytes())], "", False)


def test_bad_archive_is_rejected_and_creates_no_tasks(svc, svc_db, svc_user):
    review = start_review(svc, svc_db, svc_user, [(BAD_ZIP.name, BAD_ZIP.read_bytes())], "", True)
    assert review.status == "rejected" and "נתיב חורג" in review.intake_error
    assert svc_db.query(ReviewTask).filter_by(session_id=review.id).count() == 0
    assert not svc.storage.area_dir("source", svc_user.id, review.id).exists()


def test_purpose_then_analysis_produces_expected_cards(svc, svc_db, svc_user):
    review = upload_demo(svc, svc_db, svc_user)
    run_until_idle(svc)
    svc_db.refresh(review)
    assert review.status == "waiting_user" and review.purpose_status == "proposed"
    assert "natural" in review.purpose

    confirm_purpose(svc, svc_db, review, review.purpose)
    run_until_idle(svc)
    svc_db.refresh(review)
    assert review.stage == "decisions" and review.status == "waiting_user"

    by = items_by_defect(svc_db, review)
    assert {"broad_file_access", "concealment", "reviewer_manipulation", "network_send"} <= set(by)
    script = by["network_send"]
    assert script.safety_status == "unsafe" and script.recommendation == "remove"
    refs = [loc for loc in script.locations if loc["role"] == "reference"]
    assert refs and refs[0]["path"] == "SKILL.md"  # the line that runs the script
    assert by["reviewer_manipulation"].safety_status == "suspicious"
    assert by["broad_file_access"].relevance == "relevant" and by["broad_file_access"].recommendation == "fix"

    titles = [i.title for i in svc_db.query(ReviewItem).filter_by(session_id=review.id)]
    assert "סעיף: Style guidelines" in titles and "רישיון וייחוס" in titles

    # Relevant and OK parts get a default "keep" decision; risky parts need a choice.
    undecided = {i.defect_type for i in undecided_items(svc_db, review)}
    assert undecided == {"broad_file_access", "concealment", "reviewer_manipulation", "network_send"}
    assert all(f.coverage_status == "checked" for f in svc_db.query(FileRecord).filter_by(session_id=review.id))
    runs = svc_db.query(AgentRun).filter_by(session_id=review.id).all()
    assert runs and all(r.outcome == "completed" and r.model_id == "mock-rule-based" for r in runs)
    assert review.spent_cost_total > 0


def test_decision_rules_and_proposals(svc, svc_db, svc_user):
    review = to_decisions(svc, svc_db, svc_user)
    by = items_by_defect(svc_db, review)
    assert allowed_actions(by["broad_file_access"], None) == {"fix", "remove"}
    assert allowed_actions(by["concealment"], None) == {"adapt", "remove"}
    with pytest.raises(WorkflowError):
        set_decision(svc_db, review, by["concealment"], "keep")

    d = set_decision(svc_db, review, by["broad_file_access"], "fix")
    assert d.approval_status == "proposal_pending" and review.status == "running"
    run_until_idle(svc)
    svc_db.refresh(d)
    svc_db.refresh(review)
    assert d.approval_status == "proposed" and "sample texts" in d.proposed_text
    assert review.status == "waiting_user"
    approve_proposal(svc_db, review, d)
    assert current_decision(svc_db, by["broad_file_access"].id).approval_status == "approved"

    # No safe alternative: the user can remove the part or leave the issue open.
    a = set_decision(svc_db, review, by["concealment"], "adapt")
    run_until_idle(svc)
    svc_db.refresh(a)
    assert a.no_safe_alternative and a.approval_status == "no_alternative"
    assert "keep_open" in allowed_actions(by["concealment"], a)
    with pytest.raises(WorkflowError):
        approve_proposal(svc_db, review, a)
    set_decision(svc_db, review, by["concealment"], "keep_open")

    reset_decision(svc_db, review, by["concealment"])
    assert current_decision(svc_db, by["concealment"].id) is None


def test_rerun_of_a_task_does_not_duplicate_results(svc, svc_db, svc_user):
    review = to_decisions(svc, svc_db, svc_user)
    before = svc_db.query(ReviewItem).filter_by(session_id=review.id).count()
    task = svc_db.query(ReviewTask).filter_by(session_id=review.id, stage="analyze").first()
    task.status = "pending"
    review.stage = "analyze"
    review.status = "running"
    svc_db.commit()
    run_task(svc, svc_db, task)
    assert svc_db.query(ReviewItem).filter_by(session_id=review.id).count() == before


def test_budget_stop_resume_requires_approval_and_never_resets_spend(svc, svc_db, svc_user):
    review = upload_demo(svc, svc_db, svc_user)
    run_until_idle(svc)
    svc_db.refresh(review)
    confirm_purpose(svc, svc_db, review, review.purpose)

    # Force the review ceiling just above what was spent, so the next turn crosses it.
    review.budget_cost_ceiling = review.spent_cost_total + 1.0005
    svc_db.commit()
    svc_db.refresh(review)
    run_until_idle(svc)
    svc_db.refresh(review)
    assert review.status == "stopped"
    assert review.stop_reason in ("budget_exceeded", "budget_insufficient")
    spent_at_stop = review.spent_cost_total
    runs_at_stop = svc_db.query(AgentRun).filter_by(session_id=review.id).count()

    # Resume: the remaining budget is below the per-task limit, so approval is required.
    assert resume(svc, svc_db, review) == "needs_approval"
    assert run_until_idle(svc) == 0
    assert svc_db.query(AgentRun).filter_by(session_id=review.id).count() == runs_at_stop
    assert review.spent_cost_total == spent_at_stop  # never reset

    cap = svc.agent_cfg.limits.max_approved_ceiling
    with pytest.raises(WorkflowError, match="המרבית"):
        approve_ceiling(svc, svc_db, review, svc_user,
                        {"tokens": review.budget_tokens_ceiling, "cost_usd": cap.cost_usd + 1,
                         "processing_minutes": review.budget_minutes_ceiling})
    with pytest.raises(WorkflowError, match="אינה מספיקה"):
        approve_ceiling(svc, svc_db, review, svc_user,
                        {"tokens": review.budget_tokens_ceiling, "cost_usd": review.budget_cost_ceiling + 0.01,
                         "processing_minutes": review.budget_minutes_ceiling})
    assert svc_db.query(BudgetApproval).count() == 0

    new = {"tokens": review.budget_tokens_ceiling, "cost_usd": spent_at_stop + 3.0,
           "processing_minutes": review.budget_minutes_ceiling}
    assert approve_ceiling(svc, svc_db, review, svc_user, new) == "resumed"
    approval = svc_db.query(BudgetApproval).one()
    assert approval.spent_at_approval["cost_usd"] == pytest.approx(spent_at_stop, abs=1e-5)

    run_until_idle(svc)
    svc_db.refresh(review)
    assert review.stage == "decisions"
    assert review.spent_cost_total > spent_at_stop

    # No agent run started between the stop and the approval.
    from skill_validator.db import Checkpoint

    stop_cp = (svc_db.query(Checkpoint).filter(Checkpoint.session_id == review.id,
               Checkpoint.stop_reason.in_(("budget_exceeded", "budget_insufficient")))
               .order_by(Checkpoint.created_at).first())
    for r in svc_db.query(AgentRun).filter_by(session_id=review.id):
        assert r.started_at <= stop_cp.created_at or r.started_at >= approval.approved_at
    total_runs = sum(r.cost_usd for r in svc_db.query(AgentRun).filter_by(session_id=review.id))
    assert review.spent_cost_total == pytest.approx(total_runs, rel=1e-6)
    # Tasks completed before the stop ran exactly once.
    for t in svc_db.query(ReviewTask).filter_by(session_id=review.id, status="done"):
        completed = svc_db.query(AgentRun).filter_by(task_id=t.id, outcome="completed").count()
        assert completed == 1


def test_only_the_owner_can_approve_a_ceiling(svc, svc_db, svc_user):
    from skill_validator.db import User

    review = upload_demo(svc, svc_db, svc_user)
    review.status = "stopped"
    other = User(email="other@example.test")
    svc_db.add(other)
    svc_db.commit()
    with pytest.raises(WorkflowError, match="בעל הסקירה"):
        approve_ceiling(svc, svc_db, review, other, {"tokens": 1, "cost_usd": 1, "processing_minutes": 1})


def test_task_limit_retries_then_marks_files_not_checked(svc, svc_db, svc_user):
    svc.agent_cfg = svc.agent_cfg.model_copy(
        update={"stop_conditions": svc.agent_cfg.stop_conditions.model_copy(update={"max_agent_turns_per_task": 1})}
    )
    review = upload_demo(svc, svc_db, svc_user)
    run_until_idle(svc)
    svc_db.refresh(review)
    task = svc_db.query(ReviewTask).filter_by(session_id=review.id).one()
    assert task.status == "skipped" and task.attempts == svc.agent_cfg.stop_conditions.max_attempts_per_task
    outcomes = [r.outcome for r in svc_db.query(AgentRun).filter_by(task_id=task.id)]
    assert outcomes == ["limit_reached", "limit_reached"]


class FailingBrain:
    model_id = "failing"

    def usage_for_turn(self, conversation, calls):
        from skill_validator.agent import Usage

        return Usage(10, 10)

    def steps(self, ctx):
        raise ModelFailure("service unavailable")
        yield  # pragma: no cover


def test_model_error_stops_review_and_resume_retries(svc, svc_db, svc_user):
    svc.brain_factory = lambda cfg: FailingBrain()
    review = upload_demo(svc, svc_db, svc_user)
    run_until_idle(svc)
    svc_db.refresh(review)
    assert review.status == "stopped" and review.stop_reason == "model_error"

    from skill_validator.orchestrator import make_brain

    svc.brain_factory = make_brain
    assert resume(svc, svc_db, review) == "resumed"
    run_until_idle(svc)
    svc_db.refresh(review)
    assert review.purpose_status == "proposed"


def decide_all(svc, db, review):
    """The demo choices, including an approved proposal."""
    by = items_by_defect(db, review)
    d = set_decision(db, review, by["broad_file_access"], "fix")
    run_until_idle(svc)
    db.refresh(d)
    db.refresh(review)
    approve_proposal(db, review, d)
    for defect in ("concealment", "reviewer_manipulation", "network_send"):
        set_decision(db, review, by[defect], "remove")


def decision_snapshot(db, review) -> dict:
    """Decisions keyed by finding identity (title + main path), independent of item ids."""
    out = {}
    for item in db.query(ReviewItem).filter_by(session_id=review.id):
        d = current_decision(db, item.id)
        key = (item.title, item.locations[0]["path"])
        out[key] = None if d is None else (d.action, d.is_default, d.approval_status, d.proposed_text)
    return out


def force_rerun_analysis(db, review):
    for t in db.query(ReviewTask).filter_by(session_id=review.id, stage="analyze"):
        t.status = "pending"
    review.stage, review.status = "analyze", "running"
    db.commit()


def test_rerun_with_unchanged_source_and_purpose_keeps_every_decision(svc, svc_db, svc_user):
    review = to_decisions(svc, svc_db, svc_user)
    decide_all(svc, svc_db, review)
    before = decision_snapshot(svc_db, review)
    assert all(v is not None for v in before.values())

    force_rerun_analysis(svc_db, review)
    run_until_idle(svc)
    svc_db.refresh(review)
    assert review.stage == "decisions"
    assert decision_snapshot(svc_db, review) == before  # choices, approvals and wording all kept
    assert undecided_items(svc_db, review) == []
    from skill_validator.db import UserDecision

    assert svc_db.query(UserDecision).filter_by(session_id=review.id).count() == len(before)


def test_changed_source_drops_only_affected_decisions(svc, svc_db, svc_user):
    review = to_decisions(svc, svc_db, svc_user)
    decide_all(svc, svc_db, review)
    before = decision_snapshot(svc_db, review)
    review.status = "stopped"
    svc_db.commit()

    path = svc.storage.resolve("source", svc_user.id, review.id, "scripts/send_text.py")
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    path.write_text(path.read_text(encoding="utf-8").replace("collector", "uploader"), encoding="utf-8")
    assert resume(svc, svc_db, review) == "resumed"
    assert review.stage == "analyze"  # generation is blocked until the affected group is re-analyzed
    run_until_idle(svc)
    svc_db.refresh(review)
    assert review.stage == "decisions"

    after = decision_snapshot(svc_db, review)
    script_key = next(k for k in before if k[1] == "scripts/send_text.py")
    assert after[script_key] is None  # its text changed, so the user must decide again
    for key, value in before.items():
        if key != script_key:
            assert after[key] == value
    assert [i.locations[0]["path"] for i in undecided_items(svc_db, review)] == ["scripts/send_text.py"]


def test_changed_purpose_drops_only_decisions_it_affects(svc, svc_db, svc_user):
    review = to_decisions(svc, svc_db, svc_user)
    decide_all(svc, svc_db, review)
    before = decision_snapshot(svc_db, review)

    # A purpose that needs sending text changes how the script is classified, and makes the
    # wording proposed for the file-access finding outdated.
    review.purpose = "לשכתב טקסט ולשלוח אותו ל-API של שירות גיבוי"
    force_rerun_analysis(svc_db, review)
    run_until_idle(svc)
    svc_db.refresh(review)

    after = decision_snapshot(svc_db, review)
    by_path_title = {k[0]: v for k, v in after.items()}
    assert by_path_title["גישה רחבה מדי לקבצי המשתמש"] is None  # proposal was written for the old purpose
    assert all(v is None for k, v in after.items() if k[1] == "scripts/send_text.py")  # reclassified
    assert by_path_title["הוראה להסתיר פעולה מהמשתמש"] == before[("הוראה להסתיר פעולה מהמשתמש", "SKILL.md")]
    assert by_path_title["הוראה שמנסה להשפיע על הבודק"] == before[("הוראה שמנסה להשפיע על הבודק", "SKILL.md")]
    assert by_path_title["סעיף: Style guidelines"] == before[("סעיף: Style guidelines", "SKILL.md")]
