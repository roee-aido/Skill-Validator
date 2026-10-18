"""Data model (Architecture.md section 8). SQLite for the local setup (O2)."""

import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LoginToken(Base):
    """One-time login links (local development only, O2)."""

    __tablename__ = "login_tokens"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(320))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReviewSession(Base):
    __tablename__ = "review_sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    skill_name: Mapped[str] = mapped_column(String(200), default="")
    usage_description: Mapped[str] = mapped_column(Text, default="")

    purpose: Mapped[str] = mapped_column(Text, default="")
    purpose_evidence: Mapped[str] = mapped_column(Text, default="")
    purpose_question: Mapped[str] = mapped_column(Text, default="")
    purpose_status: Mapped[str] = mapped_column(String(20), default="none")  # none/proposed/confirmed

    source_hash: Mapped[str] = mapped_column(String(64), default="")
    config_version: Mapped[str] = mapped_column(String(50), default="")
    config_sha256: Mapped[str] = mapped_column(String(64), default="")
    model_id: Mapped[str] = mapped_column(String(100), default="")

    # intake / purpose / analyze / decisions / generating / revalidate / delivered / rejected
    stage: Mapped[str] = mapped_column(String(30), default="intake")
    # running / waiting_user / stopped / done / rejected
    status: Mapped[str] = mapped_column(String(20), default="running")
    stop_reason: Mapped[str] = mapped_column(Text, default="")
    final_status: Mapped[str] = mapped_column(String(30), default="")
    intake_error: Mapped[str] = mapped_column(Text, default="")
    limits: Mapped[dict] = mapped_column(JSON, default=dict)
    unchecked_resources: Mapped[list] = mapped_column(JSON, default=list)

    # Cumulative ceilings and spend (D8). Spend never resets.
    budget_tokens_ceiling: Mapped[int] = mapped_column(Integer, default=0)
    budget_cost_ceiling: Mapped[float] = mapped_column(Float, default=0.0)
    budget_minutes_ceiling: Mapped[float] = mapped_column(Float, default=0.0)
    spent_tokens_total: Mapped[int] = mapped_column(Integer, default=0)
    spent_cost_total: Mapped[float] = mapped_column(Float, default=0.0)
    spent_minutes_total: Mapped[float] = mapped_column(Float, default=0.0)

    decisions_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    files: Mapped[list["FileRecord"]] = relationship(cascade="all, delete-orphan")
    items: Mapped[list["ReviewItem"]] = relationship(cascade="all, delete-orphan")
    tasks: Mapped[list["ReviewTask"]] = relationship(cascade="all, delete-orphan")


class FileRecord(Base):
    __tablename__ = "file_records"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("review_sessions.id"))
    area: Mapped[str] = mapped_column(String(20))  # source / candidate
    relative_path: Mapped[str] = mapped_column(String(500))
    type: Mapped[str] = mapped_column(String(30))
    size: Mapped[int] = mapped_column(Integer)
    hash: Mapped[str] = mapped_column(String(64))
    supported: Mapped[bool] = mapped_column(Boolean, default=True)
    # pending / checked / not_checked / unsupported
    coverage_status: Mapped[str] = mapped_column(String(20), default="pending")


