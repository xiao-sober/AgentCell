"""Read-only workspace tools with path, symlink, size, and text safety boundaries."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import os
import re
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentcell.errors import (
    ConfigurationError,
    WorkspaceBinaryFileError,
    WorkspacePatchConflictError,
    WorkspacePathDeniedError,
    WorkspacePathError,
    WorkspacePathNotFoundError,
    WorkspacePathTypeError,
    WorkspaceStateConflictError,
)
from agentcell.policy import Capability, RiskLevel, ToolPolicy
from agentcell.tools.models import (
    ToolApprovalPreview,
    ToolDefinition,
    ToolExecutionContext,
)
from agentcell.tools.registry import ToolRegistry

_SENSITIVE_EXACT_NAMES = frozenset(
    {
        ".git",
        ".ssh",
        ".aws",
        ".azure",
        ".kube",
        ".gnupg",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "id_rsa",
        "id_ed25519",
    }
)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class WorkspaceListParams(BaseModel):
    """Arguments for listing one allowed workspace directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(default=".", min_length=1, max_length=1024)
    include_hidden: bool = False
    max_entries: int = Field(default=200, ge=1, le=500, strict=True)


class WorkspaceEntry(BaseModel):
    """Safe metadata for one directory entry without link target disclosure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    path: str
    kind: Literal["file", "directory", "symlink", "other"]
    size_bytes: int | None = Field(default=None, ge=0)


class WorkspaceListResult(BaseModel):
    """Bounded stable directory listing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    entries: tuple[WorkspaceEntry, ...]
    truncated: bool


class WorkspaceReadParams(BaseModel):
    """Arguments for one UTF-8 byte-range read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=1024)
    offset_bytes: int = Field(default=0, ge=0, strict=True)
    max_bytes: int = Field(default=64 * 1024, ge=4, le=64 * 1024, strict=True)


class WorkspaceReadResult(BaseModel):
    """UTF-8 content chunk and continuation metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    content: str
    offset_bytes: int
    bytes_read: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    truncated: bool
    next_offset_bytes: int | None = Field(default=None, ge=0)


class WorkspaceSearchParams(BaseModel):
    """Arguments for bounded literal text search."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1, max_length=500)
    path: str = Field(default=".", min_length=1, max_length=1024)
    case_sensitive: bool = False
    include_hidden: bool = False
    max_results: int = Field(default=100, ge=1, le=500, strict=True)
    max_files: int = Field(default=1000, ge=1, le=5000, strict=True)
    max_file_bytes: int = Field(default=1024 * 1024, ge=1, le=4 * 1024 * 1024, strict=True)

    @field_validator("query")
    @classmethod
    def reject_multiline_query(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("query must be a single-line literal")
        return value


class WorkspaceSearchMatch(BaseModel):
    """One bounded source location for a literal match."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    preview: str = Field(max_length=300)


