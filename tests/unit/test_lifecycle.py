from __future__ import annotations

import pytest

from agentcell.errors import InvalidStateTransitionError
from agentcell.kernel.lifecycle import (
    RunStatus,
    available_transitions,
    can_transition,
    ensure_transition,
)

EXPECTED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_APPROVAL,
            RunStatus.PAUSED,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.WAITING_APPROVAL: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.PAUSED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.PAUSED: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


def test_transition_table_covers_every_status() -> None:
    assert set(EXPECTED_TRANSITIONS) == set(RunStatus)
    for current, expected_targets in EXPECTED_TRANSITIONS.items():
        assert available_transitions(current) == expected_targets


def test_every_status_pair_obeys_the_transition_table() -> None:
    for current in RunStatus:
        for target in RunStatus:
            expected = target in EXPECTED_TRANSITIONS[current]
            assert can_transition(current, target) is expected


def test_ensure_transition_returns_a_valid_target() -> None:
    assert ensure_transition(RunStatus.CREATED, RunStatus.RUNNING) is RunStatus.RUNNING


def test_ensure_transition_rejects_self_transition_with_context() -> None:
    with pytest.raises(InvalidStateTransitionError) as captured:
        ensure_transition(RunStatus.RUNNING, RunStatus.RUNNING)

    assert captured.value.current_status == "running"
    assert captured.value.target_status == "running"
    assert captured.value.code == "invalid_state_transition"


def test_terminal_statuses_have_no_outgoing_transitions() -> None:
    terminal_statuses = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}

    assert {status for status in RunStatus if status.is_terminal} == terminal_statuses
    assert all(not available_transitions(status) for status in terminal_statuses)
