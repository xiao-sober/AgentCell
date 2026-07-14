"""Deterministic, checkpointed Coordinator-to-Finalizer handoff workflow."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

from opentelemetry import trace

from agentcell.agents import (
    AgentDelegation,
    DelegationKind,
    DelegationResult,
    DelegationStatus,
    HandoffRequest,
    HandoffResult,
    HandoffStage,
)
from agentcell.budgets import Budget, BudgetTracker, Usage
from agentcell.events import (
    AgentChildCompletedPayload,
    ErrorPayload,
    EventType,
    GenericEventPayload,
    JsonValue,
    RunCompletedPayload,
    RunStartedPayload,
    RunStatusChangedPayload,
)
from agentcell.kernel.checkpoint import Checkpoint, CheckpointKind
from agentcell.kernel.lifecycle import RunStatus
from agentcell.kernel.models import Run
from agentcell.kernel.run_service import RunRequest, RunService
from agentcell.policy import CapabilityLease
from agentcell.storage import (
    AgentDelegationRepository,
    CheckpointRepository,
    Database,
    EventStore,
    RunRepository,
)

_STAGES = (
    HandoffStage.COORDINATOR,
    HandoffStage.CODER,
    HandoffStage.REVIEWER,
    HandoffStage.FINALIZER,
)
_TRACER = trace.get_tracer("agentcell.kernel.handoff")


class HandoffService:
    """Run a fixed four-stage workflow under one root budget and authority lease."""

    def __init__(self, database: Database, runs: RunService) -> None:
        self._database = database
        self._runs = runs

    async def run(self, request: HandoffRequest) -> HandoffResult:
        self._validate_request(request)
        root = Run(
            conversation_id=request.conversation_id,
            agent_id=HandoffStage.COORDINATOR.value,
        )
        async with self._database.transaction() as session:
            await RunRepository(session).create(root)
            store = EventStore(session)
            await store.append(
                run_id=root.id,
                event_type=EventType.RUN_STARTED,
                payload=RunStartedPayload(
                    conversation_id=root.conversation_id,
                    agent_id=root.agent_id,
                ),
            )
        root = await self._transition(root, RunStatus.RUNNING)
        tracker = BudgetTracker(request.budget)
        return await self._continue(
            root,
            request=request,
            tracker=tracker,
            start_index=0,
            prompt=request.task,
            completed=(),
        )

    async def resume(self, root_run_id: UUID) -> HandoffResult:
        async with self._database.session() as session:
            root = await RunRepository(session).get(root_run_id)
            checkpoint = await CheckpointRepository(session).latest(root_run_id)
            delegations = await AgentDelegationRepository(session).list_for_parent(root_run_id)
        if root is None:
            raise ValueError(f"Unknown handoff Run {root_run_id}")
        if checkpoint.kind is not CheckpointKind.HANDOFF or checkpoint.workflow_state is None:
            raise ValueError("Run does not have a handoff checkpoint")
        state = checkpoint.workflow_state
        index = int(cast(int, state["stage_index"]))
        prompt = str(state["prompt"])
        current_call_id = f"handoff:{index}:{_STAGES[index].value}"
        completed = tuple(
            delegation.result
            for delegation in delegations
            if (
                delegation.provider_call_id != current_call_id
                and delegation.result is not None
                and delegation.result.status.is_terminal
            )
        )
        pending = next(
            (item for item in delegations if not item.status.is_terminal),
            None,
        )
        if pending is not None:
            pending_result = await self._runs.recover_delegation_child(
                pending,
                workspace=Path(checkpoint.workspace),
                user_id=checkpoint.user_id,
                conversation_id=root.conversation_id,
            )
            if not pending_result.status.is_terminal:
                return HandoffResult(
                    root_run_id=root.id,
                    status=DelegationStatus.WAITING_APPROVAL,
                    stages=completed,
                )
            delegations = [
                (
                    item.model_copy(
                        update={"status": pending_result.status, "result": pending_result}
                    )
                    if item.id == pending.id
                    else item
                )
                for item in delegations
            ]
        tracker = BudgetTracker(
            checkpoint.budget.budget,
            initial_usage=checkpoint.budget.used,
        )
        current = next(
            (item for item in delegations if item.provider_call_id == current_call_id),
            None,
        )
        if current is not None and current.result is not None and current.status.is_terminal:
            checkpoint_usage = Usage.model_validate(state.get("accounted_usage", {}))
            tracker.record_child_usage(self._usage_delta(current.result.usage, checkpoint_usage))
            await self._budget_updated(root.id, tracker, source="handoff_child_resumed")
            await self._child_completed(root.id, current.result)
            if current.result.status is not DelegationStatus.COMPLETED:
                await self._finish_noncompleted(root, current.result)
                return HandoffResult(
                    root_run_id=root.id,
                    status=current.result.status,
                    stages=tuple([*completed, current.result]),
                )
            completed = (*completed, current.result)
            prompt = self._next_prompt(
                str(state["task"]),
                _STAGES[index],
                current.result.output or "",
            )
            index += 1
        if root.status is RunStatus.PAUSED:
            root = await self._transition(root, RunStatus.RUNNING)
        request = HandoffRequest(
            task=str(state["task"]),
            workspace=checkpoint.workspace,
            user_id=checkpoint.user_id,
            conversation_id=root.conversation_id,
            lease=checkpoint.lease,
            budget=checkpoint.budget.budget,
            stage_budgets={
                HandoffStage(key): Budget.model_validate(value)
                for key, value in cast(dict[str, object], state["stage_budgets"]).items()
            },
            stage_leases={
                HandoffStage(key): CapabilityLease.model_validate(value)
                for key, value in cast(dict[str, object], state["stage_leases"]).items()
            },
        )
        return await self._continue(
            root,
            request=request,
            tracker=tracker,
            start_index=index,
            prompt=prompt,
            completed=completed,
        )

    async def _continue(
        self,
        root: Run,
        *,
        request: HandoffRequest,
        tracker: BudgetTracker,
        start_index: int,
        prompt: str,
        completed: tuple[DelegationResult, ...],
    ) -> HandoffResult:
        results = list(completed)
        workspace = await asyncio.to_thread(Path(request.workspace).resolve, strict=True)
        for index in range(start_index, len(_STAGES)):
            stage = _STAGES[index]
            child_budget = request.stage_budgets[stage]
            child_lease = request.stage_leases[stage]
            request.lease.ensure_child_subset(child_lease)
            tracker.reserve_child(depth=1, child_budget=child_budget)
            await self._budget_updated(root.id, tracker, source="handoff_child_reserved")
            child_request = RunRequest(
                prompt=prompt,
                workspace=workspace,
                agent_id=stage.value,
                conversation_id=root.conversation_id,
                user_id=request.user_id,
                lease=child_lease,
                budget=child_budget,
                parent_run_id=root.id,
                depth=1,
            )
            child, _, delegation = await self._runs.prepare_delegation(
                child_request,
                provider_call_id=f"handoff:{index}:{stage.value}",
                kind=DelegationKind.HANDOFF,
                task=prompt,
            )
            await self._checkpoint(
                root,
                request=request,
                tracker=tracker,
                stage_index=index,
                prompt=prompt,
                delegation=delegation,
                accounted_usage=Usage(),
            )
            try:
                with _TRACER.start_as_current_span(
                    "agentcell.agent.handoff",
                    attributes={
                        "agentcell.delegation.id": str(delegation.id),
                        "agentcell.parent_run.id": str(root.id),
                        "agentcell.child_run.id": str(child.id),
                        "agentcell.agent.id": stage.value,
                        "agentcell.handoff.stage": stage.value,
                    },
                ):
                    result = await self._runs.recover_delegation_child(
                        delegation,
                        workspace=workspace,
                        user_id=request.user_id,
                        conversation_id=root.conversation_id,
                    )
            except asyncio.CancelledError:
                await asyncio.shield(self._cancel(root))
                raise
            except Exception as error:
                failed = DelegationResult(
                    delegation_id=delegation.id,
                    child_run_id=child.id,
                    agent_id=stage.value,
                    status=DelegationStatus.FAILED,
                    error_code=getattr(error, "code", "handoff_stage_failed"),
                    error_message=str(error),
                )
                await self._save_delegation(delegation, failed)
                await self._finish_failed(root, failed)
                return HandoffResult(
                    root_run_id=root.id,
                    status=DelegationStatus.FAILED,
                    stages=tuple([*results, failed]),
                )
            tracker.record_child_usage(result.usage)
            await self._budget_updated(root.id, tracker, source="handoff_child_settled")
            await self._save_delegation(delegation, result)
            if result.status is DelegationStatus.WAITING_APPROVAL:
                root = await self._transition(root, RunStatus.PAUSED)
                await self._checkpoint(
                    root,
                    request=request,
                    tracker=tracker,
                    stage_index=index,
                    prompt=prompt,
                    delegation=delegation,
                    accounted_usage=result.usage,
                )
                return HandoffResult(
                    root_run_id=root.id,
                    status=result.status,
                    stages=tuple(results),
                )
            results.append(result)
            await self._child_completed(root.id, result)
            if result.status is not DelegationStatus.COMPLETED:
                await self._finish_noncompleted(root, result)
                return HandoffResult(
                    root_run_id=root.id,
                    status=result.status,
                    stages=tuple(results),
                )
            prompt = self._next_prompt(request.task, stage, result.output or "")

        output = results[-1].output if results else None
        usage = tracker.usage
        await self._finish(
            root,
            RunStatus.COMPLETED,
            EventType.RUN_COMPLETED,
            RunCompletedPayload(
                output_characters=len(output or ""),
                requests=usage.requests,
                input_tokens=usage.input_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                output_tokens=usage.output_tokens,
                tool_calls=usage.tool_calls,
            ),
        )
        return HandoffResult(
            root_run_id=root.id,
            status=DelegationStatus.COMPLETED,
            stages=tuple(results),
            output=output,
        )

    @staticmethod
    def _validate_request(request: HandoffRequest) -> None:
        if set(request.stage_budgets) != set(_STAGES):
            raise ValueError("stage_budgets must define all handoff stages")
        if set(request.stage_leases) != set(_STAGES):
            raise ValueError("stage_leases must define all handoff stages")
        if request.budget.max_children < len(_STAGES) or request.budget.max_depth < 1:
            raise ValueError("handoff root budget must allow four direct children")

    async def _checkpoint(
        self,
        root: Run,
        *,
        request: HandoffRequest,
        tracker: BudgetTracker,
        stage_index: int,
        prompt: str,
        delegation: AgentDelegation,
        accounted_usage: Usage,
    ) -> None:
        state: dict[str, JsonValue] = {
            "task": request.task,
            "stage_index": stage_index,
            "prompt": prompt,
            "accounted_usage": cast(
                dict[str, JsonValue],
                accounted_usage.model_dump(mode="json", exclude_computed_fields=True),
            ),
            "stage_budgets": cast(
                dict[str, JsonValue],
                {
                    key.value: value.model_dump(mode="json")
                    for key, value in request.stage_budgets.items()
                },
            ),
            "stage_leases": cast(
                dict[str, JsonValue],
                {
                    key.value: value.model_dump(mode="json")
                    for key, value in request.stage_leases.items()
                },
            ),
        }
        async with self._database.transaction() as session:
            store = EventStore(session)
            event = await store.append(
                run_id=root.id,
                event_type=EventType.CHECKPOINT_CREATED,
                payload=GenericEventPayload(
                    data={"reason": "handoff", "stage": _STAGES[stage_index].value}
                ),
            )
            await CheckpointRepository(session).create(
                Checkpoint(
                    run_id=root.id,
                    user_id=request.user_id,
                    event_sequence=event.sequence,
                    kind=CheckpointKind.HANDOFF,
                    agent_id=root.agent_id,
                    prompt=request.task,
                    workspace=request.workspace,
                    lease=request.lease,
                    budget=tracker.snapshot(),
                    messages=[],
                    pending_delegation_ids=(delegation.id,),
                    child_run_ids=(delegation.child_run_id,),
                    workflow_state=state,
                    run_status=root.status,
                    depth=0,
                )
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

    async def _child_completed(self, root_run_id: UUID, result: DelegationResult) -> None:
        async with self._database.transaction() as session:
            await EventStore(session).append(
                run_id=root_run_id,
                event_type=EventType.AGENT_CHILD_COMPLETED,
                payload=AgentChildCompletedPayload(
                    delegation_id=result.delegation_id,
                    parent_run_id=root_run_id,
                    child_run_id=result.child_run_id,
                    agent_id=result.agent_id,
                    status=result.status.value,
                    trace_id=(
                        await AgentDelegationRepository(session).get_required(result.delegation_id)
                    ).trace_id,
                    usage=cast(dict[str, JsonValue], result.usage.model_dump(mode="json")),
                ),
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
                            dict[str, JsonValue], tracker.snapshot().model_dump(mode="json")
                        ),
                    }
                ),
            )

    @staticmethod
    def _next_prompt(task: str, stage: HandoffStage, output: str) -> str:
        return f"Original task:\n{task}\n\nCompleted {stage.value} result:\n{output}"

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

    async def _transition(self, root: Run, status: RunStatus) -> Run:
        transitioned = root.transition_to(status)
        async with self._database.transaction() as session:
            await RunRepository(session).save(transitioned)
            await EventStore(session).append(
                run_id=root.id,
                event_type=EventType.RUN_STATUS_CHANGED,
                payload=RunStatusChangedPayload(
                    previous_status=root.status,
                    status=status,
                ),
            )
        return transitioned

    async def _finish(
        self,
        root: Run,
        status: RunStatus,
        event_type: EventType,
        payload: RunCompletedPayload | ErrorPayload | GenericEventPayload,
    ) -> None:
        transitioned = root.transition_to(status)
        async with self._database.transaction() as session:
            await RunRepository(session).save(transitioned)
            store = EventStore(session)
            await store.append(
                run_id=root.id,
                event_type=EventType.RUN_STATUS_CHANGED,
                payload=RunStatusChangedPayload(previous_status=root.status, status=status),
            )
            await store.append(run_id=root.id, event_type=event_type, payload=payload)

    async def _finish_failed(self, root: Run, result: DelegationResult) -> None:
        await self._finish(
            root,
            RunStatus.FAILED,
            EventType.RUN_FAILED,
            ErrorPayload(
                code=result.error_code or "handoff_stage_failed",
                message=result.error_message or f"Handoff stage {result.agent_id} failed",
            ),
        )

    async def _finish_noncompleted(self, root: Run, result: DelegationResult) -> None:
        if result.status is DelegationStatus.CANCELLED:
            await self._finish(
                root,
                RunStatus.CANCELLED,
                EventType.RUN_CANCELLED,
                GenericEventPayload(
                    data={
                        "reason": "handoff_child_cancelled",
                        "child_run_id": str(result.child_run_id),
                    }
                ),
            )
            return
        await self._finish_failed(root, result)

    async def _cancel(self, root: Run) -> None:
        current = await self._runs.get(root.id)
        if current is None or current.status.is_terminal:
            return
        await self._runs.cancel(root.id)
