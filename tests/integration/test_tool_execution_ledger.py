"""Durable tool idempotency records survive process-local executor loss."""

from __future__ import annotations

from uuid import uuid4

import pytest

from agentcell.errors import ToolReplayBlockedError
from agentcell.kernel.models import Run
from agentcell.storage import Database, RunRepository, SqliteToolExecutionLedger
from agentcell.tools import ToolCall, ToolResult


async def _run(database: Database) -> Run:
    run = Run(conversation_id=uuid4(), agent_id="coordinator")
    async with database.transaction() as session:
        await RunRepository(session).create(run)
    return run


@pytest.mark.asyncio
async def test_started_non_idempotent_call_is_never_claimed_twice(database: Database) -> None:
    run = await _run(database)
    first_ledger = SqliteToolExecutionLedger(database, run.id)
    call = ToolCall(provider_call_id="danger-1", tool_name="test.danger")
    assert await first_ledger.begin(call, idempotent=False) is None

    restarted_ledger = SqliteToolExecutionLedger(database, run.id)
    with pytest.raises(ToolReplayBlockedError):
        await restarted_ledger.begin(
            ToolCall(provider_call_id="danger-1", tool_name="test.danger"),
            idempotent=False,
        )


@pytest.mark.asyncio
async def test_completed_call_returns_persisted_result_without_execution(
    database: Database,
) -> None:
    run = await _run(database)
    ledger = SqliteToolExecutionLedger(database, run.id)
    call = ToolCall(provider_call_id="complete-1", tool_name="test.action")
    assert await ledger.begin(call, idempotent=False) is None
    result = ToolResult(
        call_id=call.call_id,
        tool_name=call.tool_name,
        output={"done": True},
        output_bytes=13,
        duration_ms=1,
    )
    await ledger.complete(call, result)

    restored = await SqliteToolExecutionLedger(database, run.id).begin(
        ToolCall(provider_call_id="complete-1", tool_name="test.action"),
        idempotent=False,
    )

    assert restored == result
