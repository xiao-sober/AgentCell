"""Application-layer deterministic routing and authoritative task-root preparation."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

from pydantic_ai import Agent, ModelMessagesTypeAdapter, PromptedOutput
from pydantic_ai.messages import ModelMessage
from pydantic_ai.usage import UsageLimits

from agentcell.agents import (
    AgentDelegation,
    AgentRegistry,
    DelegationKind,
    DelegationResult,
    DelegationStatus,
    HandoffRequest,
    HandoffResult,
    TeamRegistry,
)
from agentcell.budgets import Budget, BudgetSnapshot, BudgetTracker, Usage
from agentcell.errors import RunExecutionError, RunNotFoundError
from agentcell.events import (
    AgentChildCompletedPayload,
    ErrorPayload,
    EventPayload,
    EventType,
    GenericEventPayload,
    JsonValue,
    RunCompletedPayload,
    RunStartedPayload,
    RunStatusChangedPayload,
    TaskRouteEventPayload,
    TextDeltaPayload,
)
from agentcell.kernel import RunStatus
from agentcell.kernel.checkpoint import Checkpoint, CheckpointKind
from agentcell.kernel.child_projection import ChildEventProjector
from agentcell.kernel.event_recorder import RunEventRecorder
from agentcell.kernel.handoff import HandoffService
from agentcell.kernel.model_runtime import RunModel
from agentcell.kernel.models import Run
from agentcell.kernel.run_service import RunRequest, RunService
from agentcell.policy import Approval, ApprovalDecision, Capability, CapabilityLease
from agentcell.providers import ProviderFactory
from agentcell.routing.models import (
    ModelRouteClassification,
    RouteBudgetProfile,
    RoutingPolicy,
    TaskExecutionResult,
    TaskRouteDecision,
    TaskRouteIssue,
    TaskRouteIssueCode,
    TaskRouteMode,
    TaskRouteRequest,
    TaskRouteSource,
    TaskRouteStatus,
)
from agentcell.routing.policy import capability_gaps, validate_target
from agentcell.routing.rules import DeterministicRouteMatch, deterministic_route, intent_signals
from agentcell.storage import (
    AgentDelegationRepository,
    ApprovalRepository,
    CheckpointRepository,
    Database,
    EventStore,
    RunRepository,
)
from agentcell.tools import ToolEventSink

TASK_ROUTER_AGENT_ID = "task-router"


class _NullEventSink:
    async def emit(self, event_type: EventType, payload: EventPayload) -> None:
        del event_type, payload


@dataclass(frozen=True, slots=True)
class PreparedTaskRoute:
    root: Run
    decision: TaskRouteDecision
    request: TaskRouteRequest
    history: tuple[JsonValue, ...] = ()


class TaskRoutingService:
    """Route and execute tasks without exceeding caller-supplied authority or budget."""

    def __init__(
        self,
        *,
        database: Database,
        agents: AgentRegistry,
        teams: TeamRegistry,
        providers: ProviderFactory,
        runs: RunService | None = None,
        handoffs: HandoffService | None = None,
        routing_model_ref: str | None = None,
        policy: RoutingPolicy | None = None,
    ) -> None:
        self._database = database
        self._agents = agents
        self._teams = teams
        self._providers = providers
        self._runs = runs
        self._handoffs = handoffs
        self._routing_model_ref = routing_model_ref
        self._policy = policy or RoutingPolicy()

    @property
    def policy(self) -> RoutingPolicy:
        return self._policy

    async def preview(self, request: TaskRouteRequest) -> TaskRouteDecision:
        """Return a non-authoritative decision without creating a Run or executing a tool."""

        return await self._decide(request, tracker=BudgetTracker(request.budget))

    async def prepare(
        self,
        request: TaskRouteRequest,
        *,
        history: Sequence[JsonValue] = (),
    ) -> PreparedTaskRoute:
        """Create the authoritative task root before routing and persist its decision events."""

        root = Run(
            id=request.root_run_id,
            conversation_id=request.conversation_id,
            agent_id=TASK_ROUTER_AGENT_ID,
        )
        async with self._database.transaction() as session:
            await RunRepository(session).create(root)
            await EventStore(session).append(
                run_id=root.id,
                event_type=EventType.RUN_STARTED,
                payload=RunStartedPayload(
                    conversation_id=root.conversation_id,
                    agent_id=root.agent_id,
                    budget=cast(dict[str, JsonValue], request.budget.model_dump(mode="json")),
                ),
            )
        tracker = BudgetTracker(request.budget)
        try:
            decision = await self._decide(
                request,
                tracker=tracker,
                events=RunEventRecorder(self._database, root.id),
            )
        except Exception:
            decision = self._rejected_decision(
                issue=TaskRouteIssue(
                    code=TaskRouteIssueCode.TARGET_UNAVAILABLE,
                    message="Task routing failed before an execution identity could be selected.",
                )
            )
        persisted = await self._persist_decision(root, request, decision)
        route_state = {
            TaskRouteStatus.READY: "confirmed",
            TaskRouteStatus.CONFIRMATION_REQUIRED: "waiting_confirmation",
            TaskRouteStatus.REJECTED: "rejected",
        }[decision.status]
        await self._checkpoint_route(
            persisted,
            request=request,
            decision=decision,
            route_state=route_state,
            budget=tracker.snapshot(),
            messages=list(history),
        )
        return PreparedTaskRoute(
            root=persisted,
            decision=decision,
            request=request,
            history=tuple(history),
        )

    async def confirm(
        self,
        root_run_id: UUID,
        *,
        decision_hash: str,
        authorized_lease: CapabilityLease | None = None,
    ) -> PreparedTaskRoute:
        """Confirm exactly one persisted proposal, optionally with an explicit new lease."""

        root, checkpoint, request, decision, route_state = await self._load_route(root_run_id)
        if root.status is not RunStatus.PAUSED or route_state != "waiting_confirmation":
            raise RunExecutionError("Task route is not waiting for confirmation")
        if decision.decision_hash != decision_hash:
            raise RunExecutionError("Task route decision hash does not match")
        updated_request = (
            request
            if authorized_lease is None
            else request.model_copy(update={"lease": authorized_lease})
        )
        if any(
            not updated_request.lease.allows(capability)
            for capability in decision.required_capabilities
        ):
            raise RunExecutionError("Confirmed lease does not grant all routed capabilities")
        workspace_issue = await self._workspace_issue(updated_request.workspace)
        validation_issues = validate_target(
            mode=decision.mode,
            target_id=decision.target_id,
            required_capabilities=decision.required_capabilities,
            model_ref=updated_request.model_ref,
            budget=updated_request.budget,
            agents=self._agents,
            teams=self._teams,
            providers=self._providers,
            policy=self._policy,
        )
        if workspace_issue is not None or validation_issues:
            raise RunExecutionError("Confirmed authority no longer validates for this route")
        async with self._database.transaction() as session:
            await EventStore(session).append(
                run_id=root.id,
                event_type=EventType.TASK_ROUTE_CONFIRMED,
                payload=self._event_payload(updated_request, decision),
            )
        await self._checkpoint_route(
            root,
            request=updated_request,
            decision=decision,
            route_state="confirmed",
            budget=checkpoint.budget,
            messages=checkpoint.messages,
        )
        return PreparedTaskRoute(
            root=root,
            decision=decision,
            request=updated_request,
            history=tuple(checkpoint.messages),
        )

    async def reject(self, root_run_id: UUID, *, decision_hash: str) -> Run:
        """Reject one pending route without executing an Agent or tool."""

        root, checkpoint, request, decision, route_state = await self._load_route(root_run_id)
        if root.status is not RunStatus.PAUSED or route_state != "waiting_confirmation":
            raise RunExecutionError("Task route is not waiting for confirmation")
        if decision.decision_hash != decision_hash:
            raise RunExecutionError("Task route decision hash does not match")
        failed = root.transition_to(RunStatus.FAILED)
        async with self._database.transaction() as session:
            await RunRepository(session).save(failed)
            events = EventStore(session)
            await events.append(
                run_id=root.id,
                event_type=EventType.TASK_ROUTE_REJECTED,
                payload=self._event_payload(request, decision),
            )
            await events.append(
                run_id=root.id,
                event_type=EventType.RUN_STATUS_CHANGED,
                payload=RunStatusChangedPayload(
                    previous_status=root.status,
                    status=RunStatus.FAILED,
                ),
            )
            await events.append(
                run_id=root.id,
                event_type=EventType.RUN_FAILED,
                payload=ErrorPayload(code="task_route_rejected", message="Task route rejected"),
            )
        await self._checkpoint_route(
            failed,
            request=request,
            decision=decision,
            route_state="rejected",
            budget=checkpoint.budget,
            messages=checkpoint.messages,
        )
        return failed

    async def execute(self, prepared: PreparedTaskRoute) -> TaskExecutionResult:
        """Execute the persisted route under its existing authoritative root."""

        root, checkpoint, request, decision, route_state = await self._load_route(prepared.root.id)
        if request.task_sha256 != prepared.request.task_sha256:
            raise RunExecutionError("Prepared task does not match the persisted route")
        if route_state != "confirmed":
            raise RunExecutionError("Task route must be confirmed before execution")
        return await self._execute_loaded(root, checkpoint, request, decision)

    async def resume(self, root_run_id: UUID) -> TaskExecutionResult:
        """Resume either a routed child or a routed Team from durable checkpoints."""

        root, route_checkpoint, request, decision, route_state = await self._load_route(root_run_id)
        async with self._database.session() as session:
            latest = await CheckpointRepository(session).latest(root_run_id)
        if root.status.is_terminal and route_state in {
            "completed",
            "failed",
            "cancelled",
            "rejected",
        }:
            return self._terminal_result(root, route_checkpoint, decision)
        if latest.kind is CheckpointKind.HANDOFF:
            handoffs = self._require_handoffs()
            outcome = await handoffs.resume(root_run_id)
            return await self._finish_team_route(request, decision, outcome)
        if route_state not in {"confirmed", "executing", "waiting_approval"}:
            raise RunExecutionError("Task route is not executable")
        return await self._execute_loaded(root, route_checkpoint, request, decision)

    async def decide_approval(
        self,
        root_run_id: UUID,
        approval_id: UUID,
        decision: ApprovalDecision,
    ) -> TaskExecutionResult:
        """Decide a child approval and reconcile the authoritative task root."""

        async with self._database.session() as session:
            latest = await CheckpointRepository(session).latest(root_run_id)
            approval = await ApprovalRepository(session).get_required(approval_id)
            delegations = await AgentDelegationRepository(session).list_for_parent(root_run_id)
        if approval.run_id not in {item.child_run_id for item in delegations}:
            raise RunExecutionError("Approval does not belong to this task root")
        if latest.kind is CheckpointKind.HANDOFF:
            outcome = await self._require_handoffs().decide_approval(
                root_run_id,
                approval_id,
                decision,
            )
            _, _, request, route_decision, _ = await self._load_route(root_run_id)
            return await self._finish_team_route(request, route_decision, outcome)
        runs = self._require_runs()
        await runs.resume(approval_id, decision)
        return await self.resume(root_run_id)

    async def _execute_loaded(
        self,
        root: Run,
        checkpoint: Checkpoint,
        request: TaskRouteRequest,
        decision: TaskRouteDecision,
    ) -> TaskExecutionResult:
        if root.status in {RunStatus.CREATED, RunStatus.PAUSED}:
            root = await self._transition(root, RunStatus.RUNNING)
        elif root.status is not RunStatus.RUNNING:
            raise RunExecutionError(f"Task root is not executable from {root.status.value}")
        try:
            if decision.mode is TaskRouteMode.TEAM:
                outcome = await self._require_handoffs().run_prepared(
                    root,
                    request=self._handoff_request(
                        request,
                        decision,
                        history=checkpoint.messages,
                    ),
                    initial_usage=checkpoint.budget.used,
                )
                return await self._finish_team_route(request, decision, outcome)
            return await self._execute_single(root, checkpoint, request, decision)
        except asyncio.CancelledError:
            current = await self._required_run(root.id)
            if not current.status.is_terminal:
                await self._require_runs().cancel(current.id)
            raise
        except Exception as error:
            current = await self._required_run(root.id)
            if current.status.is_terminal:
                raise
            usage = checkpoint.budget.used
            failed = await self._finish_root(
                current,
                status=RunStatus.FAILED,
                output=None,
                usage=usage,
                error_code=str(getattr(error, "code", "task_execution_failed")),
                error_message=str(error),
            )
            snapshot = checkpoint.budget
            await self._checkpoint_route(
                failed,
                request=request,
                decision=decision,
                route_state="failed",
                budget=snapshot,
                messages=checkpoint.messages,
            )
            return TaskExecutionResult(
                run=failed,
                decision=decision,
                budget=snapshot,
            )

    async def _execute_single(
        self,
        root: Run,
        checkpoint: Checkpoint,
        request: TaskRouteRequest,
        decision: TaskRouteDecision,
    ) -> TaskExecutionResult:
        runs = self._require_runs()
        tracker = BudgetTracker(
            checkpoint.budget.budget,
            initial_usage=checkpoint.budget.used,
        )
        call_id = f"task-route:0:{decision.target_id}"
        async with self._database.session() as session:
            delegation = await AgentDelegationRepository(session).find_by_parent_call(
                root.id,
                call_id,
            )
        if delegation is None:
            child_budget = self._execution_budget(
                request.budget,
                tracker.usage,
            ).model_copy(update={"max_children": 0, "max_depth": 0})
            tracker.reserve_child(depth=1, child_budget=child_budget)
            await self._budget_updated(root.id, tracker, source="task_child_reserved")
            child_request = RunRequest(
                prompt=request.task,
                workspace=request.workspace,
                agent_id=decision.target_id,
                conversation_id=root.conversation_id,
                user_id=request.user_id,
                lease=request.lease,
                permission_mode=request.permission_mode,
                budget=child_budget,
                parent_run_id=root.id,
                depth=1,
            )
            _, _, delegation = await runs.prepare_delegation(
                child_request,
                provider_call_id=call_id,
                kind=DelegationKind.TASK_ROUTE,
                task=request.task,
                model_ref=request.model_ref,
            )
            await self._checkpoint_route(
                root,
                request=request,
                decision=decision,
                route_state="executing",
                budget=tracker.snapshot(),
                pending_delegation_ids=(delegation.id,),
                child_run_ids=(delegation.child_run_id,),
                accounted_usage=Usage(),
                messages=checkpoint.messages,
            )
        result, projected_text = await self._recover_child_with_projection(
            root,
            delegation,
            workspace=request.workspace,
            user_id=request.user_id,
            conversation_id=root.conversation_id,
            root_budget=tracker.snapshot(),
            accounted_child_usage=delegation.accounted_usage,
            message_history=(
                ModelMessagesTypeAdapter.validate_python(checkpoint.messages)
                if checkpoint.messages
                else ()
            ),
        )
        delta = self._usage_delta(result.usage, delegation.accounted_usage)
        tracker.record_child_usage(delta)
        await self._save_delegation(delegation, result)
        await self._budget_updated(root.id, tracker, source="task_child_settled")
        if result.status is DelegationStatus.WAITING_APPROVAL:
            current = await self._required_run(root.id)
            if current.status is RunStatus.RUNNING:
                current = await self._transition(current, RunStatus.PAUSED)
            approvals = await self._approvals(result.approval_ids)
            snapshot = tracker.snapshot()
            await self._checkpoint_route(
                current,
                request=request,
                decision=decision,
                route_state="waiting_approval",
                budget=snapshot,
                pending_delegation_ids=(delegation.id,),
                child_run_ids=(delegation.child_run_id,),
                accounted_usage=result.usage,
                messages=checkpoint.messages,
            )
            return TaskExecutionResult(
                run=current,
                decision=decision,
                budget=snapshot,
                child_run_ids=(delegation.child_run_id,),
                approvals=approvals,
            )
        await self._child_completed(root.id, delegation, result)
        current = await self._required_run(root.id)
        status = {
            DelegationStatus.COMPLETED: RunStatus.COMPLETED,
            DelegationStatus.CANCELLED: RunStatus.CANCELLED,
        }.get(result.status, RunStatus.FAILED)
        current = await self._finish_root(
            current,
            status=status,
            output=result.output,
            emit_output=not projected_text,
            usage=tracker.usage,
            error_code=result.error_code,
            error_message=result.error_message,
        )
        route_state = {
            RunStatus.COMPLETED: "completed",
            RunStatus.CANCELLED: "cancelled",
            RunStatus.FAILED: "failed",
        }[status]
        snapshot = tracker.snapshot()
        await self._checkpoint_route(
            current,
            request=request,
            decision=decision,
            route_state=route_state,
            budget=snapshot,
            child_run_ids=(delegation.child_run_id,),
            accounted_usage=result.usage,
            output=result.output,
            messages=checkpoint.messages,
        )
        return TaskExecutionResult(
            run=current,
            decision=decision,
            output=result.output,
            budget=snapshot,
            child_run_ids=(delegation.child_run_id,),
        )

    def _handoff_request(
        self,
        request: TaskRouteRequest,
        decision: TaskRouteDecision,
        *,
        history: Sequence[JsonValue] = (),
    ) -> HandoffRequest:
        team = self._teams.get(decision.target_id)
        execution_budget = self._execution_budget(request.budget, decision.routing_usage)
        stage_models = {stage.stage: request.model_ref or stage.model_ref for stage in team.stages}
        return HandoffRequest(
            task=request.task,
            workspace=str(request.workspace),
            history=list(history),
            root_run_id=request.root_run_id,
            user_id=request.user_id,
            conversation_id=request.conversation_id,
            team_id=team.id,
            team_version=team.schema_version,
            permission_mode=request.permission_mode,
            lease=request.lease,
            budget=request.budget,
            stage_budgets=team.allocate_stage_budgets(execution_budget),
            stage_leases=team.allocate_stage_leases(request.lease),
            stage_agents={stage.stage: stage.agent_id for stage in team.stages},
            stage_model_refs=stage_models,
            stage_instructions={stage.stage: stage.instructions for stage in team.stages},
            stage_output_contracts={stage.stage: stage.output_contract for stage in team.stages},
            review_gate=team.review_gate,
        )

    async def _finish_team_route(
        self,
        request: TaskRouteRequest,
        decision: TaskRouteDecision,
        outcome: HandoffResult,
    ) -> TaskExecutionResult:
        root = await self._required_run(outcome.root_run_id)
        child_ids = tuple(stage.child_run_id for stage in outcome.stages)
        approval_ids = tuple(
            approval_id for stage in outcome.stages for approval_id in stage.approval_ids
        )
        approvals = await self._approvals(approval_ids)
        if not root.status.is_terminal:
            return TaskExecutionResult(
                run=root,
                decision=decision,
                output=outcome.output,
                budget=outcome.budget,
                child_run_ids=child_ids,
                approvals=approvals,
            )
        route_state = {
            RunStatus.COMPLETED: "completed",
            RunStatus.CANCELLED: "cancelled",
            RunStatus.FAILED: "failed",
        }[root.status]
        async with self._database.session() as session:
            route_checkpoint = await CheckpointRepository(session).latest_by_kind(
                root.id,
                CheckpointKind.TASK_ROUTE,
            )
        await self._checkpoint_route(
            root,
            request=request,
            decision=decision,
            route_state=route_state,
            budget=outcome.budget,
            child_run_ids=child_ids,
            accounted_usage=outcome.budget.used,
            output=outcome.output,
            messages=route_checkpoint.messages,
        )
        return TaskExecutionResult(
            run=root,
            decision=decision,
            output=outcome.output,
            budget=outcome.budget,
            child_run_ids=child_ids,
            approvals=approvals,
        )

    async def _load_route(
        self,
        root_run_id: UUID,
    ) -> tuple[Run, Checkpoint, TaskRouteRequest, TaskRouteDecision, str]:
        async with self._database.session() as session:
            root = await RunRepository(session).get(root_run_id)
            checkpoint = await CheckpointRepository(session).latest_by_kind(
                root_run_id,
                CheckpointKind.TASK_ROUTE,
            )
        if root is None:
            raise RunNotFoundError(str(root_run_id))
        state = checkpoint.workflow_state
        if state is None:
            raise RunExecutionError("Task route checkpoint has no workflow state")
        request = TaskRouteRequest.model_validate(state["request"])
        decision = TaskRouteDecision.model_validate(state["decision"])
        if request.root_run_id != root.id or request.conversation_id != root.conversation_id:
            raise RunExecutionError("Task route checkpoint identity does not match its root")
        return root, checkpoint, request, decision, str(state["route_state"])

    async def _checkpoint_route(
        self,
        root: Run,
        *,
        request: TaskRouteRequest,
        decision: TaskRouteDecision,
        route_state: str,
        budget: BudgetSnapshot,
        pending_delegation_ids: tuple[UUID, ...] = (),
        child_run_ids: tuple[UUID, ...] = (),
        accounted_usage: Usage | None = None,
        output: str | None = None,
        messages: list[JsonValue] | None = None,
    ) -> Checkpoint:
        state = cast(
            dict[str, JsonValue],
            {
                "schema_version": 1,
                "route_state": route_state,
                "request": request.model_dump(mode="json"),
                "decision": decision.model_dump(
                    mode="json",
                    exclude_computed_fields=True,
                ),
                "accounted_usage": (accounted_usage or Usage()).model_dump(
                    mode="json",
                    exclude_computed_fields=True,
                ),
                "output": output,
            },
        )
        async with self._database.transaction() as session:
            events = EventStore(session)
            event = await events.append(
                run_id=root.id,
                event_type=EventType.CHECKPOINT_CREATED,
                payload=GenericEventPayload(data={"reason": "task_route", "state": route_state}),
            )
            checkpoint = Checkpoint(
                run_id=root.id,
                user_id=request.user_id,
                event_sequence=event.sequence,
                kind=CheckpointKind.TASK_ROUTE,
                agent_id=TASK_ROUTER_AGENT_ID,
                prompt=request.task,
                workspace=str(request.workspace),
                lease=request.lease,
                permission_mode=request.permission_mode,
                budget=budget,
                messages=messages or [],
                pending_delegation_ids=pending_delegation_ids,
                child_run_ids=child_run_ids,
                workflow_state=state,
                run_status=root.status,
            )
            await CheckpointRepository(session).create(checkpoint)
        return checkpoint

    @staticmethod
    def _terminal_result(
        root: Run,
        checkpoint: Checkpoint,
        decision: TaskRouteDecision,
    ) -> TaskExecutionResult:
        state = checkpoint.workflow_state or {}
        output = state.get("output")
        return TaskExecutionResult(
            run=root,
            decision=decision,
            output=output if isinstance(output, str) else None,
            budget=checkpoint.budget,
            child_run_ids=checkpoint.child_run_ids,
        )

    async def _transition(self, root: Run, status: RunStatus) -> Run:
        transitioned = root.transition_to(status)
        async with self._database.transaction() as session:
            await RunRepository(session).save(transitioned)
            await EventStore(session).append(
                run_id=root.id,
                event_type=EventType.RUN_STATUS_CHANGED,
                payload=RunStatusChangedPayload(previous_status=root.status, status=status),
            )
        return transitioned

    async def _finish_root(
        self,
        root: Run,
        *,
        status: RunStatus,
        output: str | None,
        emit_output: bool = True,
        usage: Usage,
        error_code: str | None,
        error_message: str | None,
    ) -> Run:
        transitioned = root.transition_to(status)
        async with self._database.transaction() as session:
            await RunRepository(session).save(transitioned)
            events = EventStore(session)
            if output and emit_output:
                await events.append(
                    run_id=root.id,
                    event_type=EventType.MODEL_TEXT_DELTA,
                    payload=TextDeltaPayload(delta=output),
                )
            await events.append(
                run_id=root.id,
                event_type=EventType.RUN_STATUS_CHANGED,
                payload=RunStatusChangedPayload(previous_status=root.status, status=status),
            )
            if status is RunStatus.COMPLETED:
                await events.append(
                    run_id=root.id,
                    event_type=EventType.RUN_COMPLETED,
                    payload=RunCompletedPayload(
                        output_characters=len(output or ""),
                        requests=usage.requests,
                        input_tokens=usage.input_tokens,
                        cache_write_tokens=usage.cache_write_tokens,
                        cache_read_tokens=usage.cache_read_tokens,
                        output_tokens=usage.output_tokens,
                        tool_calls=usage.tool_calls,
                    ),
                )
            elif status is RunStatus.CANCELLED:
                await events.append(
                    run_id=root.id,
                    event_type=EventType.RUN_CANCELLED,
                    payload=GenericEventPayload(data={"reason": "task_child_cancelled"}),
                )
            else:
                await events.append(
                    run_id=root.id,
                    event_type=EventType.RUN_FAILED,
                    payload=ErrorPayload(
                        code=error_code or "task_child_failed",
                        message=error_message or "Routed child failed",
                    ),
                )
        return transitioned

    async def _recover_child_with_projection(
        self,
        root: Run,
        delegation: AgentDelegation,
        *,
        workspace: Path,
        user_id: UUID,
        conversation_id: UUID,
        message_history: Sequence[ModelMessage],
        root_budget: BudgetSnapshot,
        accounted_child_usage: Usage,
    ) -> tuple[DelegationResult, bool]:
        """Recover a routed child while projecting its public output onto the root."""

        return await ChildEventProjector(
            self._database,
            self._require_runs(),
        ).recover(
            root,
            delegation,
            workspace=workspace,
            user_id=user_id,
            conversation_id=conversation_id,
            message_history=message_history,
            root_budget=root_budget,
            accounted_child_usage=accounted_child_usage,
        )

    async def _budget_updated(
        self,
        root_run_id: UUID,
        tracker: BudgetTracker,
        *,
        source: str,
    ) -> None:
        async with self._database.transaction() as session:
            await EventStore(session).append(
                run_id=root_run_id,
                event_type=EventType.BUDGET_UPDATED,
                payload=GenericEventPayload(
                    data={
                        "source": source,
                        "snapshot": cast(
                            dict[str, JsonValue],
                            tracker.snapshot().model_dump(mode="json"),
                        ),
                    }
                ),
            )

    async def _save_delegation(
        self,
        delegation: AgentDelegation,
        result: DelegationResult,
    ) -> None:
        updated = delegation.model_copy(
            update={
                "status": result.status,
                "accounted_usage": result.usage,
                "result": result,
                "updated_at": datetime.now(UTC),
            }
        )
        async with self._database.transaction() as session:
            await AgentDelegationRepository(session).save(updated)

    async def _child_completed(
        self,
        root_run_id: UUID,
        delegation: AgentDelegation,
        result: DelegationResult,
    ) -> None:
        async with self._database.transaction() as session:
            await EventStore(session).append(
                run_id=root_run_id,
                event_type=EventType.AGENT_CHILD_COMPLETED,
                payload=AgentChildCompletedPayload(
                    delegation_id=delegation.id,
                    parent_run_id=root_run_id,
                    child_run_id=result.child_run_id,
                    agent_id=result.agent_id,
                    status=result.status.value,
                    trace_id=delegation.trace_id,
                    usage=cast(dict[str, JsonValue], result.usage.model_dump(mode="json")),
                ),
            )

    async def _approvals(self, ids: tuple[UUID, ...]) -> tuple[Approval, ...]:
        if not ids:
            return ()
        async with self._database.session() as session:
            repository = ApprovalRepository(session)
            return tuple([await repository.get_required(item) for item in ids])

    async def _required_run(self, run_id: UUID) -> Run:
        async with self._database.session() as session:
            run = await RunRepository(session).get(run_id)
        if run is None:
            raise RunNotFoundError(str(run_id))
        return run

    def _require_runs(self) -> RunService:
        if self._runs is None:
            raise RunExecutionError("Task routing execution is not configured")
        return self._runs

    def _require_handoffs(self) -> HandoffService:
        if self._handoffs is None:
            raise RunExecutionError("Task Team execution is not configured")
        return self._handoffs

    @staticmethod
    def _execution_budget(budget: Budget, usage: Usage) -> Budget:
        """Return the child-allocation envelope after root-owned routing usage."""

        return Budget(
            max_requests=max(0, budget.max_requests - usage.requests),
            max_input_tokens=max(0, budget.max_input_tokens - usage.input_tokens),
            max_output_tokens=max(0, budget.max_output_tokens - usage.output_tokens),
            max_total_tokens=max(0, budget.max_total_tokens - usage.total_tokens),
            max_tool_calls=max(0, budget.max_tool_calls - usage.tool_calls),
            max_duration_seconds=max(
                0,
                int(budget.max_duration_seconds - usage.duration_seconds),
            ),
            max_cost=(
                None if budget.max_cost is None else max(Decimal("0"), budget.max_cost - usage.cost)
            ),
            max_children=budget.max_children,
            max_depth=budget.max_depth,
        )

    @staticmethod
    def _usage_delta(current: Usage, accounted: Usage) -> Usage:
        return Usage(
            requests=max(0, current.requests - accounted.requests),
            input_tokens=max(0, current.input_tokens - accounted.input_tokens),
            cache_write_tokens=max(0, current.cache_write_tokens - accounted.cache_write_tokens),
            cache_read_tokens=max(0, current.cache_read_tokens - accounted.cache_read_tokens),
            output_tokens=max(0, current.output_tokens - accounted.output_tokens),
            tool_calls=max(0, current.tool_calls - accounted.tool_calls),
            cost=max(Decimal("0"), current.cost - accounted.cost),
            children=max(0, current.children - accounted.children),
            max_depth_reached=max(0, current.max_depth_reached - accounted.max_depth_reached),
        )

    async def _decide(
        self,
        request: TaskRouteRequest,
        *,
        tracker: BudgetTracker,
        events: ToolEventSink | None = None,
    ) -> TaskRouteDecision:
        workspace_issue = await self._workspace_issue(request.workspace)
        model_fallback_failed = False
        if request.agent_id is not None:
            mode = TaskRouteMode.SINGLE_AGENT
            target_id = request.agent_id
            source = TaskRouteSource.OVERRIDE
            confidence = 1.0
            reason = "使用调用方显式指定的 Agent；该覆盖仍需通过所有安全校验。"
            profile = self._profile_for_agent(target_id)
            required = self._required_for_override(mode, target_id, request.task)
            ambiguous = False
        elif request.team_id is not None:
            mode = TaskRouteMode.TEAM
            target_id = request.team_id
            source = TaskRouteSource.OVERRIDE
            confidence = 1.0
            reason = "使用调用方显式指定的 Team；该覆盖仍需通过所有安全校验。"
            profile = RouteBudgetProfile.DELIVERY
            required = self._required_for_override(mode, target_id, request.task)
            ambiguous = False
        else:
            match = deterministic_route(request.task)
            source = (
                TaskRouteSource.SAFE_FALLBACK if match.ambiguous else TaskRouteSource.DETERMINISTIC
            )
            if (
                match.ambiguous
                and self._policy.model_fallback_enabled
                and self._routing_model_ref is not None
            ):
                try:
                    match = await self._model_route(
                        request,
                        tracker=tracker,
                        events=events,
                    )
                    source = TaskRouteSource.MODEL
                except Exception:
                    model_fallback_failed = True
            mode = match.mode
            target_id = match.target_id
            confidence = match.confidence
            reason = match.reason_summary
            profile = match.budget_profile
            required = match.required_capabilities
            ambiguous = match.ambiguous

        issues = list(
            validate_target(
                mode=mode,
                target_id=target_id,
                required_capabilities=required,
                model_ref=request.model_ref,
                budget=self._execution_budget(request.budget, tracker.usage),
                agents=self._agents,
                teams=self._teams,
                providers=self._providers,
                policy=self._policy,
            )
        )
        if workspace_issue is not None:
            issues.insert(0, workspace_issue)
        if model_fallback_failed:
            issues.append(
                TaskRouteIssue(
                    code=TaskRouteIssueCode.MODEL_FALLBACK_FAILED,
                    message=(
                        "Routing model fallback failed; confirmation-only Coordinator "
                        "fallback was retained."
                    ),
                )
            )
        gaps = capability_gaps(required, request.lease)
        issues.extend(
            TaskRouteIssue(
                code=TaskRouteIssueCode.CAPABILITY_MISSING,
                message=f"Current CapabilityLease does not grant {capability.value}.",
                capability=capability,
            )
            for capability in sorted(gaps, key=lambda item: item.value)
        )
        non_hard_codes = {
            TaskRouteIssueCode.CAPABILITY_MISSING,
            TaskRouteIssueCode.MODEL_FALLBACK_FAILED,
        }
        hard_issues = tuple(issue for issue in issues if issue.code not in non_hard_codes)
        if hard_issues:
            status = TaskRouteStatus.REJECTED
        elif ambiguous or confidence < self._policy.automatic_confidence_threshold or gaps:
            status = TaskRouteStatus.CONFIRMATION_REQUIRED
            if ambiguous:
                issues.append(
                    TaskRouteIssue(
                        code=TaskRouteIssueCode.CLASSIFICATION_AMBIGUOUS,
                        message="Deterministic rules could not classify the task confidently.",
                    )
                )
        else:
            status = TaskRouteStatus.READY
        values: dict[str, object] = {
            "policy_version": self._policy.policy_version,
            "mode": mode,
            "source": source,
            "status": status,
            "confidence": confidence,
            "reason_summary": reason,
            "required_capabilities": required,
            "capability_gaps": gaps,
            "budget_profile": profile,
            "requires_confirmation": status is TaskRouteStatus.CONFIRMATION_REQUIRED,
            "issues": tuple(issues),
            "routing_usage": tracker.usage,
        }
        values["agent_id" if mode is TaskRouteMode.SINGLE_AGENT else "team_id"] = target_id
        return TaskRouteDecision.model_validate(values)

    async def _model_route(
        self,
        request: TaskRouteRequest,
        *,
        tracker: BudgetTracker,
        events: ToolEventSink | None,
    ) -> DeterministicRouteMatch:
        model_ref = cast(str, self._routing_model_ref)
        model_spec = self._providers.model_spec(model_ref)
        base_model = await self._providers.build_model(model_ref)
        instrumented = RunModel(
            base_model,
            provider=model_spec.provider.value,
            model_name=model_spec.model,
            budget=tracker,
            events=events or _NullEventSink(),
        )
        agent = Agent(
            instrumented,
            output_type=PromptedOutput(ModelRouteClassification),
            instructions=(
                "Classify only the supplied task. Select coordinator for read-only analysis, "
                "coder for workspace changes, reviewer for independent read-only review, "
                "researcher for approved external evidence, or software for a task explicitly "
                "requiring change, tests, and independent review. Return a short public reason; "
                "never include hidden reasoning and never request tools."
            ),
            name="task-router",
        )
        limits = UsageLimits(
            request_limit=1,
            tool_calls_limit=0,
            input_tokens_limit=request.budget.max_input_tokens,
            output_tokens_limit=min(
                request.budget.max_output_tokens,
                self._policy.model_max_output_tokens,
            ),
            total_tokens_limit=request.budget.max_total_tokens,
        )
        async with asyncio.timeout(
            min(request.budget.max_duration_seconds, self._policy.model_timeout_seconds)
        ):
            result = await agent.run(request.task, usage_limits=limits, retries=0)
        classification = result.output
        mode = (
            TaskRouteMode.TEAM
            if classification.target_id == "software"
            else TaskRouteMode.SINGLE_AGENT
        )
        required = self._required_for_override(mode, classification.target_id, request.task)
        return DeterministicRouteMatch(
            mode=mode,
            target_id=classification.target_id,
            confidence=classification.confidence,
            reason_summary=classification.reason_summary,
            required_capabilities=required,
            budget_profile=(
                RouteBudgetProfile.DELIVERY
                if mode is TaskRouteMode.TEAM
                else self._profile_for_agent(classification.target_id)
            ),
            ambiguous=classification.requires_clarification,
        )

    async def _persist_decision(
        self,
        root: Run,
        request: TaskRouteRequest,
        decision: TaskRouteDecision,
    ) -> Run:
        payload = self._event_payload(request, decision)
        async with self._database.transaction() as session:
            runs = RunRepository(session)
            events = EventStore(session)
            current = root
            if decision.status is not TaskRouteStatus.READY:
                running = current.transition_to(RunStatus.RUNNING)
                await runs.save(running)
                await events.append(
                    run_id=root.id,
                    event_type=EventType.RUN_STATUS_CHANGED,
                    payload=RunStatusChangedPayload(
                        previous_status=RunStatus.CREATED,
                        status=RunStatus.RUNNING,
                    ),
                )
                current = running
            await events.append(
                run_id=root.id,
                event_type=EventType.TASK_ROUTE_PROPOSED,
                payload=payload,
            )
            if decision.source is TaskRouteSource.OVERRIDE:
                await events.append(
                    run_id=root.id,
                    event_type=EventType.TASK_ROUTE_OVERRIDDEN,
                    payload=payload,
                )
            if decision.status is TaskRouteStatus.READY:
                await events.append(
                    run_id=root.id,
                    event_type=EventType.TASK_ROUTE_CONFIRMED,
                    payload=payload,
                )
                return current
            if decision.status is TaskRouteStatus.CONFIRMATION_REQUIRED:
                paused = current.transition_to(RunStatus.PAUSED)
                await runs.save(paused)
                await events.append(
                    run_id=root.id,
                    event_type=EventType.RUN_STATUS_CHANGED,
                    payload=RunStatusChangedPayload(
                        previous_status=RunStatus.RUNNING,
                        status=RunStatus.PAUSED,
                    ),
                )
                return paused
            await events.append(
                run_id=root.id,
                event_type=EventType.TASK_ROUTE_REJECTED,
                payload=payload,
            )
            failed = current.transition_to(RunStatus.FAILED)
            await runs.save(failed)
            await events.append(
                run_id=root.id,
                event_type=EventType.RUN_STATUS_CHANGED,
                payload=RunStatusChangedPayload(
                    previous_status=RunStatus.RUNNING,
                    status=RunStatus.FAILED,
                ),
            )
            first_issue = decision.issues[0] if decision.issues else None
            await events.append(
                run_id=root.id,
                event_type=EventType.RUN_FAILED,
                payload=ErrorPayload(
                    code=("task_route_rejected" if first_issue is None else first_issue.code.value),
                    message=(
                        "Task route was rejected" if first_issue is None else first_issue.message
                    ),
                ),
            )
            return failed

    def _event_payload(
        self,
        request: TaskRouteRequest,
        decision: TaskRouteDecision,
    ) -> TaskRouteEventPayload:
        return TaskRouteEventPayload(
            policy_version=decision.policy_version,
            task_sha256=request.task_sha256,
            decision_hash=cast(str, decision.decision_hash),
            mode=decision.mode.value,
            agent_id=decision.agent_id,
            team_id=decision.team_id,
            source=decision.source.value,
            status=decision.status.value,
            confidence=decision.confidence,
            reason_summary=decision.reason_summary,
            required_capabilities=tuple(
                sorted(item.value for item in decision.required_capabilities)
            ),
            capability_gaps=tuple(sorted(item.value for item in decision.capability_gaps)),
            budget_profile=decision.budget_profile.value,
            requires_confirmation=decision.requires_confirmation,
            issues=tuple(
                cast(dict[str, JsonValue], item.model_dump(mode="json")) for item in decision.issues
            ),
            routing_usage=cast(
                dict[str, JsonValue],
                decision.routing_usage.model_dump(mode="json"),
            ),
        )

    async def _workspace_issue(self, workspace: Path) -> TaskRouteIssue | None:
        try:
            resolved = await asyncio.to_thread(workspace.resolve, strict=True)
            is_directory = await asyncio.to_thread(resolved.is_dir)
        except (OSError, RuntimeError):
            is_directory = False
        if is_directory:
            return None
        return TaskRouteIssue(
            code=TaskRouteIssueCode.WORKSPACE_INVALID,
            message="Workspace must resolve to an existing directory.",
        )

    def _required_for_override(
        self,
        mode: TaskRouteMode,
        target_id: str,
        task: str,
    ) -> frozenset[Capability]:
        if mode is TaskRouteMode.TEAM:
            return frozenset(
                {
                    Capability.FILESYSTEM_READ,
                    Capability.FILESYSTEM_WRITE,
                    Capability.SHELL_EXECUTE,
                }
            )
        signals = intent_signals(task)
        required: set[Capability] = {Capability.FILESYSTEM_READ}
        if target_id == "coder" and signals.change:
            required.add(Capability.FILESYSTEM_WRITE)
            if signals.test:
                required.add(Capability.SHELL_EXECUTE)
        if target_id == "researcher" and signals.research:
            required.add(Capability.NETWORK_REQUEST)
        return frozenset(required)

    @staticmethod
    def _profile_for_agent(agent_id: str) -> RouteBudgetProfile:
        return {
            "coder": RouteBudgetProfile.CHANGE,
            "reviewer": RouteBudgetProfile.REVIEW,
            "researcher": RouteBudgetProfile.RESEARCH,
        }.get(agent_id, RouteBudgetProfile.READ_ONLY)

    def _rejected_decision(self, *, issue: TaskRouteIssue) -> TaskRouteDecision:
        return TaskRouteDecision(
            policy_version=self._policy.policy_version,
            mode=TaskRouteMode.SINGLE_AGENT,
            agent_id="coordinator",
            source=TaskRouteSource.SAFE_FALLBACK,
            status=TaskRouteStatus.REJECTED,
            confidence=0,
            reason_summary="路由服务无法安全选择执行身份。",
            required_capabilities=frozenset({Capability.FILESYSTEM_READ}),
            capability_gaps=frozenset(),
            budget_profile=RouteBudgetProfile.READ_ONLY,
            issues=(issue,),
        )
