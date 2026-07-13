from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config

from agentcell.storage import Database, sqlite_url

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def make_alembic_config(database_url: str) -> Config:
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "agentcell-test.db"


@pytest.fixture
def migrated_database_url(database_path: Path) -> str:
    url = sqlite_url(database_path)
    command.upgrade(make_alembic_config(url), "head")
    return url


@pytest.fixture
def migrated_alembic_config(migrated_database_url: str) -> Config:
    return make_alembic_config(migrated_database_url)


@pytest_asyncio.fixture
async def database(migrated_database_url: str) -> AsyncGenerator[Database]:
    instance = Database(migrated_database_url)
    try:
        yield instance
    finally:
        await instance.dispose()
