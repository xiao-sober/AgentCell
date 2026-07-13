"""Deterministic in-memory registry for immutable Agent declarations."""

from __future__ import annotations

from collections.abc import Iterable

from agentcell.agents.models import AgentSpec
from agentcell.errors import AgentNotFoundError, AgentRegistrationError


class AgentRegistry:
    """Resolve stable Agent IDs without storing any Run state."""

    def __init__(self, specs: Iterable[AgentSpec] = ()) -> None:
        self._specs: dict[str, AgentSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: AgentSpec) -> None:
        if spec.id in self._specs:
            raise AgentRegistrationError(f"Agent {spec.id!r} is already registered")
        self._specs[spec.id] = spec

    def get(self, agent_id: str) -> AgentSpec:
        try:
            return self._specs[agent_id]
        except KeyError as error:
            raise AgentNotFoundError(agent_id) from error

    def list(self) -> tuple[AgentSpec, ...]:
        return tuple(self._specs[agent_id] for agent_id in sorted(self._specs))
