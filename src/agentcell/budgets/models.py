"""Validated Run budget limits, usage, remaining capacity, and snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
NonNegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class Budget(BaseModel):
    """Hard resource ceilings assigned to one Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_requests: NonNegativeInt
    max_input_tokens: NonNegativeInt
    max_output_tokens: NonNegativeInt
    max_total_tokens: NonNegativeInt
    max_tool_calls: NonNegativeInt
    max_duration_seconds: NonNegativeInt
    max_cost: NonNegativeDecimal | None = None
    max_children: NonNegativeInt
    max_depth: NonNegativeInt


class Usage(BaseModel):
    """Resources consumed by one Run, including restored checkpoint usage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requests: NonNegativeInt = 0
    input_tokens: NonNegativeInt = 0
    cache_write_tokens: NonNegativeInt = 0
    cache_read_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0
    tool_calls: NonNegativeInt = 0
    duration_seconds: NonNegativeFloat = 0.0
    cost: NonNegativeDecimal = Decimal("0")
    children: NonNegativeInt = 0
    max_depth_reached: NonNegativeInt = 0

    @computed_field
    @property
    def total_tokens(self) -> int:
        """Return combined input and output Token usage."""

        return self.input_tokens + self.output_tokens


class BudgetRemaining(BaseModel):
    """Non-negative capacity remaining at a snapshot boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requests: NonNegativeInt
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    total_tokens: NonNegativeInt
    tool_calls: NonNegativeInt
    duration_seconds: NonNegativeFloat
    cost: NonNegativeDecimal | None
    children: NonNegativeInt
    depth: NonNegativeInt


class BudgetSnapshot(BaseModel):
    """Checkpoint-safe view of limits, consumed usage, and remaining capacity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    budget: Budget
    used: Usage
    remaining: BudgetRemaining
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("captured_at")
    @classmethod
    def normalize_captured_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        return value.astimezone(UTC)
