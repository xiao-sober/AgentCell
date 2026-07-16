"""Stage 9.2 durable file-change recording and safe rollback tests."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from agentcell.api import create_app
from agentcell.application import build_application
from agentcell.budgets import Budget, BudgetTracker
from agentcell.changes import FileChangeStatus, FileOperation
from agentcell.changes.git import GitWorkspaceInspector
from agentcell.changes.service import ChangeService
from agentcell.errors import ArtifactTooLargeError, ChangeConflictError
from agentcell.events import EventPayload, EventType
from agentcell.kernel.models import Run
from agentcell.policy import CapabilityLease
from agentcell.storage import Database, FileArtifactStore, RunRepository
from agentcell.tools import (
    ToolCall,
    ToolExecutionContext,
    ToolExecutor,
    ToolRegistry,
    WorkspaceWriteParams,
    register_workspace_tools,
)


@dataclass
class RecordingEvents:
    values: list[tuple[EventType, EventPayload]] = field(
        default_factory=lambda: list[tuple[EventType, EventPayload]]()
    )

    async def emit(self, event_type: EventType, payload: EventPayload) -> None:
        self.values.append((event_type, payload))


def _budget() -> BudgetTracker:
    return BudgetTracker(
        Budget(
            max_requests=0,
            max_input_tokens=0,
            max_output_tokens=0,
            max_total_tokens=0,
            max_tool_calls=10,
            max_duration_seconds=30,
            max_cost=Decimal("0"),
            max_children=0,
            max_depth=0,
        )
    )


async def _context(
    database: Database,
    workspace: Path,
    artifacts: Path,
) -> tuple[ToolExecutionContext, RecordingEvents, Run]:
    run = Run(conversation_id=uuid4(), agent_id="coder")
    async with database.transaction() as session:
        await RunRepository(session).create(run)
    events = RecordingEvents()
    store = FileArtifactStore(database, artifacts)
    return (
        ToolExecutionContext(
            workspace=workspace,
            lease=CapabilityLease(
                filesystem_read=(".",),
                filesystem_write=(".",),
            ),
            budget=_budget(),
            events=events,
            changes=ChangeService(database, store),
            artifacts=store,
            run_id=run.id,
            conversation_id=run.conversation_id,
            user_id=uuid4(),
            agent_id="coder",
        ),
        events,
        run,
    )


def _executor() -> ToolExecutor:
    registry = ToolRegistry()
    register_workspace_tools(registry)
    return ToolExecutor(registry)


@pytest.mark.asyncio
async def test_write_records_verified_artifacts_and_survives_restart(
    database: Database,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context, events, run = await _context(database, workspace, tmp_path / "artifacts")

    result = await _executor().execute(
        ToolCall(
            provider_call_id="write-1",
            tool_name="workspace.write",
            arguments={"path": "hello.txt", "content": "hello\n"},
        ),
        context,
        approval_granted=True,
    )

    assert result.output is not None
    restarted = ChangeService(database, FileArtifactStore(database, tmp_path / "artifacts"))
    changes = await restarted.list_for_run(run.id)
    assert len(changes) == 1
    change = changes[0]
    assert change.operation is FileOperation.CREATED
    assert change.status is FileChangeStatus.COMPLETED
    assert change.before_sha256 is None
    assert change.after_sha256 is not None
    assert change.before_artifact is None
    assert change.after_artifact is not None
    details = await restarted.details(change.id)
    assert "+hello" in details.diff
    assert [event for event, _ in events.values][-3:] == [
        EventType.FILE_CHANGE_APPLIED,
        EventType.FILE_CHANGE_COMPLETED,
        EventType.TOOL_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_revert_creates_reverse_record_and_refuses_newer_user_edits(
    database: Database,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts = tmp_path / "artifacts"
    context, events, run = await _context(database, workspace, artifacts)
    executor = _executor()
    await executor.execute(
        ToolCall(
            provider_call_id="write-1",
            tool_name="workspace.write",
            arguments={"path": "hello.txt", "content": "agent\n"},
        ),
        context,
        approval_granted=True,
    )
    service = ChangeService(database, FileArtifactStore(database, artifacts))
    original = (await service.list_for_run(run.id))[0]

    (workspace / "hello.txt").write_bytes(b"user\n")
    with pytest.raises(ChangeConflictError):
        await service.revert(
            original.id,
            events=events,
        )
    assert (workspace / "hello.txt").read_text(encoding="utf-8") == "user\n"

    (workspace / "hello.txt").write_bytes(b"agent\n")
    reverse = await service.revert(
        original.id,
        events=events,
    )
    assert not (workspace / "hello.txt").exists()
    assert reverse.reverts_change_id == original.id
    assert reverse.status is FileChangeStatus.COMPLETED
    values = await service.list_for_run(run.id)
    assert values[0].status is FileChangeStatus.REVERTED
    assert values[0].reverted_by_change_id == reverse.id
    with pytest.raises(ChangeConflictError):
        await service.revert(
            original.id,
            events=events,
        )


@pytest.mark.asyncio
async def test_change_api_queries_and_explicit_revert(
    database: Database,
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts = tmp_path / "artifacts"
    context, _, run = await _context(database, workspace, artifacts)
    await _executor().execute(
        ToolCall(
            provider_call_id="api-write-1",
            tool_name="workspace.write",
            arguments={"path": "api.txt", "content": "api\n"},
        ),
        context,
        approval_granted=True,
    )
    service = ChangeService(database, FileArtifactStore(database, artifacts))
    change = (await service.list_for_run(run.id))[0]
    application = await build_application(
        database_url=migrated_database_url,
        offline_fake=True,
    )
    application.changes = service
    api = create_app(application)
    async with api.router.lifespan_context(api):
        async with AsyncClient(
            transport=ASGITransport(app=api),
            base_url="http://test",
        ) as client:
            listed = await client.get(f"/api/runs/{run.id}/changes")
            assert listed.status_code == 200
            assert listed.json()[0]["id"] == str(change.id)
            shown = await client.get(f"/api/changes/{change.id}")
            assert shown.status_code == 200
            diff = await client.get(f"/api/changes/{change.id}/diff")
            assert diff.status_code == 200
            assert "+api" in diff.text
            client_scoped = await client.post(
                f"/api/changes/{change.id}/revert",
                json={
                    "confirm": True,
                    "lease": {
                        "filesystem_read": ["../outside"],
                        "filesystem_write": ["../outside"],
                    },
                },
            )
            assert client_scoped.status_code == 422
            assert (workspace / "api.txt").exists()
            reverted = await client.post(
                f"/api/changes/{change.id}/revert",
                json={"confirm": True},
            )
            assert reverted.status_code == 200, reverted.text
            assert reverted.json()["reverts_change_id"] == str(change.id)
    assert not (workspace / "api.txt").exists()


@pytest.mark.asyncio
async def test_restart_reconciles_file_applied_after_prepared_record(
    database: Database,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts = tmp_path / "artifacts"
    context, events, run = await _context(database, workspace, artifacts)
    service = ChangeService(database, FileArtifactStore(database, artifacts))
    change_id = await service.prepare(
        ToolCall(
            provider_call_id="crash-window-1",
            tool_name="workspace.write",
            arguments={"path": "recovered.txt", "content": "recovered\n"},
        ),
        WorkspaceWriteParams(path="recovered.txt", content="recovered\n"),
        context,
    )
    assert change_id is not None
    (workspace / "recovered.txt").write_bytes(b"recovered\n")

    restarted = ChangeService(database, FileArtifactStore(database, artifacts))
    reconciled = await restarted.reconcile(
        run.id,
        workspace=workspace,
        lease=context.lease,
        events=events,
    )

    assert len(reconciled) == 1
    assert reconciled[0].status is FileChangeStatus.COMPLETED
    assert (await restarted.get(change_id)).status is FileChangeStatus.COMPLETED


@pytest.mark.asyncio
async def test_change_storage_budget_rejects_before_recording_artifacts(
    database: Database,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts = tmp_path / "artifacts"
    context, _, run = await _context(database, workspace, artifacts)
    service = ChangeService(
        database,
        FileArtifactStore(database, artifacts),
        max_file_change_bytes=8,
        max_run_change_bytes=16,
    )
    context = replace(context, changes=service)

    with pytest.raises(ArtifactTooLargeError, match="FileChange requires"):
        await _executor().execute(
            ToolCall(
                provider_call_id="oversize-write-1",
                tool_name="workspace.write",
                arguments={"path": "large.txt", "content": "too large\n"},
            ),
            context,
            approval_granted=True,
        )

    assert not (workspace / "large.txt").exists()
    assert await service.list_for_run(run.id) == ()


@pytest.mark.asyncio
async def test_git_inspector_is_optional_and_path_scoped(tmp_path: Path) -> None:
    inspector = GitWorkspaceInspector()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert await inspector.baseline(plain, "file.txt") is None

    if shutil.which("git") is None:
        pytest.skip("Git executable is unavailable")
    repository = tmp_path / "repository"
    repository.mkdir()
    await asyncio.to_thread(
        subprocess.run,
        ["git", "init", str(repository)],
        check=True,
        capture_output=True,
    )
    target = repository / "tracked.txt"
    target.write_bytes(b"before\n")
    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(repository), "add", "--", "tracked.txt"],
        check=True,
        capture_output=True,
    )
    await asyncio.to_thread(
        subprocess.run,
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=AgentCell Test",
            "-c",
            "user.email=agentcell@example.invalid",
            "commit",
            "-m",
            "baseline",
        ],
        check=True,
        capture_output=True,
    )
    target.write_bytes(b"after\n")

    baseline = await inspector.baseline(repository, "tracked.txt")
    assert baseline is not None
    assert baseline.head is not None
    assert baseline.dirty
    assert "tracked.txt" in (baseline.path_status or "")
    diff = await inspector.diff(repository, "tracked.txt")
    assert diff is not None
    assert b"-before" in diff
    assert b"+after" in diff
