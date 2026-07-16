"""Rich Live and bounded non-TTY adapters for the neutral Run display state."""

from __future__ import annotations

import json
import time

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from agentcell.display import (
    ActivityStatus,
    RunDisplayActivity,
    RunDisplayPhase,
    RunDisplayProjector,
    RunDisplayState,
    redact_display_text,
)
from agentcell.events import DomainEvent, EventPayload, EventType

_MAX_ACTIVITIES = 6


class CliEventRenderer:
    """Sequence-aware JSON, Rich Live, or bounded non-TTY event renderer."""

    def __init__(
        self,
        *,
        enabled: bool,
        json_events: bool = False,
        output: Console,
        projector: RunDisplayProjector | None = None,
    ) -> None:
        self.enabled = enabled
        self.json_events = json_events
        self.output = output
        self.projector = projector or RunDisplayProjector()
        self.answer_printed = False
        self._live: Live | None = None
        self._printed_milestones: set[str] = set()
        self._last_event_at = time.monotonic()
        self._last_tick_at = self._last_event_at

    @property
    def last_sequence(self) -> int:
        return self.projector.state.last_sequence

    @property
    def text_streamed(self) -> bool:
        """Backward-compatible name meaning the final answer was already rendered."""

        return self.answer_printed

    def render(self, event: DomainEvent[EventPayload]) -> None:
        previous = self.projector.state
        state = self.projector.apply(event)
        if state.last_sequence == previous.last_sequence:
            return
        self._last_event_at = time.monotonic()
        if not self.enabled:
            return
        if self.json_events:
            self.output.print(
                json.dumps(
                    {
                        "event_id": str(event.event_id),
                        "run_id": str(event.run_id),
                        "sequence": event.sequence,
                        "event_type": event.event_type.value,
                        "occurred_at": event.occurred_at.isoformat(),
                        "payload": event.safe_payload(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                markup=False,
                soft_wrap=True,
            )
            return
        if self.output.is_terminal:
            self._render_terminal(state)
        else:
            self._render_milestone(event.event_type, state)

    def tick(self) -> None:
        """Refresh elapsed wait feedback even when no persisted event arrives."""

        if (
            not self.enabled
            or self.json_events
            or not self.output.is_terminal
            or self._live is None
        ):
            return
        now = time.monotonic()
        if now - self._last_tick_at < 0.25:
            return
        self._last_tick_at = now
        self._live.update(
            _live_renderable(
                self.projector.state,
                width=self.output.size.width,
                height=self.output.size.height,
                idle_seconds=now - self._last_event_at,
            ),
            refresh=True,
        )

    def suspend(self) -> None:
        self._stop_live()

    def finish(self) -> None:
        self._stop_live()
        state = self.projector.state
        if (
            self.enabled
            and not self.json_events
            and state.phase is RunDisplayPhase.COMPLETED
            and state.answer
            and not self.answer_printed
        ):
            self.output.print(state.answer, markup=False, soft_wrap=True)
            self.answer_printed = True

    def _render_terminal(self, state: RunDisplayState) -> None:
        if state.phase in {
            RunDisplayPhase.COMPLETED,
            RunDisplayPhase.FAILED,
            RunDisplayPhase.CANCELLED,
            RunDisplayPhase.WAITING_APPROVAL,
        }:
            self._stop_live()
            if (
                state.phase is RunDisplayPhase.COMPLETED
                and state.answer
                and not self.answer_printed
            ):
                self.output.print(state.answer, markup=False, soft_wrap=True)
                self.answer_printed = True
            return
        renderable = _live_renderable(
            state,
            width=self.output.size.width,
            height=self.output.size.height,
        )
        if self._live is None:
            self._live = Live(
                renderable,
                console=self.output,
                auto_refresh=False,
                refresh_per_second=8,
                transient=True,
                vertical_overflow="crop",
            )
            self._live.start(refresh=True)
        else:
            self._live.update(renderable, refresh=True)

    def _render_milestone(self, event_type: EventType, state: RunDisplayState) -> None:
        if event_type is EventType.TOOL_COMPLETED and state.activities:
            activity = state.activities[-1]
            key = f"completed:{activity.key}"
            if key not in self._printed_milestones:
                self._printed_milestones.add(key)
                suffix = _activity_suffix(activity)
                self.output.print(f"✓ {activity.label}{suffix}", markup=False)
        elif event_type is EventType.TOOL_APPROVAL_REQUIRED and state.active_approval is not None:
            key = f"approval:{state.active_approval.approval_id}"
            if key not in self._printed_milestones:
                self._printed_milestones.add(key)
                self.output.print(
                    f"等待审批 · {state.active_approval.tool_name}",
                    markup=False,
                )
        elif event_type is EventType.RUN_COMPLETED and state.answer and not self.answer_printed:
            self.output.print(state.answer, markup=False, soft_wrap=True)
            self.answer_printed = True

    def _stop_live(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None


def _live_renderable(
    state: RunDisplayState,
    *,
    width: int,
    height: int,
    idle_seconds: float = 0.0,
) -> RenderableType:
    agent = state.active_agent
    identity = "Agent"
    if agent is not None:
        identity = agent.agent_id
        model = agent.model or agent.provider or agent.model_ref
        if model:
            identity = f"{identity} / {model}"
    lines = Text()
    for activity in state.activities[-_MAX_ACTIVITIES:]:
        icon = {
            ActivityStatus.PENDING: "○",
            ActivityStatus.RUNNING: "●",
            ActivityStatus.COMPLETED: "✓",
            ActivityStatus.FAILED: "×",
            ActivityStatus.WAITING: "!",
        }[activity.status]
        lines.append(f"{icon} {activity.label}{_activity_suffix(activity)}\n")
    if not state.activities:
        lines.append("● 正在分析任务\n")
    if idle_seconds >= 1:
        lines.append(f"等待新响应 {int(idle_seconds)} 秒\n", style="dim")
    budget = state.budget
    lines.append(
        f"请求 {budget.requests}/{_limit(budget.max_requests)} · "
        f"工具 {budget.tool_calls}/{_limit(budget.max_tool_calls)} · "
        f"Token {_compact_number(budget.total_tokens)}/{_compact_limit(budget.max_total_tokens)} · "
        f"缓存命中 {budget.cache_hit_ratio:.0%}",
        style="dim",
    )
    panel = Panel(
        lines,
        title=f"Agent 正在工作 · {identity}",
        border_style="cyan",
        width=max(30, min(max(30, width), 100)),
    )
    candidate = state.answer_candidate or "正在生成最终分析……"
    candidate = streaming_answer_preview(candidate, width=width, height=height)
    return Group(panel, Text("回答", style="bold green"), Text(candidate, overflow="ellipsis"))


def streaming_answer_preview(value: str, *, width: int, height: int) -> str:
    """Keep the newest streamed text visible inside a bounded Rich Live viewport."""

    line_budget = max(3, min(12, height - 12))
    character_budget = max(240, min(2_400, max(30, width) * line_budget))
    lines = value.splitlines()
    selected = "\n".join(lines[-line_budget:]) if lines else value
    truncated = len(lines) > line_budget or len(selected) > character_budget
    if len(selected) > character_budget:
        selected = selected[-character_budget:]
    selected = redact_display_text(selected, limit=character_budget + 1)
    return f"…\n{selected}" if truncated else selected


def _activity_suffix(activity: RunDisplayActivity) -> str:
    values: list[str] = []
    if activity.agent_id:
        values.append(activity.agent_id)
    if activity.tool_name:
        values.append(activity.tool_name)
    if activity.count > 1:
        values.append(f"{activity.count} 次")
    if activity.detail:
        values.append(activity.detail)
    return "" if not values else " · " + " · ".join(values)


def _limit(value: int | None) -> str:
    return "?" if value is None else str(value)


def _compact_limit(value: int | None) -> str:
    return "?" if value is None else _compact_number(value)


def _compact_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)
