"""End-to-end safety tests for the first read-only workspace toolset."""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest

from agentcell.budgets import Budget, BudgetTracker
from agentcell.errors import (
    WorkspaceBinaryFileError,
    WorkspacePathDeniedError,
)
from agentcell.events import EventPayload, EventType
from agentcell.policy import CapabilityLease
from agentcell.tools import (
    ToolCall,
    ToolExecutionContext,
    ToolExecutor,
    ToolRegistry,
    WorkspaceListResult,
    WorkspaceReadResult,
    WorkspaceSearchResult,
    register_workspace_tools,
)


@dataclass
class RecordingEventSink:
    events: list[tuple[EventType, EventPayload]] = field(default_factory=lambda: [])

    async def emit(self, event_type: EventType, payload: EventPayload) -> None:
        self.events.append((event_type, payload))


def _executor() -> ToolExecutor:
    registry = ToolRegistry()
    register_workspace_tools(registry)
    return ToolExecutor(registry)


def _context(workspace: Path, *, scopes: list[str] | None = None) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace=workspace,
        lease=CapabilityLease(filesystem_read=tuple(scopes or ["."])),
        budget=BudgetTracker(
            Budget(
                max_requests=0,
                max_input_tokens=0,
                max_output_tokens=0,
                max_total_tokens=0,
                max_tool_calls=20,
                max_duration_seconds=30,
                max_cost=Decimal("0"),
                max_children=0,
                max_depth=0,
            )
        ),
        events=RecordingEventSink(),
    )


@pytest.mark.asyncio
async def test_list_hides_sensitive_files_and_returns_stable_metadata(tmp_path: Path) -> None:
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "a.txt").write_text("aa", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=value", encoding="utf-8")
    (tmp_path / ".hidden.txt").write_text("hidden", encoding="utf-8")
    (tmp_path / "src").mkdir()

    result = await _executor().execute(
        ToolCall(tool_name="workspace.list", arguments={"path": "."}),
        _context(tmp_path),
    )
    listing = WorkspaceListResult.model_validate(result.output)

    assert [entry.name for entry in listing.entries] == ["a.txt", "b.txt", "src"]
    assert listing.entries[0].size_bytes == 2
    assert not listing.truncated


@pytest.mark.asyncio
async def test_read_is_chunked_on_complete_utf8_boundaries(tmp_path: Path) -> None:
    (tmp_path / "unicode.txt").write_text("你好", encoding="utf-8")
    context = _context(tmp_path)
    executor = _executor()

    first_raw = await executor.execute(
        ToolCall(
            tool_name="workspace.read",
            arguments={"path": "unicode.txt", "max_bytes": 4},
        ),
        context,
    )
    first = WorkspaceReadResult.model_validate(first_raw.output)
    second_raw = await executor.execute(
        ToolCall(
            tool_name="workspace.read",
            arguments={
                "path": "unicode.txt",
                "offset_bytes": first.next_offset_bytes,
                "max_bytes": 4,
            },
        ),
        context,
    )
    second = WorkspaceReadResult.model_validate(second_raw.output)

    assert first.content == "你"
    assert first.bytes_read == 3
    assert first.next_offset_bytes == 3
    assert first.truncated
    assert second.content == "好"
    assert not second.truncated


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [1, 2, 4])
async def test_read_aligns_arbitrary_utf8_continuation_offsets(
    tmp_path: Path,
    requested: int,
) -> None:
    (tmp_path / "unicode.txt").write_text("你🙂好", encoding="utf-8")

    raw = await _executor().execute(
        ToolCall(
            tool_name="workspace.read",
            arguments={
                "path": "unicode.txt",
                "offset_bytes": requested,
                "max_bytes": 4,
            },
        ),
        _context(tmp_path),
    )
    result = WorkspaceReadResult.model_validate(raw.output)

    assert result.requested_offset_bytes == requested
    assert result.offset_bytes == result.actual_offset_bytes
    assert result.actual_offset_bytes == (0 if requested < 3 else 3)
    assert result.content in {"你", "🙂"}


@pytest.mark.asyncio
async def test_read_rejects_isolated_utf8_continuation_byte(tmp_path: Path) -> None:
    (tmp_path / "invalid.txt").write_bytes(b"a\x80b")

    with pytest.raises(WorkspaceBinaryFileError):
        await _executor().execute(
            ToolCall(
                tool_name="workspace.read",
                arguments={
                    "path": "invalid.txt",
                    "offset_bytes": 1,
                    "max_bytes": 4,
                },
            ),
            _context(tmp_path),
        )


