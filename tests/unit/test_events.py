from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentcell.events import (
    REDACTED_VALUE,
    ArtifactReference,
    DomainEvent,
    ErrorPayload,
    EventType,
    GenericEventPayload,
    TaskRouteEventPayload,
    TextDeltaPayload,
)


def test_domain_event_has_stable_identity_sequence_and_utc_time() -> None:
    run_id = uuid4()
    supplied_time = datetime(2026, 7, 10, 20, 30, tzinfo=timezone(timedelta(hours=8)))
    payload = TextDeltaPayload(delta="hello")

    event = DomainEvent[TextDeltaPayload](
        run_id=run_id,
        sequence=1,
        event_type=EventType.MODEL_TEXT_DELTA,
        occurred_at=supplied_time,
        payload=payload,
    )

    assert event.run_id == run_id
    assert event.sequence == 1
    assert event.occurred_at == datetime(2026, 7, 10, 12, 30, tzinfo=UTC)
    assert event.event_id.version == 4
    assert event.payload.version == 1


def test_domain_event_rejects_zero_sequence_and_naive_time() -> None:
    payload = TextDeltaPayload(delta="hello")

    with pytest.raises(ValidationError):
        DomainEvent[TextDeltaPayload](
            run_id=uuid4(),
            sequence=0,
            event_type=EventType.MODEL_TEXT_DELTA,
            payload=payload,
        )

    with pytest.raises(ValidationError):
        DomainEvent[TextDeltaPayload](
            run_id=uuid4(),
            sequence=1,
            event_type=EventType.MODEL_TEXT_DELTA,
            occurred_at=datetime(2026, 7, 10, 12, 30),
            payload=payload,
        )


def test_generic_payload_redacts_nested_credentials_but_preserves_usage_tokens() -> None:
    payload = GenericEventPayload(
        data={
            "api_key": "top-secret",
            "api_key_env": "DASHSCOPE_API_KEY",
            "usage": {"input_tokens": 42},
            "headers": [{"Authorization": "Bearer secret"}],
        }
    )

    assert payload.data["api_key"] == REDACTED_VALUE
    assert payload.data["api_key_env"] == "DASHSCOPE_API_KEY"
    assert payload.data["usage"] == {"input_tokens": 42}
    assert payload.data["headers"] == [{"Authorization": REDACTED_VALUE}]


def test_error_payload_redacts_details_in_safe_dump() -> None:
    payload = ErrorPayload(
        code="provider_authentication_failed",
        message="Provider authentication failed",
        details={"client_secret": "do-not-log", "status": 401},
    )
    event = DomainEvent[ErrorPayload](
        run_id=uuid4(),
        sequence=2,
        event_type=EventType.MODEL_FAILED,
        payload=payload,
    )

    assert event.safe_payload() == {
        "version": 1,
        "code": "provider_authentication_failed",
        "message": "Provider authentication failed",
        "retryable": False,
        "details": {"client_secret": REDACTED_VALUE, "status": 401},
    }


def test_models_forbid_unknown_fields_and_empty_deltas() -> None:
    with pytest.raises(ValidationError):
        GenericEventPayload.model_validate({"version": 0, "data": {}})

    with pytest.raises(ValidationError):
        TextDeltaPayload.model_validate({"delta": "", "unexpected": True})

    with pytest.raises(ValidationError):
        DomainEvent[GenericEventPayload].model_validate(
            {
                "run_id": str(uuid4()),
                "sequence": 1,
                "event_type": EventType.RUN_STARTED,
                "payload": {"data": {}},
                "unexpected": True,
            }
        )


def test_event_type_catalog_contains_all_required_core_events() -> None:
    assert {event_type.value for event_type in EventType} == {
        "run.started",
        "run.status_changed",
        "model.requested",
        "model.text_delta",
        "model.completed",
        "model.failed",
        "model.output_rejected",
        "tool.proposed",
        "tool.approval_required",
        "tool.approved",
        "tool.rejected",
        "tool.started",
        "tool.output_delta",
        "tool.completed",
        "tool.failed",
        "agent.child_started",
        "agent.child_completed",
        "memory.recalled",
        "memory.written",
        "context.compacted",
        "budget.updated",
        "checkpoint.created",
        "task.route_proposed",
        "task.route_confirmed",
        "task.route_overridden",
        "task.route_rejected",
        "file.change_prepared",
        "file.change_applied",
        "file.change_completed",
        "file.change_conflict",
        "file.change_reverted",
        "run.completed",
        "run.failed",
        "run.cancelled",
    }


def test_artifact_reference_requires_content_identity_and_size() -> None:
    reference = ArtifactReference(
        artifact_id=uuid4(),
        media_type="text/plain",
        size_bytes=70_000,
        sha256="a" * 64,
    )

    assert reference.size_bytes == 70_000

    with pytest.raises(ValidationError):
        ArtifactReference(
            artifact_id=uuid4(),
            media_type="text/plain",
            size_bytes=-1,
            sha256="not-a-sha256",
        )


def test_task_route_payload_is_versioned_and_excludes_raw_task_text() -> None:
    payload = TaskRouteEventPayload(
        policy_version="9.4.1-v1",
        task_sha256="a" * 64,
        decision_hash="b" * 64,
        mode="single_agent",
        agent_id="coordinator",
        source="deterministic",
        status="ready",
        confidence=0.93,
        reason_summary="read-only analysis",
        required_capabilities=("filesystem.read",),
        budget_profile="read_only",
    )

    assert "task" not in payload.model_fields_set
    assert payload.safe_dump()["task_sha256"] == "a" * 64

    with pytest.raises(ValidationError, match="exactly one"):
        TaskRouteEventPayload.model_validate(
            {
                **payload.model_dump(mode="json"),
                "team_id": "software",
            }
        )
