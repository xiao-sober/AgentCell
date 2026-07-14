"""Typer CLI that invokes RunService directly without a local HTTP hop."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any, NoReturn
from uuid import UUID, uuid4

import typer
import uvicorn
from pydantic import BaseModel, ConfigDict, TypeAdapter
from rich.console import Console
from sqlalchemy.exc import SQLAlchemyError

from agentcell.agents import AgentRegistry, AgentSpec, coordinator_spec
from agentcell.api import create_app
from agentcell.application import AgentCellApplication, build_application
from agentcell.changes import ChangeDetails, FileChange
from agentcell.changes.service import ChangeService
from agentcell.errors import AgentCellError, RunNotFoundError
from agentcell.events import (
    DomainEvent,
    EventPayload,
    EventType,
    GenericEventPayload,
    JsonValue,
    TextDeltaPayload,
)
from agentcell.kernel.event_recorder import RunEventRecorder
from agentcell.kernel.models import Run
from agentcell.kernel.replay import ReplayService, ReplayState
from agentcell.kernel.run_service import RunRequest, RunResult, RunService
from agentcell.memory import MemoryScope, MemorySearchResult
from agentcell.policy import (
    Approval,
    ApprovalDecision,
    ApprovalDecisionKind,
    ApprovalStatus,
    CapabilityLease,
    PermissionMode,
)
from agentcell.providers import (
    FakeModelSpec,
    FakeProviderAdapter,
    FakeScript,
    FakeTextStep,
    ProviderFactory,
)
from agentcell.storage import (
    ApprovalRepository,
    ConversationRepository,
    Database,
    EventStore,
    FileArtifactStore,
    RunRepository,
)
from agentcell.tools import ToolDefinition, ToolRegistry, register_workspace_tools

app = typer.Typer(
    name="agentcell",
    help="Local-first Agent runtime.",
    no_args_is_help=True,
)
console = Console()
agents_app = typer.Typer(help="Inspect and manage Agent declarations.")
tools_app = typer.Typer(help="Inspect registered tools and policies.")
memory_app = typer.Typer(help="Search scoped long-term memory.")
changes_app = typer.Typer(help="Inspect and safely revert AgentCell file changes.")
app.add_typer(agents_app, name="agents")
app.add_typer(tools_app, name="tools")
app.add_typer(memory_app, name="memory")
app.add_typer(changes_app, name="changes")
_DEFAULT_WORKSPACE = Path.cwd()
_DEFAULT_CONFIG = Path("agentcell.toml")

PromptArgument = Annotated[str, typer.Argument(help="Task for the selected Agent.")]
WorkspaceOption = Annotated[
    Path,
    typer.Option("--workspace", "-w", help="Workspace root constrained by the Run lease."),
]
DatabaseOption = Annotated[
    str | None,
    typer.Option("--database-url", help="Migrated SQLite aiosqlite URL."),
]
ConfigOption = Annotated[
    Path,
    typer.Option("--config", help="AgentCell TOML used outside offline Fake mode."),
]
ModelOption = Annotated[
    str | None,
    typer.Option("--model-ref", help="Configured model reference."),
]
OfflineOption = Annotated[
    bool,
    typer.Option("--offline-fake", help="Use a deterministic offline Fake Provider."),
]
JsonOption = Annotated[
    bool,
    typer.Option("--json", help="Write the structured RunResult as JSON."),
]
JsonEventsOption = Annotated[
    bool,
    typer.Option("--json-events", help="Write persisted Run events as NDJSON without ANSI."),
]
MaxRequestsOption = Annotated[
    int | None,
    typer.Option(
        "--max-requests",
        min=1,
        max=100,
        help="Override the Run model-request budget (default: 10).",
    ),
]
MaxToolCallsOption = Annotated[
    int | None,
    typer.Option(
        "--max-tool-calls",
        min=1,
        max=1000,
        help="Override the Run tool-call budget (default: 20).",
    ),
]
MaxInputTokensOption = Annotated[
    int | None,
    typer.Option(
        "--max-input-tokens",
        min=1,
        max=2_000_000,
        help="Override cumulative Run input-token budget (default: 200000).",
    ),
]
MaxTotalTokensOption = Annotated[
    int | None,
    typer.Option(
        "--max-total-tokens",
        min=1,
        max=2_000_000,
        help="Override cumulative Run total-token budget (default: 240000).",
    ),
]
AgentOption = Annotated[
    str,
    typer.Option("--agent", help="Built-in or registered Agent id."),
]
PermissionModeOption = Annotated[
    PermissionMode,
    typer.Option(
        "--permission-mode",
        help="Approval policy: request, auto (guarded only), or full (leased operations).",
    ),
]
AllowWriteOption = Annotated[
    list[str] | None,
    typer.Option(
        "--allow-write",
        help="Workspace-relative writable path; repeat for multiple scopes.",
    ),
]
AllowCommandOption = Annotated[
    list[str] | None,
    typer.Option(
        "--allow-command",
        help="Executable name allowed by the Run lease; repeat as needed.",
    ),
]
StreamOption = Annotated[
    bool,
    typer.Option("--stream/--no-stream", help="Render persisted Run events while executing."),
]


class RunInspection(BaseModel):
    """Stable CLI projection for one persisted Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run: Run
    event_count: int
    last_sequence: int
    pending_approval_ids: tuple[UUID, ...]


