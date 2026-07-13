"""Minimal built-in Agent declarations used by the M1 runtime."""

from __future__ import annotations

from agentcell.agents.models import AgentSpec
from agentcell.policy import Capability


def coordinator_spec(*, model_ref: str) -> AgentSpec:
    """Return a read-only coordinator suitable for the stage 5 runtime."""

    return AgentSpec(
        id="coordinator",
        name="Coordinator",
        description="Plans and completes one local software-project task.",
        model_ref=model_ref,
        instructions=(
            "Work only inside the supplied workspace. Use the registered read-only tools when "
            "needed. Never claim to have modified files. Return a concise final result."
        ),
        tools=("workspace.list", "workspace.read", "workspace.search"),
        capabilities=frozenset({Capability.FILESYSTEM_READ}),
        max_steps=20,
        max_children=0,
        max_depth=0,
    )
