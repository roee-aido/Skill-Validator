"""Orchestrator: review state machine, tasks, checkpoints and budget (Architecture sections 3, 9).

Decisions are user actions; tasks are units of work for the agent or for deterministic
services. Task results are committed in the same transaction that marks the task done.
"""

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .agent import BudgetExceeded, LimitReached, Meter, ModelFailure, run_agent_task
from .config import AgentConfig, SystemConfig
from .db import (
    AgentRun,
    BudgetApproval,
    Checkpoint,
    FileRecord,
    ReviewItem,
    ReviewSession,
    ReviewTask,
    User,
    UserDecision,
    ValidationResult,
)
from .intake import IntakeRejected, collect_upload, write_source
from .scanners import reference_graph
from .storage import Storage, area_hash
from .tools import make_context


class WorkflowError(Exception):
    """A user action is not allowed in the current state."""


@dataclass
class Services:
    db_factory: object  # sessionmaker
    storage: Storage
    agent_cfg: AgentConfig
    system_cfg: SystemConfig
    brain_factory: object  # callable(AgentConfig) -> brain
    # Optional cap on the total spend of all real-model runs (all reviews together).
    total_cap_usd: float | None = None


def make_brain(cfg: AgentConfig):
    if cfg.model.provider == "mock":
        from .mock_model import MockBrain

        return MockBrain()
    from .anthropic_model import AnthropicBrain

    return AnthropicBrain(cfg)


def now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- budget (D8)


def remaining(review: ReviewSession) -> dict:
    return {
        "tokens": review.budget_tokens_ceiling - review.spent_tokens_total,
        "cost_usd": round(review.budget_cost_ceiling - review.spent_cost_total, 6),
        "processing_minutes": round(review.budget_minutes_ceiling - review.spent_minutes_total, 4),
    }


def ceilings(review: ReviewSession) -> dict:
    return {
        "tokens": review.budget_tokens_ceiling,
        "cost_usd": review.budget_cost_ceiling,
        "processing_minutes": review.budget_minutes_ceiling,
    }


def spent(review: ReviewSession) -> dict:
    return {
        "tokens": review.spent_tokens_total,
        "cost_usd": round(review.spent_cost_total, 6),
        "processing_minutes": round(review.spent_minutes_total, 4),
    }


def budget_sufficient(review: ReviewSession, cfg: AgentConfig) -> bool:
    """D8: the remaining budget must cover the per-task limit in every dimension."""
    need = cfg.limits.per_task
    rem = remaining(review)
    return (
        rem["tokens"] >= need.tokens
        and rem["cost_usd"] >= need.cost_usd
        and rem["processing_minutes"] >= need.processing_minutes
    )


def real_model_spend(db: Session) -> float:
    """Total cost of every run that used a real model (mock runs are simulated and excluded)."""
    rows = db.query(AgentRun.cost_usd).filter(~AgentRun.model_id.like("mock%")).all()
    return float(sum(r[0] or 0.0 for r in rows))


def total_cap_allows_task(svc: Services, db: Session) -> bool:
    """A new task may start only if a worst-case task still fits under the total cap.

    Worst case = the per-task cost limit plus one more full-length response, because limits are
    checked after each response.
    """
    if svc.total_cap_usd is None or svc.agent_cfg.model.provider == "mock":
        return True
    m = svc.agent_cfg.model
    worst_task = svc.agent_cfg.limits.per_task.cost_usd + m.max_output_tokens * m.pricing.output_per_mtok / 1e6
    return real_model_spend(db) + worst_task <= svc.total_cap_usd


# ---------------------------------------------------------------- checkpoints


def save_checkpoint(db: Session, review: ReviewSession, reason: str = "") -> Checkpoint:
    tasks = db.query(ReviewTask).filter_by(session_id=review.id).all()
    rem = remaining(review)
    cp = Checkpoint(
        session_id=review.id,
        stage=review.stage,
        completed_task_ids=[t.id for t in tasks if t.status == "done"],
        pending_task_ids=[t.id for t in tasks if t.status in ("pending", "running", "failed", "stale")],
        source_hash=review.source_hash,
        decisions_version=review.decisions_version,
        spent_tokens_total=review.spent_tokens_total,
        spent_cost_total=review.spent_cost_total,
        remaining_tokens=rem["tokens"],
        remaining_cost=rem["cost_usd"],
        stop_reason=reason,
    )
    db.add(cp)
    return cp


