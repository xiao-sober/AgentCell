"""Transport-neutral Run display projection shared by CLI and Web adapters."""

from __future__ import annotations

import re
from collections import OrderedDict
from enum import StrEnum
from typing import cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agentcell.events import DomainEvent, EventPayload, EventType, JsonValue

_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|password|secret|access[_-]?token|refresh[_-]?token)"
    r"(\s*[:=]\s*)([^\s,;\]\}\)]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_MAX_DISPLAY_TEXT = 16_000
_MAX_DETAIL = 180
_MAX_ACTIVITIES = 6


class RunDisplayPhase(StrEnum):
    CREATED = "created"
    WORKING = "working"
    ANSWERING = "answering"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActivityStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING = "waiting"


class RunDisplayActivity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    label: str
    status: ActivityStatus
    count: int = Field(default=1, ge=1, strict=True)
    agent_id: str | None = None
    tool_name: str | None = None
    detail: str | None = None


class RunDisplayBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requests: int = Field(default=0, ge=0, strict=True)
    max_requests: int | None = Field(default=None, ge=0, strict=True)
    tool_calls: int = Field(default=0, ge=0, strict=True)
    max_tool_calls: int | None = Field(default=None, ge=0, strict=True)
    input_tokens: int = Field(default=0, ge=0, strict=True)
    output_tokens: int = Field(default=0, ge=0, strict=True)
    max_total_tokens: int | None = Field(default=None, ge=0, strict=True)
    cache_read_tokens: int = Field(default=0, ge=0, strict=True)
    cache_write_tokens: int = Field(default=0, ge=0, strict=True)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache_hit_ratio(self) -> float:
        if self.input_tokens == 0:
            return 0.0
        return min(1.0, self.cache_read_tokens / self.input_tokens)


class RunDisplayApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: UUID | None = None
    tool_name: str
    risk: str | None = None
    impact: str | None = None


class RunDisplayAgent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    model_ref: str | None = None
    provider: str | None = None
    model: str | None = None


