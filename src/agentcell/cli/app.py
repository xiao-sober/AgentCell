"""Typer CLI that invokes RunService directly without a local HTTP hop."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4

import typer
import uvicorn
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from agentcell.agents import (
    AgentRegistry,
    AgentSpec,
    DelegationStatus,
    HandoffResult,
    RegisteredAgent,
    coordinator_spec,
)
from agentcell.api import create_app
from agentcell.application import AgentCellApplication, build_application
from agentcell.budgets import BudgetTracker
from agentcell.cli.approvals import prompt_approval, resume_decision
from agentcell.cli.changes import changes_app
from agentcell.cli.common import (
    console,
)
from agentcell.cli.common import (
    database as _database,
)
from agentcell.cli.common import (
    raise_cli_error as _raise_cli_error,
)
from agentcell.cli.display import CliEventRenderer
from agentcell.cli.profile import CliRunProfile, CliTaskProfile, CliTeamProfile, CommandProfile
from agentcell.conversations import ConversationRoutingMode
from agentcell.errors import AgentCellError, ConversationModelBindingError, RunNotFoundError
from agentcell.kernel.checkpoint import CheckpointKind
from agentcell.kernel.models import Run
from agentcell.kernel.replay import ReplayService, ReplayState
from agentcell.kernel.run_service import RunRequest, RunResult, RunService
from agentcell.memory import MemoryScope, MemorySearchResult
from agentcell.policy import (
    ApprovalDecision,
    ApprovalDecisionKind,
    ApprovalStatus,
    Capability,
    PermissionMode,
)
from agentcell.providers import (
    FakeModelSpec,
    FakeProviderAdapter,
    FakeScript,
    FakeTextStep,
    ProviderFactory,
)
from agentcell.routing import (
    TASK_ROUTER_AGENT_ID,
    TaskExecutionResult,
    TaskRouteDecision,
    TaskRouteRequest,
    TaskRouteStatus,
    deterministic_route,
)
from agentcell.storage import (
    ApprovalRepository,
    CheckpointRepository,
    ConversationRepository,
    EventStore,
    RunRepository,
)
from agentcell.tools import ToolDefinition, ToolRegistry, register_workspace_tools

app = typer.Typer(
    name="agentcell",
    help="Local-first Agent runtime.",
    no_args_is_help=True,
)
agents_app = typer.Typer(help="Inspect and manage Agent declarations.")
tools_app = typer.Typer(help="Inspect registered tools and policies.")
memory_app = typer.Typer(help="Search scoped long-term memory.")
app.add_typer(agents_app, name="agents")
app.add_typer(tools_app, name="tools")
app.add_typer(memory_app, name="memory")
app.add_typer(changes_app, name="changes")
_DEFAULT_WORKSPACE = Path.cwd()
_DEFAULT_CONFIG = Path("agentcell.toml")

PromptArgument = Annotated[str, typer.Argument(help="Task for the selected Agent or Team.")]
WorkspaceOption = Annotated[
    Path,
    typer.Option("--workspace", "-w", help="Workspace root constrained by the Run lease."),
]
DatabaseOption = Annotated[
    str | None,
    typer.Option(
        "--database-url",
        help="Migrated SQLite aiosqlite URL.",
        rich_help_panel="Advanced",
    ),
]
ConfigOption = Annotated[
    Path,
    typer.Option(
        "--config",
        help="AgentCell TOML used outside offline Fake mode.",
        rich_help_panel="Advanced",
    ),
]
ModelOption = Annotated[
    str | None,
    typer.Option("--model-ref", help="Configured model reference."),
]
OfflineOption = Annotated[
    bool,
    typer.Option(
        "--offline-fake",
        help="Use a deterministic offline Fake Provider.",
        rich_help_panel="Advanced",
    ),
]
JsonOption = Annotated[
    bool,
    typer.Option("--json", help="Write the structured RunResult as JSON."),
]
JsonEventsOption = Annotated[
    bool,
    typer.Option("--json-events", help="Write persisted Run events as NDJSON without ANSI."),
]
DryRouteOption = Annotated[
    bool,
    typer.Option(
        "--dry-route",
        help="Preview routing without creating a Run or executing tools.",
    ),
]
MaxRequestsOption = Annotated[
    int | None,
    typer.Option(
        "--max-requests",
        min=1,
        max=100,
        help="Override model requests (single Agent default: 10; software Team: 24).",
        rich_help_panel="Budget",
    ),
]
MaxToolCallsOption = Annotated[
    int | None,
    typer.Option(
        "--max-tool-calls",
        min=1,
        max=1000,
        help="Override tool calls (single Agent default: 20; software Team: 48).",
        rich_help_panel="Budget",
    ),
]
MaxInputTokensOption = Annotated[
    int | None,
    typer.Option(
        "--max-input-tokens",
        min=1,
        max=2_000_000,
        help="Override cumulative Run input-token budget (default: 200000).",
        rich_help_panel="Budget",
    ),
]
MaxTotalTokensOption = Annotated[
    int | None,
    typer.Option(
        "--max-total-tokens",
        min=1,
        max=2_000_000,
        help="Override cumulative Run total-token budget (default: 240000).",
        rich_help_panel="Budget",
    ),
]
AgentOption = Annotated[
    str | None,
    typer.Option("--agent", help="Built-in or registered Agent id."),
]
TeamOption = Annotated[
    str | None,
    typer.Option("--team", help="Versioned deterministic Team id (for example: software)."),
]
ApprovalModeOption = Annotated[
    PermissionMode | None,
    typer.Option(
        "--approval-mode",
        help="Approval policy: request, auto (guarded only), or full (leased operations).",
    ),
]
LegacyPermissionModeOption = Annotated[
    PermissionMode | None,
    typer.Option("--permission-mode", hidden=True),
]
WriteScopeOption = Annotated[
    list[str] | None,
    typer.Option(
        "--write-scope",
        help="Narrow writable paths; coder defaults to the workspace root.",
        rich_help_panel="Capabilities",
    ),
]
LegacyAllowWriteOption = Annotated[
    list[str] | None,
    typer.Option("--allow-write", hidden=True),
]
CommandOption = Annotated[
    list[str] | None,
    typer.Option(
        "--command",
        help="Exact executable name allowed by the Run lease; repeat as needed.",
        rich_help_panel="Capabilities",
    ),
]
LegacyAllowCommandOption = Annotated[
    list[str] | None,
    typer.Option("--allow-command", hidden=True),
]
CommandProfileOption = Annotated[
    list[CommandProfile] | None,
    typer.Option(
        "--command-profile",
        help="Named exact command profile: pytest, ruff, or pyright.",
        rich_help_panel="Capabilities",
    ),
]
NetworkDomainOption = Annotated[
    list[str] | None,
    typer.Option(
        "--network-domain",
        help="Approved HTTPS domain for a network-capable Agent; repeat as needed.",
        rich_help_panel="Capabilities",
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


@app.callback()
def root() -> None:
    """Select an AgentCell command."""


@app.command("run")
def run_command(
    prompt: PromptArgument,
    workspace: WorkspaceOption = _DEFAULT_WORKSPACE,
    agent_id: AgentOption = None,
    team_id: TeamOption = None,
    approval_mode: ApprovalModeOption = None,
    write_scope: WriteScopeOption = None,
    command_profile: CommandProfileOption = None,
    command: CommandOption = None,
    network_domain: NetworkDomainOption = None,
    permission_mode: LegacyPermissionModeOption = None,
    allow_write: LegacyAllowWriteOption = None,
    allow_command: LegacyAllowCommandOption = None,
    database_url: DatabaseOption = None,
    config: ConfigOption = _DEFAULT_CONFIG,
    model_ref: ModelOption = None,
    offline_fake: OfflineOption = False,
    json_output: JsonOption = False,
    json_events: JsonEventsOption = False,
    dry_route: DryRouteOption = False,
    stream: StreamOption = True,
    max_requests: MaxRequestsOption = None,
    max_tool_calls: MaxToolCallsOption = None,
    max_input_tokens: MaxInputTokensOption = None,
    max_total_tokens: MaxTotalTokensOption = None,
) -> None:
    """Execute one Run in-process and persist its complete event history."""

    run_id = uuid4()
    conversation_id = uuid4()
    user_id = uuid4()
    try:
        if json_output and json_events:
            raise ValueError("--json and --json-events are mutually exclusive")
        if dry_route and json_events:
            raise ValueError("--dry-route and --json-events are mutually exclusive")
        if agent_id is not None and team_id is not None:
            raise ValueError("--agent and --team are mutually exclusive")
        if dry_route:
            decision = asyncio.run(
                _preview_route_once(
                    prompt=prompt,
                    workspace=workspace,
                    agent_id=agent_id,
                    team_id=team_id,
                    approval_mode=approval_mode,
                    permission_mode=permission_mode,
                    write_scope=write_scope,
                    allow_write=allow_write,
                    command_profile=command_profile,
                    command=command,
                    allow_command=allow_command,
                    network_domain=network_domain,
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
            if json_output:
                console.print_json(decision.model_dump_json())
            else:
                console.print(
                    f"route={decision.mode.value}:{decision.target_id} "
                    f"source={decision.source.value} status={decision.status.value} "
                    f"confidence={decision.confidence:.2f} authoritative=false"
                )
                console.print(decision.reason_summary)
            return
        result, text_streamed = asyncio.run(
            _run_once(
                prompt=prompt,
                workspace=workspace,
                agent_id=agent_id,
                team_id=team_id,
                approval_mode=approval_mode,
                permission_mode=permission_mode,
                write_scope=write_scope,
                allow_write=allow_write,
                command_profile=command_profile,
                command=command,
                allow_command=allow_command,
                network_domain=network_domain,
                stream=(stream and not json_output) or json_events,
                json_events=json_events,
                show_deprecations=not json_output and not json_events,
                database_url=database_url,
                config=config,
                model_ref=model_ref,
                offline_fake=offline_fake,
                max_requests=max_requests,
                max_tool_calls=max_tool_calls,
                max_input_tokens=max_input_tokens,
                max_total_tokens=max_total_tokens,
                run_id=run_id,
                conversation_id=conversation_id,
                user_id=user_id,
            )
        )
    except KeyboardInterrupt as error:
        machine_output = json_output or json_events
        if not machine_output:
            console.print("[yellow]Run cancelled.[/yellow]")
        _print_run_failure(
            run_id=run_id,
            conversation_id=conversation_id,
            status="cancelled",
            error_code="run_cancelled",
            json_output=machine_output,
        )
        raise typer.Exit(code=130) from error
    except (AgentCellError, OSError, SQLAlchemyError, ValueError) as error:
        machine_output = json_output or json_events
        if not machine_output:
            console.print(f"[red]Run failed:[/red] {error}")
            if isinstance(error, SQLAlchemyError):
                console.print("[dim]Apply migrations first: uv run alembic upgrade head[/dim]")
        _print_run_failure(
            run_id=run_id,
            conversation_id=conversation_id,
            status=_persisted_status(run_id, database_url) or "failed",
            error_code=_cli_error_code(error),
            json_output=machine_output,
        )
        raise typer.Exit(code=1) from error

    if json_output:
        console.print_json(result.model_dump_json())
        if (
            isinstance(result, HandoffResult)
            and result.status is DelegationStatus.FAILED
            or isinstance(result, TaskExecutionResult)
            and result.run.status.value == "failed"
        ):
            raise typer.Exit(code=1)
        return
    if json_events:
        if (
            isinstance(result, HandoffResult)
            and result.status is DelegationStatus.FAILED
            or isinstance(result, TaskExecutionResult)
            and result.run.status.value == "failed"
        ):
            raise typer.Exit(code=1)
        return
    if isinstance(result, TaskExecutionResult):
        if result.output is not None and not text_streamed:
            console.print(result.output)
        for approval in result.approvals:
            console.print(
                f"[yellow]approval required[/yellow] id={approval.id} "
                f"child_run_id={approval.run_id} tool={approval.tool_name}"
            )
            console.print(
                f"[dim]Resume with: uv run agentcell resume {result.run.id} "
                f"--approval-id {approval.id} --decision approve[/dim]"
            )
        usage = result.budget.used
        child_ids = ",".join(str(item) for item in result.child_run_ids)
        target = result.decision.target_id
        console.print(
            f"[dim]root_run_id={result.run.id} child_run_ids={child_ids} "
            f"conversation_id={result.run.conversation_id} "
            f"route={result.decision.mode.value}:{target} "
            f"source={result.decision.source.value} confidence={result.decision.confidence:.2f} "
            f"status={result.run.status.value} requests={usage.requests} "
            f"tool_calls={usage.tool_calls} tokens={usage.total_tokens}[/dim]"
        )
        cache_hit_ratio = (
            0.0
            if usage.input_tokens == 0
            else min(1.0, usage.cache_read_tokens / usage.input_tokens)
        )
        console.print(
            f"[dim]tokens input={usage.input_tokens} output={usage.output_tokens} "
            f"total={usage.total_tokens} cache_read={usage.cache_read_tokens} "
            f"cache_write={usage.cache_write_tokens} cache_hit={cache_hit_ratio:.1%}[/dim]"
        )
        if result.decision.capability_gaps:
            gaps = ", ".join(item.value for item in result.decision.capability_gaps)
            console.print(f"[yellow]Route confirmation required:[/yellow] missing {gaps}")
        for issue in result.decision.issues:
            console.print(f"[yellow]route issue[/yellow] code={issue.code.value} {issue.message}")
        if result.run.status.value == "failed":
            raise typer.Exit(code=1)
        return
    if isinstance(result, HandoffResult):
        if result.output is not None and not text_streamed:
            console.print(result.output)
        for stage in result.stages:
            for approval_id in stage.approval_ids:
                console.print(
                    f"[yellow]approval required[/yellow] id={approval_id} "
                    f"child_run_id={stage.child_run_id} stage={stage.agent_id}"
                )
                console.print(
                    f"[dim]Resume with: uv run agentcell resume {result.root_run_id} "
                    f"--approval-id {approval_id} --decision approve[/dim]"
                )
        usage = result.budget.used
        child_ids = ",".join(str(stage.child_run_id) for stage in result.stages)
        console.print(
            f"[dim]root_run_id={result.root_run_id} child_run_ids={child_ids} "
            f"conversation_id={result.conversation_id} "
            f"team={result.team_id}@v{result.team_version} "
            f"status={result.status.value} requests={usage.requests} "
            f"tool_calls={usage.tool_calls} tokens={usage.total_tokens}[/dim]"
        )
        if result.status is DelegationStatus.FAILED:
            stage = result.error_stage.value if result.error_stage is not None else "unknown"
            console.print(
                f"[red]Team failed:[/red] stage={stage} "
                f"code={result.error_code or 'handoff_failed'} "
                f"message={result.error_message or 'Team execution failed'}"
            )
            raise typer.Exit(code=1)
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
    team_id: TeamOption = None,
    approval_mode: ApprovalModeOption = None,
    write_scope: WriteScopeOption = None,
    command_profile: CommandProfileOption = None,
    command: CommandOption = None,
    network_domain: NetworkDomainOption = None,
    permission_mode: LegacyPermissionModeOption = None,
    allow_write: LegacyAllowWriteOption = None,
    allow_command: LegacyAllowCommandOption = None,
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
                team_id=team_id,
                approval_mode=approval_mode,
                permission_mode=permission_mode,
                write_scope=write_scope,
                allow_write=allow_write,
                command_profile=command_profile,
                command=command,
                allow_command=allow_command,
                network_domain=network_domain,
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
        ApprovalDecisionKind | None,
        typer.Option("--decision", help="Explicit approval decision."),
    ] = None,
    arguments_json: Annotated[
        str | None,
        typer.Option("--arguments-json", help="Replacement arguments for --decision modify."),
    ] = None,
    grant_same_tool: Annotated[
        bool,
        typer.Option("--grant-same-tool", help="Approve this tool for the remainder of the Run."),
    ] = False,
    database_url: DatabaseOption = None,
    config: ConfigOption = _DEFAULT_CONFIG,
    offline_fake: OfflineOption = False,
    json_output: JsonOption = False,
) -> None:
    """Resume a delegated Run or decide a pending approval in-process."""

    try:
        approval_decision = resume_decision(
            approval_id=approval_id,
            decision=decision,
            arguments_json=arguments_json,
            grant_same_tool=grant_same_tool,
        )
        resumed = asyncio.run(
            _resume_once(
                run_id,
                approval_id=approval_id,
                decision=approval_decision,
                database_url=database_url,
                config=config,
                offline_fake=offline_fake,
            )
        )
    except (AgentCellError, OSError, SQLAlchemyError, ValueError) as error:
        _raise_cli_error(error)
    if json_output:
        console.print_json(resumed.model_dump_json())
        if (
            isinstance(resumed, HandoffResult)
            and resumed.status is DelegationStatus.FAILED
            or isinstance(resumed, TaskExecutionResult)
            and resumed.run.status.value == "failed"
        ):
            raise typer.Exit(code=1)
    elif isinstance(resumed, TaskExecutionResult):
        child_ids = ",".join(str(item) for item in resumed.child_run_ids)
        console.print(
            f"root_run_id={resumed.run.id} child_run_ids={child_ids} "
            f"route={resumed.decision.mode.value}:{resumed.decision.target_id} "
            f"status={resumed.run.status.value}"
        )
        if resumed.output:
            console.print(resumed.output)
        if resumed.run.status.value == "failed":
            raise typer.Exit(code=1)
    elif isinstance(resumed, HandoffResult):
        child_ids = ",".join(str(item.child_run_id) for item in resumed.stages)
        console.print(
            f"root_run_id={resumed.root_run_id} child_run_ids={child_ids} "
            f"status={resumed.status.value}"
        )
        if resumed.status is DelegationStatus.FAILED:
            stage = resumed.error_stage.value if resumed.error_stage is not None else "unknown"
            console.print(
                f"[red]Team failed:[/red] stage={stage} "
                f"code={resumed.error_code or 'handoff_failed'} "
                f"message={resumed.error_message or 'Team execution failed'}"
            )
            raise typer.Exit(code=1)
    else:
        console.print(f"run_id={resumed.id} status={resumed.status.value}")


@agents_app.command("list")
def agents_list_command(
    database_url: DatabaseOption = None,
    config: ConfigOption = _DEFAULT_CONFIG,
    offline_fake: OfflineOption = False,
    all_agents: Annotated[
        bool,
        typer.Option("--all", help="Include internal runtime roles."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show full Agent limits and declarations."),
    ] = False,
    json_output: JsonOption = False,
) -> None:
    """List built-in and persisted Agent declarations."""

    try:
        entries = asyncio.run(
            _list_agents(
                database_url,
                config,
                offline_fake,
                include_internal=all_agents or json_output,
            )
        )
    except (AgentCellError, OSError, SQLAlchemyError, ValueError) as error:
        _raise_cli_error(error)
    if json_output:
        values: list[dict[str, object]] = []
        for entry in entries:
            value = entry.spec.model_dump(mode="json")
            value.update(
                {
                    "source": entry.source.value,
                    "visibility": entry.visibility.value,
                    "status": entry.status,
                    "configured_model_ref": entry.spec.model_ref,
                    "access": _agent_access(entry.spec),
                }
            )
            values.append(value)
        console.print_json(json.dumps(values))
    else:
        for entry in entries:
            spec = entry.spec
            console.print(
                f"{spec.id}\t{entry.source.value}\tconfigured={spec.model_ref}\t"
                f"access={_agent_access(spec)}\t{entry.status}"
            )
            if verbose:
                console.print(
                    f"  name={spec.name} visibility={entry.visibility.value} "
                    f"max_steps={spec.max_steps} max_children={spec.max_children} "
                    f"max_depth={spec.max_depth}"
                )
                console.print(f"  tools={','.join(spec.tools) or '-'}")
                console.print(
                    "  capabilities="
                    + (",".join(sorted(item.value for item in spec.capabilities)) or "-")
                )


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


async def _preview_route_once(
    *,
    prompt: str,
    workspace: Path,
    agent_id: str | None,
    team_id: str | None,
    approval_mode: PermissionMode | None,
    permission_mode: PermissionMode | None,
    write_scope: list[str] | None,
    allow_write: list[str] | None,
    command_profile: list[CommandProfile] | None,
    command: list[str] | None,
    allow_command: list[str] | None,
    network_domain: list[str] | None,
    database_url: str | None,
    config: Path,
    model_ref: str | None,
    offline_fake: bool,
    max_requests: int | None,
    max_tool_calls: int | None,
    max_input_tokens: int | None,
    max_total_tokens: int | None,
) -> TaskRouteDecision:
    application = await build_application(
        config=config,
        database_url=database_url,
        offline_fake=offline_fake,
        model_ref=model_ref,
    )
    try:
        profile = CliTaskProfile.resolve(
            application.teams.get("software"),
            approval_mode=approval_mode,
            permission_mode=permission_mode,
            write_scopes=write_scope,
            legacy_write_scopes=allow_write,
            commands=command,
            legacy_commands=allow_command,
            command_profiles=command_profile,
            network_domains=network_domain,
            max_requests=max_requests,
            max_tool_calls=max_tool_calls,
            max_input_tokens=max_input_tokens,
            max_total_tokens=max_total_tokens,
        )
        return await application.routing.preview(
            TaskRouteRequest(
                task=prompt,
                workspace=workspace,
                lease=profile.lease,
                permission_mode=profile.approval_mode,
                budget=profile.budget,
                model_ref=model_ref,
                agent_id=agent_id,
                team_id=team_id,
            )
        )
    finally:
        await application.close()


async def _execute_cli_route(
    application: AgentCellApplication,
    request: TaskRouteRequest,
    *,
    stream: bool,
    json_events: bool,
) -> tuple[TaskExecutionResult, bool]:
    renderer = CliEventRenderer(
        enabled=stream,
        json_events=json_events,
        output=console,
    )
    prepared = await _await_with_events(
        application.routing.prepare(request),
        application=application,
        run_id=request.root_run_id,
        renderer=renderer,
    )
    if (
        prepared.decision.status is TaskRouteStatus.CONFIRMATION_REQUIRED
        and not prepared.decision.capability_gaps
        and console.is_terminal
        and typer.confirm(
            f"Use {prepared.decision.mode.value} {prepared.decision.target_id!r}?",
            default=False,
        )
    ):
        prepared = await _await_with_events(
            application.routing.confirm(
                request.root_run_id,
                decision_hash=str(prepared.decision.decision_hash),
            ),
            application=application,
            run_id=request.root_run_id,
            renderer=renderer,
        )
    if prepared.decision.status is not TaskRouteStatus.READY:
        renderer.finish()
        return TaskExecutionResult(
            run=prepared.root,
            decision=prepared.decision,
            budget=BudgetTracker(
                request.budget,
                initial_usage=prepared.decision.routing_usage,
            ).snapshot(),
        ), renderer.text_streamed
    result = await _await_with_events(
        application.routing.execute(prepared),
        application=application,
        run_id=request.root_run_id,
        renderer=renderer,
    )
    while not result.run.status.is_terminal and result.approvals and console.is_terminal:
        decision = prompt_approval(
            result.approvals[0],
            output=console,
            renderer=renderer,
        )
        if decision is None:
            break
        result = await _await_with_events(
            application.routing.decide_approval(
                result.run.id,
                result.approvals[0].id,
                decision,
            ),
            application=application,
            run_id=result.run.id,
            renderer=renderer,
        )
    renderer.finish()
    return result, renderer.text_streamed


async def _run_once(
    *,
    prompt: str,
    workspace: Path,
    agent_id: str | None,
    team_id: str | None,
    approval_mode: PermissionMode | None,
    permission_mode: PermissionMode | None,
    write_scope: list[str] | None,
    allow_write: list[str] | None,
    command_profile: list[CommandProfile] | None,
    command: list[str] | None,
    allow_command: list[str] | None,
    network_domain: list[str] | None,
    stream: bool,
    json_events: bool,
    show_deprecations: bool,
    database_url: str | None,
    config: Path,
    model_ref: str | None,
    offline_fake: bool,
    max_requests: int | None,
    max_tool_calls: int | None,
    max_input_tokens: int | None,
    max_total_tokens: int | None,
    run_id: UUID,
    conversation_id: UUID,
    user_id: UUID,
) -> tuple[RunResult | HandoffResult | TaskExecutionResult, bool]:
    application = await build_application(
        config=config,
        database_url=database_url,
        offline_fake=offline_fake,
        fake_output=(
            f"PASS\nOffline result: {prompt}"
            if team_id is not None
            or (agent_id is None and deterministic_route(prompt).target_id == "software")
            else f"Offline result: {prompt}"
        ),
        model_ref=model_ref,
    )
    try:
        if agent_id is None and team_id is None:
            profile = CliTaskProfile.resolve(
                application.teams.get("software"),
                approval_mode=approval_mode,
                permission_mode=permission_mode,
                write_scopes=write_scope,
                legacy_write_scopes=allow_write,
                commands=command,
                legacy_commands=allow_command,
                command_profiles=command_profile,
                network_domains=network_domain,
                max_requests=max_requests,
                max_tool_calls=max_tool_calls,
                max_input_tokens=max_input_tokens,
                max_total_tokens=max_total_tokens,
            )
            if show_deprecations:
                for message in profile.deprecation_messages():
                    console.print(f"[yellow]Deprecated:[/yellow] {message}")
            route_request = TaskRouteRequest(
                task=prompt,
                workspace=workspace,
                lease=profile.lease,
                permission_mode=profile.approval_mode,
                budget=profile.budget,
                model_ref=model_ref,
                root_run_id=run_id,
                conversation_id=conversation_id,
                user_id=user_id,
            )
            return await _execute_cli_route(
                application,
                route_request,
                stream=stream,
                json_events=json_events,
            )

        if team_id is not None:
            team = application.teams.get(team_id)
            team_profile = CliTeamProfile.resolve(
                team,
                application.agents,
                approval_mode=approval_mode,
                permission_mode=permission_mode,
                write_scopes=write_scope,
                legacy_write_scopes=allow_write,
                commands=command,
                legacy_commands=allow_command,
                command_profiles=command_profile,
                network_domains=network_domain,
                max_requests=max_requests,
                max_tool_calls=max_tool_calls,
                max_input_tokens=max_input_tokens,
                max_total_tokens=max_total_tokens,
            )
            if show_deprecations:
                for message in team_profile.deprecation_messages():
                    console.print(f"[yellow]Deprecated:[/yellow] {message}")
            route_request = TaskRouteRequest(
                task=prompt,
                workspace=workspace,
                root_run_id=run_id,
                user_id=user_id,
                conversation_id=conversation_id,
                team_id=team_id,
                model_ref=model_ref,
                permission_mode=team_profile.approval_mode,
                lease=team_profile.lease,
                budget=team_profile.budget,
            )
            return await _execute_cli_route(
                application,
                route_request,
                stream=stream,
                json_events=json_events,
            )

        selected_agent_id = agent_id or "coordinator"
        profile = CliRunProfile.resolve(
            application.agents.get(selected_agent_id),
            approval_mode=approval_mode,
            permission_mode=permission_mode,
            write_scopes=write_scope,
            legacy_write_scopes=allow_write,
            commands=command,
            legacy_commands=allow_command,
            command_profiles=command_profile,
            network_domains=network_domain,
        )
        if show_deprecations:
            for message in profile.deprecation_messages():
                console.print(f"[yellow]Deprecated:[/yellow] {message}")
        request = RunRequest(
            prompt=prompt,
            workspace=workspace,
            agent_id=selected_agent_id,
            lease=profile.lease,
            permission_mode=profile.approval_mode,
            run_id=run_id,
            conversation_id=conversation_id,
            user_id=user_id,
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
        request = request.model_copy(
            update={"budget": request.budget.model_copy(update={"max_children": 1, "max_depth": 1})}
        )
        return await _execute_cli_route(
            application,
            TaskRouteRequest(
                task=prompt,
                workspace=workspace,
                root_run_id=run_id,
                conversation_id=conversation_id,
                user_id=user_id,
                agent_id=selected_agent_id,
                model_ref=model_ref,
                lease=profile.lease,
                permission_mode=profile.approval_mode,
                budget=request.budget,
            ),
            stream=stream,
            json_events=json_events,
        )
    finally:
        await application.close()


async def _chat(
    *,
    workspace: Path,
    conversation_id: UUID | None,
    user_id: UUID | None,
    agent_id: str | None,
    team_id: str | None,
    approval_mode: PermissionMode | None,
    permission_mode: PermissionMode | None,
    write_scope: list[str] | None,
    allow_write: list[str] | None,
    command_profile: list[CommandProfile] | None,
    command: list[str] | None,
    allow_command: list[str] | None,
    network_domain: list[str] | None,
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
        if agent_id is not None and team_id is not None:
            raise ValueError("--agent and --team are mutually exclusive")
        if conversation_id is None:
            conversation = await application.conversations.create(
                user_id=user_id or uuid4(),
                workspace=workspace,
                agent_id=agent_id or "coordinator",
                routing_mode=(
                    ConversationRoutingMode.AUTO
                    if agent_id is None and team_id is None
                    else ConversationRoutingMode.FIXED
                ),
                team_id=team_id,
                model_ref=model_ref,
            )
        else:
            conversation = await application.conversations.get(
                conversation_id,
                user_id=user_id,
            )
            if agent_id is not None and agent_id != conversation.agent_id:
                if conversation.routing_mode is ConversationRoutingMode.FIXED:
                    raise ValueError(
                        f"Conversation agent is {conversation.agent_id!r}; cannot continue as "
                        f"{agent_id!r}"
                    )
            if team_id is not None and conversation.routing_mode is ConversationRoutingMode.FIXED:
                if team_id != conversation.team_id:
                    raise ValueError(
                        f"Conversation team is {conversation.team_id!r}; cannot continue as "
                        f"{team_id!r}"
                    )
            if conversation.model_ref is None and model_ref is None:
                raise ConversationModelBindingError(
                    "Legacy Conversation has no recoverable model binding; "
                    "continue once with --model-ref"
                )
            if (
                conversation.model_ref is not None
                and model_ref is not None
                and model_ref != conversation.model_ref
            ):
                raise ConversationModelBindingError(
                    f"Conversation model is {conversation.model_ref!r}; cannot continue as "
                    f"{model_ref!r}"
                )
        routed = (
            conversation.routing_mode is ConversationRoutingMode.AUTO
            or conversation.team_id is not None
        )
        if routed:
            profile = CliTaskProfile.resolve(
                application.teams.get(conversation.team_id or "software"),
                approval_mode=approval_mode,
                permission_mode=permission_mode,
                write_scopes=write_scope,
                legacy_write_scopes=allow_write,
                commands=command,
                legacy_commands=allow_command,
                command_profiles=command_profile,
                network_domains=network_domain,
                max_requests=max_requests,
                max_tool_calls=max_tool_calls,
                max_input_tokens=max_input_tokens,
                max_total_tokens=max_total_tokens,
            )
        else:
            profile = CliRunProfile.resolve(
                application.agents.get(conversation.agent_id),
                approval_mode=approval_mode,
                permission_mode=permission_mode,
                write_scopes=write_scope,
                legacy_write_scopes=allow_write,
                commands=command,
                legacy_commands=allow_command,
                command_profiles=command_profile,
                network_domains=network_domain,
            )
        for message in profile.deprecation_messages():
            console.print(f"[yellow]Deprecated:[/yellow] {message}")
        console.print(
            f"[dim]conversation_id={conversation.id} user_id={conversation.user_id} "
            f"routing={conversation.routing_mode.value} "
            f"model_ref={conversation.model_ref or model_ref}[/dim]"
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
            if isinstance(profile, CliTaskProfile):
                budget = profile.budget
            else:
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
            renderer = CliEventRenderer(enabled=stream, output=console)
            direct_turn = application.conversations.should_use_direct_turn(
                conversation,
                prompt=prompt,
                agent_id=agent_id,
                team_id=team_id,
            )
            turn_uses_router = routed and not direct_turn
            try:
                if direct_turn:
                    prepared_direct = await _await_with_events(
                        application.conversations.prepare_direct_turn(
                            conversation.id,
                            prompt=prompt,
                            user_id=conversation.user_id,
                            permission_mode=profile.approval_mode,
                            budget=budget,
                            model_ref=model_ref,
                            run_id=turn_run_id,
                        ),
                        application=application,
                        run_id=turn_run_id,
                        renderer=renderer,
                    )
                    result = await _await_with_events(
                        application.conversations.execute_prepared(prepared_direct),
                        application=application,
                        run_id=turn_run_id,
                        renderer=renderer,
                    )
                elif routed:
                    prepared = await _await_with_events(
                        application.conversations.prepare_routed_turn(
                            conversation.id,
                            prompt=prompt,
                            user_id=conversation.user_id,
                            lease=profile.lease,
                            permission_mode=profile.approval_mode,
                            budget=budget,
                            model_ref=model_ref,
                            agent_id=agent_id,
                            team_id=team_id,
                            run_id=turn_run_id,
                        ),
                        application=application,
                        run_id=turn_run_id,
                        renderer=renderer,
                    )
                    if prepared.decision.status is TaskRouteStatus.CONFIRMATION_REQUIRED:
                        confirmed = not prepared.decision.capability_gaps and typer.confirm(
                            f"Use {prepared.decision.mode.value} {prepared.decision.target_id!r}?",
                            default=False,
                        )
                        if confirmed:
                            prepared = await _await_with_events(
                                application.routing.confirm(
                                    prepared.root.id,
                                    decision_hash=str(prepared.decision.decision_hash),
                                ),
                                application=application,
                                run_id=turn_run_id,
                                renderer=renderer,
                            )
                        else:
                            if prepared.decision.capability_gaps:
                                gaps = ", ".join(
                                    item.value for item in prepared.decision.capability_gaps
                                )
                                console.print(
                                    f"[yellow]Route rejected: explicit lease required for "
                                    f"{gaps}.[/yellow]"
                                )
                            rejected = await _await_with_events(
                                application.routing.reject(
                                    prepared.root.id,
                                    decision_hash=str(prepared.decision.decision_hash),
                                ),
                                application=application,
                                run_id=turn_run_id,
                                renderer=renderer,
                            )
                            await application.conversations.record_task_result(
                                TaskExecutionResult(
                                    run=rejected,
                                    decision=prepared.decision,
                                    budget=BudgetTracker(budget).snapshot(),
                                )
                            )
                            renderer.finish()
                            continue
                    result = await _await_with_events(
                        application.conversations.execute_routed_prepared(prepared),
                        application=application,
                        run_id=turn_run_id,
                        renderer=renderer,
                    )
                else:
                    result = await _await_with_events(
                        application.conversations.run_turn(
                            conversation.id,
                            prompt=prompt,
                            user_id=conversation.user_id,
                            lease=profile.lease,
                            permission_mode=profile.approval_mode,
                            budget=budget,
                            model_ref=model_ref,
                            run_id=turn_run_id,
                        ),
                        application=application,
                        run_id=turn_run_id,
                        renderer=renderer,
                    )
            except (AgentCellError, OSError, SQLAlchemyError, ValueError) as error:
                renderer.finish()
                try:
                    persisted = await application.get_run(turn_run_id)
                except (AgentCellError, OSError, SQLAlchemyError, ValueError):
                    persisted = None
                _print_run_failure(
                    run_id=turn_run_id,
                    conversation_id=conversation.id,
                    status="failed" if persisted is None else persisted.status.value,
                    error_code=_cli_error_code(error),
                    json_output=False,
                )
                raise
            while not result.run.status.is_terminal:
                if result.approvals:
                    approval = result.approvals[0]
                    approval_decision = prompt_approval(
                        approval,
                        output=console,
                        renderer=renderer,
                    )
                    if approval_decision is None:
                        console.print(
                            f"[dim]Resume pending Run with: uv run agentcell resume "
                            f"{result.run.id} --approval-id {approval.id} --decision approve[/dim]"
                        )
                        console.print("[dim]Continue with:[/dim]")
                        console.print(f"uv run agentcell chat --conversation-id {conversation.id}")
                        return
                    if turn_uses_router:
                        result = await _await_with_events(
                            application.routing.decide_approval(
                                result.run.id,
                                approval.id,
                                approval_decision,
                            ),
                            application=application,
                            run_id=result.run.id,
                            renderer=renderer,
                        )
                        await application.conversations.record_task_result(result)
                    else:
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
                    if turn_uses_router:
                        result = await _await_with_events(
                            application.routing.resume(result.run.id),
                            application=application,
                            run_id=result.run.id,
                            renderer=renderer,
                        )
                        await application.conversations.record_task_result(result)
                    else:
                        result = await _await_with_events(
                            application.runs.resume_paused(result.run.id),
                            application=application,
                            run_id=result.run.id,
                            renderer=renderer,
                        )
                if isinstance(result, RunResult):
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


async def _await_with_events[T](
    execution: Coroutine[Any, Any, T],
    *,
    application: AgentCellApplication,
    run_id: UUID,
    renderer: CliEventRenderer,
) -> T:
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
            renderer.tick()
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
    decision: ApprovalDecision | None,
    database_url: str | None,
    config: Path,
    offline_fake: bool,
) -> Run | HandoffResult | TaskExecutionResult:
    application = await build_application(
        config=config,
        database_url=database_url,
        offline_fake=offline_fake,
    )
    try:
        run = await application.get_run(run_id)
        if run is None:
            raise RunNotFoundError(str(run_id))
        if run.agent_id == TASK_ROUTER_AGENT_ID:
            if approval_id is None:
                if decision is not None:
                    raise ValueError("decision requires --approval-id")
                routed = await application.routing.resume(run_id)
            else:
                if decision is None:
                    raise ValueError("--approval-id requires an explicit --decision")
                routed = await application.routing.decide_approval(
                    run_id,
                    approval_id,
                    decision,
                )
            await application.conversations.record_task_result(routed)
            return routed
        async with application.database.session() as session:
            checkpoint = await CheckpointRepository(session).latest(run_id)
        if checkpoint.kind is CheckpointKind.HANDOFF:
            if approval_id is None:
                if decision is not None:
                    raise ValueError("decision requires --approval-id")
            else:
                if decision is None:
                    raise ValueError("--approval-id requires an explicit --decision")
                return await application.handoffs.decide_approval(
                    run_id,
                    approval_id,
                    decision,
                )
            return await application.handoffs.resume(run_id)
        if approval_id is None:
            if decision is not None:
                raise ValueError("decision requires --approval-id")
            result = await application.runs.resume_paused(run_id)
        else:
            if decision is None:
                raise ValueError("--approval-id requires an explicit --decision")
            approvals = await application.approvals(run_id)
            if approval_id not in {approval.id for approval in approvals}:
                raise ValueError("Approval does not belong to the supplied Run")
            result = await application.runs.resume(
                approval_id,
                decision,
            )
        await application.conversations.record_if_managed(result)
        return result.run
    finally:
        await application.close()


async def _list_agents(
    database_url: str | None,
    config: Path,
    offline_fake: bool,
    *,
    include_internal: bool,
) -> tuple[RegisteredAgent, ...]:
    application = await build_application(
        config=config,
        database_url=database_url,
        offline_fake=offline_fake,
    )
    try:
        return application.agents.list_entries(include_internal=include_internal)
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


def _persisted_status(run_id: UUID, database_url: str | None) -> str | None:
    try:
        return asyncio.run(_persisted_status_async(run_id, database_url))
    except (OSError, SQLAlchemyError):
        return None


async def _persisted_status_async(run_id: UUID, database_url: str | None) -> str | None:
    database = _database(database_url)
    try:
        async with database.session() as session:
            run = await RunRepository(session).get(run_id)
        return None if run is None else run.status.value
    finally:
        await database.dispose()


def _cli_error_code(error: Exception) -> str:
    if isinstance(error, AgentCellError):
        return error.code
    if isinstance(error, SQLAlchemyError):
        return "storage_error"
    if isinstance(error, OSError):
        return "io_error"
    return "invalid_request"


def _agent_access(spec: AgentSpec) -> str:
    labels = {
        Capability.FILESYSTEM_READ: "read",
        Capability.FILESYSTEM_WRITE: "write",
        Capability.SHELL_EXECUTE: "shell",
        Capability.NETWORK_REQUEST: "network",
        Capability.AGENT_DELEGATE: "delegate",
    }
    return (
        ",".join(labels[capability] for capability in labels if capability in spec.capabilities)
        or "none"
    )


def _print_run_failure(
    *,
    run_id: UUID,
    conversation_id: UUID,
    status: str,
    error_code: str,
    json_output: bool,
) -> None:
    values = {
        "run_id": str(run_id),
        "conversation_id": str(conversation_id),
        "status": status,
        "error_code": error_code,
    }
    if json_output:
        console.print(
            json.dumps(values, ensure_ascii=False),
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        return
    console.print(
        f"[dim]run_id={run_id} conversation_id={conversation_id} "
        f"status={status} error_code={error_code}[/dim]"
    )


def main() -> None:
    """Console-script entry point."""

    app()


if __name__ == "__main__":
    main()
