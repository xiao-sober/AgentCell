"""Stage 7 file-backed Artifact persistence, deduplication, and verification."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from agentcell.budgets import Budget, BudgetTracker
from agentcell.errors import ArtifactIntegrityError, ArtifactTooLargeError
from agentcell.kernel.checkpoint import Checkpoint, CheckpointKind
from agentcell.kernel.lifecycle import RunStatus
from agentcell.kernel.models import Run
from agentcell.policy import CapabilityLease
from agentcell.storage import (
    ArtifactRepository,
    CheckpointRepository,
    Database,
    FileArtifactStore,
    RunRepository,
)


@pytest.mark.asyncio
async def test_artifact_round_trip_deduplicates_and_survives_store_restart(
    database: Database,
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    content = "阶段七 Artifact 内容".encode()
    first = FileArtifactStore(database, root)
    reference = await first.save(
        content,
        media_type="text/plain; charset=utf-8",
        suggested_name="中文 报告.txt",
    )
    duplicate = await first.save(
        content,
        media_type="text/plain; charset=utf-8",
        suggested_name="other.txt",
    )

    restarted = FileArtifactStore(database, root)
    assert duplicate == reference
    assert await restarted.load(reference) == content


@pytest.mark.asyncio
async def test_artifact_load_detects_content_tampering(
    database: Database,
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    store = FileArtifactStore(database, root)
    reference = await store.save(
        b"trusted",
        media_type="application/octet-stream",
        suggested_name="value.bin",
    )
    async with database.session() as session:
        metadata = await ArtifactRepository(session).get(reference.artifact_id)
    assert metadata is not None
    (root / metadata.storage_key).write_bytes(b"tampered")

    with pytest.raises(ArtifactIntegrityError):
        await store.load(reference)


@pytest.mark.asyncio
async def test_artifact_size_budget_is_enforced_before_write(
    database: Database,
    tmp_path: Path,
) -> None:
    store = FileArtifactStore(database, tmp_path / "artifacts", max_artifact_bytes=3)

    with pytest.raises(ArtifactTooLargeError):
        await store.save(
            b"four",
            media_type="application/octet-stream",
            suggested_name="large.bin",
        )

    assert not (tmp_path / "artifacts").exists()


@pytest.mark.asyncio
async def test_checkpoint_restores_artifact_reference_after_restart(
    database: Database,
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    store = FileArtifactStore(database, root)
    reference = await store.save(
        b"checkpoint artifact",
        media_type="text/plain",
        suggested_name="checkpoint.txt",
    )
    run = Run(conversation_id=uuid4(), agent_id="coordinator")
    budget = Budget(
        max_requests=1,
        max_input_tokens=100,
        max_output_tokens=100,
        max_total_tokens=200,
        max_tool_calls=1,
        max_duration_seconds=30,
        max_cost=None,
        max_children=0,
        max_depth=0,
    )
    checkpoint = Checkpoint(
        run_id=run.id,
        user_id=uuid4(),
        event_sequence=1,
        kind=CheckpointKind.BRANCH,
        agent_id=run.agent_id,
        prompt="resume",
        workspace=str(tmp_path),
        lease=CapabilityLease(),
        budget=BudgetTracker(budget).snapshot(),
        messages=[],
        artifact_ids=(reference.artifact_id,),
        run_status=RunStatus.PAUSED,
    )
    async with database.transaction() as session:
        await RunRepository(session).create(run)
        await CheckpointRepository(session).create(checkpoint)

    async with database.session() as session:
        restored = await CheckpointRepository(session).latest(run.id)
    assert restored.artifact_ids == (reference.artifact_id,)
    content = await FileArtifactStore(database, root).load(restored.artifact_ids[0])
    assert content == b"checkpoint artifact"
