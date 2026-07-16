"""Shared application composition and transport-neutral command/query services."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from agentcell.agents import (
    AgentRegistry,
    AgentSource,
    AgentSpec,
    AgentVisibility,
    TeamRegistry,
    assistant_spec,
    coder_spec,
    coordinator_spec,
    finalizer_spec,
    researcher_spec,
    reviewer_spec,
    software_team_spec,
    summarizer_spec,
)
from agentcell.changes.service import ChangeService
from agentcell.config import AgentCellSettings
from agentcell.conversations.service import ConversationService, PreparedConversationTurn
from agentcell.errors import AgentRegistrationError, ConversationConflictError
from agentcell.events import DomainEvent, EventPayload
from agentcell.kernel.handoff import HandoffService
from agentcell.kernel.models import Run
from agentcell.kernel.replay import ReplayService
from agentcell.kernel.run_service import RunRequest, RunService
from agentcell.memory.service import MemoryService
from agentcell.policy import Approval, ApprovalDecision, ApprovalStatus
from agentcell.providers import (
    FakeModelSpec,
    FakeProviderAdapter,
    FakeScript,
    FakeTextStep,
    ModelSpec,
    ProviderFactory,
    ProviderName,
)
from agentcell.routing import TASK_ROUTER_AGENT_ID, PreparedTaskRoute, TaskRoutingService
from agentcell.storage import (
    AgentDelegationRepository,
    AgentSpecRepository,
    ApprovalRepository,
    ConversationRepository,
    Database,
    EventStore,
    FileArtifactStore,
    RunRepository,
)
from agentcell.tools import (
    ToolRegistry,
    register_http_tools,
    register_shell_tools,
    register_workspace_tools,
)


@dataclass(slots=True)
class RunSupervisor:
    """Own in-process Run tasks while persisted events remain the source of truth."""

    runs: RunService
    _tasks: dict[UUID, asyncio.Task[object]] = field(
        default_factory=lambda: {},
    )

    async def start(self, request: RunRequest) -> Run:
        run, spec = await self.runs.prepare(request)
        self.start_prepared(
            run,
            self.runs.execute_prepared(run, request=request, spec=spec),
        )
        return run

    def start_prepared(
        self,
        run: Run,
        execution: Coroutine[Any, Any, object],
    ) -> None:
        task = asyncio.create_task(
            execution,
            name=f"agentcell-run-{run.id}",
        )
        self._tasks[run.id] = task
        task.add_done_callback(lambda completed, run_id=run.id: self._finish(run_id, completed))

    def active(self, run_id: UUID) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    async def close(self) -> None:
        tasks = tuple(task for task in self._tasks.values() if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def _finish(self, run_id: UUID, task: asyncio.Task[object]) -> None:
        self._tasks.pop(run_id, None)
        if not task.cancelled():
            task.exception()


@dataclass(slots=True)
class AgentCellApplication:
    """One process-local composition root shared by FastAPI and Typer."""

    database: Database
    providers: ProviderFactory
    agents: AgentRegistry
    teams: TeamRegistry
    tools: ToolRegistry
    runs: RunService
    replay: ReplayService
    memory: MemoryService
    changes: ChangeService
    model_specs: dict[str, ModelSpec[ProviderName]]
    owns_resources: bool = True
    supervisor: RunSupervisor = field(init=False)
    conversations: ConversationService = field(init=False)
    handoffs: HandoffService = field(init=False)
    routing: TaskRoutingService = field(init=False)

    def __post_init__(self) -> None:
        self.supervisor = RunSupervisor(self.runs)
        self.handoffs = HandoffService(self.database, self.runs)
        self.routing = TaskRoutingService(
            database=self.database,
            agents=self.agents,
            teams=self.teams,
            providers=self.providers,
            runs=self.runs,
            handoffs=self.handoffs,
            routing_model_ref=next(iter(self.model_specs)),
        )
        self.conversations = ConversationService(
            database=self.database,
            runs=self.runs,
            agents=self.agents,
            teams=self.teams,
            routing=self.routing,
            model_refs=self.model_specs,
            default_model_ref=next(iter(self.model_specs)),
        )

    async def start_conversation_turn(self, prepared: PreparedConversationTurn) -> Run:
        self.supervisor.start_prepared(
            prepared.run,
            self.conversations.execute_prepared(prepared),
        )
        return prepared.run

    async def start_routed_conversation_turn(self, prepared: PreparedTaskRoute) -> Run:
        self.supervisor.start_prepared(
            prepared.root,
            self.conversations.execute_routed_prepared(prepared),
        )
        return prepared.root

    async def start_task(self, prepared: PreparedTaskRoute) -> Run:
        self.supervisor.start_prepared(
            prepared.root,
            self.routing.execute(prepared),
        )
        return prepared.root

    async def start_run(self, request: RunRequest) -> Run:
        """Start a standalone Run without bypassing managed Conversation semantics."""

        async with self.database.session() as session:
            conversation = await ConversationRepository(session).get(request.conversation_id)
        if conversation is not None:
            raise ConversationConflictError(
                "Managed Conversation turns must use the Conversation runs endpoint"
            )
        return await self.supervisor.start(request)

    async def get_run(self, run_id: UUID) -> Run | None:
        async with self.database.session() as session:
            return await RunRepository(session).get(run_id)

    async def events(
        self,
        run_id: UUID,
        *,
        after_sequence: int = 0,
    ) -> list[DomainEvent[EventPayload]]:
        async with self.database.session() as session:
            return await EventStore(session).list_for_run(
                run_id,
                after_sequence=after_sequence,
            )

    async def approvals(
        self,
        run_id: UUID,
        *,
        status: ApprovalStatus | None = None,
    ) -> list[Approval]:
        async with self.database.session() as session:
            return await ApprovalRepository(session).list_for_run(run_id, status=status)

    async def decide_approval(self, approval_id: UUID, decision: ApprovalDecision) -> Run:
        """Resume a direct Run or reconcile the routed parent owning its child approval."""

        async with self.database.session() as session:
            approval = await ApprovalRepository(session).get_required(approval_id)
            delegation = await AgentDelegationRepository(session).find_by_child(approval.run_id)
            parent = (
                None
                if delegation is None
                else await RunRepository(session).get(delegation.parent_run_id)
            )
        if parent is not None and parent.agent_id == TASK_ROUTER_AGENT_ID:
            result = await self.routing.decide_approval(parent.id, approval_id, decision)
            await self.conversations.record_task_result(result)
            return result.run
        result = await self.runs.resume(approval_id, decision)
        await self.conversations.record_if_managed(result)
        return result.run

    def has_agent(self, agent_id: str) -> bool:
        return any(spec.id == agent_id for spec in self.agents.list())

    async def create_agent(self, spec: AgentSpec) -> AgentSpec:
        """Persist then publish a new Agent declaration to future Runs."""

        if self.has_agent(spec.id):
            raise AgentRegistrationError(f"Agent {spec.id!r} is already registered")
        async with self.database.transaction() as session:
            await AgentSpecRepository(session).create(spec)
        self.agents.register(spec, source=AgentSource.PERSISTED)
        return spec

    async def update_agent(self, spec: AgentSpec) -> AgentSpec:
        current = self.agents.get_entry(spec.id)
        async with self.database.transaction() as session:
            await AgentSpecRepository(session).save(spec)
        self.agents.replace(
            spec,
            source=(
                AgentSource.OVERRIDE if current.source is AgentSource.BUILTIN else current.source
            ),
        )
        return spec

    async def healthy(self) -> bool:
        try:
            async with self.database.session() as session:
                return (await session.scalar(text("SELECT 1"))) == 1
        except SQLAlchemyError:
            return False

    async def close(self) -> None:
        await self.supervisor.close()
        if self.owns_resources:
            await self.providers.aclose()
            await self.database.dispose()


async def build_application(
    *,
    config: Path = Path("agentcell.toml"),
    database_url: str | None = None,
    offline_fake: bool = False,
    fake_output: str = "Offline AgentCell response",
    model_ref: str | None = None,
) -> AgentCellApplication:
    """Build the production composition root without importing any transport."""

    if offline_fake:
        selected_ref = "offline_fake"
        model = FakeModelSpec(model="agentcell-offline-fake")
        models: dict[str, ModelSpec[ProviderName]] = {selected_ref: model}
        providers = ProviderFactory(
            models,
            adapters=(
                FakeProviderAdapter(
                    {model.model: FakeScript(steps=(FakeTextStep(text=fake_output),))}
                ),
            ),
        )
    else:
        settings = AgentCellSettings.from_toml(config)
        models = dict(settings.models)
        selected_ref = model_ref or next(iter(models))
        providers = ProviderFactory(models)
        providers.model_spec(selected_ref)

    url = database_url or os.getenv("AGENTCELL_DATABASE_URL")
    database = Database(url) if url else Database.from_path(Path(".agentcell/agentcell.db"))
    agents = _builtin_agents(selected_ref)
    async with database.session() as session:
        persisted_agents = await AgentSpecRepository(session).list()
    for spec in persisted_agents:
        if any(item.id == spec.id for item in agents.list()):
            agents.replace(spec, source=AgentSource.OVERRIDE)
        else:
            agents.register(spec, source=AgentSource.PERSISTED)
    tools = _default_tools()
    runs = RunService(database=database, providers=providers, agents=agents, tools=tools)
    changes = ChangeService(database, FileArtifactStore(database, Path(".agentcell/artifacts")))
    return AgentCellApplication(
        database=database,
        providers=providers,
        agents=agents,
        teams=TeamRegistry((software_team_spec(model_ref=selected_ref),)),
        tools=tools,
        runs=runs,
        replay=ReplayService(database),
        memory=MemoryService(database),
        changes=changes,
        model_specs=models,
    )


def _builtin_agents(model_ref: str) -> AgentRegistry:
    registry = AgentRegistry()
    for spec in (
        assistant_spec(model_ref=model_ref),
        coordinator_spec(model_ref=model_ref, collaborative=False),
        coder_spec(model_ref=model_ref),
        reviewer_spec(model_ref=model_ref),
        researcher_spec(model_ref=model_ref),
    ):
        registry.register(
            spec,
            source=AgentSource.BUILTIN,
            visibility=AgentVisibility.PUBLIC,
        )
    for spec in (
        summarizer_spec(model_ref=model_ref),
        finalizer_spec(model_ref=model_ref),
    ):
        registry.register(
            spec,
            source=AgentSource.BUILTIN,
            visibility=AgentVisibility.INTERNAL,
        )
    return registry


def _default_tools() -> ToolRegistry:
    registry = ToolRegistry()
    register_workspace_tools(registry)
    register_shell_tools(registry)
    register_http_tools(registry)
    return registry
