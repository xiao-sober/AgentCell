from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentcell.agents import coder_spec, coordinator_spec
from agentcell.errors import InvalidRunTimestampError, InvalidStateTransitionError
from agentcell.kernel import Run, RunStatus
from agentcell.kernel.identity import RunExecutionIdentity
from agentcell.providers import FakeModelSpec


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


def test_execution_identity_hashes_agent_and_model_snapshots() -> None:
    user_id = uuid4()
    agent = coordinator_spec(model_ref="stable-model")
    model = FakeModelSpec(model="stable-model")
    identity = RunExecutionIdentity.capture(
        user_id=user_id,
        agent_spec=agent,
        model_spec=model,
    )
    run = Run(
        conversation_id=uuid4(),
        agent_id=agent.id,
        execution_identity=identity,
    )

    assert run.execution_identity is not None
    assert run.execution_identity.user_id == user_id
    assert run.execution_identity.model_ref == "stable-model"
    assert run.execution_identity.matches_current(model_spec=model)
    with pytest.raises(ValidationError, match="snapshot hash"):
        RunExecutionIdentity.model_validate(
            {
                **identity.model_dump(mode="json"),
                "agent_spec_sha256": "0" * 64,
            }
        )
    with pytest.raises(ValidationError, match="agent_id does not match"):
        Run(
            conversation_id=uuid4(),
            agent_id="coder",
            execution_identity=identity,
        )


def test_execution_identity_accepts_legacy_capability_order_and_transitions() -> None:
    agent = coder_spec(model_ref="stable-model")
    model = FakeModelSpec(model="stable-model")
    captured = RunExecutionIdentity.capture(
        user_id=uuid4(),
        agent_spec=agent,
        model_spec=model,
    )
    payload = captured.model_dump(mode="json")
    legacy_agent = dict(payload["agent_spec"])
    legacy_agent["capabilities"] = list(reversed(legacy_agent["capabilities"]))
    encoded = json.dumps(
        legacy_agent,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    payload.update(
        schema_version=1,
        agent_spec=legacy_agent,
        agent_spec_sha256=hashlib.sha256(encoded).hexdigest(),
    )

    restored = RunExecutionIdentity.model_validate(payload)
    run = Run(
        conversation_id=uuid4(),
        agent_id="coder",
        execution_identity=restored,
    )

    assert run.transition_to(RunStatus.RUNNING).status is RunStatus.RUNNING