@pytest.mark.asyncio
async def test_literal_search_is_bounded_and_skips_binary_files(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("AgentCell\nother\nagentcell runtime\n", encoding="utf-8")
    (src / "binary.bin").write_bytes(b"AgentCell\x00binary")
    (src / "large.txt").write_text("AgentCell" * 100, encoding="utf-8")

    raw = await _executor().execute(
        ToolCall(
            tool_name="workspace.search",
            arguments={
                "query": "agentcell",
                "path": "src",
                "max_results": 10,
                "max_file_bytes": 100,
            },
        ),
        _context(tmp_path, scopes=["src"]),
    )
    result = WorkspaceSearchResult.model_validate(raw.output)

    assert [(match.path, match.line) for match in result.matches] == [
        ("src/a.py", 1),
        ("src/a.py", 3),
    ]
    assert result.skipped_large_or_binary == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "..\\outside.txt",
        "C:/Windows/System32/drivers/etc/hosts",
        "\\\\server\\share\\secret.txt",
    ],
)
async def test_read_rejects_traversal_and_absolute_paths(tmp_path: Path, path: str) -> None:
    with pytest.raises(WorkspacePathDeniedError):
        await _executor().execute(
            ToolCall(tool_name="workspace.read", arguments={"path": path}),
            _context(tmp_path),
        )


@pytest.mark.asyncio
async def test_sensitive_files_are_denied_even_with_root_lease(tmp_path: Path) -> None:
    (tmp_path / ".env.local").write_text("API_KEY=secret", encoding="utf-8")

    with pytest.raises(WorkspacePathDeniedError, match="sensitive"):
        await _executor().execute(
            ToolCall(tool_name="workspace.read", arguments={"path": ".env.local"}),
            _context(tmp_path),
        )


@pytest.mark.asyncio
async def test_read_scope_cannot_access_sibling_directory(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "allowed.txt").write_text("allowed", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "denied.txt").write_text("denied", encoding="utf-8")
    context = _context(tmp_path, scopes=["src"])

    allowed_raw = await _executor().execute(
        ToolCall(tool_name="workspace.read", arguments={"path": "src/allowed.txt"}),
        context,
    )
    assert WorkspaceReadResult.model_validate(allowed_raw.output).content == "allowed"

    with pytest.raises(WorkspacePathDeniedError, match="lease"):
        await _executor().execute(
            ToolCall(tool_name="workspace.read", arguments={"path": "docs/denied.txt"}),
            context,
        )


@pytest.mark.asyncio
async def test_symlink_escape_is_rejected_when_platform_allows_symlinks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    link = workspace / "escape"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError as error:
        if os.name != "nt":
            pytest.skip(f"symlink creation unavailable: {error}")
        junction = await asyncio.to_thread(
            subprocess.run,
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=False,
            capture_output=True,
        )
        if junction.returncode != 0:
            pytest.skip("symlink and directory junction creation are unavailable")

    with pytest.raises(WorkspacePathDeniedError, match="escapes"):
        await _executor().execute(
            ToolCall(tool_name="workspace.read", arguments={"path": "escape/secret.txt"}),
            _context(workspace),
        )
    with pytest.raises(WorkspacePathDeniedError, match="link|reparse"):
        await _executor().execute(
            ToolCall(
                tool_name="workspace.write",
                arguments={"path": "escape/new.txt", "content": "denied"},
            ),
            ToolExecutionContext(
                workspace=workspace,
                lease=CapabilityLease(
                    filesystem_read=(".",),
                    filesystem_write=(".",),
                ),
                budget=_context(workspace).budget,
                events=RecordingEventSink(),
            ),
            approval_granted=True,
        )


@pytest.mark.asyncio
async def test_binary_file_read_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "binary.dat").write_bytes(b"text\x00binary")

    with pytest.raises(WorkspaceBinaryFileError):
        await _executor().execute(
            ToolCall(tool_name="workspace.read", arguments={"path": "binary.dat"}),
            _context(tmp_path),
        )


@pytest.mark.asyncio
async def test_list_and_search_report_truncation(tmp_path: Path) -> None:
    for index in range(4):
        (tmp_path / f"file-{index}.txt").write_text("needle", encoding="utf-8")

    listed_raw = await _executor().execute(
        ToolCall(
            tool_name="workspace.list",
            arguments={"path": ".", "max_entries": 2},
        ),
        _context(tmp_path),
    )
    searched_raw = await _executor().execute(
        ToolCall(
            tool_name="workspace.search",
            arguments={"query": "needle", "max_results": 2},
        ),
        _context(tmp_path),
    )

    assert WorkspaceListResult.model_validate(listed_raw.output).truncated
    assert WorkspaceSearchResult.model_validate(searched_raw.output).truncated
