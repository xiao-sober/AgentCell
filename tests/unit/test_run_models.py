from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentcell.errors import InvalidRunTimestampError, InvalidStateTransitionError
from agentcell.kernel import Run, RunStatus


def test_run_normalizes_timestamps_and_transitions_immutably() -> None:
    supplied = datetime(2026, 7, 10, 20, 30, tzinfo=timezone(timedelta(hours=8)))
    run = Run(
        conversation_id=uuid4(),
        agent_id="coordinator",
        created_at=supplied,
        updated_at=supplied,
    )

    transitioned = run.transition_to(
        RunStatus.RUNNING,
        at=datetime(2026, 7, 10, 12, 31, tzinfo=UTC),
    )

    assert run.status is RunStatus.CREATED
    assert run.created_at == datetime(2026, 7, 10, 12, 30, tzinfo=UTC)
    assert transitioned.status is RunStatus.RUNNING
    assert transitioned.updated_at == datetime(2026, 7, 10, 12, 31, tzinfo=UTC)


def test_run_rejects_illegal_transition_and_backwards_update_time() -> None:
    now = datetime(2026, 7, 10, 12, 30, tzinfo=UTC)
    run = Run(
        conversation_id=uuid4(),
        agent_id="coordinator",
        created_at=now,
        updated_at=now,
    )

    with pytest.raises(InvalidStateTransitionError):
        run.transition_to(RunStatus.COMPLETED)

    with pytest.raises(InvalidRunTimestampError):
        run.transition_to(RunStatus.RUNNING, at=now - timedelta(seconds=1))


def test_run_rejects_naive_time_self_parent_and_unknown_fields() -> None:
    run_id = uuid4()

    with pytest.raises(ValidationError):
        Run(
            id=run_id,
            conversation_id=uuid4(),
            agent_id="coordinator",
            parent_run_id=run_id,
        )

    with pytest.raises(ValidationError):
        Run(
            conversation_id=uuid4(),
            agent_id="coordinator",
            created_at=datetime(2026, 7, 10, 12, 30),
        )

    with pytest.raises(ValidationError):
        Run.model_validate(
            {
                "conversation_id": uuid4(),
                "agent_id": "coordinator",
                "unexpected": True,
            }
        )
