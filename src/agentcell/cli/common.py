"""Shared CLI infrastructure without command or application orchestration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from sqlalchemy.exc import SQLAlchemyError

from agentcell.errors import AgentCellError
from agentcell.storage import Database

console = Console()


def database(database_url: str | None) -> Database:
    """Resolve the explicit or process-local SQLite database without migrating it."""

    url = database_url or os.getenv("AGENTCELL_DATABASE_URL")
    return Database(url) if url else Database.from_path(Path(".agentcell/agentcell.db"))


def raise_cli_error(error: Exception) -> NoReturn:
    """Render one transport error without leaking a traceback or secret-bearing details."""

    console.print(f"[red]Command failed:[/red] {error}")
    if isinstance(error, SQLAlchemyError):
        console.print("[dim]Apply migrations first: uv run alembic upgrade head[/dim]")
    raise typer.Exit(code=1) from error


CLI_EXCEPTIONS = (AgentCellError, OSError, SQLAlchemyError, ValueError)
