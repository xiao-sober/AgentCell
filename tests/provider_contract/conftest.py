"""Provider contract safety guard: online model requests are opt-in only."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from pydantic_ai import models

from agentcell.storage import Database, sqlite_url

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def disable_online_model_requests() -> Iterator[None]:
    previous = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = False
    try:
        yield
    finally:
        models.ALLOW_MODEL_REQUESTS = previous


@pytest.fixture
def migrated_database_url(tmp_path: Path) -> str:
    url = sqlite_url(tmp_path / "live-provider-contract.db")
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")
    return url


@pytest_asyncio.fixture
async def database(migrated_database_url: str) -> AsyncGenerator[Database]:
    instance = Database(migrated_database_url)
    try:
        yield instance
    finally:
        await instance.dispose()