def stop_review(db: Session, review: ReviewSession, reason: str) -> None:
    review.status = "stopped"
    review.stop_reason = reason
    save_checkpoint(db, review, reason)


# ---------------------------------------------------------------- T1: start


def start_review(svc: Services, db: Session, user: User, uploads, usage_description: str, disclosure_confirmed: bool) -> ReviewSession:
    if not disclosure_confirmed:
        raise WorkflowError("יש לאשר את הגילוי הנאות לפני שליחה לניתוח")
    cfg, lim = svc.agent_cfg, svc.agent_cfg.limits.per_review
    review = ReviewSession(
        user_id=user.id,
        usage_description=usage_description.strip()[:2000],
        config_version=cfg.config_version,
        config_sha256=cfg.file_sha256,
        model_id=cfg.model.model_id,
        budget_tokens_ceiling=lim.tokens,
        budget_cost_ceiling=lim.cost_usd,
        budget_minutes_ceiling=lim.processing_minutes,
        limits=svc.system_cfg.intake.model_dump(),
    )
    db.add(review)
    db.flush()
    try:
        res = collect_upload(uploads, svc.system_cfg.intake, svc.system_cfg.supported_formats)
    except IntakeRejected as e:
        review.stage, review.status, review.intake_error = "rejected", "rejected", str(e)
        db.commit()
        return review
    write_source(svc.storage, user.id, review.id, res)
    review.skill_name, review.source_hash = res.skill_name, res.source_hash
    for f in res.files:
        db.add(FileRecord(
            session_id=review.id, area="source", relative_path=f.path, type=f.type, size=f.size,
            hash=f.hash, supported=f.supported, coverage_status="pending" if f.supported else "unsupported",
        ))
    review.unchecked_resources = [f.path for f in res.files if not f.supported]
    review.stage, review.status = "purpose", "running"
    db.add(ReviewTask(session_id=review.id, stage="detect_purpose", task_type="detect_purpose", target_ref={}))
    db.flush()
    save_checkpoint(db, review, "")
    db.commit()
    return review


# ---------------------------------------------------------------- T2: purpose


def analysis_groups(texts: dict[str, str]) -> list[list[str]]:
    """O6: one task per group of files that reference each other."""
    parent = {p: p for p in texts}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in reference_graph(texts)["edges"]:
        parent[find(e["from"])] = find(e["target"])
    groups: dict[str, list[str]] = {}
    for p in texts:
        groups.setdefault(find(p), []).append(p)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: (0 if "SKILL.md" in g else 1, g))


def confirm_purpose(svc: Services, db: Session, review: ReviewSession, purpose: str) -> None:
    if review.stage != "purpose" or review.purpose_status != "proposed":
        raise WorkflowError("אין מטרה שממתינה לאישור")
    if not purpose.strip():
        raise WorkflowError("המטרה ריקה")
    review.purpose = purpose.strip()
    review.purpose_status = "confirmed"
    review.stage, review.status = "analyze", "running"
    files = db.query(FileRecord).filter_by(session_id=review.id, area="source", supported=True).all()
    texts = {
        f.relative_path: svc.storage.resolve("source", review.user_id, review.id, f.relative_path).read_text(encoding="utf-8")
        for f in files
    }
    for group in analysis_groups(texts):
        db.add(ReviewTask(session_id=review.id, stage="analyze", task_type="analyze_group", target_ref={"paths": group}))
    db.flush()
    save_checkpoint(db, review)
    db.commit()


# ---------------------------------------------------------------- decisions (T3)

ALLOWED_ACTIONS = {
    ("relevant", "ok"): {"keep", "remove"},
    ("relevant", "risky"): {"fix", "remove"},
    ("not_relevant", "risky"): {"adapt", "remove"},
    ("not_relevant", "ok"): {"adapt", "remove"},
}


def classification(item: ReviewItem) -> tuple[str, str]:
    return item.relevance, ("ok" if item.safety_status == "ok" else "risky")


def allowed_actions(item: ReviewItem, decision: UserDecision | None) -> set[str]:
    actions = set(ALLOWED_ACTIONS[classification(item)])
    if decision is not None and decision.no_safe_alternative:
        actions.add("keep_open")
    return actions


def item_snapshot(item: ReviewItem) -> dict:
    return {
        k: getattr(item, k)
        for k in ("id", "title", "locations", "evidence", "relevance", "safety_status", "defect_type",
                  "severity", "certainty", "reason", "dependencies", "impact", "recommendation")
    }


