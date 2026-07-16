"""Run, approval, replay, and AG-UI stream transport adapters."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, status
from fastapi.responses import StreamingResponse

from agentcell.api.dependencies import ApplicationDependency
from agentcell.api.schemas import (
    ApprovalDecisionRequest,
    ApprovalResponse,
    BranchRunRequest,
    ResumeRunRequest,
    RunCreateRequest,
    RunResponse,
)
from agentcell.api.sse import StreamCursor, stream_run_events
from agentcell.errors import ApprovalConflictError, RunNotFoundError
from agentcell.kernel.run_service import RunRequest
from agentcell.policy import Approval, ApprovalStatus
from agentcell.routing import TASK_ROUTER_AGENT_ID

router = APIRouter(prefix="/runs", tags=["runs"])
approval_router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.post("", response_model=RunResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    body: RunCreateRequest,
    application: ApplicationDependency,
) -> RunResponse:
    values = body.model_dump(exclude={"budget"})
    if body.budget is not None:
        values["budget"] = body.budget
    run = await application.start_run(RunRequest.model_validate(values))
    return RunResponse.from_domain(run)


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(run_id: UUID, application: ApplicationDependency) -> RunResponse:
    run = await application.get_run(run_id)
    if run is None:
        raise RunNotFoundError(str(run_id))
    return RunResponse.from_domain(run)


@router.get("/{run_id}/events")
async def run_events(
    run_id: UUID,
    request: Request,
    application: ApplicationDependency,
    after_sequence: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    if await application.get_run(run_id) is None:
        raise RunNotFoundError(str(run_id))
    cursor = StreamCursor.parse(last_event_id, after_sequence=after_sequence)
    return StreamingResponse(
        stream_run_events(application, request, run_id, cursor),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(run_id: UUID, application: ApplicationDependency) -> RunResponse:
    run = await application.runs.cancel(run_id)
    await application.conversations.release_if_managed(run)
    return RunResponse.from_domain(run)


@router.post("/{run_id}/resume", response_model=RunResponse)
async def resume_run(
    run_id: UUID,
    body: ResumeRunRequest,
    request: Request,
    application: ApplicationDependency,
) -> RunResponse:
    run = await application.get_run(run_id)
    if run is None:
        raise RunNotFoundError(str(run_id))
    request.state.run_context = {
        "run_id": str(run.id),
        "conversation_id": str(run.conversation_id),
        "run_status": run.status.value,
    }
    if run.status.is_terminal:
        raise ApprovalConflictError("Terminal Run cannot be resumed")
    if run.agent_id == TASK_ROUTER_AGENT_ID:
        if body.approval_id is None:
            if body.decision is not None:
                raise ApprovalConflictError("decision requires approval_id")
            routed = await application.routing.resume(run_id)
        else:
            if body.decision is None:
                raise ApprovalConflictError("approval_id requires decision")
            routed = await application.routing.decide_approval(
                run_id,
                body.approval_id,
                body.decision,
            )
        await application.conversations.record_task_result(routed)
        return RunResponse.from_domain(routed.run)
    if body.approval_id is None:
        if body.decision is not None:
            raise ApprovalConflictError("decision requires approval_id")
        result = await application.runs.resume_paused(run_id)
    else:
        if body.decision is None:
            raise ApprovalConflictError("approval_id requires decision")
        approvals = await application.approvals(run_id)
        if body.approval_id not in {approval.id for approval in approvals}:
            raise ApprovalConflictError("Approval does not belong to this Run")
        result = await application.runs.resume(body.approval_id, body.decision)
    await application.conversations.record_if_managed(result)
    return RunResponse.from_domain(result.run)


@router.post("/{run_id}/branch", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
async def branch_run(
    run_id: UUID,
    body: BranchRunRequest,
    application: ApplicationDependency,
) -> RunResponse:
    branched = await application.replay.branch(run_id, from_sequence=body.from_sequence)
    return RunResponse.from_domain(branched)


@router.get("/{run_id}/approvals", response_model=list[ApprovalResponse])
async def list_run_approvals(
    run_id: UUID,
    application: ApplicationDependency,
    approval_status: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
) -> list[ApprovalResponse]:
    if await application.get_run(run_id) is None:
        raise RunNotFoundError(str(run_id))
    approvals = await application.approvals(run_id, status=approval_status)
    return [_approval_response(item) for item in approvals]


@approval_router.post("/{approval_id}/decision", response_model=RunResponse)
async def decide_approval(
    approval_id: UUID,
    body: ApprovalDecisionRequest,
    application: ApplicationDependency,
) -> RunResponse:
    run = await application.decide_approval(approval_id, body.decision)
    return RunResponse.from_domain(run)


def _approval_response(approval: Approval) -> ApprovalResponse:
    return ApprovalResponse(
        id=approval.id,
        run_id=approval.run_id,
        provider_call_id=approval.provider_call_id,
        agent_id=approval.agent_id,
        agent_name=approval.agent_name,
        provider=approval.provider,
        model=approval.model,
        tool_name=approval.tool_name,
        arguments=approval.arguments,
        risk=approval.risk,
        impact=approval.impact,
        diff=approval.diff,
        status=approval.status,
        decision_source=approval.decision_source,
        idempotent=approval.idempotent,
        timeout_seconds=approval.timeout_seconds,
        created_at=approval.created_at,
        decided_at=approval.decided_at,
    )
