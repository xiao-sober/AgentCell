"""Core Run domain models independent of storage and transport adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from agentcell.errors import InvalidRunTimestampError
from agentcell.kernel.identity import RunExecutionIdentity
from agentcell.kernel.lifecycle import RunStatus, ensure_transition


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


class Run(BaseModel):
    """Immutable domain projection for one Agent execution Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID
    agent_id: str = Field(min_length=1)
    execution_identity: RunExecutionIdentity | None = None
    parent_run_id: UUID | None = None
    status: RunStatus = RunStatus.CREATED
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime, info: ValidationInfo) -> datetime:
        field_name = info.field_name or "timestamp"
        return _normalize_utc(value, field_name=field_name)

    @model_validator(mode="after")
    def validate_relationships_and_time(self) -> Run:
        if self.parent_run_id == self.id:
            raise ValueError("parent_run_id cannot equal id")
        if (
            self.execution_identity is not None
            and self.execution_identity.agent_spec.id != self.agent_id
        ):
            raise ValueError("agent_id does not match execution identity")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be earlier than created_at")
        return self

    def transition_to(self, target: RunStatus, *, at: datetime | None = None) -> Run:
        """Return a new Run after validating the lifecycle transition and UTC time."""

        ensure_transition(self.status, target)
        transitioned_at = _normalize_utc(at or _utc_now(), field_name="updated_at")
        if transitioned_at < self.updated_at:
            raise InvalidRunTimestampError

        values = self.model_dump()
        values.update(status=target, updated_at=transitioned_at)
        return Run.model_validate(values)