def current_decision(db: Session, item_id: str) -> UserDecision | None:
    return db.query(UserDecision).filter_by(item_id=item_id).order_by(UserDecision.updated_at.desc()).first()


def _require_decisions_open(review: ReviewSession):
    if review.stage != "decisions":
        raise WorkflowError("לא ניתן לשנות החלטות בשלב הזה")


def set_decision(db: Session, review: ReviewSession, item: ReviewItem, action: str) -> UserDecision:
    _require_decisions_open(review)
    existing = current_decision(db, item.id)
    if action not in allowed_actions(item, existing):
        raise WorkflowError("הפעולה אינה זמינה לסיווג של החלק הזה")
    review.decisions_version += 1
    if existing is None:
        existing = UserDecision(session_id=review.id, item_id=item.id)
        db.add(existing)
    existing.action = action
    existing.is_default = False
    existing.source_version = review.source_hash
    existing.decision_version = review.decisions_version
    if action in ("fix", "adapt"):
        existing.approval_status = "proposal_pending"
        existing.proposed_text = existing.proposal_explanation = ""
        existing.no_safe_alternative = False
        db.flush()
        task = ReviewTask(
            session_id=review.id, stage="propose_fix", task_type="propose_fix",
            target_ref={"item_id": item.id, "item": item_snapshot(item), "action": action, "decision_id": existing.id},
        )
        db.add(task)
        db.flush()
        existing.task_id = task.id
        review.status = "running"
    else:
        existing.approval_status = "approved"
    db.flush()
    save_checkpoint(db, review)
    db.commit()
    return existing


def approve_proposal(db: Session, review: ReviewSession, decision: UserDecision) -> None:
    _require_decisions_open(review)
    if decision.approval_status != "proposed" or decision.no_safe_alternative:
        raise WorkflowError("אין נוסח מוצע שממתין לאישור")
    decision.approval_status = "approved"
    review.decisions_version += 1
    decision.decision_version = review.decisions_version
    db.commit()


def reset_decision(db: Session, review: ReviewSession, item: ReviewItem) -> None:
    """Undo the user's choice for an item and return to the original (default keep where it applies)."""
    _require_decisions_open(review)
    for d in db.query(UserDecision).filter_by(item_id=item.id).all():
        db.delete(d)
    db.flush()
    review.decisions_version += 1
    if classification(item) == ("relevant", "ok"):
        _add_default(db, review, item)
    db.commit()


def _add_default(db: Session, review: ReviewSession, item: ReviewItem) -> None:
    db.add(UserDecision(
        session_id=review.id, item_id=item.id, action="keep", is_default=True, approval_status="approved",
        source_version=review.source_hash, decision_version=review.decisions_version,
    ))


def undecided_items(db: Session, review: ReviewSession) -> list[ReviewItem]:
    out = []
    for item in db.query(ReviewItem).filter_by(session_id=review.id).all():
        d = current_decision(db, item.id)
        if d is None or d.approval_status != "approved":
            out.append(item)
    return out


# ---------------------------------------------------------------- running tasks


def _ready(db: Session, task: ReviewTask) -> bool:
    if not task.depends_on:
        return True
    deps = db.query(ReviewTask).filter(ReviewTask.id.in_(task.depends_on)).all()
    return all(d.status in ("done", "skipped") for d in deps)


def next_task(db: Session) -> ReviewTask | None:
    q = (
        db.query(ReviewTask)
        .join(ReviewSession, ReviewSession.id == ReviewTask.session_id)
        .filter(ReviewTask.status == "pending", ReviewSession.status == "running")
        .order_by(ReviewTask.created_at)
    )
    for t in q:
        if _ready(db, t):
            return t
    return None


def _render_prompt(cfg: AgentConfig, review: ReviewSession, task: ReviewTask) -> str:
    template = cfg.prompt_text(cfg.stage(task.stage).prompt)
    ref = task.target_ref or {}
    return template.format(
        usage_description=review.usage_description or "(לא סופק)",
        purpose=review.purpose,
        targets=", ".join(ref.get("paths", [])),
        finding=ref.get("item", ""),
        action=ref.get("action", ""),
        decision=ref.get("decision", ""),
    )