class RunDisplayState(BaseModel):
    """Stable DTO reconstructed from one ordered, persistence-safe event sequence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID | None = None
    last_sequence: int = Field(default=0, ge=0, strict=True)
    phase: RunDisplayPhase = RunDisplayPhase.CREATED
    activities: tuple[RunDisplayActivity, ...] = ()
    answer_candidate: str = ""
    answer: str | None = None
    budget: RunDisplayBudget = Field(default_factory=RunDisplayBudget)
    active_approval: RunDisplayApproval | None = None
    active_agent: RunDisplayAgent | None = None
    error_code: str | None = None


class ToolDisplaySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pending: str
    running: str
    completed: str
    failed: str
    detail_fields: tuple[str, ...] = ()
    aggregate: bool = False


class ToolDisplayCatalog:
    """Central allowlisted human wording and argument summaries for tools."""

    _DEFAULT = ToolDisplaySpec(
        pending="准备工具操作",
        running="正在执行工具操作",
        completed="工具操作已完成",
        failed="工具操作失败",
    )
    _SPECS: dict[str, ToolDisplaySpec] = {
        "workspace.list": ToolDisplaySpec(
            pending="准备扫描目录",
            running="正在扫描目录",
            completed="目录扫描完成",
            failed="目录扫描失败",
            detail_fields=("path",),
            aggregate=True,
        ),
        "workspace.read": ToolDisplaySpec(
            pending="准备读取文件",
            running="正在读取文件",
            completed="文件读取完成",
            failed="文件读取失败",
            detail_fields=("path",),
            aggregate=True,
        ),
        "workspace.search": ToolDisplaySpec(
            pending="准备搜索代码",
            running="正在搜索代码",
            completed="代码搜索完成",
            failed="代码搜索失败",
            detail_fields=("query", "path"),
            aggregate=True,
        ),
        "workspace.write": ToolDisplaySpec(
            pending="准备写入文件",
            running="正在写入文件",
            completed="文件写入完成",
            failed="文件写入失败",
            detail_fields=("path",),
        ),
        "workspace.patch": ToolDisplaySpec(
            pending="准备应用补丁",
            running="正在应用补丁",
            completed="补丁应用完成",
            failed="补丁应用失败",
            detail_fields=("path",),
        ),
        "workspace.delete": ToolDisplaySpec(
            pending="准备删除文件",
            running="正在删除文件",
            completed="文件删除完成",
            failed="文件删除失败",
            detail_fields=("path",),
        ),
        "shell.test": ToolDisplaySpec(
            pending="准备运行检查",
            running="正在运行检查",
            completed="检查运行完成",
            failed="检查运行失败",
            detail_fields=("command", "args", "cwd"),
        ),
        "shell.run": ToolDisplaySpec(
            pending="准备执行命令",
            running="正在执行命令",
            completed="命令执行完成",
            failed="命令执行失败",
            detail_fields=("command", "args", "cwd"),
        ),
        "http.request": ToolDisplaySpec(
            pending="准备请求网络资源",
            running="正在请求网络资源",
            completed="网络请求完成",
            failed="网络请求失败",
            detail_fields=("method", "url"),
        ),
        "agent.delegate": ToolDisplaySpec(
            pending="准备委派子任务",
            running="正在执行子任务",
            completed="子任务已完成",
            failed="子任务失败",
            detail_fields=("agent_id",),
        ),
    }

    @classmethod
    def get(cls, tool_name: str) -> ToolDisplaySpec:
        return cls._SPECS.get(tool_name, cls._DEFAULT)

    @classmethod
    def detail(cls, tool_name: str, arguments: object) -> str | None:
        if not isinstance(arguments, dict):
            return None
        values = cast(dict[object, object], arguments)
        parts: list[str] = []
        for field in cls.get(tool_name).detail_fields:
            value = values.get(field)
            if field == "args" and isinstance(value, (list, tuple)):
                argument_items = cast(list[object] | tuple[object, ...], value)
                rendered_args = [
                    str(item)
                    for item in argument_items
                    if isinstance(item, (str, int, float))
                ]
                value = " ".join(rendered_args)
            if not isinstance(value, (str, int, float, bool)):
                continue
            rendered = str(value)
            if field == "url":
                rendered = _safe_url(rendered)
            rendered = redact_display_text(rendered, limit=96)
            if rendered:
                parts.append(rendered)
        return " · ".join(parts)[:_MAX_DETAIL] or None


class RunDisplayProjector:
    """Pure ordered-event projection used by terminal and future Web DTO adapters."""

    def __init__(self) -> None:
        self._state = RunDisplayState()
        self._activities: OrderedDict[str, RunDisplayActivity] = OrderedDict()
        self._call_tools: dict[str, str] = {}
        self._call_details: dict[str, str | None] = {}

    @property
    def state(self) -> RunDisplayState:
        return self._state

    def apply(self, event: DomainEvent[EventPayload]) -> RunDisplayState:
        if event.sequence <= self._state.last_sequence:
            return self._state
        safe = event.safe_payload()
        self._state = self._state.model_copy(
            update={"run_id": event.run_id, "last_sequence": event.sequence}
        )
        event_type = event.event_type
        if event_type is EventType.RUN_STARTED:
            self._on_started(safe)
        elif event_type is EventType.RUN_STATUS_CHANGED:
            self._on_status(safe)
        elif event_type is EventType.MODEL_REQUESTED:
            self._state = self._state.model_copy(update={"phase": RunDisplayPhase.WORKING})
        elif event_type is EventType.MODEL_TEXT_DELTA:
            delta = safe.get("delta")
            if isinstance(delta, str):
                candidate = redact_display_text(
                    self._state.answer_candidate + delta,
                    limit=_MAX_DISPLAY_TEXT,
                )
                self._state = self._state.model_copy(
                    update={
                        "phase": RunDisplayPhase.ANSWERING,
                        "answer_candidate": candidate,
                    }
                )
        elif event_type is EventType.MODEL_OUTPUT_REJECTED:
            self._state = self._state.model_copy(
                update={
                    "phase": RunDisplayPhase.WORKING,
                    "answer_candidate": "",
                }
            )
            self._put_activity(
                RunDisplayActivity(
                    key="model.output.retry",
                    label="正在重新生成安全回答",
                    status=ActivityStatus.RUNNING,
                )
            )
        elif event_type in {
            EventType.TOOL_PROPOSED,
            EventType.TOOL_STARTED,
            EventType.TOOL_COMPLETED,
            EventType.TOOL_FAILED,
        }:
            self._on_tool(event_type, safe)
        elif event_type is EventType.TOOL_APPROVAL_REQUIRED:
            self._on_approval(safe)
        elif event_type in {EventType.TOOL_APPROVED, EventType.TOOL_REJECTED}:
            self._state = self._state.model_copy(
                update={
                    "phase": RunDisplayPhase.WORKING,
                    "active_approval": None,
                }
            )
        elif event_type is EventType.BUDGET_UPDATED:
            self._on_budget(safe)
        elif event_type in {EventType.AGENT_CHILD_STARTED, EventType.AGENT_CHILD_COMPLETED}:
            self._on_child(event_type, safe)
        elif event_type is EventType.CONTEXT_COMPACTED:
            self._put_activity(
                RunDisplayActivity(
                    key="context.compacted",
                    label="上下文已压缩",
                    status=ActivityStatus.COMPLETED,
                )
            )
        elif event_type in {
            EventType.FILE_CHANGE_PREPARED,
            EventType.FILE_CHANGE_APPLIED,
            EventType.FILE_CHANGE_COMPLETED,
            EventType.FILE_CHANGE_CONFLICT,
            EventType.FILE_CHANGE_REVERTED,
        }:
            self._on_file_change(event_type, safe)
        elif event_type is EventType.RUN_COMPLETED:
            self._state = self._state.model_copy(
                update={
                    "phase": RunDisplayPhase.COMPLETED,
                    "answer": self._state.answer_candidate or self._state.answer,
                    "active_approval": None,
                }
            )
        elif event_type is EventType.RUN_FAILED:
            code = safe.get("code")
            self._state = self._state.model_copy(
                update={
                    "phase": RunDisplayPhase.FAILED,
                    "answer_candidate": "",
                    "active_approval": None,
                    "error_code": code if isinstance(code, str) else "run_failed",
                }
            )
        elif event_type is EventType.RUN_CANCELLED:
            self._state = self._state.model_copy(
                update={
                    "phase": RunDisplayPhase.CANCELLED,
                    "answer_candidate": "",
                    "active_approval": None,
                }
            )
        self._sync_activities()
        return self._state

    def _on_started(self, safe: dict[str, JsonValue]) -> None:
        agent_id = safe.get("agent_id")
        if not isinstance(agent_id, str):
            return
        self._state = self._state.model_copy(
            update={
                "phase": RunDisplayPhase.WORKING,
                "active_agent": RunDisplayAgent(
                    agent_id=agent_id,
                    model_ref=_optional_string(safe.get("model_ref")),
                    provider=_optional_string(safe.get("provider")),
                    model=_optional_string(safe.get("model")),
                ),
                "budget": _budget_from_limit_payload(safe.get("budget"), self._state.budget),
            }
        )

    def _on_status(self, safe: dict[str, JsonValue]) -> None:
        status = safe.get("status")
        phases = {
            "created": RunDisplayPhase.CREATED,
            "running": RunDisplayPhase.WORKING,
            "waiting_approval": RunDisplayPhase.WAITING_APPROVAL,
            "paused": RunDisplayPhase.WORKING,
            "completed": RunDisplayPhase.COMPLETED,
            "failed": RunDisplayPhase.FAILED,
            "cancelled": RunDisplayPhase.CANCELLED,
        }
        if isinstance(status, str) and status in phases:
            self._state = self._state.model_copy(update={"phase": phases[status]})

    def _on_tool(self, event_type: EventType, safe: dict[str, JsonValue]) -> None:
        data = _event_data(safe)
        details = data
        if event_type is EventType.TOOL_FAILED:
            details = _mapping(safe.get("details"))
        call_id = str(details.get("provider_call_id") or details.get("call_id") or "unknown")
        tool_name = str(details.get("tool_name") or self._call_tools.get(call_id) or "unknown")
        spec = ToolDisplayCatalog.get(tool_name)
        if event_type is EventType.TOOL_PROPOSED:
            if self._state.answer_candidate:
                self._put_activity(
                    RunDisplayActivity(
                        key="model.stage-summary",
                        label="阶段分析已完成",
                        status=ActivityStatus.COMPLETED,
                    )
                )
            self._state = self._state.model_copy(
                update={"phase": RunDisplayPhase.WORKING, "answer_candidate": ""}
            )
            self._call_tools[call_id] = tool_name
            self._call_details[call_id] = ToolDisplayCatalog.detail(
                tool_name, data.get("arguments")
            )
        detail = self._call_details.get(call_id)
        status, label = {
            EventType.TOOL_PROPOSED: (ActivityStatus.PENDING, spec.pending),
            EventType.TOOL_STARTED: (ActivityStatus.RUNNING, spec.running),
            EventType.TOOL_COMPLETED: (ActivityStatus.COMPLETED, spec.completed),
            EventType.TOOL_FAILED: (ActivityStatus.FAILED, spec.failed),
        }[event_type]
        key = f"tool:{tool_name}" if spec.aggregate else f"tool:{call_id}"
        existing = self._activities.get(key)
        count = 1
        if existing is not None:
            count = existing.count
            if event_type is EventType.TOOL_PROPOSED and spec.aggregate:
                count += 1
        self._put_activity(
            RunDisplayActivity(
                key=key,
                label=label,
                status=status,
                count=count,
                agent_id=_optional_string(details.get("source_agent_id")),
                tool_name=tool_name,
                detail=detail or (None if existing is None else existing.detail),
            )
        )

    def _on_approval(self, safe: dict[str, JsonValue]) -> None:
        data = _event_data(safe)
        approval_id = None
        raw_id = data.get("approval_id")
        if isinstance(raw_id, str):
            try:
                approval_id = UUID(raw_id)
            except ValueError:
                pass
        self._state = self._state.model_copy(
            update={
                "phase": RunDisplayPhase.WAITING_APPROVAL,
                "active_approval": RunDisplayApproval(
                    approval_id=approval_id,
                    tool_name=str(data.get("tool_name") or "unknown"),
                    risk=_optional_string(data.get("risk")),
                    impact=(
                        redact_display_text(str(data["impact"]), limit=_MAX_DETAIL)
                        if isinstance(data.get("impact"), str)
                        else None
                    ),
                ),
            }
        )

    def _on_budget(self, safe: dict[str, JsonValue]) -> None:
        data = _event_data(safe)
        snapshot = _mapping(data.get("snapshot"))
        if snapshot:
            used = _mapping(snapshot.get("used"))
            budget = _mapping(snapshot.get("budget"))
            self._state = self._state.model_copy(
                update={"budget": _budget_from_values(used, budget, self._state.budget)}
            )
            return
        usage = _mapping(data.get("usage"))
        if usage:
            self._state = self._state.model_copy(
                update={"budget": _budget_from_values(usage, {}, self._state.budget)}
            )

    def _on_child(self, event_type: EventType, safe: dict[str, JsonValue]) -> None:
        agent_id = _optional_string(safe.get("agent_id")) or "child"
        completed = event_type is EventType.AGENT_CHILD_COMPLETED
        self._put_activity(
            RunDisplayActivity(
                key=f"child:{agent_id}",
                label=(
                    f"子 Agent {agent_id} 已完成" if completed else f"子 Agent {agent_id} 正在工作"
                ),
                status=ActivityStatus.COMPLETED if completed else ActivityStatus.RUNNING,
            )
        )

    def _on_file_change(self, event_type: EventType, safe: dict[str, JsonValue]) -> None:
        data = _event_data(safe)
        path = _optional_string(data.get("path"))
        labels = {
            EventType.FILE_CHANGE_PREPARED: ("文件变更已准备", ActivityStatus.PENDING),
            EventType.FILE_CHANGE_APPLIED: ("文件变更已应用", ActivityStatus.RUNNING),
            EventType.FILE_CHANGE_COMPLETED: ("文件变更已完成", ActivityStatus.COMPLETED),
            EventType.FILE_CHANGE_CONFLICT: ("文件变更发生冲突", ActivityStatus.FAILED),
            EventType.FILE_CHANGE_REVERTED: ("文件变更已回滚", ActivityStatus.COMPLETED),
        }
        label, status = labels[event_type]
        key = f"change:{data.get('change_id') or path or 'unknown'}"
        self._put_activity(
            RunDisplayActivity(
                key=key,
                label=label,
                status=status,
                detail=None if path is None else redact_display_text(path, limit=_MAX_DETAIL),
            )
        )

    def _put_activity(self, activity: RunDisplayActivity) -> None:
        self._activities[activity.key] = activity
        self._activities.move_to_end(activity.key)
        while len(self._activities) > _MAX_ACTIVITIES:
            self._activities.popitem(last=False)

    def _sync_activities(self) -> None:
        self._state = self._state.model_copy(
            update={"activities": tuple(self._activities.values())}
        )


def redact_display_text(value: str, *, limit: int) -> str:
    """Remove common inline credentials and cap product-surface text."""

    redacted = _CREDENTIAL_ASSIGNMENT.sub(r"\1\2[REDACTED]", value)
    redacted = _BEARER.sub("Bearer [REDACTED]", redacted)
    if len(redacted) <= limit:
        return redacted
    return redacted[: max(0, limit - 1)] + "…"


def _event_data(safe: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return _mapping(safe.get("data"))


def _mapping(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        return {}
    return cast(dict[str, JsonValue], value)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _budget_from_limit_payload(value: object, current: RunDisplayBudget) -> RunDisplayBudget:
    return _budget_from_values({}, _mapping(value), current)


def _budget_from_values(
    used: dict[str, JsonValue],
    budget: dict[str, JsonValue],
    current: RunDisplayBudget,
) -> RunDisplayBudget:
    return current.model_copy(
        update={
            "requests": _int_value(used.get("requests"), current.requests),
            "tool_calls": _int_value(used.get("tool_calls"), current.tool_calls),
            "input_tokens": _int_value(used.get("input_tokens"), current.input_tokens),
            "output_tokens": _int_value(used.get("output_tokens"), current.output_tokens),
            "cache_read_tokens": _int_value(
                used.get("cache_read_tokens"), current.cache_read_tokens
            ),
            "cache_write_tokens": _int_value(
                used.get("cache_write_tokens"), current.cache_write_tokens
            ),
            "max_requests": _optional_int(budget.get("max_requests"), current.max_requests),
            "max_tool_calls": _optional_int(budget.get("max_tool_calls"), current.max_tool_calls),
            "max_total_tokens": _optional_int(
                budget.get("max_total_tokens"), current.max_total_tokens
            ),
        }
    )


def _int_value(value: object, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return default


def _optional_int(value: object, default: int | None) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return default


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[invalid URL]"
    host = parsed.hostname or ""
    port = None
    try:
        port = parsed.port
    except ValueError:
        pass
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
