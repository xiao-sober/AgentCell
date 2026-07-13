"""Event-sourced Run orchestration with durable deferred-tool recovery."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

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

from agentcell.agents import AgentFactory, AgentRegistry, AgentSpec
from agentcell.budgets import Budget, BudgetSnapshot, BudgetTracker
from agentcell.errors import (
    AgentCellError,
    ApprovalConflictError,
    BudgetExceededError,
    CapabilityDeniedError,
    RunExecutionError,
    RunNotFoundError,
    ToolArgumentsError,
)
from agentcell.events import (
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
from agentcell.kernel.checkpoint import Checkpoint, CheckpointKind
from agentcell.kernel.deps import RunDeps
from agentcell.kernel.event_recorder import RunEventRecorder
from agentcell.kernel.lifecycle import RunStatus
from agentcell.kernel.model_runtime import RunModel
from agentcell.kernel.models import Run
from agentcell.kernel.tool_bridge import build_agent_tools
from agentcell.memory.compaction import ContextManager
from agentcell.memory.injector import MemoryInjector
from agentcell.memory.models import MemoryScope
from agentcell.memory.service import MemoryService
from agentcell.policy import (
    Approval,
    ApprovalDecision,
    ApprovalDecisionKind,
    ApprovalStatus,
    CapabilityLease,
)
from agentcell.providers import ProviderFactory
from agentcell.storage import (
    ApprovalRepository,
    CheckpointRepository,
    Database,
    EventStore,
    FileArtifactStore,
    RunRepository,
    SqliteToolExecutionLedger,
)
from agentcell.tools import ToolExecutor, ToolRegistry


def _default_budget() -> Budget:
    return Budget(
        max_requests=10,
        max_input_tokens=100_000,
        max_output_tokens=20_000,
        max_total_tokens=120_000,
        max_tool_calls=20,
        max_duration_seconds=300,
        max_cost=None,
        max_children=0,
        max_depth=0,
    )


class RunRequest(BaseModel):
    """Validated input for one new, immediately executed Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1)
    workspace: Path
    agent_id: str = "coordinator"
    conversation_id: UUID = Field(default_factory=uuid4)
    user_id: UUID = Field(default_factory=uuid4)
    lease: CapabilityLease = Field(default_factory=CapabilityLease)
    budget: Budget = Field(default_factory=_default_budget)

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

    async def run(self, request: RunRequest) -> RunResult:
        spec = self._agents.get(request.agent_id)
        self._authorize_agent(spec, request.lease)
        run = Run(conversation_id=request.conversation_id, agent_id=spec.id)
        await self._create_run(run)
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
            )
            return await self._handle_result(
                run,
                request=request,
                spec=spec,
                tracker=tracker,
                result=result,
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._cancel_running(run, reason="cancelled"))
            raise
        except UsageLimitExceeded as error:
            classified = BudgetExceededError("pydantic_ai_usage", None, None)
            await self._fail(run, classified)
            raise classified from error
        except AgentCellError as error:
            await self._fail(run, error)
            raise
        except Exception as error:
            classified = RunExecutionError()
            await self._fail(run, classified)
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
            budget=checkpoint.budget.budget,
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
            return await self._handle_result(
                run,
                request=request,
                spec=spec,
                tracker=tracker,
                result=result,
                temporary_approved_tools=frozenset(temporary),
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._cancel_running(run, reason="cancelled"))
            raise
        except UsageLimitExceeded as error:
            classified = BudgetExceededError("pydantic_ai_usage", None, None)
            await self._fail(run, classified)
            raise classified from error
        except AgentCellError as error:
            await self._fail(run, error)
            raise
        except Exception as error:
            classified = RunExecutionError()
            await self._fail(run, classified)
            raise classified from error

    async def cancel(self, run_id: UUID) -> Run:
        async with self._database.session() as session:
            run = await RunRepository(session).get(run_id)
        if run is None:
            raise RunNotFoundError(str(run_id))
        if run.status is RunStatus.CANCELLED or run.status.is_terminal:
            return run
        return await self._finish(
            run,
            RunStatus.CANCELLED,
            EventType.RUN_CANCELLED,
            GenericEventPayload(data={"reason": "requested"}),
        )

    async def get(self, run_id: UUID) -> Run | None:
        async with self._database.session() as session:
            return await RunRepository(session).get(run_id)

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
            artifacts=self._artifacts,
            memory=MemoryService(self._database, events=recorder),
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
            tools=build_agent_tools(spec.tools, self._tool_registry),
            model=model,
        )

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

        limits = UsageLimits(
            request_limit=min(budget.max_requests, spec.max_steps),
            tool_calls_limit=budget.max_tool_calls,
            input_tokens_limit=budget.max_input_tokens,
            output_tokens_limit=budget.max_output_tokens,
            total_tokens_limit=budget.max_total_tokens,
        )
        try:
            async with asyncio.timeout(budget.max_duration_seconds):
                result = await agent.run(
                    prompt,
                    output_type=[str, DeferredToolRequests],
                    deps=deps,
                    message_history=message_history,
                    deferred_tool_results=deferred_tool_results,
                    usage_limits=limits,
                    event_stream_handler=stream_events,
                )
        except TimeoutError as error:
            raise BudgetExceededError(
                "duration_seconds", budget.max_duration_seconds, tracker.usage.duration_seconds
            ) from error
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
            return await self._pause_for_approval(
                run,
                request=request,
                spec=spec,
                tracker=tracker,
                deferred=result.output,
                messages=result.all_messages(),
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
                output_tokens=usage.output_tokens,
                tool_calls=usage.tool_calls,
            ),
        )
        return RunResult(run=completed, output=output, budget=tracker.snapshot())

    async def _pause_for_approval(
        self,
        run: Run,
        *,
        request: RunRequest,
        spec: AgentSpec,
        tracker: BudgetTracker,
        deferred: DeferredToolRequests,
        messages: list[ModelMessage],
        temporary_approved_tools: frozenset[str],
    ) -> RunResult:
        if not deferred.approvals:
            raise RunExecutionError("Deferred result contained no approval requests")
        model_spec = self._providers.model_spec(spec.model_ref)
        approvals: list[Approval] = []
        for call in deferred.approvals:
            definition = self._tool_registry.get(call.tool_name)
            metadata = deferred.metadata.get(call.tool_call_id, {})
            approvals.append(
                Approval(
                    run_id=run.id,
                    provider_call_id=call.tool_call_id,
                    agent_id=spec.id,
                    agent_name=spec.name,
                    provider=model_spec.provider.value,
                    model=model_spec.model,
                    tool_name=call.tool_name,
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
        )

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

    @staticmethod
    def _authorize_agent(spec: AgentSpec, lease: CapabilityLease) -> None:
        for capability in spec.capabilities:
            if not lease.allows(capability):
                raise CapabilityDeniedError(capability)

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
        return transitioned

    async def _cancel_running(self, run: Run, *, reason: str) -> None:
        await self._finish(
            run,
            RunStatus.CANCELLED,
            EventType.RUN_CANCELLED,
            GenericEventPayload(data={"reason": reason}),
        )

    async def _fail(self, run: Run, error: AgentCellError) -> None:
        current = await self.get(run.id)
        if current is None or current.status.is_terminal:
            return
        await self._finish(
            current,
            RunStatus.FAILED,
            EventType.RUN_FAILED,
            ErrorPayload(code=error.code, message=str(error), retryable=error.retryable),
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