class ReviewItem(Base):
    __tablename__ = "review_items"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("review_sessions.id"))
    task_id: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(300))
    locations: Mapped[list] = mapped_column(JSON)  # [{"path":..,"start":..,"end":..,"role":"main"|"reference"}]
    evidence: Mapped[str] = mapped_column(Text)
    relevance: Mapped[str] = mapped_column(String(20))  # relevant / not_relevant
    safety_status: Mapped[str] = mapped_column(String(20))  # ok / suspicious / unsafe / unknown
    defect_type: Mapped[str] = mapped_column(String(50))
    severity: Mapped[str] = mapped_column(String(20))  # info / low / medium / high / critical
    certainty: Mapped[str] = mapped_column(String(20))  # low / medium / high
    reason: Mapped[str] = mapped_column(Text)
    dependencies: Mapped[str] = mapped_column(Text, default="")
    impact: Mapped[str] = mapped_column(Text, default="")
    recommendation: Mapped[str] = mapped_column(String(20))  # keep / remove / fix / adapt
    sort_key: Mapped[str] = mapped_column(String(600), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UserDecision(Base):
    __tablename__ = "user_decisions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("review_sessions.id"))
    item_id: Mapped[str] = mapped_column(ForeignKey("review_items.id"))
    # keep / keep_open / remove / fix / adapt
    action: Mapped[str] = mapped_column(String(20))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    proposed_text: Mapped[str] = mapped_column(Text, default="")
    proposal_explanation: Mapped[str] = mapped_column(Text, default="")
    no_safe_alternative: Mapped[bool] = mapped_column(Boolean, default=False)
    # approved / proposal_pending / proposed / rejected
    approval_status: Mapped[str] = mapped_column(String(20))
    source_version: Mapped[str] = mapped_column(String(64))
    decision_version: Mapped[int] = mapped_column(Integer)
    task_id: Mapped[str] = mapped_column(String(32), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ReviewTask(Base):
    __tablename__ = "review_tasks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("review_sessions.id"))
    stage: Mapped[str] = mapped_column(String(30))  # detect_purpose / analyze / propose_fix / revalidate
    task_type: Mapped[str] = mapped_column(String(30))
    target_ref: Mapped[dict] = mapped_column(JSON, default=dict)
    depends_on: Mapped[list] = mapped_column(JSON, default=list)
    # pending / running / done / failed / stale / skipped
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    input_hash: Mapped[str] = mapped_column(String(64), default="")
    result_ref: Mapped[str] = mapped_column(Text, default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Checkpoint(Base):
    __tablename__ = "checkpoints"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("review_sessions.id"))
    stage: Mapped[str] = mapped_column(String(30))
    completed_task_ids: Mapped[list] = mapped_column(JSON)
    pending_task_ids: Mapped[list] = mapped_column(JSON)
    source_hash: Mapped[str] = mapped_column(String(64))
    decisions_version: Mapped[int] = mapped_column(Integer)
    spent_tokens_total: Mapped[int] = mapped_column(Integer)
    spent_cost_total: Mapped[float] = mapped_column(Float)
    remaining_tokens: Mapped[int] = mapped_column(Integer)
    remaining_cost: Mapped[float] = mapped_column(Float)
    stop_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BudgetApproval(Base):
    __tablename__ = "budget_approvals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("review_sessions.id"))
    previous_ceiling: Mapped[dict] = mapped_column(JSON)
    new_ceiling: Mapped[dict] = mapped_column(JSON)
    spent_at_approval: Mapped[dict] = mapped_column(JSON)
    approved_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("review_sessions.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id"))
    config_version: Mapped[str] = mapped_column(String(50))
    model_id: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    processing_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    tool_calls: Mapped[list] = mapped_column(JSON, default=list)
    # completed / budget_exceeded / limit_reached / tool_error / model_error / refused / denied
    outcome: Mapped[str] = mapped_column(String(30), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ValidationResult(Base):
    """One row per check: structural check of the copy, or semantic re-check of one finding."""

    __tablename__ = "validation_results"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("review_sessions.id"))
    task_id: Mapped[str] = mapped_column(String(32), default="")
    item_id: Mapped[str] = mapped_column(String(32), default="")
    candidate_hash: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(20))  # structural / semantic
    # closed / remained / accepted_by_user / new / passed / failed
    outcome: Mapped[str] = mapped_column(String(30))
    evidence: Mapped[str] = mapped_column(Text, default="")
    new_issues: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OutputArtifact(Base):
    __tablename__ = "output_artifacts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("review_sessions.id"))
    kind: Mapped[str] = mapped_column(String(30))  # package_zip / review_md / changes_md
    area: Mapped[str] = mapped_column(String(20))  # delivery
    storage_path: Mapped[str] = mapped_column(String(500))
    hash: Mapped[str] = mapped_column(String(64))
    final_status: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def make_engine(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    return engine


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
