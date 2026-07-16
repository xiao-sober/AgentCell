"""File-change inspection and hash-safe reverse-change CLI commands."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

from agentcell.changes import ChangeDetails, FileChange
from agentcell.changes.service import ChangeService
from agentcell.cli.common import CLI_EXCEPTIONS, console, database, raise_cli_error
from agentcell.kernel.event_recorder import RunEventRecorder
from agentcell.storage import FileArtifactStore

changes_app = typer.Typer(help="Inspect and safely revert AgentCell file changes.")

DatabaseOption = Annotated[
    str | None,
    typer.Option("--database-url", help="Migrated SQLite aiosqlite URL."),
]
JsonOption = Annotated[
    bool,
    typer.Option("--json", help="Write structured JSON."),
]


@changes_app.command("list")
def changes_list_command(
    run_id: Annotated[UUID, typer.Option("--run", help="Run whose changes are listed.")],
    database_url: DatabaseOption = None,
    json_output: JsonOption = False,
) -> None:
    """List durable FileChange records for one Run."""

    try:
        values = asyncio.run(_list_changes(run_id, database_url))
    except CLI_EXCEPTIONS as error:
        raise_cli_error(error)
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
    except CLI_EXCEPTIONS as error:
        raise_cli_error(error)
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
    except CLI_EXCEPTIONS as error:
        raise_cli_error(error)
    console.print(details.diff, markup=False)


@changes_app.command("revert")
def changes_revert_command(
    change_id: Annotated[UUID, typer.Argument(help="Completed FileChange to reverse.")],
    yes: Annotated[bool, typer.Option("--yes", help="Confirm the displayed reverse Diff.")] = False,
    database_url: DatabaseOption = None,
) -> None:
    """Apply one hash-safe reverse change without destructive Git commands."""

    try:
        reverse_diff = asyncio.run(_change_reverse_diff(change_id, database_url))
        console.print(reverse_diff, markup=False)
        if not yes and not typer.confirm("Apply the reverse change shown above?"):
            raise typer.Abort()
        value = asyncio.run(_revert_change(change_id, database_url))
    except CLI_EXCEPTIONS as error:
        raise_cli_error(error)
    console.print(f"reverted change_id={change_id} reverse_change_id={value.id} path={value.path}")


async def _list_changes(run_id: UUID, database_url: str | None) -> tuple[FileChange, ...]:
    instance = database(database_url)
    service = ChangeService(
        instance,
        FileArtifactStore(instance, Path(".agentcell/artifacts")),
    )
    try:
        return await service.list_for_run(run_id)
    finally:
        await instance.dispose()


async def _change_details(change_id: UUID, database_url: str | None) -> ChangeDetails:
    instance = database(database_url)
    service = ChangeService(
        instance,
        FileArtifactStore(instance, Path(".agentcell/artifacts")),
    )
    try:
        return await service.details(change_id)
    finally:
        await instance.dispose()


async def _change_reverse_diff(change_id: UUID, database_url: str | None) -> str:
    instance = database(database_url)
    service = ChangeService(
        instance,
        FileArtifactStore(instance, Path(".agentcell/artifacts")),
    )
    try:
        return await service.reverse_diff(change_id)
    finally:
        await instance.dispose()


async def _revert_change(change_id: UUID, database_url: str | None) -> FileChange:
    instance = database(database_url)
    service = ChangeService(
        instance,
        FileArtifactStore(instance, Path(".agentcell/artifacts")),
    )
    try:
        details = await service.details(change_id)
        return await service.revert(
            change_id,
            events=RunEventRecorder(instance, details.change.run_id),
        )
    finally:
        await instance.dispose()
