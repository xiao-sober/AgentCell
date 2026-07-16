"""Deterministic, secret-safe Run display projection and terminal adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from uuid import UUID, uuid4

from rich.console import Console

from agentcell.cli.display import CliEventRenderer, streaming_answer_preview
from agentcell.display import RunDisplayPhase, RunDisplayProjector
from agentcell.events import (
    DomainEvent,
    ErrorPayload,
    EventPayload,
    EventType,
    GenericEventPayload,
    ModelRequestedPayload,
    RunCompletedPayload,
    RunStartedPayload,
    RunStatusChangedPayload,
    TextDeltaPayload,
)


def _event(
    run_id: UUID,
    sequence: int,
    event_type: EventType,
    payload: EventPayload,
) -> DomainEvent[EventPayload]:
    return DomainEvent(
        run_id=run_id,
        sequence=sequence,
        event_type=event_type,
        occurred_at=datetime(2026, 7, 15, tzinfo=UTC),
        payload=payload,
    )


def _sequence() -> list[DomainEvent[EventPayload]]:
    run_id = uuid4()
    return [
        _event(
            run_id,
            1,
            EventType.RUN_STARTED,
            RunStartedPayload(
                conversation_id=uuid4(),
                agent_id="coder",
                model_ref="fake",
                provider="fake",
                model="test-model",
                budget={
                    "max_requests": 10,
                    "max_tool_calls": 20,
                    "max_total_tokens": 240_000,
                },
            ),
        ),
        _event(
            run_id,
            2,
            EventType.MODEL_REQUESTED,
            ModelRequestedPayload(provider="fake", model="test-model", request_index=1),
        ),
        _event(
            run_id,
            3,
            EventType.MODEL_TEXT_DELTA,
            TextDeltaPayload(delta="阶段说明 api_key=topsecret"),
        ),
        _event(
            run_id,
            4,
            EventType.TOOL_PROPOSED,
            GenericEventPayload(
                data={
                    "call_id": "call-1",
                    "provider_call_id": "provider-1",
                    "tool_name": "workspace.read",
                    "arguments": {
                        "path": "src/one.py",
                        "reasoning_content": "private-chain",
                    },
                }
            ),
        ),
        _event(
            run_id,
            5,
            EventType.TOOL_STARTED,
            GenericEventPayload(
                data={
                    "call_id": "call-1",
                    "provider_call_id": "provider-1",
                    "tool_name": "workspace.read",
                }
            ),
        ),
        _event(
            run_id,
            6,
            EventType.TOOL_COMPLETED,
            GenericEventPayload(
                data={
                    "call_id": "call-1",
                    "provider_call_id": "provider-1",
                    "tool_name": "workspace.read",
                    "output": "password=tool-secret",
                }
            ),
        ),
        _event(
            run_id,
            7,
            EventType.TOOL_PROPOSED,
            GenericEventPayload(
                data={
                    "call_id": "call-2",
                    "provider_call_id": "provider-2",
                    "tool_name": "workspace.read",
                    "arguments": {"path": "src/two.py"},
                }
            ),
        ),
        _event(
            run_id,
            8,
            EventType.TOOL_COMPLETED,
            GenericEventPayload(
                data={
                    "call_id": "call-2",
                    "provider_call_id": "provider-2",
                    "tool_name": "workspace.read",
                }
            ),
        ),
        _event(
            run_id,
            9,
            EventType.BUDGET_UPDATED,
            GenericEventPayload(
                data={
                    "source": "model_usage_recorded",
                    "snapshot": {
                        "budget": {
                            "max_requests": 10,
                            "max_tool_calls": 20,
                            "max_total_tokens": 240_000,
                        },
                        "used": {
                            "requests": 2,
                            "tool_calls": 2,
                            "input_tokens": 1_000,
                            "output_tokens": 100,
                            "cache_read_tokens": 250,
                            "cache_write_tokens": 0,
                        },
                    },
                }
            ),
        ),
        _event(
            run_id,
            10,
            EventType.MODEL_REQUESTED,
            ModelRequestedPayload(provider="fake", model="test-model", request_index=2),
        ),
        _event(
            run_id,
            11,
            EventType.MODEL_TEXT_DELTA,
            TextDeltaPayload(delta="最终回答"),
        ),
        _event(
            run_id,
            12,
            EventType.RUN_STATUS_CHANGED,
            RunStatusChangedPayload(previous_status="running", status="completed"),
        ),
        _event(
            run_id,
            13,
            EventType.RUN_COMPLETED,
            RunCompletedPayload(
                output_characters=4,
                requests=2,
                input_tokens=1_000,
                output_tokens=100,
                tool_calls=2,
            ),
        ),
    ]


def test_projection_aggregates_tools_promotes_answer_and_is_replay_deterministic() -> None:
    events = _sequence()
    first = RunDisplayProjector()
    second = RunDisplayProjector()
    for event in events:
        first.apply(event)
        second.apply(event)
    first.apply(events[-1])

    state = first.state
    assert state == second.state
    assert state.phase is RunDisplayPhase.COMPLETED
    assert state.answer == "最终回答"
    assert state.answer_candidate == "最终回答"
    read = next(activity for activity in state.activities if activity.key == "tool:workspace.read")
    assert read.count == 2
    assert read.detail == "src/two.py"
    assert state.budget.requests == 2
    assert state.budget.total_tokens == 1_100
    assert state.budget.cache_hit_ratio == 0.25
    serialized = state.model_dump_json()
    assert "topsecret" not in serialized
    assert "tool-secret" not in serialized
    assert "private-chain" not in serialized
    assert "reasoning_content" not in serialized


def test_rejected_or_failed_output_is_never_promoted_to_answer() -> None:
    run_id = uuid4()
    projector = RunDisplayProjector()
    events = (
        _event(
            run_id,
            1,
            EventType.MODEL_TEXT_DELTA,
            TextDeltaPayload(delta="<invoke>bad</invoke>"),
        ),
        _event(
            run_id,
            2,
            EventType.MODEL_OUTPUT_REJECTED,
            GenericEventPayload(data={"reason": "unresolved_tool_protocol"}),
        ),
        _event(
            run_id,
            3,
            EventType.RUN_FAILED,
            ErrorPayload(code="invalid_final_output", message="rejected"),
        ),
    )
    for event in events:
        projector.apply(event)

    assert projector.state.phase is RunDisplayPhase.FAILED
    assert projector.state.answer is None
    assert projector.state.answer_candidate == ""


def test_routed_child_text_streams_and_resets_provisional_output() -> None:
    run_id = uuid4()
    child_run_id = uuid4()
    projector = RunDisplayProjector()
    projector.apply(
        _event(
            run_id,
            1,
            EventType.MODEL_TEXT_DELTA,
            TextDeltaPayload(
                delta="provisional",
                source_run_id=child_run_id,
                source_agent_id="coordinator",
                source_sequence=3,
            ),
        )
    )
    assert projector.state.answer_candidate == "provisional"

    projector.apply(
        _event(
            run_id,
            2,
            EventType.MODEL_OUTPUT_REJECTED,
            GenericEventPayload(
                data={"child_run_id": str(child_run_id), "child_sequence": 4}
            ),
        )
    )
    projector.apply(
        _event(
            run_id,
            3,
            EventType.MODEL_TEXT_DELTA,
            TextDeltaPayload(
                delta="final",
                source_run_id=child_run_id,
                source_agent_id="coordinator",
                source_sequence=8,
            ),
        )
    )

    assert projector.state.answer_candidate == "final"


def test_shell_activity_includes_allowlisted_command_arguments_and_cwd() -> None:
    run_id = uuid4()
    projector = RunDisplayProjector()
    projector.apply(
        _event(
            run_id,
            1,
            EventType.TOOL_PROPOSED,
            GenericEventPayload(
                data={
                    "call_id": "shell-1",
                    "provider_call_id": "shell-1",
                    "tool_name": "shell.test",
                    "arguments": {
                        "command": "pytest",
                        "args": ["-x", "--tb=short"],
                        "cwd": ".",
                    },
                }
            ),
        )
    )

    activity = projector.state.activities[-1]
    assert activity.label == "准备运行检查"
    assert activity.tool_name == "shell.test"
    assert activity.detail == "pytest · -x --tb=short · ."


def test_streaming_answer_preview_keeps_latest_visible_lines() -> None:
    value = "\n".join(f"line-{index}" for index in range(1, 31))

    preview = streaming_answer_preview(value, width=60, height=24)

    assert preview.startswith("…\n")
    assert "line-1\n" not in preview
    assert "line-30" in preview
    assert len(preview.splitlines()) <= 13


def test_non_tty_renderer_prints_bounded_milestones_and_answer_once() -> None:
    stream = StringIO()
    output = Console(file=stream, force_terminal=False, color_system=None, width=60)
    renderer = CliEventRenderer(enabled=True, output=output)
    for event in _sequence():
        renderer.render(event)
    renderer.finish()

    rendered = stream.getvalue()
    assert rendered.count("最终回答") == 1
    assert rendered.count("文件读取完成") == 1
    assert "budget.updated" not in rendered
    assert "model_usage_recorded" not in rendered
    assert "topsecret" not in rendered
    assert "tool-secret" not in rendered


def test_narrow_tty_live_stops_for_approval_without_leaking_arguments() -> None:
    run_id = uuid4()
    stream = StringIO()
    output = Console(file=stream, force_terminal=True, color_system=None, width=36)
    renderer = CliEventRenderer(enabled=True, output=output)
    renderer.render(
        _event(
            run_id,
            1,
            EventType.RUN_STARTED,
            RunStartedPayload(conversation_id=uuid4(), agent_id="coder"),
        )
    )
    renderer.render(
        _event(
            run_id,
            2,
            EventType.TOOL_APPROVAL_REQUIRED,
            GenericEventPayload(
                data={
                    "approval_id": str(uuid4()),
                    "tool_name": "workspace.write",
                    "risk": "guarded",
                    "impact": "write src/app.py password=hidden",
                    "arguments": {"content": "private"},
                }
            ),
        )
    )
    renderer.finish()

    rendered = stream.getvalue()
    assert "hidden" not in rendered
    assert "private" not in rendered
