"""Package Generator, Structural Validator and delivery (Architecture sections 6, 9, 10).

The corrected copy is built from the read-only source plus approved decisions only, into the
candidate area. Re-check tasks then read that copy. Only after every re-check task has finished
is the copy published to the delivery area together with REVIEW.md and CHANGES.md.
"""

import io
import zipfile
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from .db import FileRecord, OutputArtifact, ReviewItem, ReviewSession, ReviewTask, ValidationResult
from .orchestrator import (
    Services,
    WorkflowError,
    current_decision,
    item_snapshot,
    save_checkpoint,
    undecided_items,
)
from .scanners import reference_graph
from .storage import area_hash, sha256_bytes, tree_hash, write_read_only


@dataclass
class CandidatePlan:
    texts: dict[str, str]
    removed_files: list[str] = field(default_factory=list)
    not_copied: list[str] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    broken: list[dict] = field(default_factory=list)
    essential_unsupported: list[str] = field(default_factory=list)


def _source_texts(svc: Services, db: Session, review: ReviewSession) -> tuple[dict[str, str], list[str]]:
    texts, unsupported = {}, []
    for f in db.query(FileRecord).filter_by(session_id=review.id, area="source").order_by(FileRecord.relative_path):
        if f.supported:
            p = svc.storage.resolve("source", review.user_id, review.id, f.relative_path)
            texts[f.relative_path] = p.read_text(encoding="utf-8")
        else:
            unsupported.append(f.relative_path)
    return texts, unsupported


def plan_candidate(svc: Services, db: Session, review: ReviewSession) -> CandidatePlan:
    """Compute the corrected copy in memory from approved decisions only. Writes nothing."""
    texts, unsupported = _source_texts(svc, db, review)
    lines = {p: t.splitlines() for p, t in texts.items()}
    edits: dict[str, list[tuple[int, int, list[str] | None, str]]] = {}  # path -> (start, end, new, item_id)
    drop_files: set[str] = set()
    changes = []

    items = db.query(ReviewItem).filter_by(session_id=review.id).order_by(ReviewItem.sort_key).all()
    for item in items:
        d = current_decision(db, item.id)
        if d is None or d.approval_status != "approved" or d.action in ("keep", "keep_open"):
            continue
        for loc in item.locations:
            path = loc["path"]
            total = len(lines.get(path, []))
            whole = loc["start"] == 1 and loc["end"] >= total
            before = "\n".join(lines.get(path, [])[loc["start"] - 1 : loc["end"]])
            if d.action == "remove":
                if whole and loc.get("role", "main") == "main":
                    drop_files.add(path)
                    after = None
                else:
                    edits.setdefault(path, []).append((loc["start"], loc["end"], [], item.id))
                    after = ""
            elif loc.get("role", "main") == "main":  # fix / adapt replace the main location only
                new_lines = d.proposed_text.splitlines()
                edits.setdefault(path, []).append((loc["start"], loc["end"], new_lines, item.id))
                after = d.proposed_text
            else:
                continue
            changes.append({
                "item_id": item.id, "title": item.title, "action": d.action, "path": path,
                "start": loc["start"], "end": loc["end"], "role": loc.get("role", "main"),
                "before": before, "after": after, "impact": item.impact,
            })

    for path, file_edits in edits.items():
        if path in drop_files:
            continue
        file_edits.sort()
        for (s1, e1, _, i1), (s2, e2, _, i2) in zip(file_edits, file_edits[1:]):
            if s2 <= e1 and i1 != i2:
                raise WorkflowError(f"שני חלקים חופפים ב־{path} (שורות {s1}-{e1} ו־{s2}-{e2}). יש לשנות אחת ההחלטות.")
        new = list(lines[path])
        for start, end, replacement, _ in sorted(file_edits, reverse=True):
            new[start - 1 : end] = replacement
        texts[path] = "\n".join(_collapse_blank_runs(new)) + "\n"

    for path in drop_files:
        texts.pop(path, None)
    graph = reference_graph(texts)
    referenced = {e["target"] for e in reference_graph({**texts, **{u: "" for u in unsupported}})["edges"]}
    plan = CandidatePlan(texts=texts, removed_files=sorted(drop_files), not_copied=unsupported, changes=changes)
    plan.broken = graph["broken"]
    plan.essential_unsupported = sorted(set(unsupported) & referenced)
    return plan


