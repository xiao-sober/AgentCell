"""Stage 7.1 workspace mutation, Shell, and HTTPS security integration tests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from agentcell.agents import AgentRegistry, AgentSpec
from agentcell.budgets import Budget, BudgetTracker
from agentcell.errors import (
    HttpRequestDeniedError,
    HttpResponseTooLargeError,
    ShellCommandDeniedError,
    ShellOutputTooLargeError,
    ToolApprovalRequiredError,
    WorkspacePathDeniedError,
    WorkspaceStateConflictError,
)
from agentcell.events import EventPayload, EventType, JsonValue
from agentcell.kernel.run_service import RunRequest, RunService
from agentcell.policy import (
    ApprovalDecision,
    ApprovalDecisionKind,
    Capability,
    CapabilityLease,
)
from agentcell.providers import (
    FakeModelSpec,
    FakeProviderAdapter,
    FakeScript,
    FakeTextStep,
    FakeToolCallStep,
    ProviderFactory,
)
from agentcell.storage import CheckpointRepository, Database, EventStore, FileArtifactStore
from agentcell.tools import (
    HostResolver,
    HttpRequestParams,
    ShellRunResult,
    ShellTestResult,
    ToolCall,
    ToolExecutionContext,
    ToolExecutor,
    ToolRegistry,
    WorkspaceMutationResult,
    WorkspaceReadResult,
    WorkspaceWriteParams,
    preview_workspace_write,
    register_http_tools,
    register_shell_tools,
    register_workspace_tools,
)


@dataclass
class RecordingEventSink:
    events: list[tuple[EventType, EventPayload]] = field(default_factory=lambda: [])

    async def emit(self, event_type: EventType, payload: EventPayload) -> None:
        self.events.append((event_type, payload))


@dataclass(frozen=True)
class FakeResolver(HostResolver):
    addresses: dict[str, tuple[str, ...]]

    async def resolve(self, host: str, port: int) -> tuple[str, ...]:
        assert port == 443
        return self.addresses[host]


def _budget() -> BudgetTracker:
    return BudgetTracker(
        Budget(
            max_requests=0,
            max_input_tokens=0,
            max_output_tokens=0,
            max_total_tokens=0,
            max_tool_calls=30,
            max_duration_seconds=300,
            max_cost=Decimal("0"),
            max_children=0,
            max_depth=0,
        )
    )


def _context(
    workspace: Path,
    *,
    lease: CapabilityLease,
    artifacts: FileArtifactStore | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace=workspace,
        lease=lease,
        budget=_budget(),
        events=RecordingEventSink(),
        artifacts=artifacts,
    )


@pytest.mark.asyncio
async def test_workspace_mutations_require_approval_and_expected_hash(tmp_path: Path) -> None:
    target = tmp_path / "example.txt"
    target.write_text("before\n", encoding="utf-8")
    registry = ToolRegistry()
    register_workspace_tools(registry)
    executor = ToolExecutor(registry)
    context = _context(
        tmp_path,
        lease=CapabilityLease(filesystem_read=(".",), filesystem_write=(".",)),
    )
    read_raw = await executor.execute(
        ToolCall(tool_name="workspace.read", arguments={"path": "example.txt"}),
        context,
    )
    read = WorkspaceReadResult.model_validate(read_raw.output)
    assert read.sha256 == hashlib.sha256(target.read_bytes()).hexdigest()
    assert read.sha256 is not None

    patch_call = ToolCall(
        tool_name="workspace.patch",
        arguments={
            "path": "example.txt",
            "old_text": "before",
            "new_text": "after",
            "expected_sha256": read.sha256,
        },
    )
    with pytest.raises(ToolApprovalRequiredError):
        await executor.execute(patch_call, context)
    patched_raw = await executor.execute(patch_call, context, approval_granted=True)
    patched = WorkspaceMutationResult.model_validate(patched_raw.output)
    assert patched.operation == "patched"
    assert target.read_text(encoding="utf-8") == "after\n"

    with pytest.raises(WorkspaceStateConflictError):
        await executor.execute(
            ToolCall(
                tool_name="workspace.delete",
                arguments={"path": "example.txt", "expected_sha256": read.sha256},
            ),
            context,
            approval_granted=True,
        )
    current_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    delete_call = ToolCall(
        tool_name="workspace.delete",
        arguments={"path": "example.txt", "expected_sha256": current_hash},
    )
    with pytest.raises(ToolApprovalRequiredError):
        await executor.execute(delete_call, context)
    deleted_raw = await executor.execute(delete_call, context, approval_granted=True)
    deleted = WorkspaceMutationResult.model_validate(deleted_raw.output)
    assert deleted.operation == "deleted"
    assert not target.exists()

    created_raw = await executor.execute(
        ToolCall(
            tool_name="workspace.write",
            arguments={"path": "created.txt", "content": "new content"},
        ),
        context,
        approval_granted=True,
    )
    assert WorkspaceMutationResult.model_validate(created_raw.output).operation == "created"
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "new content"

    (tmp_path / "src").mkdir()
    mismatched_scope = _context(
        tmp_path,
        lease=CapabilityLease(filesystem_read=("src",), filesystem_write=(".",)),
    )
    with pytest.raises(WorkspacePathDeniedError, match="filesystem_read"):
        await executor.execute(
            ToolCall(
                tool_name="workspace.write",
                arguments={"path": "outside-read-scope.txt", "content": "denied"},
            ),
            mismatched_scope,
            approval_granted=True,
        )


@pytest.mark.asyncio
async def test_large_workspace_diff_is_saved_as_artifact(
    database: Database,
    tmp_path: Path,
) -> None:
    artifacts = FileArtifactStore(database, tmp_path / "artifacts")
    context = _context(
        tmp_path,
        lease=CapabilityLease(filesystem_read=(".",), filesystem_write=(".",)),
        artifacts=artifacts,
    )
    content = "".join(f"line-{index:05d} changed\n" for index in range(5000))
    preview = await preview_workspace_write(
        WorkspaceWriteParams(path="generated.txt", content=content),
        context,
    )

    assert preview.diff_artifact is not None
    restored = await artifacts.load(preview.diff_artifact)
    assert b"line-04999 changed" in restored
    assert len(preview.diff or "") < 32_000


@pytest.mark.asyncio
async def test_shell_uses_argv_allowlist_minimal_environment_and_artifact(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTCELL_TEST_SECRET", "must-not-leak")
    registry = ToolRegistry()
    register_shell_tools(registry)
    executor = ToolExecutor(registry)
    artifacts = FileArtifactStore(database, tmp_path / "artifacts")
    context = _context(
        tmp_path,
        lease=CapabilityLease(
            filesystem_read=(".",),
            filesystem_write=(".",),
            commands=frozenset({"python"}),
        ),
        artifacts=artifacts,
    )
    code = (
        "import os; print('secret=' + str('AGENTCELL_TEST_SECRET' in os.environ)); "
        "print('x' * 70000)"
    )
    with pytest.raises(ToolApprovalRequiredError):
        await executor.execute(
            ToolCall(
                tool_name="shell.run",
                arguments={"command": "python", "args": ["-c", "print('blocked')"]},
            ),
            context,
        )
    result = await executor.execute(
        ToolCall(
            tool_name="shell.run",
            arguments={"command": "python", "args": ["-c", code]},
        ),
        context,
        approval_granted=True,
    )

    assert result.truncated
    assert result.artifact is not None
    full = ShellRunResult.model_validate_json(await artifacts.load(result.artifact))
    assert "secret=False" in full.stdout
    assert full.exit_code == 0
    literal_raw = await executor.execute(
        ToolCall(
            tool_name="shell.run",
            arguments={
                "command": "python",
                "args": ["-c", "import sys; print(sys.argv[1])", "&& echo injected"],
            },
        ),
        context,
        approval_granted=True,
    )
    literal = ShellRunResult.model_validate(literal_raw.output)
    assert literal.stdout.strip() == "&& echo injected"
    with pytest.raises(ShellOutputTooLargeError):
        await executor.execute(
            ToolCall(
                tool_name="shell.run",
                arguments={
                    "command": "python",
                    "args": ["-c", "print('y' * 5000)"],
                    "max_output_bytes": 1024,
                },
            ),
            context,
            approval_granted=True,
        )


@pytest.mark.asyncio
async def test_shell_test_distinguishes_collection_from_execution(
    database: Database,
    tmp_path: Path,
) -> None:
    (tmp_path / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    registry = ToolRegistry()
    register_shell_tools(registry)
    executor = ToolExecutor(registry)
    context = _context(
        tmp_path,
        lease=CapabilityLease(
            filesystem_read=(".",),
            filesystem_write=(".",),
            commands=frozenset({"pytest"}),
        ),
    )

    collected = await executor.execute(
        ToolCall(
            tool_name="shell.test",
            arguments={
                "command": "pytest",
                "args": ["test_sample.py", "--collect-only", "-q"],
            },
        ),
        context,
        approval_granted=True,
    )
    executed = await executor.execute(
        ToolCall(
            tool_name="shell.test",
            arguments={"command": "pytest", "args": ["test_sample.py", "-q"]},
        ),
        context,
        approval_granted=True,
    )

    collected_result = ShellTestResult.model_validate(collected.output)
    executed_result = ShellTestResult.model_validate(executed.output)
    assert collected_result.exit_code == 0
    assert collected_result.test_execution.collected_only is True
    assert collected_result.test_execution.executed is False
    assert executed_result.exit_code == 0
    assert executed_result.test_execution.executed is True
    assert executed_result.test_execution.successful is True
    with pytest.raises(ShellCommandDeniedError):
        await executor.execute(
            ToolCall(
                tool_name="shell.run",
                arguments={"command": "git", "args": ["status"]},
            ),
            context,
            approval_granted=True,
        )


@pytest.mark.asyncio
async def test_http_pins_dns_checks_redirects_and_externalizes_large_body(
    database: Database,
    tmp_path: Path,
) -> None:
    seen: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers["host"]))
        if request.headers["host"] == "example.com":
            return httpx.Response(302, headers={"location": "https://api.example.com/data"})
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"z" * 100_000,
        )

    registry = ToolRegistry()
    register_http_tools(
        registry,
        resolver=FakeResolver(
            {
                "example.com": ("93.184.216.34",),
                "api.example.com": ("93.184.216.35",),
            }
        ),
        transport=httpx.MockTransport(respond),
    )
    artifacts = FileArtifactStore(database, tmp_path / "artifacts")
    executor = ToolExecutor(registry)
    context = _context(
        tmp_path,
        lease=CapabilityLease(network_domains=("example.com",)),
        artifacts=artifacts,
    )
    call = ToolCall(
        tool_name="http.request",
        arguments={"method": "GET", "url": "https://example.com/start"},
    )
    with pytest.raises(ToolApprovalRequiredError):
        await executor.execute(call, context)
    result = await executor.execute(call, context, approval_granted=True)

    assert seen == [
        ("93.184.216.34", "example.com"),
        ("93.184.216.35", "api.example.com"),
    ]
    assert result.truncated
    assert result.artifact is not None


@pytest.mark.asyncio
async def test_http_rejects_private_dns_and_sensitive_request_fields(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_http_tools(
        registry,
        resolver=FakeResolver({"example.com": ("127.0.0.1",)}),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)),
    )
    context = _context(
        tmp_path,
        lease=CapabilityLease(network_domains=("example.com",)),
    )
    with pytest.raises(HttpRequestDeniedError):
        await ToolExecutor(registry).execute(
            ToolCall(
                tool_name="http.request",
                arguments={"url": "https://example.com/data"},
            ),
            context,
            approval_granted=True,
        )
    public_registry = ToolRegistry()
    register_http_tools(
        public_registry,
        resolver=FakeResolver({"example.com": ("93.184.216.34",)}),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)),
    )
    with pytest.raises(HttpRequestDeniedError):
        await ToolExecutor(public_registry).execute(
            ToolCall(
                tool_name="http.request",
                arguments={"url": "https://example.com/?token=secret"},
            ),
            context,
            approval_granted=True,
        )
    oversized_registry = ToolRegistry()
    register_http_tools(
        oversized_registry,
        resolver=FakeResolver({"example.com": ("93.184.216.34",)}),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * 5000, request=request)
        ),
    )
    with pytest.raises(HttpResponseTooLargeError):
        await ToolExecutor(oversized_registry).execute(
            ToolCall(
                tool_name="http.request",
                arguments={
                    "url": "https://example.com/data",
                    "max_response_bytes": 1024,
                },
            ),
            context,
            approval_granted=True,
        )
    with pytest.raises(ValidationError):
        HttpRequestParams(
            url="https://example.com/",
            headers={"authorization": "secret"},
        )


def _workspace_run_service(
    database: Database,
    script: FakeScript,
    *,
    tool_name: str,
    artifact_root: Path,
) -> tuple[RunService, ProviderFactory]:
    model = FakeModelSpec(model=f"production-{tool_name}")
    providers = ProviderFactory(
        {"production": model},
        adapters=(FakeProviderAdapter({model.model: script}),),
    )
    registry = ToolRegistry()
    register_workspace_tools(registry)
    agent = AgentSpec(
        id="operator",
        name="Operator",
        description="Production workspace tool test.",
        model_ref="production",
        instructions="Use the requested workspace tool.",
        tools=(tool_name,),
        capabilities=frozenset({Capability.FILESYSTEM_READ, Capability.FILESYSTEM_WRITE}),
    )
    return (
        RunService(
            database=database,
            providers=providers,
            agents=AgentRegistry((agent,)),
            tools=registry,
            artifact_root=artifact_root,
        ),
        providers,
    )


@pytest.mark.asyncio
async def test_workspace_patch_diff_survives_approval_restart(
    database: Database,
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.txt"
    target.write_text("before", encoding="utf-8")
    arguments: dict[str, JsonValue] = {
        "path": "state.txt",
        "old_text": "before",
        "new_text": "after",
        "expected_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }
    first, providers = _workspace_run_service(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="workspace.patch",
                    arguments=arguments,
                    tool_call_id="patch-approval-1",
                ),
            )
        ),
        tool_name="workspace.patch",
        artifact_root=tmp_path / "artifacts",
    )
    try:
        waiting = await first.run(
            RunRequest(
                prompt="patch state",
                workspace=tmp_path,
                agent_id="operator",
                lease=CapabilityLease(
                    filesystem_read=(".",),
                    filesystem_write=(".",),
                ),
            )
        )
    finally:
        await providers.aclose()

    assert waiting.run.status.value == "waiting_approval"
    assert "-before" in (waiting.approvals[0].diff or "")
    restarted, restarted_providers = _workspace_run_service(
        database,
        FakeScript(steps=(FakeTextStep(text="patch complete"),)),
        tool_name="workspace.patch",
        artifact_root=tmp_path / "artifacts",
    )
    try:
        completed = await restarted.resume(
            waiting.approvals[0].id,
            ApprovalDecision(kind=ApprovalDecisionKind.APPROVE),
        )
    finally:
        await restarted_providers.aclose()

    assert completed.run.status.value == "completed"
    assert target.read_text(encoding="utf-8") == "after"


@pytest.mark.asyncio
async def test_changed_workspace_state_is_rejected_before_approval_is_persisted(
    database: Database,
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.txt"
    target.write_text("before", encoding="utf-8")
    arguments: dict[str, JsonValue] = {
        "path": "state.txt",
        "content": "after",
        "expected_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }
    first, providers = _workspace_run_service(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="workspace.write",
                    arguments=arguments,
                    tool_call_id="write-stale-approval-1",
                ),
            )
        ),
        tool_name="workspace.write",
        artifact_root=tmp_path / "artifacts",
    )
    try:
        waiting = await first.run(
            RunRequest(
                prompt="replace state",
                workspace=tmp_path,
                agent_id="operator",
                lease=CapabilityLease(
                    filesystem_read=(".",),
                    filesystem_write=(".",),
                ),
            )
        )
    finally:
        await providers.aclose()

    target.write_text("user edit", encoding="utf-8")
    restarted, restarted_providers = _workspace_run_service(
        database,
        FakeScript(steps=(FakeTextStep(text="must not execute"),)),
        tool_name="workspace.write",
        artifact_root=tmp_path / "artifacts",
    )
    try:
        with pytest.raises(WorkspaceStateConflictError):
            await restarted.resume(
                waiting.approvals[0].id,
                ApprovalDecision(kind=ApprovalDecisionKind.APPROVE),
            )
        stored = await restarted.get(waiting.run.id)
    finally:
        await restarted_providers.aclose()

    assert stored is not None
    assert stored.status.value == "waiting_approval"
    assert target.read_text(encoding="utf-8") == "user edit"
    async with database.session() as session:
        events = await EventStore(session).list_for_run(waiting.run.id)
    assert EventType.TOOL_APPROVED not in {event.event_type for event in events}


@pytest.mark.asyncio
async def test_large_approval_diff_artifact_is_referenced_by_checkpoint(
    database: Database,
    tmp_path: Path,
) -> None:
    content = "".join(f"generated-{index:05d}\n" for index in range(5000))
    service, providers = _workspace_run_service(
        database,
        FakeScript(
            steps=(
                FakeToolCallStep(
                    tool_name="workspace.write",
                    arguments={"path": "large.txt", "content": content},
                    tool_call_id="large-write-1",
                ),
            )
        ),
        tool_name="workspace.write",
        artifact_root=tmp_path / "artifacts",
    )
    try:
        waiting = await service.run(
            RunRequest(
                prompt="create large file",
                workspace=tmp_path,
                agent_id="operator",
                lease=CapabilityLease(
                    filesystem_read=(".",),
                    filesystem_write=(".",),
                ),
            )
        )
    finally:
        await providers.aclose()

    approval = waiting.approvals[0]
    assert approval.diff_artifact is not None
    async with database.session() as session:
        checkpoint = await CheckpointRepository(session).latest(waiting.run.id)
    assert approval.diff_artifact.artifact_id in checkpoint.artifact_ids
