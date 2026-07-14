"""Event-sourced Run orchestration with durable deferred-tool recovery."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_ai import (
    AgentRunResult,
    AgentStreamEvent,
    DeferredToolRequests,
    DeferredToolResults,
    ModelMessagesTypeAdapter,
    RunContext,
    ToolApproved,
    ToolDenied,
    UnexpectedModelBehavior,
)
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    ModelMessage,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.usage import UsageLimits

from agentcell.agents import (
    AgentDelegation,
    AgentFactory,
    AgentRegistry,
    AgentSpec,
    DelegationKind,
    DelegationRequest,
    DelegationResult,
    DelegationStatus,
)
from agentcell.budgets import Budget, BudgetSnapshot, BudgetTracker, Usage
from agentcell.changes import ChangeSetStatus
from agentcell.changes.service import ChangeService
from agentcell.errors import (
    AgentCellError,
    ApprovalConflictError,
    BudgetExceededError,
    CapabilityDeniedError,
    ModelOutputError,
    RunExecutionError,
    RunNotFoundError,
    ToolArgumentsError,
)
from agentcell.events import (
    AgentChildCompletedPayload,
    AgentChildStartedPayload,
    ArtifactReference,
    ErrorPayload,
    EventPayload,
    EventType,
    GenericEventPayload,
    JsonValue,
    RunCompletedPayload,
    RunStartedPayload,
    RunStatusChangedPayload,
    TextDeltaPayload,
    redact_sensitive_data,
)
from agentcell.kernel.approval_recorder import RunApprovalRecorder
from agentcell.kernel.checkpoint import Checkpoint, CheckpointKind
from agentcell.kernel.deps import RunDeps
from agentcell.kernel.event_recorder import RunEventRecorder
from agentcell.kernel.lifecycle import RunStatus
from agentcell.kernel.model_runtime import RunModel
from agentcell.kernel.models import Run
from agentcell.kernel.tool_bridge import (
    FINAL_OUTPUT_RETRIES,
    FINAL_REQUEST_ATTEMPTS,
    budget_instructions,
    build_agent_tools,
)
from agentcell.memory.compaction import ContextManager
from agentcell.memory.injector import MemoryInjector
from agentcell.memory.models import MemoryScope
from agentcell.memory.service import MemoryService
from agentcell.policy import (
    Approval,
    ApprovalDecision,
    ApprovalDecisionKind,
    ApprovalDecisionSource,
    ApprovalStatus,
    CapabilityLease,
    PermissionMode,
)
from agentcell.providers import ProviderFactory
from agentcell.providers.tool_names import portable_tool_name
from agentcell.storage import (
    AgentDelegationRepository,
    ApprovalRepository,
    ChangeSetRepository,
    CheckpointRepository,
    Database,
    EventStore,
    FileArtifactStore,
    RunRepository,
    SqliteToolExecutionLedger,
)
from agentcell.tools import (
    ToolExecutionContext,
    ToolExecutor,
    ToolRegistry,
    register_delegation_tool,
)

_TRACER = trace.get_tracer("agentcell.kernel")
_USAGE_LIMIT_PATTERN = re.compile(
    r"(?P<name>request|tool_calls|input_tokens|output_tokens|total_tokens)_limit of "
    r"(?P<limit>\d+)(?: \((?:\w+)=(?P<attempted>\d+)\))?"
)


def _default_budget() -> Budget:
    return Budget(
        max_requests=10,
        max_input_tokens=200_000,
        max_output_tokens=40_000,
        max_total_tokens=240_000,
        max_tool_calls=20,
        max_duration_seconds=300,
        max_cost=None,
        max_children=0,
        max_depth=0,
    )


def classify_usage_limit(
    error: UsageLimitExceeded,
    *,
    budget: Budget,
    usage_at_start: Usage,
) -> BudgetExceededError:
    """Translate PydanticAI's textual limit error into AgentCell budget dimensions."""

    match = _USAGE_LIMIT_PATTERN.search(str(error))
    if match is None:
        return BudgetExceededError("model_usage", None, None)

    name = match.group("name")
    resource = "requests" if name == "request" else name
    local_limit = int(match.group("limit"))
    local_attempted = (
        int(attempted) if (attempted := match.group("attempted")) is not None else local_limit + 1
    )
    budget_limit = {
        "requests": min(budget.max_requests, local_limit + usage_at_start.requests),
        "tool_calls": budget.max_tool_calls,
        "input_tokens": budget.max_input_tokens,
        "output_tokens": budget.max_output_tokens,
        "total_tokens": budget.max_total_tokens,
    }[resource]
    used_before = (
        usage_at_start.total_tokens
        if resource == "total_tokens"
        else cast(int, getattr(usage_at_start, resource))
    )
    return BudgetExceededError(resource, budget_limit, used_before + local_attempted)


class RunRequest(BaseModel):
    """Validated input for one new, immediately executed Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1)
    workspace: Path
    agent_id: str = "coordinator"
    conversation_id: UUID = Field(default_factory=uuid4)
    user_id: UUID = Field(default_factory=uuid4)
    lease: CapabilityLease = Field(default_factory=CapabilityLease)
    permission_mode: PermissionMode = PermissionMode.REQUEST
    budget: Budget = Field(default_factory=_default_budget)
    run_id: UUID = Field(default_factory=uuid4)
    parent_run_id: UUID | None = None
    depth: int = Field(default=0, ge=0, strict=True)

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, value: Path) -> Path:
        try:
            resolved = value.resolve(strict=True)
        except OSError as error:
            raise ValueError("workspace must exist") from error
        if not resolved.is_dir():
            raise ValueError("workspace must be a directory")
        return resolved


class RunResult(BaseModel):
    """Current Run outcome, including deferred approvals when execution pauses."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run: Run
    output: str | None
    budget: BudgetSnapshot
    approvals: tuple[Approval, ...] = ()
    messages_json: str = Field(default="[]", exclude=True)


