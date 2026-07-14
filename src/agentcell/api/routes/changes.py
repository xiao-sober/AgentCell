"""Read-only change queries and explicit hash-safe rollback command."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from agentcell.api.dependencies import ApplicationDependency
from agentcell.api.schemas import ChangeRevertRequest
from agentcell.changes import ChangeDetails, FileChange
from agentcell.errors import RunNotFoundError
from agentcell.kernel.event_recorder import RunEventRecorder

router = APIRouter(tags=["changes"])


@router.get("/runs/{run_id}/changes", response_model=list[FileChange])
async def list_run_changes(
    run_id: UUID,
    application: ApplicationDependency,
) -> list[FileChange]:
    if await application.get_run(run_id) is None:
        raise RunNotFoundError(str(run_id))
    return list(await application.changes.list_for_run(run_id))


@router.get("/changes/{change_id}", response_model=ChangeDetails)
async def get_change(
    change_id: UUID,
    application: ApplicationDependency,
) -> ChangeDetails:
    return await application.changes.details(change_id)


@router.get("/changes/{change_id}/diff", response_class=PlainTextResponse)
async def get_change_diff(
    change_id: UUID,
    application: ApplicationDependency,
) -> str:
    return (await application.changes.details(change_id)).diff


@router.post("/changes/{change_id}/revert", response_model=FileChange)
async def revert_change(
    change_id: UUID,
    body: ChangeRevertRequest,
    application: ApplicationDependency,
) -> FileChange:
    details = await application.changes.details(change_id)
    return await application.changes.revert(
        change_id,
        workspace=Path(details.change_set.workspace),
        lease=body.lease,
        events=RunEventRecorder(application.database, details.change.run_id),
    )
