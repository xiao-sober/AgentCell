"""HTTP route modules grouped by stable frontend-facing resources."""

from agentcell.api.routes.changes import router as change_router
from agentcell.api.routes.conversations import router as conversation_router
from agentcell.api.routes.resources import (
    agent_router,
    memory_router,
    provider_router,
    tool_router,
)
from agentcell.api.routes.runs import approval_router
from agentcell.api.routes.runs import router as run_router
from agentcell.api.routes.system import router as system_router
from agentcell.api.routes.tasks import router as task_router

__all__ = [
    "agent_router",
    "approval_router",
    "change_router",
    "conversation_router",
    "memory_router",
    "provider_router",
    "run_router",
    "system_router",
    "task_router",
    "tool_router",
]
