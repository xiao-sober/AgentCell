"""Stateless Agent declarations independent of Run-scoped mutable state."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentcell.policy import Capability


class AgentSpec(BaseModel):
    """Immutable role, model, tool, and hard loop limits for one Agent type."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    model_ref: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    tools: tuple[str, ...] = ()
    capabilities: frozenset[Capability] = frozenset()
    max_steps: int = Field(default=20, ge=1, le=200, strict=True)
    max_children: int = Field(default=0, ge=0, le=20, strict=True)
    max_depth: int = Field(default=0, ge=0, le=10, strict=True)

    @field_validator("tools")
    @classmethod
    def unique_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("Agent tools must be unique")
        return value
