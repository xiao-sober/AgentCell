"""Unified Task Router preview, creation, confirmation, and rejection endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from agentcell.api.dependencies import ApplicationDependency
from agentcell.api.schemas import (
    RunResponse,
    TaskRouteConfirmationRequest,
    TaskRoutePreviewRequest,
    TaskRouteResponse,
)
from agentcell.routing import TaskRouteRequest, TaskRouteStatus

router = APIRouter(tags=["tasks"])


def _request(body: TaskRoutePreviewRequest, application: ApplicationDependency) -> TaskRouteRequest:
    values = body.model_dump(exclude={"budget"})
    values["budget"] = body.budget or application.teams.get("software").default_budget
    return TaskRouteRequest.model_validate(values)


@router.post("/task-routes", response_model=TaskRouteResponse)
async def preview_task_route(
    body: TaskRoutePreviewRequest,
    application: ApplicationDependency,
) -> TaskRouteResponse:
    decision = await application.routing.preview(_request(body, application))
    return TaskRouteResponse(authoritative=False, decision=decision)


@router.post(
    "/tasks",
    response_model=TaskRouteResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_task(
    body: TaskRoutePreviewRequest,
    application: ApplicationDependency,
) -> TaskRouteResponse:
    prepared = await application.routing.prepare(_request(body, application))
    if prepared.decision.status is TaskRouteStatus.READY:
        await application.start_task(prepared)
    return TaskRouteResponse(
        authoritative=True,
        run=RunResponse.from_domain(prepared.root),
        decision=prepared.decision,
    )


@router.post(
    "/tasks/{run_id}/confirm",
    response_model=TaskRouteResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def confirm_task_route(
    run_id: UUID,
    body: TaskRouteConfirmationRequest,
    application: ApplicationDependency,
) -> TaskRouteResponse:
    prepared = await application.routing.confirm(
        run_id,
        decision_hash=body.decision_hash,
        authorized_lease=body.authorized_lease,
    )
    await application.start_task(prepared)
    return TaskRouteResponse(
        authoritative=True,
        run=RunResponse.from_domain(prepared.root),
        decision=prepared.decision,
    )


@router.post("/tasks/{run_id}/reject", response_model=RunResponse)
async def reject_task_route(
    run_id: UUID,
    body: TaskRouteConfirmationRequest,
    application: ApplicationDependency,
) -> RunResponse:
    run = await application.routing.reject(run_id, decision_hash=body.decision_hash)
    return RunResponse.from_domain(run)