class WorkspaceSearchResult(BaseModel):
    """Bounded search matches with scan diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str
    matches: tuple[WorkspaceSearchMatch, ...]
    files_scanned: int = Field(ge=0)
    skipped_large_or_binary: int = Field(ge=0)
    truncated: bool


class WorkspaceWriteParams(BaseModel):
    """Create or replace one UTF-8 file with an expected-state guard."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(max_length=1_048_576)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class WorkspacePatchParams(BaseModel):
    """Replace an exact UTF-8 fragment only when the file hash still matches."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=1024)
    old_text: str = Field(min_length=1, max_length=524_288)
    new_text: str = Field(max_length=524_288)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_replacements: int = Field(default=1, ge=1, le=1000, strict=True)


class WorkspaceDeleteParams(BaseModel):
    """Delete one file only when its current content hash was explicitly approved."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=1024)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkspaceMutationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    operation: Literal["created", "replaced", "patched", "deleted"]
    bytes_written: int = Field(ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class WorkspacePathResolver:
    """Resolve existing paths inside workspace and lease scopes after following links."""

    def __init__(self, workspace: Path) -> None:
        try:
            root = workspace.resolve(strict=True)
        except OSError as error:
            raise ConfigurationError("Workspace root does not exist") from error
        if not root.is_dir():
            raise ConfigurationError("Workspace root must be a directory")
        self.root = root

    def resolve_read(
        self,
        requested: str,
        *,
        allowed_scopes: tuple[str, ...],
        expected: Literal["file", "directory", "any"] = "any",
    ) -> Path:
        relative = _relative_path(requested)
        _ensure_not_sensitive(relative.parts)
        candidate = self.root.joinpath(relative)
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, NotADirectoryError) as error:
            raise WorkspacePathNotFoundError(requested) from error
        except OSError as error:
            raise WorkspacePathDeniedError("path could not be resolved safely") from error

        workspace_relative = _relative_to(resolved, self.root)
        _ensure_not_sensitive(workspace_relative.parts)
        if not any(self._within_scope(resolved, scope) for scope in allowed_scopes):
            raise WorkspacePathDeniedError("path is outside the filesystem_read lease")
        if expected == "file" and not resolved.is_file():
            raise WorkspacePathTypeError(requested, "file")
        if expected == "directory" and not resolved.is_dir():
            raise WorkspacePathTypeError(requested, "directory")
        return resolved

    def relative_name(self, path: Path) -> str:
        return _relative_to(path, self.root).as_posix() or "."

    def ensure_within(
        self,
        path: Path,
        *,
        allowed_scopes: tuple[str, ...],
        lease_name: str,
    ) -> None:
        if not any(self._within_scope(path, scope) for scope in allowed_scopes):
            raise WorkspacePathDeniedError(f"path is outside the {lease_name} lease")

    def resolve_write(
        self,
        requested: str,
        *,
        allowed_scopes: tuple[str, ...],
        must_exist: bool,
    ) -> Path:
        """Resolve a file target without allowing links or non-existent parents."""

        relative = _relative_path(requested)
        _ensure_not_sensitive(relative.parts)
        self._ensure_existing_components_are_plain(relative)
        candidate = self.root.joinpath(relative)
        try:
            if candidate.exists():
                metadata = candidate.lstat()
                if candidate.is_symlink() or _is_reparse_point(metadata):
                    raise WorkspacePathDeniedError("write target cannot be a link")
                resolved = candidate.resolve(strict=True)
                if not resolved.is_file():
                    raise WorkspacePathTypeError(requested, "file")
            else:
                if must_exist:
                    raise WorkspacePathNotFoundError(requested)
                parent = candidate.parent.resolve(strict=True)
                if not parent.is_dir():
                    raise WorkspacePathTypeError(str(relative.parent), "directory")
                resolved = parent / candidate.name
        except WorkspacePathError:
            raise
        except (FileNotFoundError, NotADirectoryError) as error:
            raise WorkspacePathNotFoundError(requested) from error
        except OSError as error:
            raise WorkspacePathDeniedError("write path could not be resolved safely") from error
        _relative_to(resolved, self.root)
        if not any(self._within_scope(resolved, scope) for scope in allowed_scopes):
            raise WorkspacePathDeniedError("path is outside the filesystem_write lease")
        return resolved

    def _ensure_existing_components_are_plain(self, relative: Path) -> None:
        current = self.root
        for part in relative.parts:
            current /= part
            if not current.exists() and not current.is_symlink():
                break
            try:
                metadata = current.lstat()
            except OSError as error:
                raise WorkspacePathDeniedError("path component could not be inspected") from error
            if current.is_symlink() or _is_reparse_point(metadata):
                raise WorkspacePathDeniedError("write path cannot cross a link or reparse point")

    def _within_scope(self, resolved: Path, scope: str) -> bool:
        scope_path = self.root if scope == "." else self.root.joinpath(*scope.split("/"))
        try:
            resolved_scope = scope_path.resolve(strict=False)
            _relative_to(resolved_scope, self.root)
            resolved.relative_to(resolved_scope)
        except (OSError, ValueError, WorkspacePathDeniedError):
            return False
        return True


