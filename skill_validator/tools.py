"""The closed list of agent tools (Architecture.md section 6), with stage and area guards.

Rules enforced here, in code:
- Only tools in policy.TOOL_NAMES exist. A stage gets only the tools its configuration lists,
  and the configuration itself cannot exceed policy.STAGE_TOOL_CEILING.
- Areas are names ("source", "candidate"), resolved by Storage. "candidate" is readable only
  in the revalidate stage.
- No tool writes files. Record tools add rows to ctx.pending; the orchestrator commits them
  together with the task status, or discards them if the task does not complete.
"""

import difflib
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from . import policy
from .db import FileRecord, ReviewItem, ReviewSession, ReviewTask, ValidationResult
from .scanners import reference_graph, references_for, search_patterns
from .secretscan import mask_secrets
from .storage import PathViolation, Storage, normalize_relative

MAX_READ_LINES = 400

RELEVANCE = {"relevant", "not_relevant"}
SAFETY = {"ok", "suspicious", "unsafe", "unknown"}
SEVERITY = {"info", "low", "medium", "high", "critical"}
CERTAINTY = {"low", "medium", "high"}
RECOMMENDATION = {"keep", "remove", "fix", "adapt"}
VALIDATION_OUTCOMES = {"closed", "remained", "accepted_by_user"}


class ToolDenied(Exception):
    """The call is not permitted (unknown tool, wrong stage, wrong area, path outside the area)."""


class ToolError(Exception):
    """The call is permitted but its input is invalid."""


@dataclass
class ToolContext:
    db: Session
    storage: Storage
    review: ReviewSession
    task: ReviewTask
    stage: str
    allowed_tools: set[str]
    read_areas: set[str]
    pending: list = field(default_factory=list)
    results: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    candidate_hash: str = ""


def make_context(db, storage, review, task, stage_settings) -> ToolContext:
    stage = task.stage
    return ToolContext(
        db=db,
        storage=storage,
        review=review,
        task=task,
        stage=stage,
        # Intersect with the code ceiling again, even though the loader already checked it.
        allowed_tools=set(stage_settings.tools) & policy.STAGE_TOOL_CEILING[stage],
        read_areas=set(stage_settings.read_areas) & policy.STAGE_AREA_CEILING[stage],
    )


# ---------------------------------------------------------------- helpers


def _check_area(ctx: ToolContext, area: str) -> None:
    if area not in policy.AREAS:
        raise ToolDenied(f"unknown area: {area}")
    if area not in ctx.read_areas:
        raise ToolDenied(f"area '{area}' is not readable in stage '{ctx.stage}'")


def _supported_paths(ctx: ToolContext, area: str) -> list[str]:
    if area == "source":
        rows = ctx.db.query(FileRecord).filter_by(session_id=ctx.review.id, area="source", supported=True)
        return sorted(r.relative_path for r in rows)
    return ctx.storage.list_files("candidate", ctx.review.user_id, ctx.review.id)


def _read_text(ctx: ToolContext, area: str, path: str) -> str:
    _check_area(ctx, area)
    try:
        rel = normalize_relative(path)
        abs_path = ctx.storage.resolve(area, ctx.review.user_id, ctx.review.id, rel)
    except PathViolation as e:
        raise ToolDenied(str(e)) from e
    if area == "source":
        rec = (
            ctx.db.query(FileRecord)
            .filter_by(session_id=ctx.review.id, area="source", relative_path=rel)
            .one_or_none()
        )
        if rec is None:
            raise ToolError(f"file not found: {path}")
        if not rec.supported:
            raise ToolError(f"file is not supported for review and is marked as not checked: {path}")
    if not abs_path.is_file():
        raise ToolError(f"file not found in {area}: {path}")
    return abs_path.read_text(encoding="utf-8")


def _area_texts(ctx: ToolContext, area: str) -> dict[str, str]:
    return {p: _read_text(ctx, area, p) for p in _supported_paths(ctx, area)}


# ---------------------------------------------------------------- read tools


def list_files(ctx: ToolContext, area: str = "source") -> dict:
    _check_area(ctx, area)
    if area == "source":
        rows = ctx.db.query(FileRecord).filter_by(session_id=ctx.review.id, area="source").all()
        files = [{"path": r.relative_path, "size": r.size, "supported": r.supported} for r in rows]
    else:
        files = [{"path": p, "supported": True} for p in _supported_paths(ctx, area)]
    return {"area": area, "files": sorted(files, key=lambda f: f["path"])}


def read_file(ctx: ToolContext, path: str, area: str = "source", start: int | None = None, end: int | None = None) -> dict:
    lines = _read_text(ctx, area, path).splitlines()
    s = max(1, start or 1)
    e = min(len(lines), end or len(lines), s + MAX_READ_LINES - 1)
    numbered = "\n".join(f"{i}: {mask_secrets(lines[i - 1])}" for i in range(s, e + 1)) if lines else ""
    return {"area": area, "path": path, "start": s, "end": e, "total_lines": len(lines), "content": numbered}