def run_task(svc: Services, db: Session, task: ReviewTask) -> str:
    """Run one task. Returns the outcome string recorded on the AgentRun (or a skip reason)."""
    review = db.get(ReviewSession, task.session_id)
    cfg = svc.agent_cfg
    if not budget_sufficient(review, cfg):
        stop_review(db, review, "budget_insufficient")
        db.commit()
        return "budget_insufficient"
    if not total_cap_allows_task(svc, db):
        stop_review(db, review, "total_budget_cap")
        db.commit()
        return "total_budget_cap"

    task.status, task.attempts = "running", task.attempts + 1
    db.commit()

    ctx = make_context(db, svc.storage, review, task, cfg.stage(task.stage))
    if task.stage == "revalidate":
        ctx.candidate_hash = task.target_ref.get("candidate_hash", "")
    brain = svc.brain_factory(cfg)
    run = AgentRun(
        session_id=review.id, task_id=task.id, config_version=cfg.config_version, model_id=brain.model_id,
        started_at=now(),  # the real start, not the time the row is saved
    )
    meter = Meter(cfg=cfg, review=review)
    outcome, error = "completed", ""
    try:
        run_agent_task(ctx, brain, cfg, meter, cfg.prompt_text(cfg.prompts.system), _render_prompt(cfg, review, task))
        _check_task_output(task, ctx)
    except BudgetExceeded as e:
        outcome, error = "budget_exceeded", str(e)
    except LimitReached as e:
        outcome, error = "limit_reached", str(e)
    except ModelFailure as e:
        outcome, error = "model_error", str(e)
    except _MissingOutput as e:
        outcome, error = "tool_error", str(e)

    elapsed_min = (time.monotonic() - meter.started) / 60
    review.spent_minutes_total += elapsed_min
    run.input_tokens, run.output_tokens, run.cost_usd = meter.input_tokens, meter.output_tokens, meter.cost_usd
    run.processing_seconds, run.tool_calls = elapsed_min * 60, ctx.calls
    run.outcome, run.error, run.ended_at = outcome, error, now()
    db.add(run)

    if outcome == "completed":
        _apply_results(db, review, task, ctx)
        task.status, task.last_error = "done", ""
        _after_task(svc, db, review)
        save_checkpoint(db, review)
    elif outcome == "budget_exceeded":
        # Spend is real and stays recorded; the task's buffered results are discarded.
        task.status, task.last_error = "pending", error
        task.attempts -= 1  # running out of budget is not a failed attempt
        stop_review(db, review, "budget_exceeded")
    elif outcome == "model_error":
        task.status, task.last_error = "failed", error
        stop_review(db, review, "model_error")
    else:
        task.last_error = error
        if task.attempts >= cfg.stop_conditions.max_attempts_per_task:
            task.status = "skipped"
            _mark_not_checked(db, review, task)
            _after_task(svc, db, review)
        else:
            task.status = "pending"  # retry_then_checkpoint
        save_checkpoint(db, review, f"task {outcome}")
    db.commit()
    return outcome


class _MissingOutput(Exception):
    pass


def _check_task_output(task: ReviewTask, ctx) -> None:
    if task.stage == "detect_purpose" and "purpose" not in ctx.results:
        raise _MissingOutput("the purpose was not recorded")
    if task.stage == "propose_fix" and "proposal" not in ctx.results:
        raise _MissingOutput("no proposal was recorded")
    if task.stage == "revalidate" and not any(isinstance(p, ValidationResult) for p in ctx.pending):
        raise _MissingOutput("no validation result was recorded")


def _item_key(item: ReviewItem) -> tuple:
    return (item.defect_type, item.relevance, item.safety_status, item.content_hash)


def _carry_over_decisions(db: Session, review: ReviewSession, old_items: list, new_items: list) -> None:
    """Keep the user's choices across a re-run; drop only the ones the re-run affected.

    A decision moves to the equivalent new finding: same defect type, same classification and the
    same text at its locations (line numbers may shift). It is dropped when no equivalent finding
    exists, when its action is no longer allowed for the new finding, or when it relies on a
    proposed wording that was written for a different purpose.
    """
    pool: dict[tuple, list[ReviewItem]] = {}
    for new in new_items:
        pool.setdefault(_item_key(new), []).append(new)
    for old in old_items:
        candidates = pool.get(_item_key(old), [])
        new = candidates.pop(0) if candidates else None
        for d in db.query(UserDecision).filter_by(item_id=old.id).all():
            proposal_stale = d.action in ("fix", "adapt") and new is not None and new.purpose_hash != old.purpose_hash
            if new is None or proposal_stale or d.action not in allowed_actions(new, d):
                db.delete(d)
                continue
            d.item_id = new.id
            d.source_version = review.source_hash
    db.flush()