class RunService:
    """Create, pause, restart, resume, and finish one PydanticAI Run."""

    def __init__(
        self,
        *,
        database: Database,
        providers: ProviderFactory,
        agents: AgentRegistry,
        tools: ToolRegistry,
        artifact_root: Path = Path(".agentcell/artifacts"),
    ) -> None:
        self._database = database
        self._providers = providers
        self._agents = agents
        self._tool_registry = tools
        self._tool_executor = ToolExecutor(tools)
        self._agent_factory = AgentFactory(providers)
        self._artifacts = FileArtifactStore(database, artifact_root)
        self._changes = ChangeService(database, self._artifacts)
        if any("agent.delegate" in spec.tools for spec in agents.list()):
            if not tools.contains("agent.delegate"):
                register_delegation_tool(tools)

    async def run(self, request: RunRequest) -> RunResult:
        run, spec = await self.prepare(request)
        return await self.execute_prepared(run, request=request, spec=spec)

    async def prepare(self, request: RunRequest) -> tuple[Run, AgentSpec]:
        """Persist a created Run so an orchestrator can link it before execution."""

        spec = self._agents.get(request.agent_id)
        self._authorize_agent(spec, request.lease)
        self._authorize_agent_limits(spec, request.budget)
        run = Run(
            id=request.run_id,
            conversation_id=request.conversation_id,
            agent_id=spec.id,
            parent_run_id=request.parent_run_id,
        )
        await self._create_run(run)
        return run, spec

    async def execute_prepared(
        self,
        run: Run,
        *,
        request: RunRequest,
        spec: AgentSpec,
        message_history: Sequence[ModelMessage] | None = None,
    ) -> RunResult:
        run = await self._transition(run, RunStatus.RUNNING)
        tracker = BudgetTracker(request.budget)
        recorder = RunEventRecorder(self._database, run.id)
        deps = self._deps(
            run=run,
            request=request,
            spec=spec,
            tracker=tracker,
            recorder=recorder,
        )
        try:
            result = await self._execute_agent(
                spec,
                request.prompt,
                request.budget,
                deps,
                tracker,
                recorder,
                message_history=message_history,
            )
            outcome = await self._handle_result(
                run,
                request=request,
                spec=spec,
                tracker=tracker,
                result=result,
            )
            return outcome
        except asyncio.CancelledError:
            await asyncio.shield(self._cancel_running(run, reason="cancelled", usage=tracker.usage))
            raise
        except AgentCellError as error:
            await self._fail(run, error, usage=tracker.usage)
            raise
        except Exception as error:
            classified = RunExecutionError()
            await self._fail(run, classified, usage=tracker.usage)
            raise classified from error

    async def resume(self, approval_id: UUID, decision: ApprovalDecision) -> RunResult:
        async with self._database.session() as session:
            approval = await ApprovalRepository(session).get_required(approval_id)
            run = await RunRepository(session).get(approval.run_id)
            if run is None:
                raise RunNotFoundError(str(approval.run_id))
            checkpoint = await CheckpointRepository(session).latest(run.id)

        if approval.status is not ApprovalStatus.PENDING:
            self._ensure_same_decision(approval, decision)
            if run.status.is_terminal:
                return RunResult(
                    run=run,
                    output=None,
                    budget=checkpoint.budget,
                    approvals=(approval,),
                )
            if run.status is not RunStatus.RUNNING:
                raise ApprovalConflictError("Resolved approval is not resumable")
        else:
            if run.status is not RunStatus.WAITING_APPROVAL:
                raise ApprovalConflictError("Pending approval Run is not waiting")
            approval = await self._persist_decision(run, approval, decision)
            run = run.transition_to(RunStatus.RUNNING)

        spec = self._agents.get(checkpoint.agent_id)
        workspace = await asyncio.to_thread(
            Path(checkpoint.workspace).resolve,
            strict=True,
        )
        tracker = BudgetTracker(
            checkpoint.budget.budget,
            initial_usage=checkpoint.budget.used,
        )
        recorder = RunEventRecorder(self._database, run.id)
        request = RunRequest(
            prompt=checkpoint.prompt,
            workspace=workspace,
            agent_id=checkpoint.agent_id,
            conversation_id=run.conversation_id,
            user_id=checkpoint.user_id,
            lease=checkpoint.lease,
            permission_mode=checkpoint.permission_mode,
            budget=checkpoint.budget.budget,
            run_id=run.id,
            parent_run_id=run.parent_run_id,
            depth=checkpoint.depth,
        )
        await self._changes.reconcile(
            run.id,
            workspace=request.workspace,
            lease=request.lease,
            events=recorder,
        )
        temporary = set(checkpoint.temporary_approved_tools)
        if approval.grant_same_tool and approval.status is ApprovalStatus.APPROVED:
            temporary.add(approval.tool_name)
        deps = self._deps(
            run=run,
            request=request,
            spec=spec,
            tracker=tracker,
            recorder=recorder,
            temporary_approved_tools=frozenset(temporary),
        )
        messages = ModelMessagesTypeAdapter.validate_python(checkpoint.messages)
        deferred = self._deferred_result(approval)
        try:
            result = await self._execute_agent(
                spec,
                None,
                checkpoint.budget.budget,
                deps,
                tracker,
                recorder,
                message_history=messages,
                deferred_tool_results=deferred,
                context_query=request.prompt,
            )
            outcome = await self._handle_result(
                run,
                request=request,
                spec=spec,
                tracker=tracker,
                result=result,
                temporary_approved_tools=frozenset(temporary),
            )
            return await self._resume_parent_after_child(outcome)
        except asyncio.CancelledError:
            await asyncio.shield(self._cancel_running(run, reason="cancelled", usage=tracker.usage))
            raise
        except AgentCellError as error:
            await self._fail(run, error, usage=tracker.usage)
            raise
        except Exception as error:
            classified = RunExecutionError()
            await self._fail(run, classified, usage=tracker.usage)
            raise classified from error

    async def resume_paused(self, run_id: UUID) -> RunResult:
        """Resume a paused Run according to its durable checkpoint kind."""

        async with self._database.session() as session:
            run = await RunRepository(session).get(run_id)
            if run is None:
                raise RunNotFoundError(str(run_id))
            checkpoint = await CheckpointRepository(session).latest(run_id)
        if checkpoint.kind is CheckpointKind.DELEGATION:
            return await self.resume_delegation(run_id)
        if checkpoint.kind is not CheckpointKind.BRANCH:
            raise ApprovalConflictError(
                f"Run checkpoint {checkpoint.kind.value!r} requires a specific resume operation"
            )
        if run.status.is_terminal:
            return RunResult(
                run=run,
                output=None,
                budget=checkpoint.budget,
            )
        if run.status is not RunStatus.PAUSED:
            raise ApprovalConflictError("Branch Run is not paused")

        spec = self._agents.get(checkpoint.agent_id)
        workspace = await asyncio.to_thread(Path(checkpoint.workspace).resolve, strict=True)
        tracker = BudgetTracker(
            checkpoint.budget.budget,
            initial_usage=checkpoint.budget.used,
        )
        run = await self._transition(run, RunStatus.RUNNING)
        recorder = RunEventRecorder(self._database, run.id)
        request = RunRequest(
            prompt=checkpoint.prompt,
            workspace=workspace,
            agent_id=checkpoint.agent_id,
            conversation_id=run.conversation_id,
            user_id=checkpoint.user_id,
            lease=checkpoint.lease,
            permission_mode=checkpoint.permission_mode,
            budget=checkpoint.budget.budget,
            run_id=run.id,
            parent_run_id=run.parent_run_id,
            depth=checkpoint.depth,
        )
        await self._changes.reconcile(
            run.id,
            workspace=request.workspace,
            lease=request.lease,
            events=recorder,
        )
        deps = self._deps(
            run=run,
            request=request,
            spec=spec,
            tracker=tracker,
            recorder=recorder,
            temporary_approved_tools=checkpoint.temporary_approved_tools,
        )
        messages = ModelMessagesTypeAdapter.validate_python(checkpoint.messages)
        try:
            result = await self._execute_agent(
                spec,
                None if messages else checkpoint.prompt,
                checkpoint.budget.budget,
                deps,
                tracker,
                recorder,
                message_history=messages,
                context_query=request.prompt,
            )
            return await self._handle_result(
                run,
                request=request,
                spec=spec,
                tracker=tracker,
                result=result,
                temporary_approved_tools=checkpoint.temporary_approved_tools,
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._cancel_running(run, reason="cancelled", usage=tracker.usage))
            raise
        except AgentCellError as error:
            await self._fail(run, error, usage=tracker.usage)
            raise
        except Exception as error:
            classified = RunExecutionError()
            await self._fail(run, classified, usage=tracker.usage)
            raise classified from error

    async def cancel(self, run_id: UUID) -> Run:
        async with self._database.session() as session:
            run = await RunRepository(session).get(run_id)
            active_children = await AgentDelegationRepository(session).list_active_for_parent(
                run_id
            )
        if run is None:
            raise RunNotFoundError(str(run_id))
        if run.status is RunStatus.CANCELLED or run.status.is_terminal:
            return run
        for delegation in active_children:
            child = await self.get(delegation.child_run_id)
            if child is not None and not child.status.is_terminal:
                await self.cancel(child.id)
            cancelled = DelegationResult(
                delegation_id=delegation.id,
                child_run_id=delegation.child_run_id,
                agent_id=delegation.target_agent_id,
                status=DelegationStatus.CANCELLED,
                usage=delegation.accounted_usage,
            )
            await self._save_delegation_result(delegation, cancelled)
        return await self._finish(
            run,
            RunStatus.CANCELLED,
            EventType.RUN_CANCELLED,
            GenericEventPayload(data={"reason": "requested"}),
        )

    async def delegate(
        self,
        request: DelegationRequest,
        context: ToolExecutionContext,
        *,
        provider_call_id: str,
    ) -> DelegationResult:
        """Persist one bounded child Run before deferring its execution."""

        if (
            context.run_id is None
            or context.conversation_id is None
            or context.user_id is None
            or context.agent_id is None
        ):
            raise RunExecutionError("Delegation context is incomplete")
        parent_spec = self._agents.get(context.agent_id)
        child_spec = self._agents.get(request.agent_id)
        if parent_spec.max_children == 0 or parent_spec.max_depth == 0:
            raise BudgetExceededError("agent.depth", parent_spec.max_depth, 1)
        context.lease.ensure_child_subset(request.lease)
        self._authorize_agent(child_spec, request.lease)
        self._authorize_agent_limits(child_spec, request.budget)

        async with self._database.session() as session:
            existing = await AgentDelegationRepository(session).find_by_parent_call(
                context.run_id,
                provider_call_id,
            )
        if existing is not None:
            if existing.result is not None:
                return existing.result
            return self._pending_delegation_result(existing)

        context.budget.reserve_child(depth=1, child_budget=request.budget)
        await context.events.emit(
            EventType.BUDGET_UPDATED,
            GenericEventPayload(
                data={
                    "source": "child_reserved",
                    "target_agent_id": request.agent_id,
                    "snapshot": cast(
                        dict[str, JsonValue],
                        context.budget.snapshot().model_dump(mode="json"),
                    ),
                }
            ),
        )

        child_request = RunRequest(
            prompt=request.task,
            workspace=context.workspace,
            agent_id=child_spec.id,
            conversation_id=context.conversation_id,
            user_id=context.user_id,
            lease=request.lease,
            budget=request.budget,
            parent_run_id=context.run_id,
            depth=context.depth + 1,
        )
        _, _, delegation = await self.prepare_delegation(
            child_request,
            provider_call_id=provider_call_id,
            kind=DelegationKind.AGENT_TOOL,
            task=request.task,
        )
        return self._pending_delegation_result(delegation)

    async def prepare_delegation(
        self,
        request: RunRequest,
        *,
        provider_call_id: str,
        kind: DelegationKind,
        task: str,
    ) -> tuple[Run, AgentSpec, AgentDelegation]:
        """Atomically create a child Run, its delegation, and parent audit event."""

        if request.parent_run_id is None or request.depth < 1:
            raise RunExecutionError("Delegated Run must identify its parent and depth")
        spec = self._agents.get(request.agent_id)
        self._authorize_agent(spec, request.lease)
        self._authorize_agent_limits(spec, request.budget)
        child = Run(
            id=request.run_id,
            conversation_id=request.conversation_id,
            agent_id=spec.id,
            parent_run_id=request.parent_run_id,
        )
        delegation = AgentDelegation(
            parent_run_id=request.parent_run_id,
            child_run_id=child.id,
            provider_call_id=provider_call_id,
            kind=kind,
            target_agent_id=spec.id,
            task=task,
            depth=request.depth,
            lease=request.lease,
            allocated_budget=request.budget,
            status=DelegationStatus.PENDING,
        )
        async with self._database.transaction() as session:
            await RunRepository(session).create(child)
            child_store = EventStore(session)
            await child_store.append(
                run_id=child.id,
                event_type=EventType.RUN_STARTED,
                payload=RunStartedPayload(
                    conversation_id=child.conversation_id,
                    agent_id=child.agent_id,
                ),
            )
            await AgentDelegationRepository(session).create(delegation)
            await child_store.append(
                run_id=request.parent_run_id,
                event_type=EventType.AGENT_CHILD_STARTED,
                payload=AgentChildStartedPayload(
                    delegation_id=delegation.id,
                    parent_run_id=request.parent_run_id,
                    child_run_id=child.id,
                    agent_id=spec.id,
                    depth=delegation.depth,
                    trace_id=delegation.trace_id,
                    allocated_budget=cast(
                        dict[str, JsonValue], request.budget.model_dump(mode="json")
                    ),
                ),
            )
        return child, spec, delegation

    async def resume_delegation(self, parent_run_id: UUID) -> RunResult:
        """Recover one checkpointed delegation and continue its parent Run."""

        async with self._database.session() as session:
            parent = await RunRepository(session).get(parent_run_id)
            checkpoint = await CheckpointRepository(session).latest(parent_run_id)
        if parent is None:
            raise RunNotFoundError(str(parent_run_id))
        if checkpoint.kind is not CheckpointKind.DELEGATION:
            raise RunExecutionError("Run does not have a delegation checkpoint")
        if len(checkpoint.pending_delegation_ids) != 1:
            raise RunExecutionError("Delegation checkpoint is ambiguous")
        async with self._database.session() as session:
            delegation = await AgentDelegationRepository(session).get_required(
                checkpoint.pending_delegation_ids[0]
            )
        if delegation.parent_run_id != parent.id:
            raise RunExecutionError("Delegation checkpoint does not belong to this Run")

        result = await self.recover_delegation_child(
            delegation,
            workspace=Path(checkpoint.workspace),
            user_id=checkpoint.user_id,
            conversation_id=parent.conversation_id,
        )
        if not result.status.is_terminal:
            tracker = BudgetTracker(
                checkpoint.budget.budget,
                initial_usage=checkpoint.budget.used,
            )
            tracker.record_child_usage(self._usage_delta(result.usage, delegation.accounted_usage))
            await self._save_delegation_result(
                delegation,
                result,
                account_usage=True,
            )
            recorder = RunEventRecorder(self._database, parent.id)
            await recorder.emit(
                EventType.BUDGET_UPDATED,
                GenericEventPayload(
                    data={
                        "source": "child_waiting_approval",
                        "delegation_id": str(delegation.id),
                        "snapshot": cast(
                            dict[str, JsonValue], tracker.snapshot().model_dump(mode="json")
                        ),
                    }
                ),
            )
            checkpoint = await self._refresh_delegation_checkpoint(
                parent,
                checkpoint,
                delegation,
                tracker.snapshot(),
            )
            approvals: list[Approval] = []
            async with self._database.session() as session:
                repository = ApprovalRepository(session)
                for approval_id in result.approval_ids:
                    approvals.append(await repository.get_required(approval_id))
            return RunResult(
                run=parent,
                output=None,
                budget=checkpoint.budget,
                approvals=tuple(approvals),
            )
        return await self._resume_parent_from_delegation(
            parent,
            checkpoint,
            delegation,
            result,
        )

    async def recover_delegation_child(
        self,
        delegation: AgentDelegation,
        *,
        workspace: Path,
        user_id: UUID,
        conversation_id: UUID,
    ) -> DelegationResult:
        """Finish, reconcile, or safely fail one durable child execution."""

        async with self._database.session() as session:
            current = await AgentDelegationRepository(session).get_required(delegation.id)
            child = await RunRepository(session).get(current.child_run_id)
        if child is None:
            raise RunNotFoundError(str(current.child_run_id))
        if current.result is not None and (
            current.result.status.is_terminal
            or current.result.status is DelegationStatus.WAITING_APPROVAL
        ):
            return current.result

        if child.status is RunStatus.CREATED:
            spec = self._agents.get(current.target_agent_id)
            request = RunRequest(
                prompt=current.task,
                workspace=workspace,
                agent_id=current.target_agent_id,
                conversation_id=conversation_id,
                user_id=user_id,
                lease=current.lease,
                budget=current.allocated_budget,
                run_id=child.id,
                parent_run_id=current.parent_run_id,
                depth=current.depth,
            )
            try:
                with _TRACER.start_as_current_span(
                    "agentcell.agent.delegate",
                    attributes={
                        "agentcell.delegation.id": str(current.id),
                        "agentcell.parent_run.id": str(current.parent_run_id),
                        "agentcell.child_run.id": str(child.id),
                        "agentcell.agent.id": current.target_agent_id,
                        "agentcell.agent.depth": current.depth,
                    },
                ):
                    outcome = await self.execute_prepared(child, request=request, spec=spec)
            except AgentCellError:
                async with self._database.session() as session:
                    settled = await AgentDelegationRepository(session).get_required(current.id)
                if settled.result is None:
                    raise
                return settled.result
            result = self._delegation_result_from_outcome(current, outcome)
            if not result.status.is_terminal:
                await self._save_delegation_result(current, result)
                return result
            async with self._database.session() as session:
                settled = await AgentDelegationRepository(session).get_required(current.id)
            return settled.result or result

        if child.status is RunStatus.RUNNING:
            usage = await self._latest_usage(child.id)
            message = "Child Run was interrupted before a recoverable checkpoint"
            await self._finish(
                child,
                RunStatus.FAILED,
                EventType.RUN_FAILED,
                ErrorPayload(code="child_run_interrupted", message=message),
                usage=usage,
                error_code="child_run_interrupted",
                error_message=message,
            )
            async with self._database.session() as session:
                settled = await AgentDelegationRepository(session).get_required(current.id)
            if settled.result is None:
                raise RunExecutionError("Interrupted child delegation was not settled")
            return settled.result

        if child.status is RunStatus.PAUSED:
            outcome = await self.resume_delegation(child.id)
            async with self._database.session() as session:
                settled = await AgentDelegationRepository(session).get_required(current.id)
            if settled.result is not None:
                return settled.result
            result = self._delegation_result_from_outcome(current, outcome)
            await self._save_delegation_result(current, result)
            return result

        if child.status is RunStatus.WAITING_APPROVAL:
            async with self._database.session() as session:
                approvals = await ApprovalRepository(session).list_for_run(child.id)
            result = DelegationResult(
                delegation_id=current.id,
                child_run_id=child.id,
                agent_id=child.agent_id,
                status=DelegationStatus.WAITING_APPROVAL,
                usage=await self._latest_usage(child.id),
                approval_ids=tuple(
                    item.id for item in approvals if item.status is ApprovalStatus.PENDING
                ),
            )
            await self._save_delegation_result(current, result)
            return result

        if child.status.is_terminal:
            message = "Terminal child Run has no durable delegation result"
            result = DelegationResult(
                delegation_id=current.id,
                child_run_id=child.id,
                agent_id=child.agent_id,
                status=DelegationStatus.FAILED,
                error_code="delegation_result_missing",
                error_message=message,
                usage=await self._latest_usage(child.id),
            )
            await self._save_delegation_result(current, result)
            return result
        raise RunExecutionError(f"Unsupported child Run status {child.status.value}")

    @staticmethod
    def _pending_delegation_result(delegation: AgentDelegation) -> DelegationResult:
        return DelegationResult(
            delegation_id=delegation.id,
            child_run_id=delegation.child_run_id,
            agent_id=delegation.target_agent_id,
            status=delegation.status,
            usage=delegation.accounted_usage,
        )

    @staticmethod
    def _delegation_result_from_outcome(
        delegation: AgentDelegation,
        outcome: RunResult,
    ) -> DelegationResult:
        return DelegationResult(
            delegation_id=delegation.id,
            child_run_id=outcome.run.id,
            agent_id=outcome.run.agent_id,
            status=RunService._delegation_status(outcome.run.status),
            output=outcome.output,
            usage=outcome.budget.used,
            approval_ids=tuple(item.id for item in outcome.approvals),
        )

    async def get(self, run_id: UUID) -> Run | None:
        async with self._database.session() as session:
            return await RunRepository(session).get(run_id)

    async def _resume_parent_after_child(self, child_outcome: RunResult) -> RunResult:
        async with self._database.session() as session:
            delegation = await AgentDelegationRepository(session).find_by_child(
                child_outcome.run.id
            )
        if delegation is None or not child_outcome.run.status.is_terminal:
            return child_outcome
        result = delegation.result or self._delegation_result_from_outcome(
            delegation,
            child_outcome,
        )
        async with self._database.session() as session:
            parent = await RunRepository(session).get(delegation.parent_run_id)
            checkpoint = await CheckpointRepository(session).latest(delegation.parent_run_id)
        if parent is None:
            raise RunNotFoundError(str(delegation.parent_run_id))
        if (
            parent.status is not RunStatus.PAUSED
            or checkpoint.kind is not CheckpointKind.DELEGATION
        ):
            await self._save_delegation_result(delegation, result)
            return child_outcome

        return await self._resume_parent_from_delegation(
            parent,
            checkpoint,
            delegation,
            result,
        )

    async def _resume_parent_from_delegation(
        self,
        parent: Run,
        checkpoint: Checkpoint,
        delegation: AgentDelegation,
        result: DelegationResult,
    ) -> RunResult:
        if parent.status is not RunStatus.PAUSED:
            raise RunExecutionError("Delegation parent is not paused")

        tracker = BudgetTracker(
            checkpoint.budget.budget,
            initial_usage=checkpoint.budget.used,
        )
        delta = self._usage_delta(result.usage, delegation.accounted_usage)
        tracker.record_child_usage(delta)
        await self._save_delegation_result(delegation, result, account_usage=True)
        recorder = RunEventRecorder(self._database, parent.id)
        await recorder.emit(
            EventType.BUDGET_UPDATED,
            GenericEventPayload(
                data={
                    "source": "child_settled",
                    "delegation_id": str(delegation.id),
                    "snapshot": cast(
                        dict[str, JsonValue], tracker.snapshot().model_dump(mode="json")
                    ),
                }
            ),
        )
        await recorder.emit(
            EventType.AGENT_CHILD_COMPLETED,
            AgentChildCompletedPayload(
                delegation_id=delegation.id,
                parent_run_id=parent.id,
                child_run_id=result.child_run_id,
                agent_id=result.agent_id,
                status=result.status.value,
                trace_id=delegation.trace_id,
                usage=cast(dict[str, JsonValue], result.usage.model_dump(mode="json")),
            ),
        )
        parent = await self._transition(parent, RunStatus.RUNNING)
        deferred_output = cast(
            JsonValue,
            result.model_dump(mode="json", exclude_none=True),
        )
        await SqliteToolExecutionLedger(self._database, parent.id).complete_deferred(
            delegation.provider_call_id,
            deferred_output,
        )
        await recorder.emit(
            EventType.TOOL_COMPLETED,
            GenericEventPayload(
                data={
                    "provider_call_id": delegation.provider_call_id,
                    "tool_name": "agent.delegate",
                    "deferred": True,
                    "delegation_id": str(delegation.id),
                }
            ),
        )
        workspace = await asyncio.to_thread(Path(checkpoint.workspace).resolve, strict=True)
        spec = self._agents.get(checkpoint.agent_id)
        request = RunRequest(
            prompt=checkpoint.prompt,
            workspace=workspace,
            agent_id=checkpoint.agent_id,
            conversation_id=parent.conversation_id,
            user_id=checkpoint.user_id,
            lease=checkpoint.lease,
            permission_mode=checkpoint.permission_mode,
            budget=checkpoint.budget.budget,
            run_id=parent.id,
            parent_run_id=parent.parent_run_id,
            depth=max(0, delegation.depth - 1),
        )
        await self._changes.reconcile(
            parent.id,
            workspace=request.workspace,
            lease=request.lease,
            events=recorder,
        )
        deps = self._deps(
            run=parent,
            request=request,
            spec=spec,
            tracker=tracker,
            recorder=recorder,
            temporary_approved_tools=checkpoint.temporary_approved_tools,
        )
        messages = ModelMessagesTypeAdapter.validate_python(checkpoint.messages)
        try:
            agent_result = await self._execute_agent(
                spec,
                None,
                checkpoint.budget.budget,
                deps,
                tracker,
                recorder,
                message_history=messages,
                deferred_tool_results=DeferredToolResults(
                    calls={delegation.provider_call_id: deferred_output}
                ),
                context_query=checkpoint.prompt,
            )
            outcome = await self._handle_result(
                parent,
                request=request,
                spec=spec,
                tracker=tracker,
                result=agent_result,
                temporary_approved_tools=checkpoint.temporary_approved_tools,
            )
            return await self._resume_parent_after_child(outcome)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._cancel_running(parent, reason="cancelled", usage=tracker.usage)
            )
            raise
        except AgentCellError as error:
            await self._fail(parent, error, usage=tracker.usage)
            raise

    @staticmethod
    def _usage_delta(current: Usage, accounted: Usage) -> Usage:
        return Usage(
            requests=max(0, current.requests - accounted.requests),
            input_tokens=max(0, current.input_tokens - accounted.input_tokens),
            cache_write_tokens=max(
                0,
                current.cache_write_tokens - accounted.cache_write_tokens,
            ),
            cache_read_tokens=max(
                0,
                current.cache_read_tokens - accounted.cache_read_tokens,
            ),
            output_tokens=max(0, current.output_tokens - accounted.output_tokens),
            tool_calls=max(0, current.tool_calls - accounted.tool_calls),
            cost=max(Decimal("0"), current.cost - accounted.cost),
            children=max(0, current.children - accounted.children),
            max_depth_reached=max(
                0,
                current.max_depth_reached - accounted.max_depth_reached,
            ),
        )

    def _deps(
        self,
        *,
        run: Run,
        request: RunRequest,
        spec: AgentSpec,
        tracker: BudgetTracker,
        recorder: RunEventRecorder,
        temporary_approved_tools: frozenset[str] = frozenset(),
    ) -> RunDeps:
        model_spec = self._providers.model_spec(spec.model_ref)
        return RunDeps(
            run_id=run.id,
            conversation_id=run.conversation_id,
            user_id=request.user_id,
            workspace=request.workspace,
            lease=request.lease,
            permission_mode=request.permission_mode,
            budget=tracker,
            events=recorder,
            tools=self._tool_executor,
            agents=self._agents,
            agent_id=spec.id,
            agent_name=spec.name,
            provider=model_spec.provider.value,
            model=model_spec.model,
            temporary_approved_tools=temporary_approved_tools,
            ledger=SqliteToolExecutionLedger(self._database, run.id),
            changes=self._changes,
            approvals=RunApprovalRecorder(
                self._database,
                run_id=run.id,
                agent_id=spec.id,
                agent_name=spec.name,
                provider=model_spec.provider.value,
                model=model_spec.model,
                budget=tracker,
            ),
            artifacts=self._artifacts,
            memory=MemoryService(self._database, events=recorder),
            depth=request.depth,
            delegation=self,
        )

    async def _execute_agent(
        self,
        spec: AgentSpec,
        prompt: str | None,
        budget: Budget,
        deps: RunDeps,
        tracker: BudgetTracker,
        recorder: RunEventRecorder,
        *,
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        context_query: str | None = None,
    ) -> AgentRunResult[str | DeferredToolRequests]:
        model_spec = self._providers.model_spec(spec.model_ref)
        base_model = await self._providers.build_model(spec.model_ref)
        memory_scope = MemoryScope(
            user_id=deps.user_id,
            project_id=str(deps.workspace),
            agent_id=deps.agent_id,
        )
        model = RunModel(
            base_model,
            provider=model_spec.provider,
            model_name=model_spec.model,
            budget=tracker,
            events=recorder,
            context_manager=ContextManager(
                self._artifacts,
                memory_injector=(None if deps.memory is None else MemoryInjector(deps.memory)),
                memory_scope=memory_scope,
                memory_query=context_query or prompt,
            ),
        )
        agent = await self._agent_factory.create(
            spec,
            deps_type=RunDeps,
            tools=build_agent_tools(
                self._effective_tool_names(spec, deps.lease),
                self._tool_registry,
            ),
            model=model,
        )
        agent.instructions(budget_instructions)

        async def stream_events(
            context: RunContext[RunDeps],
            events: AsyncIterable[AgentStreamEvent],
        ) -> None:
            del context
            async for event in events:
                delta: str | None = None
                if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                    delta = event.part.content
                elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                    delta = event.delta.content_delta
                if delta:
                    await recorder.emit(EventType.MODEL_TEXT_DELTA, TextDeltaPayload(delta=delta))

        usage_at_start = tracker.usage
        limits = UsageLimits(
            # PydanticAI reconstructs parent usage from message history when a paused
            # graph resumes. Absolute Run limits avoid treating that restored usage as
            # newly consumed remaining capacity; BudgetTracker remains authoritative
            # for parent/child aggregate reservations before every actual request.
            request_limit=min(budget.max_requests, spec.max_steps),
            tool_calls_limit=budget.max_tool_calls,
            input_tokens_limit=budget.max_input_tokens,
            output_tokens_limit=budget.max_output_tokens,
            total_tokens_limit=budget.max_total_tokens,
        )
        try:
            run_deps = replace(
                deps,
                has_deferred_tool_results=deferred_tool_results is not None,
            )
            async with asyncio.timeout(budget.max_duration_seconds):
                result = await agent.run(
                    prompt,
                    output_type=[str, DeferredToolRequests],
                    deps=run_deps,
                    message_history=message_history,
                    deferred_tool_results=deferred_tool_results,
                    usage_limits=limits,
                    event_stream_handler=stream_events,
                    retries=FINAL_OUTPUT_RETRIES,
                )
        except UsageLimitExceeded as error:
            raise classify_usage_limit(
                error, budget=budget, usage_at_start=usage_at_start
            ) from error
        except TimeoutError as error:
            raise BudgetExceededError(
                "duration_seconds", budget.max_duration_seconds, tracker.usage.duration_seconds
            ) from error
        except UnexpectedModelBehavior as error:
            raise ModelOutputError(FINAL_REQUEST_ATTEMPTS) from error
        return result

    async def _handle_result(
        self,
        run: Run,
        *,
        request: RunRequest,
        spec: AgentSpec,
        tracker: BudgetTracker,
        result: AgentRunResult[str | DeferredToolRequests],
        temporary_approved_tools: frozenset[str] = frozenset(),
    ) -> RunResult:
        if isinstance(result.output, DeferredToolRequests):
            if result.output.calls:
                return await self._pause_for_delegation(
                    run,
                    request=request,
                    spec=spec,
                    tracker=tracker,
                    deferred=result.output,
                    messages=result.all_messages(),
                    new_messages=result.new_messages(),
                    temporary_approved_tools=temporary_approved_tools,
                )
            return await self._pause_for_approval(
                run,
                request=request,
                spec=spec,
                tracker=tracker,
                deferred=result.output,
                messages=result.all_messages(),
                new_messages=result.new_messages(),
                temporary_approved_tools=temporary_approved_tools,
            )
        output = result.output
        usage = tracker.usage
        completed = await self._finish(
            run,
            RunStatus.COMPLETED,
            EventType.RUN_COMPLETED,
            RunCompletedPayload(
                output_characters=len(output),
                requests=usage.requests,
                input_tokens=usage.input_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                output_tokens=usage.output_tokens,
                tool_calls=usage.tool_calls,
            ),
            output=output,
            usage=usage,
        )
        return RunResult(
            run=completed,
            output=output,
            budget=tracker.snapshot(),
            messages_json=ModelMessagesTypeAdapter.dump_json(result.new_messages()).decode("utf-8"),
        )

    async def _pause_for_delegation(
        self,
        run: Run,
        *,
        request: RunRequest,
        spec: AgentSpec,
        tracker: BudgetTracker,
        deferred: DeferredToolRequests,
        messages: list[ModelMessage],
        new_messages: list[ModelMessage],
        temporary_approved_tools: frozenset[str],
    ) -> RunResult:
        if len(deferred.calls) != 1:
            raise RunExecutionError("Exactly one pending delegation is supported per Run")
        call = deferred.calls[0]
        metadata = deferred.metadata.get(call.tool_call_id, {})
        try:
            delegation_id = UUID(str(metadata["delegation_id"]))
            child_run_id = UUID(str(metadata["child_run_id"]))
        except (KeyError, ValueError) as error:
            raise RunExecutionError("Deferred delegation metadata is invalid") from error
        async with self._database.session() as session:
            delegation = await AgentDelegationRepository(session).get_required(delegation_id)
        if delegation.parent_run_id != run.id or delegation.child_run_id != child_run_id:
            raise RunExecutionError("Deferred delegation does not belong to this Run")

        paused = run.transition_to(RunStatus.PAUSED)
        serialized_messages = cast(
            list[JsonValue],
            redact_sensitive_data(
                cast(
                    list[JsonValue],
                    ModelMessagesTypeAdapter.dump_python(messages, mode="json"),
                )
            ),
        )
        async with self._database.transaction() as session:
            store = EventStore(session)
            checkpoint_event = await store.append(
                run_id=run.id,
                event_type=EventType.CHECKPOINT_CREATED,
                payload=GenericEventPayload(
                    data={
                        "reason": "delegation",
                        "delegation_id": str(delegation.id),
                        "child_run_id": str(child_run_id),
                    }
                ),
            )
            await CheckpointRepository(session).create(
                Checkpoint(
                    run_id=run.id,
                    user_id=request.user_id,
                    event_sequence=checkpoint_event.sequence,
                    kind=CheckpointKind.DELEGATION,
                    agent_id=spec.id,
                    prompt=request.prompt,
                    workspace=str(request.workspace),
                    lease=request.lease,
                    permission_mode=request.permission_mode,
                    budget=tracker.snapshot(),
                    messages=serialized_messages,
                    pending_delegation_ids=(delegation.id,),
                    child_run_ids=(child_run_id,),
                    temporary_approved_tools=temporary_approved_tools,
                    artifact_ids=self._artifact_ids(serialized_messages),
                    run_status=RunStatus.PAUSED,
                    parent_run_id=run.parent_run_id,
                    depth=request.depth,
                )
            )
            await RunRepository(session).save(paused)
            await store.append(
                run_id=run.id,
                event_type=EventType.RUN_STATUS_CHANGED,
                payload=RunStatusChangedPayload(
                    previous_status=run.status,
                    status=RunStatus.PAUSED,
                ),
            )
        outcome = await self.resume_delegation(paused.id)
        continued = ModelMessagesTypeAdapter.validate_json(outcome.messages_json)
        return outcome.model_copy(
            update={
                "messages_json": ModelMessagesTypeAdapter.dump_json(
                    [*new_messages, *continued]
                ).decode("utf-8")
            }
        )

    async def _refresh_delegation_checkpoint(
        self,
        parent: Run,
        checkpoint: Checkpoint,
        delegation: AgentDelegation,
        budget: BudgetSnapshot,
    ) -> Checkpoint:
        """Persist child progress without losing the parent's provider continuation."""

        async with self._database.transaction() as session:
            store = EventStore(session)
            event = await store.append(
                run_id=parent.id,
                event_type=EventType.CHECKPOINT_CREATED,
                payload=GenericEventPayload(
                    data={
                        "reason": "delegation_progress",
                        "delegation_id": str(delegation.id),
                        "child_run_id": str(delegation.child_run_id),
                    }
                ),
            )
            refreshed = checkpoint.model_copy(
                update={
                    "id": uuid4(),
                    "event_sequence": event.sequence,
                    "budget": budget,
                    "pending_delegation_ids": (delegation.id,),
                    "child_run_ids": (delegation.child_run_id,),
                    "run_status": parent.status,
                    "created_at": datetime.now(UTC),
                }
            )
            await CheckpointRepository(session).create(refreshed)
        return refreshed

    async def _pause_for_approval(
        self,
        run: Run,
        *,
        request: RunRequest,
        spec: AgentSpec,
        tracker: BudgetTracker,
        deferred: DeferredToolRequests,
        messages: list[ModelMessage],
        new_messages: list[ModelMessage],
        temporary_approved_tools: frozenset[str],
    ) -> RunResult:
        if not deferred.approvals:
            raise RunExecutionError("Deferred result contained no approval requests")
        model_spec = self._providers.model_spec(spec.model_ref)
        approvals: list[Approval] = []
        for call in deferred.approvals:
            domain_tool_name = self._domain_tool_name(spec, call.tool_name)
            definition = self._tool_registry.get(domain_tool_name)
            metadata = deferred.metadata.get(call.tool_call_id, {})
            approvals.append(
                Approval(
                    run_id=run.id,
                    provider_call_id=call.tool_call_id,
                    agent_id=spec.id,
                    agent_name=spec.name,
                    provider=model_spec.provider.value,
                    model=model_spec.model,
                    tool_name=domain_tool_name,
                    arguments=cast(dict[str, JsonValue], call.args_as_dict()),
                    risk=definition.policy.risk,
                    impact=str(metadata.get("impact") or definition.description),
                    diff=cast(str | None, metadata.get("diff")),
                    diff_artifact=(
                        None
                        if metadata.get("diff_artifact") is None
                        else ArtifactReference.model_validate(metadata["diff_artifact"])
                    ),
                    remaining_budget=tracker.snapshot(),
                    idempotent=definition.policy.idempotent,
                    timeout_seconds=definition.policy.timeout_seconds,
                )
            )
        waiting = run.transition_to(RunStatus.WAITING_APPROVAL)
        serialized_messages = cast(
            list[JsonValue],
            redact_sensitive_data(
                cast(
                    list[JsonValue],
                    ModelMessagesTypeAdapter.dump_python(messages, mode="json"),
                )
            ),
        )
        async with self._database.transaction() as session:
            approval_repository = ApprovalRepository(session)
            store = EventStore(session)
            for approval in approvals:
                await approval_repository.create(approval)
                await store.append(
                    run_id=run.id,
                    event_type=EventType.TOOL_APPROVAL_REQUIRED,
                    payload=GenericEventPayload(
                        data={
                            "approval_id": str(approval.id),
                            "provider_call_id": approval.provider_call_id,
                            "tool_name": approval.tool_name,
                            "risk": approval.risk.value,
                            "arguments": self._bounded_event_arguments(approval.arguments),
                            "impact": approval.impact,
                            "diff": approval.diff,
                            "diff_artifact": (
                                None
                                if approval.diff_artifact is None
                                else approval.diff_artifact.model_dump(mode="json")
                            ),
                            "remaining_budget": cast(
                                dict[str, JsonValue],
                                approval.remaining_budget.model_dump(mode="json"),
                            ),
                            "idempotent": approval.idempotent,
                            "timeout_seconds": approval.timeout_seconds,
                        }
                    ),
                )
            checkpoint_event = await store.append(
                run_id=run.id,
                event_type=EventType.CHECKPOINT_CREATED,
                payload=GenericEventPayload(
                    data={"reason": "waiting_approval", "approval_count": len(approvals)}
                ),
            )
            checkpoint = Checkpoint(
                run_id=run.id,
                user_id=request.user_id,
                event_sequence=checkpoint_event.sequence,
                kind=CheckpointKind.APPROVAL,
                agent_id=spec.id,
                prompt=request.prompt,
                workspace=str(request.workspace),
                lease=request.lease,
                permission_mode=request.permission_mode,
                budget=tracker.snapshot(),
                messages=serialized_messages,
                pending_approval_ids=tuple(approval.id for approval in approvals),
                temporary_approved_tools=temporary_approved_tools,
                artifact_ids=tuple(
                    sorted(
                        {
                            *self._artifact_ids(serialized_messages),
                            *(
                                approval.diff_artifact.artifact_id
                                for approval in approvals
                                if approval.diff_artifact is not None
                            ),
                        },
                        key=str,
                    )
                ),
                run_status=RunStatus.WAITING_APPROVAL,
                parent_run_id=run.parent_run_id,
                depth=request.depth,
            )
            await CheckpointRepository(session).create(checkpoint)
            await RunRepository(session).save(waiting)
            await store.append(
                run_id=run.id,
                event_type=EventType.RUN_STATUS_CHANGED,
                payload=RunStatusChangedPayload(
                    previous_status=run.status,
                    status=RunStatus.WAITING_APPROVAL,
                ),
            )
        return RunResult(
            run=waiting,
            output=None,
            budget=tracker.snapshot(),
            approvals=tuple(approvals),
            messages_json=ModelMessagesTypeAdapter.dump_json(new_messages).decode("utf-8"),
        )

    def _domain_tool_name(self, spec: AgentSpec, provider_name: str) -> str:
        """Resolve a model-facing alias without leaking it into domain persistence."""

        if provider_name in spec.tools:
            return provider_name
        matches = [name for name in spec.tools if portable_tool_name(name) == provider_name]
        if len(matches) != 1:
            raise ToolArgumentsError(provider_name)
        return matches[0]

    async def _persist_decision(
        self,
        run: Run,
        approval: Approval,
        decision: ApprovalDecision,
    ) -> Approval:
        arguments = approval.arguments
        status = ApprovalStatus.APPROVED
        event_type = EventType.TOOL_APPROVED
        if decision.kind is ApprovalDecisionKind.REJECT:
            status = ApprovalStatus.REJECTED
            event_type = EventType.TOOL_REJECTED
        elif decision.kind is ApprovalDecisionKind.MODIFY:
            assert decision.arguments is not None
            definition = self._tool_registry.get(approval.tool_name)
            try:
                definition.params_model.model_validate(decision.arguments)
            except ValidationError as error:
                raise ToolArgumentsError(approval.tool_name) from error
            arguments = decision.arguments
        decided = approval.model_copy(
            update={
                "status": status,
                "approved_arguments": arguments if status is ApprovalStatus.APPROVED else None,
                "grant_same_tool": decision.grant_same_tool,
                "decision_message": decision.message,
                "decision_source": ApprovalDecisionSource.HUMAN,
                "decided_at": datetime.now(UTC),
            }
        )
        running = run.transition_to(RunStatus.RUNNING)
        async with self._database.transaction() as session:
            await ApprovalRepository(session).save(decided)
            store = EventStore(session)
            await store.append(
                run_id=run.id,
                event_type=event_type,
                payload=GenericEventPayload(
                    data={
                        "approval_id": str(approval.id),
                        "provider_call_id": approval.provider_call_id,
                        "tool_name": approval.tool_name,
                        "arguments": arguments,
                        "grant_same_tool": decision.grant_same_tool,
                        "decision_source": ApprovalDecisionSource.HUMAN.value,
                    }
                ),
            )
            await RunRepository(session).save(running)
            await store.append(
                run_id=run.id,
                event_type=EventType.RUN_STATUS_CHANGED,
                payload=RunStatusChangedPayload(
                    previous_status=run.status,
                    status=RunStatus.RUNNING,
                ),
            )
        return decided

    @staticmethod
    def _deferred_result(approval: Approval) -> DeferredToolResults:
        if approval.status is ApprovalStatus.REJECTED:
            value: ToolApproved | ToolDenied = ToolDenied(
                approval.decision_message or "The user rejected this operation."
            )
        else:
            override = (
                approval.approved_arguments
                if approval.approved_arguments != approval.arguments
                else None
            )
            value = ToolApproved(override_args=override)
        return DeferredToolResults(approvals={approval.provider_call_id: value})

    @staticmethod
    def _ensure_same_decision(approval: Approval, decision: ApprovalDecision) -> None:
        expected_status = (
            ApprovalStatus.REJECTED
            if decision.kind is ApprovalDecisionKind.REJECT
            else ApprovalStatus.APPROVED
        )
        expected_arguments = (
            decision.arguments
            if decision.kind is ApprovalDecisionKind.MODIFY
            else approval.arguments
        )
        if (
            approval.status is not expected_status
            or approval.approved_arguments
            != (expected_arguments if expected_status is ApprovalStatus.APPROVED else None)
            or approval.grant_same_tool != decision.grant_same_tool
            or approval.decision_message != decision.message
        ):
            raise ApprovalConflictError("Approval already has a different decision")

    def _authorize_agent(self, spec: AgentSpec, lease: CapabilityLease) -> None:
        """Validate AgentSpec maxima; unused extra lease scopes never expose a tool."""

        del lease
        for name in spec.tools:
            definition = self._tool_registry.get(name)
            for capability in definition.policy.capabilities:
                if capability not in spec.capabilities:
                    raise CapabilityDeniedError(capability)

    def _effective_tool_names(
        self,
        spec: AgentSpec,
        lease: CapabilityLease,
    ) -> tuple[str, ...]:
        """Hide tools whose declared capabilities are not leased for this Run."""

        visible: list[str] = []
        for name in spec.tools:
            definition = self._tool_registry.get(name)
            if all(lease.allows(capability) for capability in definition.policy.capabilities):
                visible.append(name)
        return tuple(visible)

    @staticmethod
    def _authorize_agent_limits(spec: AgentSpec, budget: Budget) -> None:
        if budget.max_children > spec.max_children:
            raise BudgetExceededError("agent.max_children", spec.max_children, budget.max_children)
        if budget.max_depth > spec.max_depth:
            raise BudgetExceededError("agent.max_depth", spec.max_depth, budget.max_depth)

    @staticmethod
    def _delegation_status(status: RunStatus) -> DelegationStatus:
        return {
            RunStatus.WAITING_APPROVAL: DelegationStatus.WAITING_APPROVAL,
            RunStatus.COMPLETED: DelegationStatus.COMPLETED,
            RunStatus.FAILED: DelegationStatus.FAILED,
            RunStatus.CANCELLED: DelegationStatus.CANCELLED,
        }.get(status, DelegationStatus.RUNNING)

    async def _save_delegation_result(
        self,
        delegation: AgentDelegation,
        result: DelegationResult,
        *,
        account_usage: bool = False,
    ) -> None:
        updated = delegation.model_copy(
            update={
                "status": result.status,
                "accounted_usage": (result.usage if account_usage else delegation.accounted_usage),
                "result": result,
                "updated_at": datetime.now(UTC),
            }
        )
        async with self._database.transaction() as session:
            await AgentDelegationRepository(session).save(updated)

    async def _latest_usage(self, run_id: UUID) -> Usage:
        async with self._database.session() as session:
            events = await EventStore(session).list_for_run(run_id)
        for event in reversed(events):
            if event.event_type is not EventType.BUDGET_UPDATED:
                continue
            if not isinstance(event.payload, GenericEventPayload):
                continue
            snapshot = event.payload.data.get("snapshot")
            if isinstance(snapshot, dict) and isinstance(snapshot.get("used"), dict):
                return Usage.model_validate(snapshot["used"])
        return Usage()

    async def _create_run(self, run: Run) -> None:
        async with self._database.transaction() as session:
            await RunRepository(session).create(run)
            await EventStore(session).append(
                run_id=run.id,
                event_type=EventType.RUN_STARTED,
                payload=RunStartedPayload(
                    conversation_id=run.conversation_id,
                    agent_id=run.agent_id,
                ),
            )

    async def _transition(self, run: Run, status: RunStatus) -> Run:
        transitioned = run.transition_to(status)
        async with self._database.transaction() as session:
            await RunRepository(session).save(transitioned)
            await EventStore(session).append(
                run_id=run.id,
                event_type=EventType.RUN_STATUS_CHANGED,
                payload=RunStatusChangedPayload(
                    previous_status=run.status,
                    status=status,
                ),
            )
        return transitioned

    async def _finish(
        self,
        run: Run,
        status: RunStatus,
        event_type: EventType,
        payload: EventPayload,
        *,
        output: str | None = None,
        usage: Usage | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> Run:
        transitioned = run.transition_to(status)
        async with self._database.transaction() as session:
            await RunRepository(session).save(transitioned)
            store = EventStore(session)
            await store.append(
                run_id=run.id,
                event_type=EventType.RUN_STATUS_CHANGED,
                payload=RunStatusChangedPayload(
                    previous_status=run.status,
                    status=status,
                ),
            )
            await store.append(run_id=run.id, event_type=event_type, payload=payload)
            change_sets = ChangeSetRepository(session)
            change_set = await change_sets.get_for_run(run.id)
            if change_set is not None:
                await change_sets.save(
                    change_set.model_copy(
                        update={
                            "status": (
                                ChangeSetStatus.COMPLETED
                                if status is RunStatus.COMPLETED
                                else ChangeSetStatus.CONFLICT
                            ),
                            "completed_at": datetime.now(UTC),
                        }
                    )
                )
            repository = AgentDelegationRepository(session)
            delegation = await repository.find_by_child(run.id)
            if delegation is not None:
                result = DelegationResult(
                    delegation_id=delegation.id,
                    child_run_id=run.id,
                    agent_id=run.agent_id,
                    status=self._delegation_status(status),
                    output=output,
                    error_code=error_code,
                    error_message=error_message,
                    usage=usage or Usage(),
                )
                await repository.save(
                    delegation.model_copy(
                        update={
                            "status": result.status,
                            "result": result,
                            "updated_at": datetime.now(UTC),
                        }
                    )
                )
        return transitioned

    async def _cancel_running(
        self,
        run: Run,
        *,
        reason: str,
        usage: Usage | None = None,
    ) -> None:
        current = await self.get(run.id)
        if current is None or current.status.is_terminal:
            return
        await self._finish(
            current,
            RunStatus.CANCELLED,
            EventType.RUN_CANCELLED,
            GenericEventPayload(data={"reason": reason}),
            usage=usage,
            error_code="run_cancelled",
            error_message=reason,
        )

    async def _fail(
        self,
        run: Run,
        error: AgentCellError,
        *,
        usage: Usage | None = None,
    ) -> None:
        current = await self.get(run.id)
        if current is None or current.status.is_terminal:
            return
        await self._finish(
            current,
            RunStatus.FAILED,
            EventType.RUN_FAILED,
            ErrorPayload(code=error.code, message=str(error), retryable=error.retryable),
            usage=usage,
            error_code=error.code,
            error_message=str(error),
        )

    @staticmethod
    def _artifact_ids(value: JsonValue) -> tuple[UUID, ...]:
        found: set[UUID] = set()

        def visit(item: JsonValue) -> None:
            if isinstance(item, list):
                for child in item:
                    visit(child)
            elif isinstance(item, dict):
                candidate = item.get("artifact_id")
                if isinstance(candidate, str):
                    try:
                        found.add(UUID(candidate))
                    except ValueError:
                        pass
                for child in item.values():
                    visit(child)

        visit(value)
        return tuple(sorted(found, key=str))

    @staticmethod
    def _bounded_event_arguments(arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        argument_bytes = len(
            json.dumps(arguments, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if argument_bytes <= 32 * 1024:
            return arguments
        return {
            "summary": "Approval arguments omitted from event payload; see approval record",
            "argument_bytes": argument_bytes,
        }