def _collapse_blank_runs(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        if not line.strip() and out and not out[-1].strip():
            continue
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return out


# ---------------------------------------------------------------- T4: generate


def request_generation(svc: Services, db: Session, review: ReviewSession) -> None:
    if review.stage != "decisions":
        raise WorkflowError("לא ניתן ליצור גרסה בשלב הזה")
    if review.status == "running":
        raise WorkflowError("יש משימות שעדיין רצות")
    pending = undecided_items(db, review)
    if pending:
        raise WorkflowError(f"יש {len(pending)} חלקים שטרם הוכרעו או שהנוסח שלהם טרם אושר")

    plan = plan_candidate(svc, db, review)
    storage = svc.storage
    storage_dir = storage.area_dir("candidate", review.user_id, review.id)
    if storage_dir.exists():
        raise WorkflowError("כבר נוצר עותק מתוקן לסקירה הזו")
    for path, text in plan.texts.items():
        write_read_only(storage.resolve("candidate", review.user_id, review.id, path), text.encode("utf-8"))
        db.add(FileRecord(
            session_id=review.id, area="candidate", relative_path=path, type=path.rsplit(".", 1)[-1],
            size=len(text.encode("utf-8")), hash=sha256_bytes(text.encode("utf-8")), coverage_status="pending",
        ))
    candidate_hash = area_hash(storage, "candidate", review.user_id, review.id)

    # Structural Validator: references and required files in the copy itself.
    issues = [f"{b['from']}:{b['line']} -> {b['token']}" for b in plan.broken]
    if "SKILL.md" not in plan.texts:
        issues.append("SKILL.md חסר בעותק המתוקן")
    issues += [f"קובץ חיוני שלא נבדק ולא הועתק: {p}" for p in plan.essential_unsupported]
    db.add(ValidationResult(
        session_id=review.id, candidate_hash=candidate_hash, kind="structural",
        outcome="failed" if issues else "passed",
        evidence="בדיקת הפניות בעותק המתוקן" + (f"; קבצים שהוסרו: {', '.join(plan.removed_files)}" if plan.removed_files else ""),
        new_issues=issues,
    ))

    review.stage, review.status = "revalidate", "running"
    created = 0
    for item in db.query(ReviewItem).filter_by(session_id=review.id).all():
        d = current_decision(db, item.id)
        if item.safety_status == "ok" and d.action == "keep":
            db.add(ValidationResult(
                session_id=review.id, item_id=item.id, candidate_hash=candidate_hash, kind="semantic",
                outcome="not_applicable", evidence="חלק שסווג תקין ונשמר ללא שינוי.",
            ))
            continue
        db.add(ReviewTask(
            session_id=review.id, stage="revalidate", task_type="revalidate_item",
            target_ref={
                "item_id": item.id, "item": item_snapshot(item), "candidate_hash": candidate_hash,
                "decision": {"action": d.action, "proposed_text": d.proposed_text, "is_default": d.is_default},
            },
        ))
        created += 1
    db.flush()
    if created == 0:
        finalize_review(svc, db, review)
    save_checkpoint(db, review)
    db.commit()


# ---------------------------------------------------------------- finalize and deliver

FINAL_LABELS = {
    "completed_no_open": "הבדיקה הושלמה ללא ממצאים פתוחים",
    "completed_with_open": "הבדיקה הושלמה עם ממצאים פתוחים",
    "partial": "הבדיקה חלקית",
}


def compute_final_status(db: Session, review: ReviewSession) -> str:
    tasks = db.query(ReviewTask).filter_by(session_id=review.id).all()
    results = db.query(ValidationResult).filter_by(session_id=review.id).all()
    structural = [r for r in results if r.kind == "structural"]
    essential_missing = any("קובץ חיוני" in i for r in structural for i in r.new_issues)
    if any(t.status == "skipped" for t in tasks) or essential_missing:
        return "partial"
    open_semantic = any(r.outcome in ("remained", "accepted_by_user") for r in results if r.kind == "semantic")
    if open_semantic or any(r.outcome == "failed" for r in structural) or any(r.new_issues for r in results):
        return "completed_with_open"
    return "completed_no_open"


def finalize_review(svc: Services, db: Session, review: ReviewSession) -> None:
    from .reports import build_changes_md, build_review_md

    db.flush()
    storage = svc.storage
    final = compute_final_status(db, review)
    review.final_status = final

    files = storage.list_files("candidate", review.user_id, review.id)
    base = storage.area_dir("candidate", review.user_id, review.id)
    pkg_name = f"{review.skill_name or 'skill'}-corrected"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            zf.writestr(f"{pkg_name}/{rel}", (base / rel).read_bytes())
    package_bytes = buf.getvalue()
    plan = plan_candidate(svc, db, review)
    outputs = {
        "package_zip": (f"{pkg_name}.zip", package_bytes),
        "review_md": ("REVIEW.md", build_review_md(svc, db, review, final).encode("utf-8")),
        "changes_md": ("CHANGES.md", build_changes_md(svc, db, review, plan).encode("utf-8")),
    }
    for kind, (name, data) in outputs.items():
        path = storage.resolve("delivery", review.user_id, review.id, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        db.add(OutputArtifact(
            session_id=review.id, kind=kind, area="delivery", storage_path=name,
            hash=sha256_bytes(data), final_status=final,
        ))
    review.stage, review.status = "delivered", "done"
    save_checkpoint(db, review, "delivered")


def package_tree_hash(package_bytes: bytes) -> str:
    """Tree hash of a delivered package zip, comparable with the candidate area hash."""
    zf = zipfile.ZipFile(io.BytesIO(package_bytes))
    entries = []
    for info in zf.infolist():
        rel = info.filename.split("/", 1)[1]
        entries.append((rel, sha256_bytes(zf.read(info))))
    return tree_hash(entries)
