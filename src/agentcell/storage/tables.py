"""SQLAlchemy ORM tables kept separate from AgentCell domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Persist timezone-aware datetimes as sortable UTC ISO-8601 text."""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        del dialect
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def process_result_value(self, value: str | None, dialect: Dialect) -> datetime | None:
        del dialect
        if value is None:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class Base(DeclarativeBase):
    """Declarative metadata root used by Alembic."""


class RunRow(Base):
    """Mutable Run projection and atomic event-sequence allocator."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created', 'running', 'waiting_approval', 'paused', "
            "'completed', 'failed', 'cancelled')",
            name="ck_runs_status",
        ),
        CheckConstraint("next_event_sequence >= 1", name="ck_runs_next_event_sequence"),
        Index("ix_runs_conversation_id", "conversation_id"),
        Index("ix_runs_parent_run_id", "parent_run_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    next_event_sequence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )


class RunEventRow(Base):
    """Append-only persisted domain event."""

    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_events_run_sequence"),
        CheckConstraint("sequence >= 1", name="ck_run_events_sequence"),
        CheckConstraint("payload_version >= 1", name="ck_run_events_payload_version"),
        Index("ix_run_events_run_occurred", "run_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ApprovalRow(Base):
    """Mutable decision projection for one deferred tool approval."""

    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("run_id", "provider_call_id", name="uq_approvals_run_provider_call"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_approvals_status",
        ),
        Index("ix_approvals_run_status", "run_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
    )
    provider_call_id: Mapped[str] = mapped_column(String(255), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class CheckpointRow(Base):
    """Immutable restart-safe Run checkpoint."""

    __tablename__ = "checkpoints"
    __table_args__ = (
        UniqueConstraint("run_id", "event_sequence", name="uq_checkpoints_run_sequence"),
        CheckConstraint("event_sequence >= 1", name="ck_checkpoints_sequence"),
        Index("ix_checkpoints_run_created", "run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
    )
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ToolExecutionRow(Base):
    """Durable tool-call ledger used to prevent non-idempotent re-execution."""

    __tablename__ = "tool_executions"
    __table_args__ = (
        UniqueConstraint("run_id", "provider_call_id", name="uq_tool_executions_run_provider_call"),
        CheckConstraint(
            "status IN ('started', 'completed', 'failed')",
            name="ck_tool_executions_status",
        ),
        Index("ix_tool_executions_run_status", "run_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
    )
    provider_call_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    call_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotent: Mapped[bool] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class ArtifactRow(Base):
    """Content-address-verified metadata for a file-backed Artifact."""

    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="ck_artifacts_size"),
        UniqueConstraint("sha256", "size_bytes", name="uq_artifacts_hash_size"),
        Index("ix_artifacts_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    suggested_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class MemoryItemRow(Base):
    """Scoped memory record indexed separately by SQLite FTS5."""

    __tablename__ = "memory_items"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('working', 'conversation', 'episodic', 'semantic')",
            name="ck_memory_items_kind",
        ),
        CheckConstraint("importance >= 0 AND importance <= 1", name="ck_memory_importance"),
        Index("ix_memory_scope", "user_id", "project_id", "agent_id"),
        Index("ix_memory_expiry", "expires_at"),
        Index("ix_memory_hash", "normalized_hash"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[str] = mapped_column(String(512), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content: Mapped[str] = mapped_column(Text(), nullable=False)
    normalized_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    importance: Mapped[float] = mapped_column(Float(), nullable=False)
    sensitive: Mapped[bool] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