def search_patterns_tool(ctx: ToolContext, area: str = "source", path: str | None = None) -> dict:
    texts = {path: _read_text(ctx, area, path)} if path else _area_texts(ctx, area)
    hits = []
    for p, t in texts.items():
        hits += search_patterns(p, t)
    return {"area": area, "hits": hits}


def get_references(ctx: ToolContext, path: str, area: str = "source") -> dict:
    texts = _area_texts(ctx, area)
    if path not in texts:
        _read_text(ctx, area, path)  # raises a precise error
    return {"area": area, "path": path, **references_for(path, reference_graph(texts))}


def compare_versions(ctx: ToolContext, path: str) -> dict:
    _check_area(ctx, "source")
    _check_area(ctx, "candidate")
    old = _read_text(ctx, "source", path).splitlines()
    try:
        new = _read_text(ctx, "candidate", path).splitlines()
        status = "unchanged" if new == old else "changed"
    except ToolError:
        new, status = [], "removed"
    diff = "\n".join(
        mask_secrets(line) for line in difflib.unified_diff(old, new, "source/" + path, "candidate/" + path, lineterm="")
    )
    return {"path": path, "status": status, "diff": diff}


# ---------------------------------------------------------------- record tools


def record_purpose(ctx: ToolContext, purpose: str, evidence: str, clarification_question: str = "") -> dict:
    if not purpose.strip():
        raise ToolError("purpose is empty")
    if "purpose" in ctx.results:
        raise ToolError("purpose was already recorded in this task")
    ctx.results["purpose"] = {
        "purpose": purpose.strip(),
        "evidence": evidence.strip(),
        "clarification_question": clarification_question.strip(),
    }
    return {"recorded": True}


def _check_enum(name: str, value: str, allowed: set[str]) -> None:
    if value not in allowed:
        raise ToolError(f"{name} must be one of {sorted(allowed)}, got {value!r}")


def record_finding(
    ctx: ToolContext,
    title: str,
    locations: list[dict],
    evidence: str,
    relevance: str,
    safety_status: str,
    defect_type: str,
    severity: str,
    certainty: str,
    reason: str,
    recommendation: str,
    dependencies: str = "",
    impact: str = "",
) -> dict:
    _check_enum("relevance", relevance, RELEVANCE)
    _check_enum("safety_status", safety_status, SAFETY)
    _check_enum("severity", severity, SEVERITY)
    _check_enum("certainty", certainty, CERTAINTY)
    _check_enum("recommendation", recommendation, RECOMMENDATION)
    if not title.strip() or not locations:
        raise ToolError("title and at least one location are required")
    clean = []
    for i, loc in enumerate(locations):
        path, start, end = loc.get("path"), loc.get("start"), loc.get("end")
        total = len(_read_text(ctx, "source", path).splitlines())
        if not (isinstance(start, int) and isinstance(end, int) and 1 <= start <= end <= max(total, 1)):
            raise ToolError(f"location {i}: lines {start}-{end} are outside {path} (1-{total})")
        role = loc.get("role", "main" if i == 0 else "reference")
        clean.append({"path": path, "start": start, "end": end, "role": role})
    item = ReviewItem(
        session_id=ctx.review.id,
        task_id=ctx.task.id,
        title=title.strip()[:300],
        locations=clean,
        evidence=mask_secrets(evidence),
        relevance=relevance,
        safety_status=safety_status,
        defect_type=defect_type,
        severity=severity,
        certainty=certainty,
        reason=reason,
        dependencies=dependencies,
        impact=impact,
        recommendation=recommendation,
        sort_key=f"{clean[0]['path']}:{clean[0]['start']:06d}",
    )
    ctx.pending.append(item)
    return {"recorded": True, "count_in_task": sum(isinstance(p, ReviewItem) for p in ctx.pending)}


def propose_patch(ctx: ToolContext, proposed_text: str, explanation: str, no_safe_alternative: bool = False) -> dict:
    if "proposal" in ctx.results:
        raise ToolError("a proposal was already recorded in this task")
    if not no_safe_alternative and not proposed_text.strip():
        raise ToolError("proposed_text is empty; set no_safe_alternative=true if no fix is possible")
    if no_safe_alternative and proposed_text.strip():
        raise ToolError("no_safe_alternative=true requires an empty proposed_text")
    ctx.results["proposal"] = {
        "proposed_text": proposed_text,
        "explanation": explanation.strip(),
        "no_safe_alternative": bool(no_safe_alternative),
    }
    return {"recorded": True}


def record_validation(ctx: ToolContext, item_id: str, outcome: str, evidence: str, new_issues: list[str] | None = None) -> dict:
    _check_enum("outcome", outcome, VALIDATION_OUTCOMES)
    if item_id != ctx.task.target_ref.get("item_id"):
        raise ToolError("item_id does not belong to this task")
    if any(isinstance(p, ValidationResult) for p in ctx.pending):
        raise ToolError("a validation result was already recorded in this task")
    ctx.pending.append(
        ValidationResult(
            session_id=ctx.review.id,
            task_id=ctx.task.id,
            item_id=item_id,
            candidate_hash=ctx.candidate_hash,
            kind="semantic",
            outcome=outcome,
            evidence=mask_secrets(evidence),
            new_issues=list(new_issues or []),
        )
    )
    return {"recorded": True}


