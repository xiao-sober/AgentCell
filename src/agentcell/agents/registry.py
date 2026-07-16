"""Deterministic in-memory registry for immutable Agent declarations."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from agentcell.agents.models import AgentSpec
from agentcell.errors import AgentNotFoundError, AgentRegistrationError


class AgentSource(StrEnum):
    """Origin of the effective declaration exposed to product surfaces."""

    BUILTIN = "builtin"
    PERSISTED = "persisted"
    OVERRIDE = "override"


class AgentVisibility(StrEnum):
    """Whether an Agent is a normal user choice or an internal runtime role."""

    PUBLIC = "public"
    INTERNAL = "internal"


class RegisteredAgent(BaseModel):
    """Agent declaration plus source and product visibility metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec: AgentSpec
    source: AgentSource
    visibility: AgentVisibility
    status: str = "available"


class AgentRegistry:
    """Resolve stable Agent IDs without storing any Run state."""

    def __init__(
        self,
        specs: Iterable[AgentSpec] = (),
        *,
        source: AgentSource = AgentSource.BUILTIN,
    ) -> None:
        self._entries: dict[str, RegisteredAgent] = {}
        for spec in specs:
            self.register(spec, source=source)

    def register(
        self,
        spec: AgentSpec,
        *,
        source: AgentSource = AgentSource.BUILTIN,
        visibility: AgentVisibility = AgentVisibility.PUBLIC,
    ) -> None:
        if spec.id in self._entries:
            raise AgentRegistrationError(f"Agent {spec.id!r} is already registered")
        self._entries[spec.id] = RegisteredAgent(
            spec=spec,
            source=source,
            visibility=visibility,
        )

    def get(self, agent_id: str) -> AgentSpec:
        return self.get_entry(agent_id).spec

    def get_entry(self, agent_id: str) -> RegisteredAgent:
        try:
            return self._entries[agent_id]
        except KeyError as error:
            raise AgentNotFoundError(agent_id) from error

    def replace(
        self,
        spec: AgentSpec,
        *,
        source: AgentSource | None = None,
        visibility: AgentVisibility | None = None,
    ) -> None:
        """Replace an existing declaration without mutating active Run state."""

        current = self.get_entry(spec.id)
        self._entries[spec.id] = RegisteredAgent(
            spec=spec,
            source=source or current.source,
            visibility=visibility or current.visibility,
            status=current.status,
        )

    def list(self) -> tuple[AgentSpec, ...]:
        """Return all effective specs for runtime authorization, including internal roles."""

        return tuple(self._entries[agent_id].spec for agent_id in sorted(self._entries))

    def list_entries(self, *, include_internal: bool = False) -> tuple[RegisteredAgent, ...]:
        """Return product metadata, hiding internal roles unless explicitly requested."""

        return tuple(
            entry
            for agent_id in sorted(self._entries)
            if (entry := self._entries[agent_id]).visibility is AgentVisibility.PUBLIC
            or include_internal
        )
