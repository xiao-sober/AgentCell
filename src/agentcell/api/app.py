"""FastAPI application factory over transport-neutral AgentCell services."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from agentcell import __version__
from agentcell.api.errors import install_error_handlers
from agentcell.api.routes import (
    agent_router,
    approval_router,
    change_router,
    conversation_router,
    memory_router,
    provider_router,
    run_router,
    system_router,
    tool_router,
)
from agentcell.application import AgentCellApplication, build_application


def create_app(application: AgentCellApplication | None = None) -> FastAPI:
    """Create an isolated API app; no global runtime singleton is constructed."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        current = application
        if current is None:
            current = await build_application(
                config=Path(os.getenv("AGENTCELL_CONFIG", "agentcell.toml")),
                database_url=os.getenv("AGENTCELL_DATABASE_URL"),
                offline_fake=os.getenv("AGENTCELL_OFFLINE_FAKE", "").casefold()
                in {"1", "true", "yes"},
            )
        app.state.agentcell = current
        try:
            yield
        finally:
            await current.close()

    app = FastAPI(
        title="AgentCell API",
        version=__version__,
        lifespan=lifespan,
    )
    install_error_handlers(app)
    for router in (
        run_router,
        approval_router,
        change_router,
        conversation_router,
        agent_router,
        memory_router,
        provider_router,
        tool_router,
        system_router,
    ):
        app.include_router(router, prefix="/api")
    return app
