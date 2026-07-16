"""Stateless Agent specifications, registries, built-ins, and factories."""

from agentcell.agents.builtins import (
    assistant_spec,
    coder_spec,
    coordinator_spec,
    finalizer_spec,
    researcher_spec,
    reviewer_spec,
    summarizer_spec,
)
from agentcell.agents.delegation import (
    AgentDelegation,
    DelegationKind,
    DelegationRequest,
    DelegationResult,
    DelegationStatus,
    HandoffRequest,
    HandoffResult,
    HandoffStage,
)
from agentcell.agents.factory import AgentFactory
from agentcell.agents.models import AgentSpec
from agentcell.agents.registry import (
    AgentRegistry,
    AgentSource,
    AgentVisibility,
    RegisteredAgent,
)
from agentcell.agents.teams import (
    TeamRegistry,
    TeamSpec,
    TeamStageSpec,
    is_test_repair_task,
    software_team_spec,
)

__all__ = [
    "AgentDelegation",
    "AgentFactory",
    "AgentRegistry",
    "AgentSource",
    "AgentSpec",
    "AgentVisibility",
    "RegisteredAgent",
    "DelegationKind",
    "DelegationRequest",
    "DelegationResult",
    "DelegationStatus",
    "HandoffRequest",
    "HandoffResult",
    "HandoffStage",
    "TeamRegistry",
    "TeamSpec",
    "TeamStageSpec",
    "is_test_repair_task",
    "assistant_spec",
    "coder_spec",
    "coordinator_spec",
    "finalizer_spec",
    "researcher_spec",
    "reviewer_spec",
    "summarizer_spec",
    "software_team_spec",
]