async def workspace_list(
    params: WorkspaceListParams,
    context: ToolExecutionContext,
) -> WorkspaceListResult:
    resolver = WorkspacePathResolver(context.workspace)
    directory = resolver.resolve_read(
        params.path,
        allowed_scopes=context.lease.filesystem_read,
        expected="directory",
    )
    return await asyncio.to_thread(_list_directory, directory, resolver, params)


async def workspace_read(
    params: WorkspaceReadParams,
    context: ToolExecutionContext,
) -> WorkspaceReadResult:
    resolver = WorkspacePathResolver(context.workspace)
    file_path = resolver.resolve_read(
        params.path,
        allowed_scopes=context.lease.filesystem_read,
        expected="file",
    )
    return await asyncio.to_thread(_read_text_chunk, file_path, resolver, params)


async def workspace_search(
    params: WorkspaceSearchParams,
    context: ToolExecutionContext,
) -> WorkspaceSearchResult:
    resolver = WorkspacePathResolver(context.workspace)
    search_root = resolver.resolve_read(
        params.path,
        allowed_scopes=context.lease.filesystem_read,
    )
    return await asyncio.to_thread(_search_text, search_root, resolver, params)


async def workspace_write(
    params: WorkspaceWriteParams,
    context: ToolExecutionContext,
) -> WorkspaceMutationResult:
    resolver = WorkspacePathResolver(context.workspace)
    target, previous, content = await asyncio.to_thread(
        _prepare_write,
        resolver,
        params,
        context.lease.filesystem_read,
        context.lease.filesystem_write,
    )
    await asyncio.to_thread(_atomic_write, target, content, params.expected_sha256)
    encoded = content.encode("utf-8")
    return WorkspaceMutationResult(
        path=resolver.relative_name(target),
        operation="created" if previous is None else "replaced",
        bytes_written=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


async def preview_workspace_write(
    params: WorkspaceWriteParams,
    context: ToolExecutionContext,
) -> ToolApprovalPreview:
    resolver = WorkspacePathResolver(context.workspace)
    target, previous, content = await asyncio.to_thread(
        _prepare_write,
        resolver,
        params,
        context.lease.filesystem_read,
        context.lease.filesystem_write,
    )
    return await _diff_preview(
        context,
        path=resolver.relative_name(target),
        previous=previous or "",
        updated=content,
        impact="Create a UTF-8 file" if previous is None else "Replace a UTF-8 file",
    )


async def workspace_patch(
    params: WorkspacePatchParams,
    context: ToolExecutionContext,
) -> WorkspaceMutationResult:
    resolver = WorkspacePathResolver(context.workspace)
    target, _previous, updated = await asyncio.to_thread(
        _prepare_patch,
        resolver,
        params,
        context.lease.filesystem_read,
        context.lease.filesystem_write,
    )
    await asyncio.to_thread(_atomic_write, target, updated, params.expected_sha256)
    encoded = updated.encode("utf-8")
    return WorkspaceMutationResult(
        path=resolver.relative_name(target),
        operation="patched",
        bytes_written=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


async def preview_workspace_patch(
    params: WorkspacePatchParams,
    context: ToolExecutionContext,
) -> ToolApprovalPreview:
    resolver = WorkspacePathResolver(context.workspace)
    target, previous, updated = await asyncio.to_thread(
        _prepare_patch,
        resolver,
        params,
        context.lease.filesystem_read,
        context.lease.filesystem_write,
    )
    return await _diff_preview(
        context,
        path=resolver.relative_name(target),
        previous=previous,
        updated=updated,
        impact=f"Patch {params.expected_replacements} exact occurrence(s) in a UTF-8 file",
    )


async def workspace_delete(
    params: WorkspaceDeleteParams,
    context: ToolExecutionContext,
) -> WorkspaceMutationResult:
    resolver = WorkspacePathResolver(context.workspace)
    target, content = await asyncio.to_thread(
        _prepare_delete,
        resolver,
        params,
        context.lease.filesystem_read,
        context.lease.filesystem_write,
    )
    await asyncio.to_thread(
        _delete_expected,
        target,
        params.expected_sha256,
        resolver.relative_name(target),
    )
    return WorkspaceMutationResult(
        path=resolver.relative_name(target),
        operation="deleted",
        bytes_written=0,
        sha256=hashlib.sha256(content).hexdigest(),
    )


async def preview_workspace_delete(
    params: WorkspaceDeleteParams,
    context: ToolExecutionContext,
) -> ToolApprovalPreview:
    resolver = WorkspacePathResolver(context.workspace)
    target, content = await asyncio.to_thread(
        _prepare_delete,
        resolver,
        params,
        context.lease.filesystem_read,
        context.lease.filesystem_write,
    )
    text = _decode_utf8(content, resolver.relative_name(target))
    return await _diff_preview(
        context,
        path=resolver.relative_name(target),
        previous=text,
        updated="",
        impact="Permanently delete one workspace file",
    )


def register_workspace_tools(registry: ToolRegistry) -> None:
    """Register read tools plus approval-gated workspace mutations."""

    read_policy = ToolPolicy(
        risk=RiskLevel.SAFE,
        requires_approval=False,
        idempotent=True,
        timeout_seconds=10,
        max_output_bytes=256 * 1024,
        capabilities=frozenset({Capability.FILESYSTEM_READ}),
    )
    search_policy = read_policy.model_copy(update={"timeout_seconds": 20})
    registry.register(
        ToolDefinition(
            name="workspace.list",
            description="List bounded metadata for one allowed workspace directory.",
            params_model=WorkspaceListParams,
            policy=read_policy,
            handler=workspace_list,
        )
    )
    registry.register(
        ToolDefinition(
            name="workspace.read",
            description="Read one bounded UTF-8 byte range from an allowed workspace file.",
            params_model=WorkspaceReadParams,
            policy=read_policy,
            handler=workspace_read,
        )
    )
    registry.register(
        ToolDefinition(
            name="workspace.search",
            description="Search allowed UTF-8 workspace files for a bounded literal string.",
            params_model=WorkspaceSearchParams,
            policy=search_policy,
            handler=workspace_search,
        )
    )
    mutation_policy = ToolPolicy(
        risk=RiskLevel.GUARDED,
        requires_approval=True,
        idempotent=False,
        timeout_seconds=15,
        max_output_bytes=64 * 1024,
        capabilities=frozenset({Capability.FILESYSTEM_READ, Capability.FILESYSTEM_WRITE}),
    )
    registry.register(
        ToolDefinition(
            name="workspace.write",
            description="Create or replace one UTF-8 workspace file after Diff approval.",
            params_model=WorkspaceWriteParams,
            policy=mutation_policy,
            handler=workspace_write,
            approval_previewer=preview_workspace_write,
        )
    )
    registry.register(
        ToolDefinition(
            name="workspace.patch",
            description="Apply an expected-state UTF-8 replacement after Diff approval.",
            params_model=WorkspacePatchParams,
            policy=mutation_policy,
            handler=workspace_patch,
            approval_previewer=preview_workspace_patch,
        )
    )
    registry.register(
        ToolDefinition(
            name="workspace.delete",
            description="Permanently delete one expected-state workspace file.",
            params_model=WorkspaceDeleteParams,
            policy=mutation_policy.model_copy(update={"risk": RiskLevel.DANGEROUS}),
            handler=workspace_delete,
            approval_previewer=preview_workspace_delete,
        )
    )


def _list_directory(
    directory: Path,
    resolver: WorkspacePathResolver,
    params: WorkspaceListParams,
) -> WorkspaceListResult:
    entries: list[WorkspaceEntry] = []
    truncated = False
    with os.scandir(directory) as iterator:
        candidates = sorted(iterator, key=lambda entry: entry.name.casefold())
    for entry in candidates:
        if _is_sensitive(entry.name) or (not params.include_hidden and _is_hidden(entry.name)):
            continue
        if len(entries) >= params.max_entries:
            truncated = True
            break
        kind: Literal["file", "directory", "symlink", "other"] = "other"
        size: int | None = None
        try:
            metadata = entry.stat(follow_symlinks=False)
            if entry.is_symlink() or _is_reparse_point(metadata):
                kind = "symlink"
            elif entry.is_file(follow_symlinks=False):
                kind = "file"
                size = metadata.st_size
            elif entry.is_dir(follow_symlinks=False):
                kind = "directory"
        except OSError:
            kind = "other"
        entries.append(
            WorkspaceEntry(
                name=entry.name,
                path=resolver.relative_name(Path(entry.path)),
                kind=kind,
                size_bytes=size,
            )
        )
    return WorkspaceListResult(
        path=resolver.relative_name(directory),
        entries=tuple(entries),
        truncated=truncated,
    )


def _read_text_chunk(
    file_path: Path,
    resolver: WorkspacePathResolver,
    params: WorkspaceReadParams,
) -> WorkspaceReadResult:
    total_bytes = file_path.stat().st_size
    if params.offset_bytes > total_bytes:
        raise WorkspacePathDeniedError("offset exceeds file size")
    with file_path.open("rb") as stream:
        stream.seek(params.offset_bytes)
        raw = stream.read(params.max_bytes + 1)
    candidate = raw[: params.max_bytes]
    if b"\x00" in candidate:
        raise WorkspaceBinaryFileError(resolver.relative_name(file_path))
    content, decoded_bytes = _decode_complete_utf8(candidate, resolver.relative_name(file_path))
    next_offset = params.offset_bytes + decoded_bytes
    truncated = next_offset < total_bytes
    return WorkspaceReadResult(
        path=resolver.relative_name(file_path),
        content=content,
        offset_bytes=params.offset_bytes,
        bytes_read=decoded_bytes,
        total_bytes=total_bytes,
        sha256=_sha256_file(file_path) if total_bytes <= 4 * 1024 * 1024 else None,
        truncated=truncated,
        next_offset_bytes=next_offset if truncated else None,
    )


def _decode_complete_utf8(content: bytes, display_path: str) -> tuple[str, int]:
    for trim in range(0, min(4, len(content) + 1)):
        candidate = content if trim == 0 else content[:-trim]
        try:
            return candidate.decode("utf-8"), len(candidate)
        except UnicodeDecodeError as error:
            if error.reason != "unexpected end of data":
                raise WorkspaceBinaryFileError(display_path) from error
    raise WorkspaceBinaryFileError(display_path)


def _search_text(
    search_root: Path,
    resolver: WorkspacePathResolver,
    params: WorkspaceSearchParams,
) -> WorkspaceSearchResult:
    matches: list[WorkspaceSearchMatch] = []
    files_scanned = 0
    skipped = 0
    truncated = False
    query = params.query if params.case_sensitive else params.query.casefold()

    for file_path in _iter_search_files(search_root, include_hidden=params.include_hidden):
        if files_scanned >= params.max_files:
            truncated = True
            break
        files_scanned += 1
        try:
            if file_path.stat().st_size > params.max_file_bytes:
                skipped += 1
                continue
            raw = file_path.read_bytes()
            if b"\x00" in raw:
                skipped += 1
                continue
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            skipped += 1
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            comparable = line if params.case_sensitive else line.casefold()
            column = comparable.find(query)
            if column < 0:
                continue
            matches.append(
                WorkspaceSearchMatch(
                    path=resolver.relative_name(file_path),
                    line=line_number,
                    column=column + 1,
                    preview=line[:300],
                )
            )
            if len(matches) >= params.max_results:
                truncated = True
                break
        if truncated:
            break
    return WorkspaceSearchResult(
        query=params.query,
        matches=tuple(matches),
        files_scanned=files_scanned,
        skipped_large_or_binary=skipped,
        truncated=truncated,
    )


def _iter_search_files(root: Path, *, include_hidden: bool) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        except OSError:
            continue
        child_directories: list[Path] = []
        for entry in entries:
            if _is_sensitive(entry.name) or (not include_hidden and _is_hidden(entry.name)):
                continue
            try:
                metadata = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or _is_reparse_point(metadata):
                    continue
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    child_directories.append(path)
                elif entry.is_file(follow_symlinks=False):
                    yield path
            except OSError:
                continue
        stack.extend(reversed(child_directories))


def _relative_path(value: str) -> Path:
    stripped = value.strip()
    if not stripped:
        raise WorkspacePathDeniedError("path cannot be empty")
    normalized = stripped.replace("\\", "/")
    path = Path(normalized)
    if (
        path.is_absolute()
        or path.drive
        or path.root
        or normalized.startswith("//")
        or _WINDOWS_DRIVE_RE.match(normalized)
    ):
        raise WorkspacePathDeniedError("absolute and drive-relative paths are forbidden")
    if ".." in path.parts:
        raise WorkspacePathDeniedError("parent traversal is forbidden")
    return path


def _relative_to(path: Path, root: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError as error:
        raise WorkspacePathDeniedError("resolved path escapes the workspace") from error


def _ensure_not_sensitive(parts: Iterable[str]) -> None:
    if any(_is_sensitive(part) for part in parts):
        raise WorkspacePathDeniedError("sensitive files and credential directories are forbidden")


def _is_sensitive(name: str) -> bool:
    normalized = name.casefold()
    return (
        normalized in _SENSITIVE_EXACT_NAMES
        or normalized == ".env"
        or normalized.startswith(".env.")
        or normalized.endswith(".pem")
        or normalized.endswith(".key")
    )


def _is_hidden(name: str) -> bool:
    return name.startswith(".") and name not in {".", ".."}


def _is_reparse_point(metadata: os.stat_result) -> bool:
    if not hasattr(metadata, "st_file_attributes"):
        return False
    return bool(metadata.st_file_attributes & _REPARSE_POINT)


def _prepare_write(
    resolver: WorkspacePathResolver,
    params: WorkspaceWriteParams,
    allowed_read_scopes: tuple[str, ...],
    allowed_scopes: tuple[str, ...],
) -> tuple[Path, str | None, str]:
    target = resolver.resolve_write(
        params.path,
        allowed_scopes=allowed_scopes,
        must_exist=False,
    )
    resolver.ensure_within(
        target,
        allowed_scopes=allowed_read_scopes,
        lease_name="filesystem_read",
    )
    encoded = params.content.encode("utf-8")
    if len(encoded) > 1024 * 1024:
        raise WorkspacePathDeniedError("UTF-8 write exceeds the 1 MiB limit")
    if target.exists():
        resolver.resolve_read(
            params.path,
            allowed_scopes=allowed_read_scopes,
            expected="file",
        )
        previous_bytes = target.read_bytes()
        previous = _decode_utf8(previous_bytes, resolver.relative_name(target))
        _require_expected_hash(
            resolver.relative_name(target),
            previous_bytes,
            params.expected_sha256,
        )
    else:
        if params.expected_sha256 is not None:
            raise WorkspacePathDeniedError("new files cannot declare expected_sha256")
        previous = None
    return target, previous, params.content


def _prepare_patch(
    resolver: WorkspacePathResolver,
    params: WorkspacePatchParams,
    allowed_read_scopes: tuple[str, ...],
    allowed_scopes: tuple[str, ...],
) -> tuple[Path, str, str]:
    target = resolver.resolve_write(
        params.path,
        allowed_scopes=allowed_scopes,
        must_exist=True,
    )
    resolver.ensure_within(
        target,
        allowed_scopes=allowed_read_scopes,
        lease_name="filesystem_read",
    )
    resolver.resolve_read(
        params.path,
        allowed_scopes=allowed_read_scopes,
        expected="file",
    )
    previous_bytes = target.read_bytes()
    if len(previous_bytes) > 4 * 1024 * 1024:
        raise WorkspacePathDeniedError("patch target exceeds the 4 MiB limit")
    _require_expected_hash(
        resolver.relative_name(target),
        previous_bytes,
        params.expected_sha256,
    )
    previous = _decode_utf8(previous_bytes, resolver.relative_name(target))
    actual = previous.count(params.old_text)
    if actual != params.expected_replacements:
        raise WorkspacePatchConflictError(
            resolver.relative_name(target),
            params.expected_replacements,
            actual,
        )
    updated = previous.replace(
        params.old_text,
        params.new_text,
        params.expected_replacements,
    )
    if len(updated.encode("utf-8")) > 4 * 1024 * 1024:
        raise WorkspacePathDeniedError("patched file exceeds the 4 MiB limit")
    return target, previous, updated


def _prepare_delete(
    resolver: WorkspacePathResolver,
    params: WorkspaceDeleteParams,
    allowed_read_scopes: tuple[str, ...],
    allowed_scopes: tuple[str, ...],
) -> tuple[Path, bytes]:
    target = resolver.resolve_write(
        params.path,
        allowed_scopes=allowed_scopes,
        must_exist=True,
    )
    resolver.ensure_within(
        target,
        allowed_scopes=allowed_read_scopes,
        lease_name="filesystem_read",
    )
    resolver.resolve_read(
        params.path,
        allowed_scopes=allowed_read_scopes,
        expected="file",
    )
    content = target.read_bytes()
    _require_expected_hash(
        resolver.relative_name(target),
        content,
        params.expected_sha256,
    )
    return target, content


def _require_expected_hash(path: str, content: bytes, expected: str | None) -> None:
    if expected is None:
        raise WorkspacePathDeniedError("expected_sha256 is required when replacing a file")
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise WorkspaceStateConflictError(path)


def _decode_utf8(content: bytes, display_path: str) -> str:
    if b"\x00" in content:
        raise WorkspaceBinaryFileError(display_path)
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorkspaceBinaryFileError(display_path) from error


def _atomic_write(path: Path, content: str, expected_sha256: str | None) -> None:
    encoded = content.encode("utf-8")
    temporary = path.with_name(f".{path.name}.agentcell-{uuid4().hex}.tmp")
    previous_mode: int | None = None
    if path.exists():
        previous_mode = stat.S_IMODE(path.stat().st_mode)
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if previous_mode is not None:
            temporary.chmod(previous_mode)
        if expected_sha256 is None:
            if path.exists():
                raise WorkspaceStateConflictError(path.name)
            os.link(temporary, path)
            temporary.unlink()
        else:
            if not _is_plain_file(path) or _sha256_file(path) != expected_sha256:
                raise WorkspaceStateConflictError(path.name)
            temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _delete_expected(path: Path, expected_sha256: str, display_path: str) -> None:
    if not _is_plain_file(path) or _sha256_file(path) != expected_sha256:
        raise WorkspaceStateConflictError(display_path)
    path.unlink()


def _is_plain_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        not path.is_symlink() and not _is_reparse_point(metadata) and stat.S_ISREG(metadata.st_mode)
    )


async def _diff_preview(
    context: ToolExecutionContext,
    *,
    path: str,
    previous: str,
    updated: str,
    impact: str,
) -> ToolApprovalPreview:
    diff = "".join(
        difflib.unified_diff(
            previous.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    if not diff:
        diff = f"--- a/{path}\n+++ b/{path}\n(no textual line changes)\n"
    encoded = diff.encode("utf-8")
    if len(encoded) <= 30_000:
        return ToolApprovalPreview(impact=impact, diff=diff)
    artifact = None
    if context.artifacts is not None:
        artifact = await context.artifacts.save(
            encoded,
            media_type="text/x-diff",
            suggested_name=f"{Path(path).name}.diff",
        )
    snippet = encoded[:28_000].decode("utf-8", errors="ignore")
    suffix = "\n... Diff truncated"
    if artifact is not None:
        suffix += f"; full Diff Artifact: {artifact.artifact_id}"
    return ToolApprovalPreview(
        impact=impact,
        diff=snippet + suffix,
        diff_artifact=artifact,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