class CliEventRenderer:
    """Sequence-aware Rich renderer for public domain events only."""

    def __init__(self, *, enabled: bool, json_events: bool = False) -> None:
        self.enabled = enabled
        self.json_events = json_events
        self.last_sequence = 0
        self.text_streamed = False
        self._text_line_open = False

    def render(self, event: DomainEvent[EventPayload]) -> None:
        if event.sequence <= self.last_sequence:
            return
        self.last_sequence = event.sequence
        if not self.enabled:
            return
        if self.json_events:
            console.print(
                json.dumps(
                    {
                        "event_id": str(event.event_id),
                        "run_id": str(event.run_id),
                        "sequence": event.sequence,
                        "event_type": event.event_type.value,
                        "occurred_at": event.occurred_at.isoformat(),
                        "payload": event.safe_payload(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                markup=False,
                soft_wrap=True,
            )
            return
        if event.event_type is EventType.MODEL_TEXT_DELTA and isinstance(
            event.payload, TextDeltaPayload
        ):
            console.print(event.payload.delta, end="", markup=False, soft_wrap=True)
            self.text_streamed = True
            self._text_line_open = True
            return
        data = event.payload.data if isinstance(event.payload, GenericEventPayload) else {}
        if event.event_type in {
            EventType.TOOL_PROPOSED,
            EventType.TOOL_STARTED,
            EventType.TOOL_COMPLETED,
            EventType.TOOL_FAILED,
        }:
            self._finish_text_line()
            console.print(f"[dim]{event.event_type.value} tool={data.get('tool_name', '-')}[/dim]")
        elif event.event_type in {
            EventType.TOOL_APPROVAL_REQUIRED,
            EventType.TOOL_APPROVED,
            EventType.TOOL_REJECTED,
        }:
            self._finish_text_line()
            console.print(
                f"[yellow]{event.event_type.value}[/yellow] tool={data.get('tool_name', '-')}"
            )
        elif event.event_type is EventType.BUDGET_UPDATED:
            self._finish_text_line()
            console.print(f"[dim]budget.updated source={data.get('source', '-')}[/dim]")
        elif event.event_type in {
            EventType.CONTEXT_COMPACTED,
            EventType.AGENT_CHILD_STARTED,
            EventType.AGENT_CHILD_COMPLETED,
            EventType.FILE_CHANGE_PREPARED,
            EventType.FILE_CHANGE_COMPLETED,
            EventType.FILE_CHANGE_CONFLICT,
            EventType.FILE_CHANGE_REVERTED,
        }:
            self._finish_text_line()
            console.print(f"[dim]{event.event_type.value}[/dim]")

    def finish(self) -> None:
        self._finish_text_line()

    def _finish_text_line(self) -> None:
        if self._text_line_open:
            console.print()
            self._text_line_open = False


@app.callback()
def root() -> None:
    """Select an AgentCell command."""


@app.command("run")
def run_command(
    prompt: PromptArgument,
    workspace: WorkspaceOption = _DEFAULT_WORKSPACE,
    agent_id: AgentOption = "coordinator",
    permission_mode: PermissionModeOption = PermissionMode.REQUEST,
    allow_write: AllowWriteOption = None,
    allow_command: AllowCommandOption = None,
    database_url: DatabaseOption = None,
    config: ConfigOption = _DEFAULT_CONFIG,
    model_ref: ModelOption = None,
    offline_fake: OfflineOption = False,
    json_output: JsonOption = False,
    json_events: JsonEventsOption = False,
    stream: StreamOption = True,
    max_requests: MaxRequestsOption = None,
    max_tool_calls: MaxToolCallsOption = None,
    max_input_tokens: MaxInputTokensOption = None,
    max_total_tokens: MaxTotalTokensOption = None,
) -> None:
    """Execute one Run in-process and persist its complete event history."""

    try:
        if json_output and json_events:
            raise ValueError("--json and --json-events are mutually exclusive")
        result, text_streamed = asyncio.run(
            _run_once(
                prompt=prompt,
                workspace=workspace,
                agent_id=agent_id,
                permission_mode=permission_mode,
                allow_write=allow_write,
                allow_command=allow_command,
                stream=(stream and not json_output) or json_events,
                json_events=json_events,
                database_url=database_url,
                config=config,
                model_ref=model_ref,
                offline_fake=offline_fake,
                max_requests=max_requests,
                max_tool_calls=max_tool_calls,
                max_input_tokens=max_input_tokens,
                max_total_tokens=max_total_tokens,
            )
        )
    except KeyboardInterrupt as error:
        console.print("[yellow]Run cancelled.[/yellow]")
        raise typer.Exit(code=130) from error
    except (AgentCellError, OSError, SQLAlchemyError, ValueError) as error:
        console.print(f"[red]Run failed:[/red] {error}")
        if isinstance(error, SQLAlchemyError):
            console.print("[dim]Apply migrations first: uv run alembic upgrade head[/dim]")
        raise typer.Exit(code=1) from error

    if json_output:
        console.print_json(result.model_dump_json())
        return
    if json_events:
        return
    if result.output is not None and not text_streamed:
        console.print(result.output)
    for approval in result.approvals:
        console.print(
            f"[yellow]approval required[/yellow] id={approval.id} "
            f"tool={approval.tool_name} risk={approval.risk.value}"
        )
        console.print(f"  impact: {approval.impact}")
    usage = result.budget.used
    cache_hit_ratio = (
        0.0 if usage.input_tokens == 0 else min(1.0, usage.cache_read_tokens / usage.input_tokens)
    )
    console.print(
        f"[dim]run_id={result.run.id} conversation_id={result.run.conversation_id} "
        f"status={result.run.status.value} "
        f"requests={usage.requests} tool_calls={usage.tool_calls}[/dim]"
    )
    console.print(
        f"[dim]tokens input={usage.input_tokens} output={usage.output_tokens} "
        f"total={usage.total_tokens} cache_read={usage.cache_read_tokens} "
        f"cache_write={usage.cache_write_tokens} cache_hit={cache_hit_ratio:.1%}[/dim]"
    )


@app.command("chat")
def chat_command(
    workspace: WorkspaceOption = _DEFAULT_WORKSPACE,
    conversation_id: Annotated[
        UUID | None,
        typer.Option("--conversation-id", help="Continue an existing Conversation."),
    ] = None,
    user_id: Annotated[
        UUID | None,
        typer.Option("--user-id", help="Stable local user scope for a new Conversation."),
    ] = None,
    agent_id: Annotated[
        str | None,
        typer.Option("--agent", help="Agent for a new Conversation; must match when continuing."),
    ] = None,
    permission_mode: PermissionModeOption = PermissionMode.REQUEST,
    allow_write: AllowWriteOption = None,
    allow_command: AllowCommandOption = None,
    database_url: DatabaseOption = None,
    config: ConfigOption = _DEFAULT_CONFIG,
    model_ref: ModelOption = None,
    offline_fake: OfflineOption = False,
    stream: StreamOption = True,
    max_requests: MaxRequestsOption = None,
    max_tool_calls: MaxToolCallsOption = None,
    max_input_tokens: MaxInputTokensOption = None,
    max_total_tokens: MaxTotalTokensOption = None,
) -> None:
    """Create fresh Runs in one durable Conversation until `/exit`."""

    try:
        asyncio.run(
            _chat(
                workspace=workspace,
                conversation_id=conversation_id,
                user_id=user_id,
                agent_id=agent_id,
                permission_mode=permission_mode,
                allow_write=allow_write,
                allow_command=allow_command,
                stream=stream,
                database_url=database_url,
                config=config,
                model_ref=model_ref,
                offline_fake=offline_fake,
                max_requests=max_requests,
                max_tool_calls=max_tool_calls,
                max_input_tokens=max_input_tokens,
                max_total_tokens=max_total_tokens,
            )
        )
    except KeyboardInterrupt as error:
        console.print("[yellow]Chat stopped.[/yellow]")
        raise typer.Exit(code=130) from error
    except (AgentCellError, OSError, SQLAlchemyError, ValueError) as error:
        _raise_cli_error(error)


@app.command("replay")
def replay_command(
    run_id: Annotated[UUID, typer.Argument(help="Run to replay.")],
    through_sequence: Annotated[
        int | None,
        typer.Option("--through-sequence", min=1, help="Inclusive event sequence."),
    ] = None,
    database_url: DatabaseOption = None,
    json_output: JsonOption = False,
) -> None:
    """Project deterministic Run state from its append-only event stream."""

    try:
        state = asyncio.run(_replay_once(run_id, through_sequence, database_url))
    except (AgentCellError, OSError, SQLAlchemyError, ValueError) as error:
        _raise_cli_error(error)
    if json_output:
        console.print_json(state.model_dump_json())
    else:
        console.print(
            f"run_id={state.run_id} sequence={state.through_sequence} "
            f"status={state.status.value} events={state.events_applied}"
        )


@app.command("branch")
def branch_command(
    run_id: Annotated[UUID, typer.Argument(help="Source Run.")],
    from_sequence: Annotated[
        int,
        typer.Option("--from-sequence", min=1, help="Inclusive source event sequence."),
    ],
    database_url: DatabaseOption = None,
    json_output: JsonOption = False,
) -> None:
    """Create a paused child Run from a recoverable event prefix."""

    try:
        run = asyncio.run(_branch_once(run_id, from_sequence, database_url))
    except (AgentCellError, OSError, SQLAlchemyError, ValueError) as error:
        _raise_cli_error(error)
    if json_output:
        console.print_json(run.model_dump_json())
    else:
        console.print(f"branch_run_id={run.id} parent_run_id={run.parent_run_id} status=paused")


@app.command("cancel")
def cancel_command(
    run_id: Annotated[UUID, typer.Argument(help="Run to cancel idempotently.")],
    database_url: DatabaseOption = None,
    json_output: JsonOption = False,
) -> None:
    """Persist a cancellation terminal state without an HTTP hop."""

    try:
        run = asyncio.run(_cancel_once(run_id, database_url))
    except (AgentCellError, OSError, SQLAlchemyError, ValueError) as error:
        _raise_cli_error(error)
    if json_output:
        console.print_json(run.model_dump_json())
    else:
        console.print(f"run_id={run.id} status={run.status.value}")


@app.command("inspect")
def inspect_command(
    run_id: Annotated[UUID, typer.Argument(help="Run to inspect.")],
    database_url: DatabaseOption = None,
    json_output: JsonOption = False,
) -> None:
    """Inspect a Run projection, event cursor, and pending approvals."""

    try:
        inspection = asyncio.run(_inspect_once(run_id, database_url))
    except (AgentCellError, OSError, SQLAlchemyError, ValueError) as error:
        _raise_cli_error(error)
    if json_output:
        console.print_json(inspection.model_dump_json())
    else:
        console.print(
            f"run_id={inspection.run.id} status={inspection.run.status.value} "
            f"events={inspection.event_count} last_sequence={inspection.last_sequence}"
        )
        for approval_id in inspection.pending_approval_ids:
            console.print(f"[yellow]pending approval[/yellow] id={approval_id}")


@app.command("resume")
def resume_command(
    run_id: Annotated[UUID, typer.Argument(help="Paused Run to resume.")],
    approval_id: Annotated[
        UUID | None,
        typer.Option("--approval-id", help="Pending approval to decide before resuming."),
    ] = None,
    decision: Annotated[
        ApprovalDecisionKind,
        typer.Option("--decision", help="Approval decision."),
    ] = ApprovalDecisionKind.APPROVE,
    database_url: DatabaseOption = None,
    config: ConfigOption = _DEFAULT_CONFIG,
    offline_fake: OfflineOption = False,
    json_output: JsonOption = False,
) -> None:
    """Resume a delegated Run or decide a pending approval in-process."""

    try:
        run = asyncio.run(
            _resume_once(
                run_id,
                approval_id=approval_id,
                decision=decision,
                database_url=database_url,
                config=config,
                offline_fake=offline_fake,
            )
        )
    except (AgentCellError, OSError, SQLAlchemyError, ValueError) as error:
        _raise_cli_error(error)
    if json_output:
        console.print_json(run.model_dump_json())
    else:
        console.print(f"run_id={run.id} status={run.status.value}")


@agents_app.command("list")
def agents_list_command(
    database_url: DatabaseOption = None,
    config: ConfigOption = _DEFAULT_CONFIG,
    offline_fake: OfflineOption = False,
    json_output: JsonOption = False,
) -> None:
    """List built-in and persisted Agent declarations."""

    try:
        specs = asyncio.run(_list_agents(database_url, config, offline_fake))
    except (AgentCellError, OSError, SQLAlchemyError, ValueError) as error:
        _raise_cli_error(error)
    if json_output:
        console.print_json(json.dumps([item.model_dump(mode="json") for item in specs]))
    else:
        for spec in specs:
            console.print(f"{spec.id}\t{spec.model_ref}\t{spec.name}")


@tools_app.command("list")
def tools_list_command(
    database_url: DatabaseOption = None,
    config: ConfigOption = _DEFAULT_CONFIG,
    offline_fake: OfflineOption = False,
    json_output: JsonOption = False,
) -> None:
    """List tool schemas and safety policies."""

    try:
        definitions = asyncio.run(_list_tools(database_url, config, offline_fake))
    except (AgentCellError, OSError, SQLAlchemyError, ValueError) as error:
        _raise_cli_error(error)
    values = [
        {
            "name": item.name,
            "description": item.description,
            "parameters": item.params_model.model_json_schema(),
            "policy": item.policy.model_dump(mode="json"),
        }
        for item in definitions
    ]
    if json_output:
        console.print_json(json.dumps(values))
    else:
        for item in definitions:
            console.print(f"{item.name}\t{item.policy.risk.value}\t{item.description}")


@memory_app.command("search")
def memory_search_command(
    query: Annotated[str, typer.Argument(help="FTS query.")],
    user_id: Annotated[UUID, typer.Option("--user-id")],
    project_id: Annotated[str, typer.Option("--project-id")],
    agent_id: Annotated[str | None, typer.Option("--agent-id")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 20,
    database_url: DatabaseOption = None,
    config: ConfigOption = _DEFAULT_CONFIG,
    offline_fake: OfflineOption = False,
    json_output: JsonOption = False,
) -> None:
    """Search memory within an explicit user/project/Agent scope."""

    try:
        results = asyncio.run(
            _search_memory(
                query,
                scope=MemoryScope(
                    user_id=user_id,
                    project_id=project_id,
                    agent_id=agent_id,
                ),
                limit=limit,
                database_url=database_url,
                config=config,
                offline_fake=offline_fake,
            )
        )
    except (AgentCellError, OSError, SQLAlchemyError, ValueError) as error:
        _raise_cli_error(error)
    if json_output:
        console.print_json(json.dumps([item.model_dump(mode="json") for item in results]))
    else:
        for item in results:
            console.print(f"{item.score:.3f}\t{item.item.id}\t{item.item.content}")


@changes_app.command("list")
def changes_list_command(
    run_id: Annotated[UUID, typer.Option("--run", help="Run whose changes are listed.")],
    database_url: DatabaseOption = None,
    json_output: JsonOption = False,
) -> None:
    """List durable FileChange records for one Run."""

    try:
        values = asyncio.run(_list_changes(run_id, database_url))
    except (AgentCellError, OSError, SQLAlchemyError, ValueError) as error:
        _raise_cli_error(error)
    if json_output:
        console.print_json(json.dumps([item.model_dump(mode="json") for item in values]))
        return
    for item in values:
        console.print(f"{item.id} {item.status.value:10} {item.operation.value:9} {item.path}")


@changes_app.command("show")
def changes_show_command(
    change_id: Annotated[UUID, typer.Argument(help="FileChange to inspect.")],
    database_url: DatabaseOption = None,
    json_output: JsonOption = False,
) -> None:
    """Show one durable FileChange and its ChangeSet metadata."""

    try:
        details = asyncio.run(_change_details(change_id, database_url))
    except (AgentCellError, OSError, SQLAlchemyError, ValueError) as error:
        _raise_cli_error(error)
    if json_output:
        console.print_json(details.model_dump_json())
        return
    console.print(
        f"change_id={details.change.id} run_id={details.change.run_id} "
        f"status={details.change.status.value} path={details.change.path}"
    )
    console.print(details.diff)


@changes_app.command("diff")
def changes_diff_command(
    change_id: Annotated[UUID, typer.Argument(help="FileChange whose full Diff is printed.")],
    database_url: DatabaseOption = None,
) -> None:
    """Print the verified full Diff Artifact for one change."""

    try:
        details = asyncio.run(_change_details(change_id, database_url))
    except (AgentCellError, OSError, SQLAlchemyError, ValueError) as error:
        _raise_cli_error(error)
    console.print(details.diff, markup=False)


@changes_app.command("revert")
def changes_revert_command(
    change_id: Annotated[UUID, typer.Argument(help="Completed FileChange to reverse.")],
    yes: Annotated[bool, typer.Option("--yes", help="Confirm the displayed reverse Diff.")] = False,
    database_url: DatabaseOption = None,
) -> None:
    """Apply one hash-safe reverse change without invoking destructive Git commands."""

    try:
        reverse_diff = asyncio.run(_change_reverse_diff(change_id, database_url))
        console.print(reverse_diff, markup=False)
        if not yes and not typer.confirm("Apply the reverse change shown above?"):
            raise typer.Abort()
        value = asyncio.run(_revert_change(change_id, database_url))
    except (AgentCellError, OSError, SQLAlchemyError, ValueError) as error:
        _raise_cli_error(error)
    console.print(f"reverted change_id={change_id} reverse_change_id={value.id} path={value.path}")


@app.command("serve")
def serve_command(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8000,
    database_url: DatabaseOption = None,
    config: ConfigOption = _DEFAULT_CONFIG,
    offline_fake: OfflineOption = False,
) -> None:
    """Serve the FastAPI and AG-UI/SSE adapters with Uvicorn."""

    os.environ["AGENTCELL_CONFIG"] = str(config)
    if database_url is not None:
        os.environ["AGENTCELL_DATABASE_URL"] = database_url
    if offline_fake:
        os.environ["AGENTCELL_OFFLINE_FAKE"] = "1"
    uvicorn.run(create_app(), host=host, port=port)


async def _run_once(
    *,
    prompt: str,
    workspace: Path,
    agent_id: str,
    permission_mode: PermissionMode,
    allow_write: list[str] | None,
    allow_command: list[str] | None,
    stream: bool,
    json_events: bool,
    database_url: str | None,
    config: Path,
    model_ref: str | None,
    offline_fake: bool,
    max_requests: int | None,
    max_tool_calls: int | None,
    max_input_tokens: int | None,
    max_total_tokens: int | None,
) -> tuple[RunResult, bool]:
    application = await build_application(
        config=config,
        database_url=database_url,
        offline_fake=offline_fake,
        fake_output=f"Offline result: {prompt}",
        model_ref=model_ref,
    )
    try:
        request = RunRequest(
            prompt=prompt,
            workspace=workspace,
            agent_id=agent_id,
            lease=_build_cli_lease(agent_id, allow_write, allow_command),
            permission_mode=permission_mode,
        )
        overrides = {
            key: value
            for key, value in {
                "max_requests": max_requests,
                "max_tool_calls": max_tool_calls,
                "max_input_tokens": max_input_tokens,
                "max_total_tokens": max_total_tokens,
            }.items()
            if value is not None
        }
        if overrides:
            request = request.model_copy(
                update={"budget": request.budget.model_copy(update=overrides)}
            )
        renderer = CliEventRenderer(enabled=stream, json_events=json_events)
        result = await _await_with_events(
            application.runs.run(request),
            application=application,
            run_id=request.run_id,
            renderer=renderer,
        )
        while not result.run.status.is_terminal and result.approvals and console.is_terminal:
            decision = _prompt_approval(result.approvals[0])
            if decision is None:
                break
            result = await _await_with_events(
                application.runs.resume(result.approvals[0].id, decision),
                application=application,
                run_id=result.run.id,
                renderer=renderer,
            )
        renderer.finish()
        return result, renderer.text_streamed
    finally:
        await application.close()


async def _chat(
    *,
    workspace: Path,
    conversation_id: UUID | None,
    user_id: UUID | None,
    agent_id: str | None,
    permission_mode: PermissionMode,
    allow_write: list[str] | None,
    allow_command: list[str] | None,
    stream: bool,
    database_url: str | None,
    config: Path,
    model_ref: str | None,
    offline_fake: bool,
    max_requests: int | None,
    max_tool_calls: int | None,
    max_input_tokens: int | None,
    max_total_tokens: int | None,
) -> None:
    application = await build_application(
        config=config,
        database_url=database_url,
        offline_fake=offline_fake,
        fake_output="Offline chat response",
        model_ref=model_ref,
    )
    try:
        if conversation_id is None:
            conversation = await application.conversations.create(
                user_id=user_id or uuid4(),
                workspace=workspace,
                agent_id=agent_id or "coordinator",
            )
        else:
            conversation = await application.conversations.get(
                conversation_id,
                user_id=user_id,
            )
            if agent_id is not None and agent_id != conversation.agent_id:
                raise ValueError(
                    f"Conversation agent is {conversation.agent_id!r}; cannot continue as "
                    f"{agent_id!r}"
                )
        console.print(
            f"[dim]conversation_id={conversation.id} user_id={conversation.user_id}[/dim]"
        )
        console.print("[dim]Enter /exit to finish.[/dim]")
        while True:
            try:
                prompt = console.input("[bold cyan]you> [/bold cyan]").strip()
            except EOFError:
                break
            if not prompt:
                continue
            if prompt.casefold() in {"/exit", "/quit"}:
                break
            base = RunRequest(prompt=prompt, workspace=Path(conversation.workspace))
            overrides = {
                key: value
                for key, value in {
                    "max_requests": max_requests,
                    "max_tool_calls": max_tool_calls,
                    "max_input_tokens": max_input_tokens,
                    "max_total_tokens": max_total_tokens,
                }.items()
                if value is not None
            }
            budget = base.budget.model_copy(update=overrides) if overrides else base.budget
            turn_run_id = uuid4()
            renderer = CliEventRenderer(enabled=stream)
            result = await _await_with_events(
                application.conversations.run_turn(
                    conversation.id,
                    prompt=prompt,
                    user_id=conversation.user_id,
                    lease=_build_cli_lease(
                        conversation.agent_id,
                        allow_write,
                        allow_command,
                    ),
                    permission_mode=permission_mode,
                    budget=budget,
                    run_id=turn_run_id,
                ),
                application=application,
                run_id=turn_run_id,
                renderer=renderer,
            )
            while not result.run.status.is_terminal:
                if result.approvals:
                    approval = result.approvals[0]
                    approval_decision = _prompt_approval(approval)
                    if approval_decision is None:
                        console.print(
                            f"[dim]Resume pending Run with: uv run agentcell resume "
                            f"{result.run.id} --approval-id {approval.id} --decision approve[/dim]"
                        )
                        console.print("[dim]Continue with:[/dim]")
                        console.print(f"uv run agentcell chat --conversation-id {conversation.id}")
                        return
                    result = await _await_with_events(
                        application.runs.resume(
                            approval.id,
                            approval_decision,
                        ),
                        application=application,
                        run_id=result.run.id,
                        renderer=renderer,
                    )
                else:
                    result = await _await_with_events(
                        application.runs.resume_paused(result.run.id),
                        application=application,
                        run_id=result.run.id,
                        renderer=renderer,
                    )
                await application.conversations.record_if_managed(result)
            renderer.finish()
            if result.output and not renderer.text_streamed:
                console.print(f"[bold green]agent>[/bold green] {result.output}")
            usage = result.budget.used
            console.print(
                f"[dim]run_id={result.run.id} conversation_id={conversation.id} "
                f"status={result.run.status.value} "
                f"requests={usage.requests} tool_calls={usage.tool_calls} "
                f"tokens={usage.total_tokens}[/dim]"
            )
        console.print("[dim]Continue with:[/dim]")
        console.print(f"uv run agentcell chat --conversation-id {conversation.id}")
    finally:
        await application.close()


def _build_cli_lease(
    agent_id: str,
    allow_write: list[str] | None,
    allow_command: list[str] | None,
) -> CapabilityLease:
    """Build a normalized least-authority lease without expanding the AgentSpec."""

    values: dict[str, object] = {"filesystem_read": (".",)}
    if agent_id == "coordinator":
        values.update({"can_delegate": True, "max_child_depth": 2})
    if agent_id == "coder":
        values["filesystem_write"] = tuple(allow_write or (".",))
        values["commands"] = frozenset(allow_command or ())
    elif allow_write:
        values["filesystem_write"] = tuple(allow_write)
    if agent_id != "coder" and allow_command:
        values["commands"] = frozenset(allow_command)
    return CapabilityLease.model_validate(values)


def _prompt_approval(approval: Approval) -> ApprovalDecision | None:
    """Display the complete bounded approval envelope and return an explicit decision."""

    console.print(
        f"[yellow]Approval required[/yellow] agent={approval.agent_name} "
        f"provider={approval.provider}/{approval.model}"
    )
    console.print(f"tool={approval.tool_name} risk={approval.risk.value}")
    console.print(f"impact: {approval.impact}")
    console.print(
        f"idempotent={approval.idempotent} timeout={approval.timeout_seconds}s "
        f"remaining_tool_calls={approval.remaining_budget.remaining.tool_calls}"
    )
    console.print_json(json.dumps(approval.arguments, ensure_ascii=False))
    if approval.diff:
        console.print(approval.diff, markup=False)
    choice = (
        console.input(
            "[yellow][a]pprove [t] approve same tool this Run "
            "[m]odify [r]eject [q] leave pending > [/yellow]"
        )
        .strip()
        .casefold()
    )
    if choice in {"a", "approve", "y", "yes"}:
        return ApprovalDecision(kind=ApprovalDecisionKind.APPROVE)
    if choice in {"t", "temporary"}:
        return ApprovalDecision(kind=ApprovalDecisionKind.APPROVE, grant_same_tool=True)
    if choice in {"m", "modify"}:
        raw = console.input("[yellow]approved arguments as JSON> [/yellow]")
        value = TypeAdapter(dict[str, JsonValue]).validate_json(raw)
        return ApprovalDecision(kind=ApprovalDecisionKind.MODIFY, arguments=value)
    if choice in {"r", "reject", "n", "no"}:
        return ApprovalDecision(kind=ApprovalDecisionKind.REJECT)
    return None


async def _await_with_events(
    execution: Coroutine[Any, Any, RunResult],
    *,
    application: AgentCellApplication,
    run_id: UUID,
    renderer: CliEventRenderer,
) -> RunResult:
    """Await one execution while consuming its persisted event stream exactly once."""

    task = asyncio.create_task(execution)
    try:
        while True:
            try:
                events = await application.events(run_id, after_sequence=renderer.last_sequence)
            except RunNotFoundError:
                events = []
            for event in events:
                renderer.render(event)
            if task.done():
                return await task
            await asyncio.sleep(0.05)
    finally:
        try:
            events = await application.events(run_id, after_sequence=renderer.last_sequence)
        except RunNotFoundError:
            events = []
        for event in events:
            renderer.render(event)


async def _inspect_once(run_id: UUID, database_url: str | None) -> RunInspection:
    database = _database(database_url)
    try:
        async with database.session() as session:
            run = await RunRepository(session).get(run_id)
            if run is None:
                raise RunNotFoundError(str(run_id))
            events = await EventStore(session).list_for_run(run_id)
            approvals = await ApprovalRepository(session).list_for_run(run_id)
        return RunInspection(
            run=run,
            event_count=len(events),
            last_sequence=events[-1].sequence if events else 0,
            pending_approval_ids=tuple(
                item.id for item in approvals if item.status is ApprovalStatus.PENDING
            ),
        )
    finally:
        await database.dispose()


async def _resume_once(
    run_id: UUID,
    *,
    approval_id: UUID | None,
    decision: ApprovalDecisionKind,
    database_url: str | None,
    config: Path,
    offline_fake: bool,
) -> Run:
    application = await build_application(
        config=config,
        database_url=database_url,
        offline_fake=offline_fake,
    )
    try:
        if approval_id is None:
            result = await application.runs.resume_paused(run_id)
        else:
            result = await application.runs.resume(
                approval_id,
                ApprovalDecision(kind=decision),
            )
        await application.conversations.record_if_managed(result)
        return result.run
    finally:
        await application.close()


async def _list_agents(
    database_url: str | None,
    config: Path,
    offline_fake: bool,
) -> tuple[AgentSpec, ...]:
    application = await build_application(
        config=config,
        database_url=database_url,
        offline_fake=offline_fake,
    )
    try:
        return application.agents.list()
    finally:
        await application.close()


async def _list_tools(
    database_url: str | None,
    config: Path,
    offline_fake: bool,
) -> tuple[ToolDefinition[BaseModel], ...]:
    application = await build_application(
        config=config,
        database_url=database_url,
        offline_fake=offline_fake,
    )
    try:
        return application.tools.list()
    finally:
        await application.close()


async def _search_memory(
    query: str,
    *,
    scope: MemoryScope,
    limit: int,
    database_url: str | None,
    config: Path,
    offline_fake: bool,
) -> tuple[MemorySearchResult, ...]:
    application = await build_application(
        config=config,
        database_url=database_url,
        offline_fake=offline_fake,
    )
    try:
        return await application.memory.search(query, scope=scope, limit=limit)
    finally:
        await application.close()


async def _list_changes(run_id: UUID, database_url: str | None) -> tuple[FileChange, ...]:
    database = _database(database_url)
    service = ChangeService(database, FileArtifactStore(database, Path(".agentcell/artifacts")))
    try:
        return await service.list_for_run(run_id)
    finally:
        await database.dispose()


async def _change_details(change_id: UUID, database_url: str | None) -> ChangeDetails:
    database = _database(database_url)
    service = ChangeService(database, FileArtifactStore(database, Path(".agentcell/artifacts")))
    try:
        return await service.details(change_id)
    finally:
        await database.dispose()


async def _change_reverse_diff(change_id: UUID, database_url: str | None) -> str:
    database = _database(database_url)
    service = ChangeService(database, FileArtifactStore(database, Path(".agentcell/artifacts")))
    try:
        return await service.reverse_diff(change_id)
    finally:
        await database.dispose()


async def _revert_change(change_id: UUID, database_url: str | None) -> FileChange:
    database = _database(database_url)
    service = ChangeService(database, FileArtifactStore(database, Path(".agentcell/artifacts")))
    try:
        details = await service.details(change_id)
        return await service.revert(
            change_id,
            workspace=Path(details.change_set.workspace),
            lease=CapabilityLease(filesystem_read=(".",), filesystem_write=(".",)),
            events=RunEventRecorder(database, details.change.run_id),
        )
    finally:
        await database.dispose()


async def _replay_once(
    run_id: UUID,
    through_sequence: int | None,
    database_url: str | None,
) -> ReplayState:
    database = _database(database_url)
    try:
        return await ReplayService(database).replay(
            run_id,
            through_sequence=through_sequence,
        )
    finally:
        await database.dispose()


async def _branch_once(
    run_id: UUID,
    from_sequence: int,
    database_url: str | None,
) -> Run:
    database = _database(database_url)
    try:
        return await ReplayService(database).branch(run_id, from_sequence=from_sequence)
    finally:
        await database.dispose()


async def _cancel_once(run_id: UUID, database_url: str | None) -> Run:
    database = _database(database_url)
    spec = FakeModelSpec(model="agentcell-control-fake")
    providers = ProviderFactory(
        {"offline_fake": spec},
        adapters=(
            FakeProviderAdapter({spec.model: FakeScript(steps=(FakeTextStep(text="control"),))}),
        ),
    )
    agents = AgentRegistry((coordinator_spec(model_ref="offline_fake"),))
    tools = ToolRegistry()
    register_workspace_tools(tools)
    service = RunService(
        database=database,
        providers=providers,
        agents=agents,
        tools=tools,
    )
    try:
        run = await service.cancel(run_id)
        async with database.transaction() as session:
            conversation = await ConversationRepository(session).get(run.conversation_id)
            if conversation is not None:
                await ConversationRepository(session).release(run.conversation_id, run.id)
        return run
    finally:
        await providers.aclose()
        await database.dispose()


def _database(database_url: str | None) -> Database:
    url = database_url or os.getenv("AGENTCELL_DATABASE_URL")
    return Database(url) if url else Database.from_path(Path(".agentcell/agentcell.db"))


def _raise_cli_error(error: Exception) -> NoReturn:
    console.print(f"[red]Command failed:[/red] {error}")
    if isinstance(error, SQLAlchemyError):
        console.print("[dim]Apply migrations first: uv run alembic upgrade head[/dim]")
    raise typer.Exit(code=1) from error


def main() -> None:
    """Console-script entry point."""

    app()


if __name__ == "__main__":
    main()
