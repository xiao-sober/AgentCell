"""Persisted events rebuild the same transport-neutral display after restart."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcell.application import build_application
from agentcell.display import RunDisplayPhase, RunDisplayProjector
from agentcell.kernel.run_service import RunRequest


@pytest.mark.asyncio
async def test_persisted_run_display_rebuild_is_restart_deterministic(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    application = await build_application(
        database_url=migrated_database_url,
        offline_fake=True,
        fake_output="restart-safe answer",
    )
    try:
        result = await application.runs.run(
            RunRequest(prompt="project display", workspace=tmp_path)
        )
        first_events = await application.events(result.run.id)
    finally:
        await application.close()

    restarted = await build_application(
        database_url=migrated_database_url,
        offline_fake=True,
    )
    try:
        second_events = await restarted.events(result.run.id)
    finally:
        await restarted.close()

    first = RunDisplayProjector()
    second = RunDisplayProjector()
    for event in first_events:
        first.apply(event)
    for event in second_events:
        second.apply(event)

    assert first.state == second.state
    assert second.state.phase is RunDisplayPhase.COMPLETED
    assert second.state.answer == "restart-safe answer"
    assert second.state.budget.max_requests == 10
    assert second.state.budget.max_tool_calls == 20
    assert second.state.active_agent is not None
    assert second.state.active_agent.agent_id == "coordinator"
