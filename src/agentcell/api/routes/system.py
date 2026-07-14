"""Health and version routes with no sensitive configuration output."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from agentcell import __version__
from agentcell.api.dependencies import ApplicationDependency
from agentcell.api.schemas import HealthResponse, VersionResponse

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
async def health(application: ApplicationDependency, response: Response) -> HealthResponse:
    healthy = await application.healthy()
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if healthy else "degraded",
        database="ok" if healthy else "unavailable",
    )


@router.get("/version", response_model=VersionResponse)
async def version() -> VersionResponse:
    return VersionResponse(version=__version__)
