"""Typer CLI that invokes RunService directly without a local HTTP hop."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated, NoReturn
from uuid import UUID

import typer
from rich.console import Console
from sqlalchemy.exc import SQLAlchemyError

from agentcell.agents import AgentRegistry, coordinator_spec
from agentcell.config import AgentCellSettings
from agentcell.errors import AgentCellError
from agentcell.kernel.models import Run
from agentcell.kernel.replay import ReplayService, ReplayState
from agentcell.kernel.run_service import RunRequest, RunResult, RunService
from agentcell.policy import CapabilityLease
from agentcell.providers import (
    FakeModelSpec,
    FakeProviderAdapter,
    FakeScript,
    FakeTextStep,
    ProviderFactory,
)
from agentcell.storage import Database
from agentcell.tools import ToolRegistry, register_workspace_tools

app = typer.Typer(
    name="agentcell",
    help="Local-first Agent runtime.",
    no_args_is_help=True,
)
console = Console()
_DEFAULT_WORKSPACE = Path.cwd()
_DEFAULT_CONFIG = Path("agentcell.toml")

PromptArgument = Annotated[str, typer.Argument(help="Task for the selected Agent.")]
WorkspaceOption = Annotated[
    Path,
    typer.Option("--workspace", "-w", help="Workspace root exposed read-only to the Run."),
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


@app.callback()
def root() -> None:
    """Select an AgentCell command."""


@app.command("run")
def run_command(
    prompt: PromptArgument,
    workspace: WorkspaceOption = _DEFAULT_WORKSPACE,
    database_url: DatabaseOption = None,
    config: ConfigOption = _DEFAULT_CONFIG,
    model_ref: ModelOption = None,
    offline_fake: OfflineOption = False,
    json_output: JsonOption = False,
) -> None:
    """Execute one Run in-process and persist its complete event history."""

    try:
        result = asyncio.run(
            _run_once(
                prompt=prompt,
                workspace=workspace,
                database_url=database_url,
                config=config,
                model_ref=model_ref,
                offline_fake=offline_fake,
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
    if result.output is not None:
        console.print(result.output)
    for approval in result.approvals:
        console.print(
            f"[yellow]approval required[/yellow] id={approval.id} "
            f"tool={approval.tool_name} risk={approval.risk.value}"
        )
        console.print(f"  impact: {approval.impact}")
    console.print(
        f"[dim]run_id={result.run.id} status={result.run.status.value} "
        f"requests={result.budget.used.requests} "
        f"tool_calls={result.budget.used.tool_calls}[/dim]"
    )


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


async def _run_once(
    *,
    prompt: str,
    workspace: Path,
    database_url: str | None,
    config: Path,
    model_ref: str | None,
    offline_fake: bool,
) -> RunResult:
    selected_ref: str
    if offline_fake:
        selected_ref = "offline_fake"
        spec = FakeModelSpec(model="agentcell-offline-fake")
        adapter = FakeProviderAdapter(
            {spec.model: FakeScript(steps=(FakeTextStep(text=f"Offline result: {prompt}"),))}
        )
        providers = ProviderFactory({selected_ref: spec}, adapters=(adapter,))
    else:
        settings = AgentCellSettings.from_toml(config)
        selected_ref = model_ref or next(iter(settings.models))
        providers = ProviderFactory(settings.models)

    url = database_url or os.getenv("AGENTCELL_DATABASE_URL")
    database = Database(url) if url else Database.from_path(Path(".agentcell/agentcell.db"))
    agents = AgentRegistry()
    agents.register(coordinator_spec(model_ref=selected_ref))
    tools = ToolRegistry()
    register_workspace_tools(tools)
    service = RunService(
        database=database,
        providers=providers,
        agents=agents,
        tools=tools,
    )
    try:
        return await service.run(
            RunRequest(
                prompt=prompt,
                workspace=workspace,
                lease=CapabilityLease(filesystem_read=(".",)),
            )
        )
    finally:
        await providers.aclose()
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
        return await service.cancel(run_id)
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
