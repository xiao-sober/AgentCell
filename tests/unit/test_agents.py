"""Stateless Agent declarations and deterministic registry behavior."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentcell.agents import AgentRegistry, AgentSpec
from agentcell.errors import AgentNotFoundError, AgentRegistrationError
from agentcell.policy import Capability


def _spec(agent_id: str) -> AgentSpec:
    return AgentSpec(
        id=agent_id,
        name=agent_id.title(),
        description="Read-only test Agent.",
        model_ref="fake",
        instructions="Inspect the workspace.",
        tools=("workspace.read",),
        capabilities=frozenset({Capability.FILESYSTEM_READ}),
    )


def test_agent_spec_rejects_duplicate_tools() -> None:
    with pytest.raises(ValidationError, match="Agent tools must be unique"):
        AgentSpec(
            id="reader",
            name="Reader",
            description="Read-only test Agent.",
            model_ref="fake",
            instructions="Inspect the workspace.",
            tools=("workspace.read", "workspace.read"),
        )


def test_registry_sorts_and_rejects_duplicate_or_unknown_ids() -> None:
    registry = AgentRegistry([_spec("reviewer"), _spec("coordinator")])

    assert [spec.id for spec in registry.list()] == ["coordinator", "reviewer"]
    assert registry.get("reviewer").name == "Reviewer"
    with pytest.raises(AgentRegistrationError):
        registry.register(_spec("reviewer"))
    with pytest.raises(AgentNotFoundError):
        registry.get("missing")
