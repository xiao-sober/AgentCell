"""Deterministic, checkpointed Coordinator-to-Finalizer handoff workflow."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

from opentelemetry import trace
from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelMessage

from agentcell.agents import (
    AgentDelegation,
    DelegationKind,
    DelegationResult,
    DelegationStatus,
    HandoffRequest,
    HandoffResult,
    HandoffStage,
    is_test_repair_task,
)
from agentcell.budgets import Budget, BudgetSnapshot, BudgetTracker, Usage
from agentcell.errors import AgentCellError
from agentcell.events import (
    AgentChildCompletedPayload,
    ErrorPayload,
    EventType,
    GenericEventPayload,
    JsonValue,
    RunCompletedPayload,
    RunStartedPayload,
    RunStatusChangedPayload,
    TextDeltaPayload,
)
from agentcell.kernel.checkpoint import Checkpoint, CheckpointKind
from agentcell.kernel.child_projection import ChildEventProjector
from agentcell.kernel.lifecycle import RunStatus
from agentcell.kernel.models import Run
from agentcell.kernel.run_service import RunRequest, RunService
from agentcell.policy import ApprovalDecision, CapabilityLease
from agentcell.storage import (
    AgentDelegationRepository,
    ApprovalRepository,
    ChangeSetRepository,
    CheckpointRepository,
    Database,
    EventStore,
    FileChangeRepository,
    RunRepository,
)
from agentcell.tools import is_successful_test_result

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
            id=request.root_run_id,
            conversation_id=request.conversation_id,
            agent_id=request.stage_agents.get(
                HandoffStage.COORDINATOR,
                HandoffStage.COORDINATOR.value,
            ),
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
                    team_id=request.team_id,
                    team_version=request.team_version,
                    budget=cast(dict[str, JsonValue], request.budget.model_dump(mode="json")),
                ),
            )
        return await self.run_prepared(root, request=request)

    async def run_prepared(
        self,
        root: Run,
        *,
        request: HandoffRequest,
        initial_usage: Usage | None = None,
    ) -> HandoffResult:
        """Execute a Team on an existing authoritative root without creating another root."""

        self._validate_request(request)
        if root.id != request.root_run_id or root.conversation_id != request.conversation_id:
            raise ValueError("Prepared handoff root does not match the request")
        if root.status in {RunStatus.CREATED, RunStatus.PAUSED}:
            root = await self._transition(root, RunStatus.RUNNING)
        elif root.status is not RunStatus.RUNNING:
            raise ValueError(f"Cannot execute Team from root status {root.status.value}")
        tracker = BudgetTracker(request.budget, initial_usage=initial_usage)
        return await self._continue(
            root,
            request=request,
            tracker=tracker,
            start_index=0,
            prompt=await self._next_prompt(
                request.task,
                (),
                0,
                request.stage_instructions,
                request.stage_output_contracts,
            ),
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
        request = self._request_from_checkpoint(root, checkpoint, state)
        tracker = BudgetTracker(
            checkpoint.budget.budget,
            initial_usage=checkpoint.budget.used,
        )
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
            if index == 0 and request.history:
                pending_result = await self._recover_child(
                    root,
                    pending,
                    workspace=Path(checkpoint.workspace),
                    user_id=checkpoint.user_id,
                    conversation_id=root.conversation_id,
                    root_budget=tracker.snapshot(),
                    message_history=ModelMessagesTypeAdapter.validate_python(request.history),
                )
            else:
                pending_result = await self._recover_child(
                    root,
                    pending,
                    workspace=Path(checkpoint.workspace),
                    user_id=checkpoint.user_id,
                    conversation_id=root.conversation_id,
                    root_budget=tracker.snapshot(),
                )
            if not pending_result.status.is_terminal:
                return self._result(
                    root,
                    request=request,
                    tracker=tracker,
                    status=DelegationStatus.WAITING_APPROVAL,
                    stages=(*completed, pending_result),
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
        current = next(
            (item for item in delegations if item.provider_call_id == current_call_id),
            None,
        )
        if current is not None and current.result is not None and current.status.is_terminal:
            checkpoint_usage = Usage.model_validate(state.get("accounted_usage", {}))
            tracker.record_child_usage(self._usage_delta(current.result.usage, checkpoint_usage))
            await self._save_delegation(current, current.result)
            await self._budget_updated(root.id, tracker, source="handoff_child_resumed")
            await self._child_completed(root.id, current.result)
            if current.result.status is not DelegationStatus.COMPLETED:
                await self._finish_noncompleted(root, current.result)
                return self._result(
                    root,
                    request=request,
                    tracker=tracker,
                    status=current.result.status,
                    stages=tuple([*completed, current.result]),
                )
            completed = (*completed, current.result)
            rejected = await self._apply_review_gate(
                root,
                stage=_STAGES[index],
                result=current.result,
                enabled=request.review_gate,
            )
            if rejected is not None:
                return self._result(
                    root,
                    request=request,
                    tracker=tracker,
                    status=DelegationStatus.FAILED,
                    stages=completed,
                    output=current.result.output,
                    error_code=rejected[0],
                    error_message=rejected[1],
                    error_stage=_STAGES[index],
                )
            index += 1
            prompt = await self._next_prompt(
                str(state["task"]),
                completed,
                index,
                request.stage_instructions,
                request.stage_output_contracts,
            )
        if root.status is RunStatus.PAUSED:
            root = await self._transition(root, RunStatus.RUNNING)
        return await self._continue(
            root,
            request=request,
            tracker=tracker,
            start_index=index,
            prompt=prompt,
            completed=completed,
        )

    async def decide_approval(
        self,
        root_run_id: UUID,
        approval_id: UUID,
        decision: ApprovalDecision,
    ) -> HandoffResult:
        """Decide one child approval and always reconcile the Team root afterward."""

        async with self._database.session() as session:
            approval = await ApprovalRepository(session).get_required(approval_id)
            delegations = await AgentDelegationRepository(session).list_for_parent(root_run_id)
        if approval.run_id not in {item.child_run_id for item in delegations}:
            raise ValueError("Approval does not belong to a child of the supplied Team Run")
        try:
            await self._runs.resume(approval_id, decision)
        except AgentCellError:
            result = await self.resume(root_run_id)
            if result.status.is_terminal:
                return result
            raise
        return await self.resume(root_run_id)

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
            agent_id = request.stage_agents.get(stage, stage.value)
            model_ref = request.stage_model_refs.get(stage)
            child_budget = request.stage_budgets[stage]
            child_lease = request.stage_leases[stage]
            if (
                stage is HandoffStage.REVIEWER
                and is_test_repair_task(request.task)
                and await self._green_no_change_coder(tuple(results))
            ):
                child_lease = CapabilityLease()
            request.lease.ensure_child_subset(child_lease)
            tracker.reserve_child(depth=1, child_budget=child_budget)
            await self._budget_updated(root.id, tracker, source="handoff_child_reserved")
            child_request = RunRequest(
                prompt=prompt,
                workspace=workspace,
                agent_id=agent_id,
                conversation_id=root.conversation_id,
                user_id=request.user_id,
                lease=child_lease,
                budget=child_budget,
                parent_run_id=root.id,
                depth=1,
                permission_mode=request.permission_mode,
                finalize_after_successful_test=(
                    stage is HandoffStage.CODER and is_test_repair_task(request.task)
                ),
            )
            child, _, delegation = await self._runs.prepare_delegation(
                child_request,
                provider_call_id=f"handoff:{index}:{stage.value}",
                kind=DelegationKind.HANDOFF,
                task=prompt,
                model_ref=model_ref,
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
                        "agentcell.agent.id": agent_id,
                        "agentcell.handoff.stage": stage.value,
                    },
                ):
                    if index == 0 and request.history:
                        result = await self._recover_child(
                            root,
                            delegation,
                            workspace=workspace,
                            user_id=request.user_id,
                            conversation_id=root.conversation_id,
                            root_budget=tracker.snapshot(),
                            message_history=ModelMessagesTypeAdapter.validate_python(
                                request.history
                            ),
                        )
                    else:
                        result = await self._recover_child(
                            root,
                            delegation,
                            workspace=workspace,
                            user_id=request.user_id,
                            conversation_id=root.conversation_id,
                            root_budget=tracker.snapshot(),
                        )
            except asyncio.CancelledError:
                await asyncio.shield(self._cancel(root))
                raise
            except Exception as error:
                failed = DelegationResult(
                    delegation_id=delegation.id,
                    child_run_id=child.id,
                    agent_id=agent_id,
                    status=DelegationStatus.FAILED,
                    error_code=getattr(error, "code", "handoff_stage_failed"),
                    error_message=str(error),
                )
                await self._save_delegation(delegation, failed)
                await self._finish_failed(root, failed)
                return self._result(
                    root,
                    request=request,
                    tracker=tracker,
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
                return self._result(
                    root,
                    request=request,
                    tracker=tracker,
                    status=result.status,
                    stages=tuple([*results, result]),
                )
            results.append(result)
            await self._child_completed(root.id, result)
            if result.status is not DelegationStatus.COMPLETED:
                await self._finish_noncompleted(root, result)
                return self._result(
                    root,
                    request=request,
                    tracker=tracker,
                    status=result.status,
                    stages=tuple(results),
                )
            rejected = await self._apply_review_gate(
                root,
                stage=stage,
                result=result,
                enabled=request.review_gate,
            )
            if rejected is not None:
                return self._result(
                    root,
                    request=request,
                    tracker=tracker,
                    status=DelegationStatus.FAILED,
                    stages=tuple(results),
                    output=result.output,
                    error_code=rejected[0],
                    error_message=rejected[1],
                    error_stage=stage,
                )
            prompt = await self._next_prompt(
                request.task,
                tuple(results),
                index + 1,
                request.stage_instructions,
                request.stage_output_contracts,
            )

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
            output=output,
        )
        return self._result(
            root,
            request=request,
            tracker=tracker,
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
        for name, mapping in (
            ("stage_agents", request.stage_agents),
            ("stage_model_refs", request.stage_model_refs),
            ("stage_instructions", request.stage_instructions),
            ("stage_output_contracts", request.stage_output_contracts),
        ):
            if mapping and set(mapping) != set(_STAGES):
                raise ValueError(f"{name} must be empty or define all handoff stages")
        if request.budget.max_children < len(_STAGES) or request.budget.max_depth < 1:
            raise ValueError("handoff root budget must allow four direct children")
        for stage in _STAGES:
            request.lease.ensure_child_subset(request.stage_leases[stage])
            stage_budget = request.stage_budgets[stage]
            if stage_budget.max_children != 0 or stage_budget.max_depth != 0:
                raise ValueError("handoff stage budgets cannot delegate further")
        reviewer = request.stage_leases[HandoffStage.REVIEWER]
        if (
            reviewer.filesystem_write
            or reviewer.network_domains
            or reviewer.commands
            or reviewer.can_delegate
        ):
            raise ValueError("handoff reviewer lease must remain read-only")
        dimensions = (
            "max_requests",
            "max_input_tokens",
            "max_output_tokens",
            "max_total_tokens",
            "max_tool_calls",
            "max_duration_seconds",
        )
        for field in dimensions:
            allocated = sum(getattr(item, field) for item in request.stage_budgets.values())
            if allocated > getattr(request.budget, field):
                raise ValueError(f"handoff stage {field} allocation exceeds root budget")
        if request.budget.max_cost is not None:
            child_costs = tuple(item.max_cost for item in request.stage_budgets.values())
            if (
                any(cost is None for cost in child_costs)
                or sum(
                    (cast(Decimal, cost) for cost in child_costs),
                    Decimal("0"),
                )
                > request.budget.max_cost
            ):
                raise ValueError("handoff stage cost allocation exceeds root budget")

    @staticmethod
    def _request_from_checkpoint(
        root: Run,
        checkpoint: Checkpoint,
        state: dict[str, JsonValue],
    ) -> HandoffRequest:
        stage_agents_raw = cast(dict[str, object], state.get("stage_agents", {}))
        stage_models_raw = cast(dict[str, object], state.get("stage_model_refs", {}))
        stage_instructions_raw = cast(dict[str, object], state.get("stage_instructions", {}))
        stage_contracts_raw = cast(dict[str, object], state.get("stage_output_contracts", {}))
        return HandoffRequest(
            task=str(state["task"]),
            workspace=checkpoint.workspace,
            history=checkpoint.messages,
            root_run_id=root.id,
            user_id=checkpoint.user_id,
            conversation_id=root.conversation_id,
            team_id=str(state.get("team_id", "software")),
            team_version=int(cast(int, state.get("team_version", 1))),
            permission_mode=checkpoint.permission_mode,
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
            stage_agents={HandoffStage(key): str(value) for key, value in stage_agents_raw.items()},
            stage_model_refs={
                HandoffStage(key): str(value) for key, value in stage_models_raw.items()
            },
            stage_instructions={
                HandoffStage(key): str(value) for key, value in stage_instructions_raw.items()
            },
            stage_output_contracts={
                HandoffStage(key): str(value) for key, value in stage_contracts_raw.items()
            },
            review_gate=bool(state.get("review_gate", True)),
        )

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
            "team_id": request.team_id,
            "team_version": request.team_version,
            "review_gate": request.review_gate,
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
            "stage_agents": {key.value: value for key, value in request.stage_agents.items()},
            "stage_model_refs": {
                key.value: value for key, value in request.stage_model_refs.items()
            },
            "stage_instructions": {
                key.value: value for key, value in request.stage_instructions.items()
            },
            "stage_output_contracts": {
                key.value: value for key, value in request.stage_output_contracts.items()
            },
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
                    permission_mode=request.permission_mode,
                    budget=tracker.snapshot(),
                    messages=request.history,
                    pending_delegation_ids=(delegation.id,),
                    child_run_ids=(delegation.child_run_id,),
                    workflow_state=state,
                    run_status=root.status,
                    depth=0,
                )
            )

    async def _recover_child(
        self,
        root: Run,
        delegation: AgentDelegation,
        *,
        workspace: Path,
        user_id: UUID,
        conversation_id: UUID,
        root_budget: BudgetSnapshot,
        message_history: Sequence[ModelMessage] = (),
    ) -> DelegationResult:
        result, _ = await ChildEventProjector(self._database, self._runs).recover(
            root,
            delegation,
            workspace=workspace,
            user_id=user_id,
            conversation_id=conversation_id,
            message_history=message_history,
            project_text=False,
            root_budget=root_budget,
            accounted_child_usage=delegation.accounted_usage,
        )
        return result

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

    async def _next_prompt(
        self,
        task: str,
        completed: tuple[DelegationResult, ...],
        next_index: int,
        stage_instructions: dict[HandoffStage, str],
        output_contracts: dict[HandoffStage, str],
    ) -> str:
        instructions = {
            HandoffStage.COORDINATOR: "Produce a bounded implementation plan.",
            HandoffStage.CODER: "Implement the task and report changed files and checks.",
            HandoffStage.REVIEWER: (
                "Independently review the implementation. The first non-empty line must be "
                "exactly PASS or CHANGES_NEEDED, followed by evidence."
            ),
            HandoffStage.FINALIZER: (
                "Summarize the delivered result using only the persisted stage evidence below."
            ),
        }
        stage = _STAGES[next_index] if next_index < len(_STAGES) else None
        evidence_sections: list[str] = []
        for item in completed:
            runtime_evidence = await self._runtime_evidence(item.child_run_id)
            section = f"[{item.agent_id}]\n{item.output or '(no textual output)'}"
            if runtime_evidence:
                section += f"\nRuntime evidence:\n{runtime_evidence}"
            evidence_sections.append(section)
        evidence = "\n\n".join(evidence_sections)
        sections = [f"Original task:\n{task}"]
        if evidence:
            sections.append(f"Persisted completed stage evidence:\n{evidence}")
        if stage is not None:
            instruction = stage_instructions.get(stage, instructions[stage])
            contract = output_contracts.get(stage, instructions[stage])
            sections.append(
                f"Current stage: {stage.value}\n"
                f"Stage instructions: {instruction}\n"
                f"Required output: {contract}"
            )
        return "\n\n".join(sections)

    async def _runtime_evidence(self, run_id: UUID) -> str:
        """Build a bounded evidence summary only from persisted events and change records."""

        async with self._database.session() as session:
            events = await EventStore(session).list_for_run(run_id)
            change_set = await ChangeSetRepository(session).get_for_run(run_id)
            file_changes = await FileChangeRepository(session).list_for_run(run_id)
        lines: list[str] = []
        for event in events:
            if event.event_type is not EventType.TOOL_COMPLETED:
                continue
            if not isinstance(event.payload, GenericEventPayload):
                continue
            data = event.payload.data
            if data.get("tool_name") != "shell.test":
                continue
            output = data.get("output")
            if not isinstance(output, dict):
                continue
            exit_code = output.get("exit_code")
            command = output.get("command")
            command_text = (
                " ".join(str(part) for part in command)
                if isinstance(command, list)
                else "(unknown command)"
            )
            test_execution = output.get("test_execution")
            summary = (
                str(test_execution.get("summary") or "")
                if isinstance(test_execution, dict)
                else self._test_summary(str(output.get("stdout") or ""))
            )
            line = f"- persisted shell.test: `{command_text}` exit_code={exit_code}"
            if isinstance(test_execution, dict):
                line += (
                    f"; executed={test_execution.get('executed')}"
                    f"; successful={test_execution.get('successful')}"
                    f"; collected_only={test_execution.get('collected_only')}"
                )
            if summary:
                line += f"; summary={summary}"
            artifact = data.get("artifact")
            if isinstance(artifact, dict) and artifact.get("artifact_id") is not None:
                line += f"; artifact_id={artifact['artifact_id']}"
            lines.append(line)
        lines.append(
            f"- persisted changes: change_set={'present' if change_set is not None else 'none'}, "
            f"file_changes={len(file_changes)}"
        )
        return "\n".join(lines)

    async def _green_no_change_coder(
        self,
        completed: tuple[DelegationResult, ...],
    ) -> bool:
        coder = next((item for item in completed if item.agent_id == "coder"), None)
        if coder is None or coder.status is not DelegationStatus.COMPLETED:
            return False
        async with self._database.session() as session:
            events = await EventStore(session).list_for_run(coder.child_run_id)
            file_changes = await FileChangeRepository(session).list_for_run(coder.child_run_id)
        if file_changes:
            return False
        for event in events:
            if event.event_type is not EventType.TOOL_COMPLETED:
                continue
            if not isinstance(event.payload, GenericEventPayload):
                continue
            if event.payload.data.get("tool_name") != "shell.test":
                continue
            output = event.payload.data.get("output")
            if is_successful_test_result(output):
                return True
        return False

    @staticmethod
    def _test_summary(stdout: str) -> str:
        for line in reversed(stdout.splitlines()):
            normalized = line.strip()
            if normalized and any(
                marker in normalized.casefold()
                for marker in (" passed", " failed", " error", " skipped")
            ):
                return normalized[:500]
        return ""

    async def _apply_review_gate(
        self,
        root: Run,
        *,
        stage: HandoffStage,
        result: DelegationResult,
        enabled: bool,
    ) -> tuple[str, str] | None:
        if not enabled or stage is not HandoffStage.REVIEWER:
            return None
        first_line = next(
            (line.strip().upper() for line in (result.output or "").splitlines() if line.strip()),
            "",
        )
        if first_line == "PASS":
            return None
        code = (
            "reviewer_changes_requested"
            if first_line == "CHANGES_NEEDED"
            else "reviewer_decision_invalid"
        )
        message = (
            "Reviewer requested changes; finalization was not started"
            if first_line == "CHANGES_NEEDED"
            else "Reviewer output must start with PASS or CHANGES_NEEDED"
        )
        await self._finish(
            root,
            RunStatus.FAILED,
            EventType.RUN_FAILED,
            ErrorPayload(code=code, message=message),
        )
        return code, message

    @staticmethod
    def _result(
        root: Run,
        *,
        request: HandoffRequest,
        tracker: BudgetTracker,
        status: DelegationStatus,
        stages: tuple[DelegationResult, ...],
        output: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        error_stage: HandoffStage | None = None,
    ) -> HandoffResult:
        if status is DelegationStatus.FAILED and stages:
            last = stages[-1]
            error_code = error_code or last.error_code or "handoff_failed"
            error_message = error_message or last.error_message or "Team stage failed"
            error_stage = error_stage or _STAGES[len(stages) - 1]
        return HandoffResult(
            root_run_id=root.id,
            conversation_id=root.conversation_id,
            team_id=request.team_id,
            team_version=request.team_version,
            status=status,
            stages=stages,
            budget=tracker.snapshot(),
            output=output,
            error_code=error_code,
            error_message=error_message,
            error_stage=error_stage,
        )

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
        *,
        output: str | None = None,
    ) -> None:
        transitioned = root.transition_to(status)
        async with self._database.transaction() as session:
            await RunRepository(session).save(transitioned)
            store = EventStore(session)
            if output:
                await store.append(
                    run_id=root.id,
                    event_type=EventType.MODEL_TEXT_DELTA,
                    payload=TextDeltaPayload(delta=output),
                )
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
