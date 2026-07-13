"""The single source of truth for Run lifecycle transitions."""

from __future__ import annotations

from enum import StrEnum

from agentcell.errors import InvalidStateTransitionError


class RunStatus(StrEnum):
    """Persisted lifecycle states for a Run."""

    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Return whether no further transition is allowed from this status."""

        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
)

_ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
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


def available_transitions(status: RunStatus) -> frozenset[RunStatus]:
    """Return the immutable set of statuses reachable from ``status``."""

    return _ALLOWED_TRANSITIONS[status]


def can_transition(current: RunStatus, target: RunStatus) -> bool:
    """Return whether ``current`` may transition to ``target``."""

    return target in available_transitions(current)


def ensure_transition(current: RunStatus, target: RunStatus) -> RunStatus:
    """Validate and return ``target``, raising a domain error when it is illegal."""

    if not can_transition(current, target):
        raise InvalidStateTransitionError(current.value, target.value)
    return target
