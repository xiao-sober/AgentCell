"""In-memory budget accounting with checkpoint-friendly snapshots."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from time import monotonic

from agentcell.budgets.models import Budget, BudgetRemaining, BudgetSnapshot, Usage
from agentcell.errors import BudgetExceededError, InvalidBudgetUsageError


class BudgetTracker:
    """Track active Run usage and reject reservations that exceed hard limits."""

    __slots__ = ("_budget", "_clock", "_started_at", "_usage")

    def __init__(
        self,
        budget: Budget,
        *,
        initial_usage: Usage | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._budget = budget
        self._usage = initial_usage or Usage()
        self._clock = clock
        self._started_at = self._read_clock()
        self._ensure_within(self._usage)

    @property
    def budget(self) -> Budget:
        """Return immutable limits assigned to this tracker."""

        return self._budget

    @property
    def usage(self) -> Usage:
        """Return current usage including elapsed active duration."""

        now = self._read_clock()
        return self._usage_at(now)

    @property
    def remaining(self) -> BudgetRemaining:
        """Return current non-negative remaining capacity."""

        return self._remaining_for(self.usage)

    def ensure_within_budget(self) -> None:
        """Raise if elapsed time or restored usage has exceeded a limit."""

        self._ensure_within(self.usage)

    def reserve_model_request(self) -> Usage:
        """Reserve one Provider request before starting the external call."""

        return self._reserve(requests=1)

    def record_model_usage(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cost: Decimal = Decimal("0"),
    ) -> Usage:
        """Record actual Provider usage, retaining it even when it crosses a limit."""

        self._validate_non_negative_int("input_tokens", input_tokens)
        self._validate_non_negative_int("output_tokens", output_tokens)
        self._validate_non_negative_decimal("cost", cost)

        now = self._read_clock()
        current = self._usage_at(now)
        candidate = self._build_usage(
            current,
            input_tokens=current.input_tokens + input_tokens,
            output_tokens=current.output_tokens + output_tokens,
            cost=current.cost + cost,
        )
        self._commit(candidate, now)
        self._ensure_within(candidate)
        return candidate

    def reserve_tool_call(self) -> Usage:
        """Reserve one tool execution before invoking the tool."""

        return self._reserve(tool_calls=1)

    def reserve_child(self, *, depth: int, child_budget: Budget | None = None) -> Usage:
        """Reserve a child and optionally validate its limits against remaining capacity."""

        self._validate_non_negative_int("depth", depth)
        if depth == 0:
            raise InvalidBudgetUsageError("depth", depth)
        now = self._read_clock()
        current = self._usage_at(now)
        candidate = self._build_usage(
            current,
            children=current.children + 1,
            max_depth_reached=max(current.max_depth_reached, depth),
        )
        self._ensure_within(candidate)
        if child_budget is not None:
            self._ensure_child_budget_within(
                child_budget,
                remaining=self._remaining_for(candidate),
            )
        self._commit(candidate, now)
        return candidate

    def snapshot(self, *, captured_at: datetime | None = None) -> BudgetSnapshot:
        """Create a serializable snapshot suitable for checkpoints and events."""

        used = self.usage
        values: dict[str, object] = {
            "budget": self._budget,
            "used": used,
            "remaining": self._remaining_for(used),
        }
        if captured_at is not None:
            values["captured_at"] = captured_at
        return BudgetSnapshot.model_validate(values)

    def _reserve(
        self,
        *,
        requests: int = 0,
        tool_calls: int = 0,
        children: int = 0,
        max_depth_reached: int | None = None,
    ) -> Usage:
        now = self._read_clock()
        current = self._usage_at(now)
        candidate = self._build_usage(
            current,
            requests=current.requests + requests,
            tool_calls=current.tool_calls + tool_calls,
            children=current.children + children,
            max_depth_reached=(
                current.max_depth_reached if max_depth_reached is None else max_depth_reached
            ),
        )
        self._ensure_within(candidate)
        self._commit(candidate, now)
        return candidate

    def _usage_at(self, now: float) -> Usage:
        elapsed = max(0.0, now - self._started_at)
        return self._build_usage(
            self._usage,
            duration_seconds=self._usage.duration_seconds + elapsed,
        )

    def _commit(self, usage: Usage, now: float) -> None:
        self._usage = usage
        self._started_at = now

    @staticmethod
    def _build_usage(
        usage: Usage,
        *,
        requests: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        tool_calls: int | None = None,
        duration_seconds: float | None = None,
        cost: Decimal | None = None,
        children: int | None = None,
        max_depth_reached: int | None = None,
    ) -> Usage:
        return Usage(
            requests=usage.requests if requests is None else requests,
            input_tokens=usage.input_tokens if input_tokens is None else input_tokens,
            output_tokens=usage.output_tokens if output_tokens is None else output_tokens,
            tool_calls=usage.tool_calls if tool_calls is None else tool_calls,
            duration_seconds=(
                usage.duration_seconds if duration_seconds is None else duration_seconds
            ),
            cost=usage.cost if cost is None else cost,
            children=usage.children if children is None else children,
            max_depth_reached=(
                usage.max_depth_reached if max_depth_reached is None else max_depth_reached
            ),
        )

    def _ensure_within(self, usage: Usage) -> None:
        checks: tuple[tuple[str, int | float, int | float], ...] = (
            ("requests", self._budget.max_requests, usage.requests),
            ("input_tokens", self._budget.max_input_tokens, usage.input_tokens),
            ("output_tokens", self._budget.max_output_tokens, usage.output_tokens),
            ("total_tokens", self._budget.max_total_tokens, usage.total_tokens),
            ("tool_calls", self._budget.max_tool_calls, usage.tool_calls),
            (
                "duration_seconds",
                self._budget.max_duration_seconds,
                usage.duration_seconds,
            ),
            ("children", self._budget.max_children, usage.children),
            ("depth", self._budget.max_depth, usage.max_depth_reached),
        )
        for resource, limit, attempted in checks:
            if attempted > limit:
                raise BudgetExceededError(resource, limit, attempted)

        if self._budget.max_cost is not None and usage.cost > self._budget.max_cost:
            raise BudgetExceededError("cost", self._budget.max_cost, usage.cost)

    @staticmethod
    def _ensure_child_budget_within(
        child_budget: Budget,
        *,
        remaining: BudgetRemaining,
    ) -> None:
        checks: tuple[tuple[str, int | float, int | float], ...] = (
            ("child.requests", remaining.requests, child_budget.max_requests),
            (
                "child.input_tokens",
                remaining.input_tokens,
                child_budget.max_input_tokens,
            ),
            (
                "child.output_tokens",
                remaining.output_tokens,
                child_budget.max_output_tokens,
            ),
            (
                "child.total_tokens",
                remaining.total_tokens,
                child_budget.max_total_tokens,
            ),
            ("child.tool_calls", remaining.tool_calls, child_budget.max_tool_calls),
            (
                "child.duration_seconds",
                remaining.duration_seconds,
                child_budget.max_duration_seconds,
            ),
            ("child.children", remaining.children, child_budget.max_children),
            ("child.depth", remaining.depth, child_budget.max_depth),
        )
        for resource, limit, requested in checks:
            if requested > limit:
                raise BudgetExceededError(resource, limit, requested)

        if remaining.cost is not None:
            if child_budget.max_cost is None or child_budget.max_cost > remaining.cost:
                raise BudgetExceededError("child.cost", remaining.cost, child_budget.max_cost)

    def _remaining_for(self, usage: Usage) -> BudgetRemaining:
        return BudgetRemaining(
            requests=max(0, self._budget.max_requests - usage.requests),
            input_tokens=max(0, self._budget.max_input_tokens - usage.input_tokens),
            output_tokens=max(0, self._budget.max_output_tokens - usage.output_tokens),
            total_tokens=max(0, self._budget.max_total_tokens - usage.total_tokens),
            tool_calls=max(0, self._budget.max_tool_calls - usage.tool_calls),
            duration_seconds=max(
                0.0,
                self._budget.max_duration_seconds - usage.duration_seconds,
            ),
            cost=(
                None
                if self._budget.max_cost is None
                else max(Decimal("0"), self._budget.max_cost - usage.cost)
            ),
            children=max(0, self._budget.max_children - usage.children),
            depth=max(0, self._budget.max_depth - usage.max_depth_reached),
        )

    def _read_clock(self) -> float:
        value = self._clock()
        if not math.isfinite(value):
            raise InvalidBudgetUsageError("clock", value)
        return value

    @staticmethod
    def _validate_non_negative_int(resource: str, value: object) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidBudgetUsageError(resource, value)

    @staticmethod
    def _validate_non_negative_decimal(resource: str, value: object) -> None:
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise InvalidBudgetUsageError(resource, value)