def _apply_results(db: Session, review: ReviewSession, task: ReviewTask, ctx) -> None:
    # Idempotent: a re-run replaces whatever an earlier run of the same task saved, while the
    # user's decisions on unchanged findings are carried over (see _carry_over_decisions).
    old_items = db.query(ReviewItem).filter_by(task_id=task.id).all()
    db.query(ValidationResult).filter_by(task_id=task.id).delete()
    for obj in ctx.pending:
        db.add(obj)
    db.flush()
    if old_items:
        new_items = [p for p in ctx.pending if isinstance(p, ReviewItem)]
        _carry_over_decisions(db, review, old_items, new_items)
        for old in old_items:
            db.delete(old)
    if task.stage == "detect_purpose":
        p = ctx.results["purpose"]
        review.purpose, review.purpose_evidence = p["purpose"], p["evidence"]
        review.purpose_question, review.purpose_status = p["clarification_question"], "proposed"
    elif task.stage == "analyze":
        for f in db.query(FileRecord).filter(
            FileRecord.session_id == review.id, FileRecord.area == "source",
            FileRecord.relative_path.in_(task.target_ref["paths"]),
        ):
            f.coverage_status = "checked"
    elif task.stage == "propose_fix":
        d = db.get(UserDecision, task.target_ref["decision_id"])
        if d is not None and d.task_id == task.id:
            prop = ctx.results["proposal"]
            d.proposed_text, d.proposal_explanation = prop["proposed_text"], prop["explanation"]
            d.no_safe_alternative = prop["no_safe_alternative"]
            d.approval_status = "no_alternative" if prop["no_safe_alternative"] else "proposed"
    task.result_ref = f"{len(ctx.pending)} records"
    db.flush()


def _mark_not_checked(db: Session, review: ReviewSession, task: ReviewTask) -> None:
    paths = (task.target_ref or {}).get("paths", [])
    for f in db.query(FileRecord).filter(
        FileRecord.session_id == review.id, FileRecord.relative_path.in_(paths), FileRecord.area == "source"
    ):
        f.coverage_status = "not_checked"
        if f.relative_path not in review.unchecked_resources:
            review.unchecked_resources = review.unchecked_resources + [f.relative_path]


def _open_tasks(db: Session, review: ReviewSession, stage: str) -> int:
    return db.query(ReviewTask).filter(
        ReviewTask.session_id == review.id, ReviewTask.stage == stage,
        ReviewTask.status.in_(("pending", "running", "failed", "stale")),
    ).count()


def _after_task(svc: Services, db: Session, review: ReviewSession) -> None:
    db.flush()
    if review.stage == "purpose" and review.purpose_status == "proposed":
        review.status = "waiting_user"
    elif review.stage == "analyze" and _open_tasks(db, review, "analyze") == 0:
        for item in db.query(ReviewItem).filter_by(session_id=review.id).all():
            if classification(item) == ("relevant", "ok") and current_decision(db, item.id) is None:
                _add_default(db, review, item)
        review.stage, review.status = "decisions", "waiting_user"
    elif review.stage == "decisions" and _open_tasks(db, review, "propose_fix") == 0:
        review.status = "waiting_user"
    elif review.stage == "revalidate" and _open_tasks(db, review, "revalidate") == 0:
        from .generator import finalize_review

        finalize_review(svc, db, review)


def run_until_idle(svc: Services, max_tasks: int = 500) -> int:
    """Run pending tasks until none is ready. Used by tests and by the local worker."""
    count = 0
    while count < max_tasks:
        db = svc.db_factory()
        try:
            task = next_task(db)
            if task is None:
                return count
            run_task(svc, db, task)
            count += 1
        finally:
            db.close()
    return count


# ---------------------------------------------------------------- deletion


def delete_review(svc: Services, db: Session, review: ReviewSession) -> None:
    """Delete a review, all its rows and all three storage areas (SPEC section 10)."""
    from .db import OutputArtifact

    if review.status == "running":
        raise WorkflowError("לא ניתן למחוק סקירה בזמן שמשימות רצות")
    sid = review.id
    for model in (UserDecision, ValidationResult, OutputArtifact, BudgetApproval, AgentRun, Checkpoint):
        db.query(model).filter_by(session_id=sid).delete(synchronize_session=False)
    for model in (ReviewItem, ReviewTask, FileRecord):
        db.query(model).filter_by(session_id=sid).delete(synchronize_session=False)
    db.query(ReviewSession).filter_by(id=sid).delete(synchronize_session=False)
    db.commit()
    svc.storage.delete_review(review.user_id, sid)


