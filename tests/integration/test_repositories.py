from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from agentcell.errors import (
    RunAlreadyExistsError,
    RunNotFoundError,
    StorageIntegrityError,
)
from agentcell.kernel import Run, RunStatus
from agentcell.storage import Database, RunRepository


@pytest.mark.asyncio
async def test_run_repository_round_trips_domain_model_and_validated_status(
    database: Database,
) -> None:
    run = Run(conversation_id=uuid4(), agent_id="coordinator")

    async with database.transaction() as session:
        created = await RunRepository(session).create(run)
    async with database.session() as session:
        restored = await RunRepository(session).get(run.id)

    assert created is run
    assert restored == run
    assert isinstance(restored, Run)

    transitioned = run.transition_to(
        RunStatus.RUNNING,
        at=run.updated_at + timedelta(seconds=1),
    )
    async with database.transaction() as session:
        await RunRepository(session).save(transitioned)
    async with database.session() as session:
        restored_transition = await RunRepository(session).get(run.id)

    assert restored_transition == transitioned


@pytest.mark.asyncio
async def test_run_repository_classifies_duplicate_missing_and_foreign_key_errors(
    database: Database,
) -> None:
    run = Run(conversation_id=uuid4(), agent_id="coordinator")
    async with database.transaction() as session:
        await RunRepository(session).create(run)

    with pytest.raises(RunAlreadyExistsError):
        async with database.transaction() as session:
            await RunRepository(session).create(run)

    missing = Run(conversation_id=uuid4(), agent_id="missing")
    with pytest.raises(RunNotFoundError):
        async with database.transaction() as session:
            await RunRepository(session).save(missing)

    orphan = Run(
        conversation_id=uuid4(),
        agent_id="child",
        parent_run_id=uuid4(),
    )
    with pytest.raises(StorageIntegrityError):
        async with database.transaction() as session:
            await RunRepository(session).create(orphan)

    invalid_update = Run.model_validate(
        {
            **run.model_dump(),
            "parent_run_id": uuid4(),
        }
    )
    with pytest.raises(StorageIntegrityError):
        async with database.transaction() as session:
            await RunRepository(session).save(invalid_update)


@pytest.mark.asyncio
async def test_run_repository_returns_none_for_unknown_run(database: Database) -> None:
    async with database.session() as session:
        restored = await RunRepository(session).get(uuid4())

    assert restored is None