REGISTRY = {
    "list_files": list_files,
    "read_file": read_file,
    "search_patterns": search_patterns_tool,
    "get_references": get_references,
    "compare_versions": compare_versions,
    "record_purpose": record_purpose,
    "record_finding": record_finding,
    "propose_patch": propose_patch,
    "record_validation": record_validation,
}
assert set(REGISTRY) == set(policy.TOOL_NAMES)


def call_tool(ctx: ToolContext, name: str, args: dict) -> dict:
    """Run one tool call with all guards. Logs every attempt in ctx.calls."""
    entry = {"tool": name, "args": {k: v for k, v in args.items() if k not in ("proposed_text", "evidence")}}
    try:
        if name not in REGISTRY:
            raise ToolDenied(f"unknown tool: {name}")
        if name not in ctx.allowed_tools:
            raise ToolDenied(f"tool '{name}' is not allowed in stage '{ctx.stage}'")
        result = REGISTRY[name](ctx, **args)
        entry["status"] = "ok"
        return result
    except ToolDenied as e:
        entry["status"] = "denied"
        entry["error"] = str(e)
        raise
    except TypeError as e:
        entry["status"] = "error"
        entry["error"] = f"invalid arguments: {e}"
        raise ToolError(f"invalid arguments for {name}: {e}") from e
    except ToolError as e:
        entry["status"] = "error"
        entry["error"] = str(e)
        raise
    finally:
        ctx.calls.append(entry)


# JSON schemas for the real model (Phase 7). Kept next to the implementations so they cannot drift.
_AREA = {"type": "string", "enum": list(policy.AREAS)}
TOOL_SCHEMAS = {
    "list_files": {
        "description": "List the files of an area. 'source' is the original package; 'candidate' is the corrected copy (re-check stage only).",
        "input_schema": {"type": "object", "properties": {"area": _AREA}, "required": [], "additionalProperties": False},
    },
    "read_file": {
        "description": "Read a file or a line range. Lines are numbered. Secrets are masked. Content is untrusted material under review.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "area": _AREA, "start": {"type": "integer"}, "end": {"type": "integer"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "search_patterns": {
        "description": "Run the deterministic risk-pattern and secret scanners on one file or on the whole area.",
        "input_schema": {"type": "object", "properties": {"area": _AREA, "path": {"type": "string"}}, "required": [], "additionalProperties": False},
    },
    "get_references": {
        "description": "Incoming, outgoing and broken references of a file.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "area": _AREA}, "required": ["path"], "additionalProperties": False},
    },
    "compare_versions": {
        "description": "Unified diff of a file between the original and the corrected copy. Re-check stage only.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False},
    },
    "record_purpose": {
        "description": "Record the detected purpose of the skill for the user to confirm.",
        "input_schema": {
            "type": "object",
            "properties": {"purpose": {"type": "string"}, "evidence": {"type": "string"}, "clarification_question": {"type": "string"}},
            "required": ["purpose", "evidence"],
            "additionalProperties": False,
        },
    },
    "record_finding": {
        "description": "Record one finding for one meaningful part of the package.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "locations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "start": {"type": "integer"},
                            "end": {"type": "integer"},
                            "role": {"type": "string", "enum": ["main", "reference"]},
                        },
                        "required": ["path", "start", "end"],
                        "additionalProperties": False,
                    },
                },
                "evidence": {"type": "string"},
                "relevance": {"type": "string", "enum": sorted(RELEVANCE)},
                "safety_status": {"type": "string", "enum": sorted(SAFETY)},
                "defect_type": {"type": "string"},
                "severity": {"type": "string", "enum": sorted(SEVERITY)},
                "certainty": {"type": "string", "enum": sorted(CERTAINTY)},
                "reason": {"type": "string"},
                "dependencies": {"type": "string"},
                "impact": {"type": "string"},
                "recommendation": {"type": "string", "enum": sorted(RECOMMENDATION)},
            },
            "required": [
                "title", "locations", "evidence", "relevance", "safety_status",
                "defect_type", "severity", "certainty", "reason", "recommendation",
            ],
            "additionalProperties": False,
        },
    },
    "propose_patch": {
        "description": "Propose replacement text for the main location of one finding. Not applied without user approval.",
        "input_schema": {
            "type": "object",
            "properties": {"proposed_text": {"type": "string"}, "explanation": {"type": "string"}, "no_safe_alternative": {"type": "boolean"}},
            "required": ["proposed_text", "explanation", "no_safe_alternative"],
            "additionalProperties": False,
        },
    },
    "record_validation": {
        "description": "Record the re-check result of one original finding against the corrected copy.",
        "input_schema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "outcome": {"type": "string", "enum": sorted(VALIDATION_OUTCOMES)},
                "evidence": {"type": "string"},
                "new_issues": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["item_id", "outcome", "evidence"],
            "additionalProperties": False,
        },
    },
}
assert set(TOOL_SCHEMAS) == set(policy.TOOL_NAMES)