# ---------------------------------------------------------------- T5: resume (D8)


def resume(svc: Services, db: Session, review: ReviewSession) -> str:
    """Returns "resumed", "needs_approval" or raises WorkflowError."""
    if review.status != "stopped":
        raise WorkflowError("הסקירה אינה במצב עצירה")
    # Step 2: balance check. Spend is never reset.
    if not budget_sufficient(review, svc.agent_cfg):
        review.stop_reason = "budget_insufficient"
        db.commit()
        return "needs_approval"
    # Step 3: the source must be unchanged.
    current = area_hash(svc.storage, "source", review.user_id, review.id)
    expected = _source_tree_hash(db, review)
    if current != expected:
        _invalidate_changed(svc, db, review)
    # Steps 4-6: stale decisions, interrupted and failed tasks return to the queue.
    for t in db.query(ReviewTask).filter(
        ReviewTask.session_id == review.id, ReviewTask.status.in_(("running", "failed", "stale"))
    ):
        t.status = "pending"
    review.status, review.stop_reason = "running", ""
    db.flush()
    if not db.query(ReviewTask).filter_by(session_id=review.id, status="pending").count():
        _after_task(svc, db, review)  # nothing left to run: move on to the next state
    save_checkpoint(db, review, "resumed")
    db.commit()
    return "resumed"


def _source_tree_hash(db: Session, review: ReviewSession) -> str:
    from .storage import tree_hash

    rows = db.query(FileRecord).filter_by(session_id=review.id, area="source").all()
    return tree_hash([(r.relative_path, r.hash) for r in rows])


def _invalidate_changed(svc: Services, db: Session, review: ReviewSession) -> None:
    """The source changed: re-analyze only the affected groups.

    Decisions are not deleted here. The re-run carries over every decision whose finding is
    unchanged and drops the rest (_carry_over_decisions). Until then generation is blocked,
    because the review goes back to the analyze stage.
    """
    from .storage import sha256_bytes, tree_hash

    changed = set()
    for rec in db.query(FileRecord).filter_by(session_id=review.id, area="source"):
        p = svc.storage.resolve("source", review.user_id, review.id, rec.relative_path)
        if not p.is_file():
            changed.add(rec.relative_path)
            continue
        current = sha256_bytes(p.read_bytes())
        if current != rec.hash:
            changed.add(rec.relative_path)
            rec.hash, rec.size, rec.coverage_status = current, p.stat().st_size, "pending"
    for t in db.query(ReviewTask).filter_by(session_id=review.id):
        if set((t.target_ref or {}).get("paths", [])) & changed:
            t.status = "stale"
            t.attempts = 0
    rows = db.query(FileRecord).filter_by(session_id=review.id, area="source").all()
    review.source_hash = tree_hash([(r.relative_path, r.hash) for r in rows])
    if review.stage == "decisions":
        review.stage = "analyze"
    review.stop_reason = "source_changed"


def approve_ceiling(svc: Services, db: Session, review: ReviewSession, user: User, new: dict) -> str:
    """Record an explicit ceiling increase (D8), then resume."""
    if review.status != "stopped":
        raise WorkflowError("הסקירה אינה במצב עצירה")
    if user.id != review.user_id:
        raise WorkflowError("רק בעל הסקירה יכול לאשר הגדלת תקרה")
    cap = svc.agent_cfg.limits.max_approved_ceiling.model_dump()
    need = svc.agent_cfg.limits.per_task.model_dump()
    old, used = ceilings(review), spent(review)
    for k in ("tokens", "cost_usd", "processing_minutes"):
        if new[k] < old[k]:
            raise WorkflowError("התקרה החדשה אינה יכולה להיות נמוכה מהקודמת")
        if new[k] > cap[k]:
            raise WorkflowError(f"התקרה החדשה חורגת מהתקרה המרבית שבקונפיגורציה ({k}: {cap[k]})")
        if new[k] - used[k] < need[k]:
            raise WorkflowError("גם אחרי ההגדלה היתרה אינה מספיקה למשימה הבאה")
    db.add(BudgetApproval(
        session_id=review.id, previous_ceiling=old, new_ceiling=dict(new),
        spent_at_approval=spent(review), approved_by=user.id,
    ))
    review.budget_tokens_ceiling = int(new["tokens"])
    review.budget_cost_ceiling = float(new["cost_usd"])
    review.budget_minutes_ceiling = float(new["processing_minutes"])
    db.flush()
    return resume(svc, db, review)
