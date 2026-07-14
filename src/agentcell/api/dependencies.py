"""FastAPI dependency adapters for the shared application container."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from agentcell.application import AgentCellApplication


def get_application(request: Request) -> AgentCellApplication:
    application = getattr(request.app.state, "agentcell", None)
    if not isinstance(application, AgentCellApplication):
        raise RuntimeError("AgentCell application is not initialized")
    return application


ApplicationDependency = Annotated[AgentCellApplication, Depends(get_application)]
