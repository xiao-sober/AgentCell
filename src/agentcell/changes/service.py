"""Durable before/after recording and hash-safe workspace reconciliation."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel

from agentcell.changes.git import GitWorkspaceInspector
from agentcell.changes.models import (
    ChangeDetails,
    ChangeSet,
    FileChange,
    FileChangeStatus,
    FileOperation,
)
from agentcell.errors import ArtifactTooLargeError, ChangeConflictError
from agentcell.events import EventType, GenericEventPayload
from agentcell.policy import CapabilityLease
from agentcell.storage import (
    ApprovalRepository,
    ChangeSetRepository,
    Database,
    FileArtifactStore,
    FileChangeRepository,
)
from agentcell.tools.models import ToolCall, ToolEventSink, ToolExecutionContext
from agentcell.tools.workspace import (
    WorkspacePathResolver,
    prepare_workspace_change,
    revert_workspace_change,
)


class ChangeService:
    """Record exact workspace mutations without relying on a Git repository."""

    def __init__(
        self,
        database: Database,
        artifact_store: FileArtifactStore,
        *,
        git: GitWorkspaceInspector | None = None,
        max_file_change_bytes: int = 16 * 1024 * 1024,
        max_run_change_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if max_file_change_bytes < 1 or max_run_change_bytes < max_file_change_bytes:
            raise ValueError("change storage budgets are invalid")
        self._database = database
        self._artifacts = artifact_store
        self._git = git or GitWorkspaceInspector()
        self._max_file_change_bytes = max_file_change_bytes
        self._max_run_change_bytes = max_run_change_bytes

    async def prepare(
        self,
        call: ToolCall,
        params: BaseModel,
        context: ToolExecutionContext,
    ) -> UUID | None:
        if context.run_id is None or context.conversation_id is None or context.agent_id is None:
            return None
        planned = await prepare_workspace_change(call.tool_name, params, context)
        if planned is None:
            return None
        diff = _unified_diff(planned.path, planned.before, planned.after)
        storage_bytes = len(planned.before or b"") + len(planned.after or b"") + len(diff)
        if storage_bytes > self._max_file_change_bytes:
            raise ArtifactTooLargeError(
                f"FileChange requires {storage_bytes} bytes; limit is {self._max_file_change_bytes}"
            )
        async with self._database.session() as session:
            existing_set = await ChangeSetRepository(session).get_for_run(context.run_id)
        used_bytes = 0 if existing_set is None else existing_set.storage_bytes
        if used_bytes + storage_bytes > self._max_run_change_bytes:
            raise ArtifactTooLargeError(
                f"ChangeSet requires {used_bytes + storage_bytes} bytes; limit is "
                f"{self._max_run_change_bytes}"
            )
        before_ref = await self._save_snapshot(planned.before, planned.path, "before")
        after_ref = await self._save_snapshot(planned.after, planned.path, "after")
        diff_ref = await self._artifacts.save(
            diff,
            media_type="text/x-diff",
            suggested_name=f"{Path(planned.path).name}.diff",
        )
        git = await self._git.baseline(context.workspace, planned.path)
        approval_id = None
        if call.provider_call_id is not None:
            async with self._database.session() as session:
                approval = await ApprovalRepository(session).find_by_provider_call(
                    context.run_id, call.provider_call_id
                )
            approval_id = None if approval is None else approval.id
        async with self._database.transaction() as session:
            sets = ChangeSetRepository(session)
            change_set = await sets.get_for_run(context.run_id)
            if change_set is None:
                change_set = await sets.create(
                    ChangeSet(
                        run_id=context.run_id,
                        conversation_id=context.conversation_id,
                        agent_id=context.agent_id,
                        workspace=str(context.workspace),
                        git=git,
                        storage_bytes=storage_bytes,
                    )
                )
            else:
                change_set = change_set.model_copy(
                    update={"storage_bytes": change_set.storage_bytes + storage_bytes}
                )
                await sets.save(change_set)
            change = FileChange(
                change_set_id=change_set.id,
                run_id=context.run_id,
                conversation_id=context.conversation_id,
                agent_id=context.agent_id,
                provider_call_id=call.provider_call_id,
                approval_id=approval_id,
                path=planned.path,
                operation=FileOperation(planned.operation),
                before_sha256=_sha256(planned.before),
                after_sha256=_sha256(planned.after),
                before_artifact=before_ref,
                after_artifact=after_ref,
                diff_artifact=diff_ref,
                git_head=None if git is None else git.head,
                git_dirty_before=False if git is None else git.dirty,
                storage_bytes=storage_bytes,
            )
            await FileChangeRepository(session).create(change)
        await context.events.emit(
            EventType.FILE_CHANGE_PREPARED,
            GenericEventPayload(
                data={
                    "change_id": str(change.id),
                    "change_set_id": str(change.change_set_id),
                    "path": change.path,
                    "operation": change.operation.value,
                    "before_sha256": change.before_sha256,
                    "after_sha256": change.after_sha256,
                }
            ),
        )
        return change.id

    async def complete(self, change_id: UUID, context: ToolExecutionContext) -> None:
        change = await self.get(change_id)
        actual = await self._current_bytes(context.workspace, change.path, context.lease)
        if _sha256(actual) != change.after_sha256:
            await self._set_status(change, FileChangeStatus.CONFLICT)
            await context.events.emit(
                EventType.FILE_CHANGE_CONFLICT,
                GenericEventPayload(data={"change_id": str(change.id), "path": change.path}),
            )
            raise ChangeConflictError(change.path)
        git_diff = await self._git.diff(context.workspace, change.path)
        git_diff_artifact = (
            None
            if not git_diff
            else await self._artifacts.save(
                git_diff,
                media_type="text/x-diff",
                suggested_name=f"{Path(change.path).name}.git.diff",
            )
        )
        applied_at = datetime.now(UTC)
        applied = change.model_copy(
            update={
                "status": FileChangeStatus.APPLIED,
                "applied_at": applied_at,
                "git_diff_artifact": git_diff_artifact,
            }
        )
        await self._save(applied)
        await context.events.emit(
            EventType.FILE_CHANGE_APPLIED,
            GenericEventPayload(data={"change_id": str(change.id), "path": change.path}),
        )
        completed = applied.model_copy(
            update={"status": FileChangeStatus.COMPLETED, "completed_at": datetime.now(UTC)}
        )
        await self._save(completed)
        await context.events.emit(
            EventType.FILE_CHANGE_COMPLETED,
            GenericEventPayload(data={"change_id": str(change.id), "path": change.path}),
        )

    async def fail(self, change_id: UUID, context: ToolExecutionContext) -> None:
        """Reconcile ambiguous failures from current bytes instead of blindly replaying."""

        change = await self.get(change_id)
        actual = await self._current_bytes(context.workspace, change.path, context.lease)
        actual_hash = _sha256(actual)
        if actual_hash == change.after_sha256:
            await self.complete(change_id, context)
            return
        status = (
            FileChangeStatus.FAILED
            if actual_hash == change.before_sha256
            else FileChangeStatus.CONFLICT
        )
        await self._set_status(change, status)
        if status is FileChangeStatus.CONFLICT:
            await context.events.emit(
                EventType.FILE_CHANGE_CONFLICT,
                GenericEventPayload(data={"change_id": str(change.id), "path": change.path}),
            )

    async def list_for_run(self, run_id: UUID) -> tuple[FileChange, ...]:
        async with self._database.session() as session:
            return tuple(await FileChangeRepository(session).list_for_run(run_id))

    async def reconcile(
        self,
        run_id: UUID,
        *,
        workspace: Path,
        lease: CapabilityLease,
        events: ToolEventSink,
    ) -> tuple[FileChange, ...]:
        """Settle crash-window records from current hashes without replaying side effects."""

        values = list(await self.list_for_run(run_id))
        reconciled: list[FileChange] = []
        for change in values:
            if change.status not in {FileChangeStatus.PREPARED, FileChangeStatus.APPLIED}:
                continue
            actual = await self._current_bytes(workspace, change.path, lease)
            actual_hash = _sha256(actual)
            if actual_hash == change.after_sha256:
                completed = change.model_copy(
                    update={
                        "status": FileChangeStatus.COMPLETED,
                        "applied_at": change.applied_at or datetime.now(UTC),
                        "completed_at": datetime.now(UTC),
                    }
                )
                await self._save(completed)
                if completed.reverts_change_id is not None:
                    original = await self.get(completed.reverts_change_id)
                    await self._save(
                        original.model_copy(
                            update={
                                "status": FileChangeStatus.REVERTED,
                                "reverted_by_change_id": completed.id,
                            }
                        )
                    )
                await events.emit(
                    EventType.FILE_CHANGE_COMPLETED,
                    GenericEventPayload(
                        data={
                            "change_id": str(change.id),
                            "path": change.path,
                            "reconciled": True,
                        }
                    ),
                )
                reconciled.append(completed)
            elif actual_hash == change.before_sha256 and change.status is FileChangeStatus.PREPARED:
                reconciled.append(change)
            else:
                conflict = change.model_copy(
                    update={
                        "status": FileChangeStatus.CONFLICT,
                        "completed_at": datetime.now(UTC),
                    }
                )
                await self._save(conflict)
                await events.emit(
                    EventType.FILE_CHANGE_CONFLICT,
                    GenericEventPayload(
                        data={
                            "change_id": str(change.id),
                            "path": change.path,
                            "reconciled": True,
                        }
                    ),
                )
                reconciled.append(conflict)
        return tuple(reconciled)

    async def get(self, change_id: UUID) -> FileChange:
        async with self._database.session() as session:
            return await FileChangeRepository(session).get_required(change_id)

    async def details(self, change_id: UUID) -> ChangeDetails:
        async with self._database.session() as session:
            changes = FileChangeRepository(session)
            change = await changes.get_required(change_id)
            change_set = await ChangeSetRepository(session).get(change.change_set_id)
        if change_set is None:
            raise RuntimeError("FileChange references a missing ChangeSet")
        diff = (await self._artifacts.load(change.diff_artifact)).decode("utf-8")
        return ChangeDetails(change_set=change_set, change=change, diff=diff)

    async def reverse_diff(self, change_id: UUID) -> str:
        """Build the exact reverse Diff from verified before/after Artifacts."""

        change = await self.get(change_id)
        before = (
            None
            if change.after_artifact is None
            else await self._artifacts.load(change.after_artifact)
        )
        after = (
            None
            if change.before_artifact is None
            else await self._artifacts.load(change.before_artifact)
        )
        return _unified_diff(change.path, before, after).decode("utf-8")

    async def revert(
        self,
        change_id: UUID,
        *,
        workspace: Path,
        lease: CapabilityLease,
        events: ToolEventSink,
    ) -> FileChange:
        """Create and apply a new reverse FileChange after an external human decision."""

        original = await self.get(change_id)
        if original.status is not FileChangeStatus.COMPLETED or original.reverted_by_change_id:
            raise ChangeConflictError(original.path)
        current = await self._current_bytes(workspace, original.path, lease)
        if _sha256(current) != original.after_sha256:
            raise ChangeConflictError(original.path)
        current_git = await self._git.baseline(workspace, original.path)
        if (
            original.git_head is not None
            and current_git is not None
            and current_git.head != original.git_head
        ):
            raise ChangeConflictError(original.path)
        desired = (
            None
            if original.before_artifact is None
            else await self._artifacts.load(original.before_artifact)
        )
        reverse_diff = _unified_diff(original.path, current, desired)
        storage_bytes = len(current or b"") + len(desired or b"") + len(reverse_diff)
        if storage_bytes > self._max_file_change_bytes:
            raise ArtifactTooLargeError(
                f"FileChange requires {storage_bytes} bytes; limit is {self._max_file_change_bytes}"
            )
        async with self._database.session() as session:
            change_set = await ChangeSetRepository(session).get(original.change_set_id)
        if change_set is None:
            raise RuntimeError("FileChange references a missing ChangeSet")
        if change_set.storage_bytes + storage_bytes > self._max_run_change_bytes:
            raise ArtifactTooLargeError(
                f"ChangeSet requires {change_set.storage_bytes + storage_bytes} bytes; "
                f"limit is {self._max_run_change_bytes}"
            )
        diff_ref = await self._artifacts.save(
            reverse_diff,
            media_type="text/x-diff",
            suggested_name=f"{Path(original.path).name}.revert.diff",
        )
        reverse = FileChange(
            change_set_id=original.change_set_id,
            run_id=original.run_id,
            conversation_id=original.conversation_id,
            agent_id=original.agent_id,
            path=original.path,
            operation=FileOperation.REVERTED,
            before_sha256=original.after_sha256,
            after_sha256=original.before_sha256,
            before_artifact=original.after_artifact,
            after_artifact=original.before_artifact,
            diff_artifact=diff_ref,
            git_head=None if current_git is None else current_git.head,
            git_dirty_before=False if current_git is None else current_git.dirty,
            reverts_change_id=original.id,
            storage_bytes=storage_bytes,
        )
        async with self._database.transaction() as session:
            sets = ChangeSetRepository(session)
            current_set = await sets.get(original.change_set_id)
            if current_set is None:
                raise RuntimeError("FileChange references a missing ChangeSet")
            if current_set.storage_bytes + storage_bytes > self._max_run_change_bytes:
                raise ArtifactTooLargeError(
                    f"ChangeSet requires {current_set.storage_bytes + storage_bytes} bytes; "
                    f"limit is {self._max_run_change_bytes}"
                )
            await sets.save(
                current_set.model_copy(
                    update={"storage_bytes": current_set.storage_bytes + storage_bytes}
                )
            )
            await FileChangeRepository(session).create(reverse)
        await events.emit(
            EventType.FILE_CHANGE_PREPARED,
            GenericEventPayload(
                data={
                    "change_id": str(reverse.id),
                    "reverts_change_id": str(original.id),
                    "path": original.path,
                    "operation": FileOperation.REVERTED.value,
                    "decision_source": "human",
                }
            ),
        )
        await revert_workspace_change(
            workspace=workspace,
            lease=lease,
            path=original.path,
            current_sha256=original.after_sha256,
            desired=desired,
        )
        completed_at = datetime.now(UTC)
        reverse = reverse.model_copy(
            update={
                "status": FileChangeStatus.COMPLETED,
                "applied_at": completed_at,
                "completed_at": completed_at,
            }
        )
        original = original.model_copy(
            update={
                "status": FileChangeStatus.REVERTED,
                "reverted_by_change_id": reverse.id,
            }
        )
        async with self._database.transaction() as session:
            repository = FileChangeRepository(session)
            await repository.save(reverse)
            await repository.save(original)
        await events.emit(
            EventType.FILE_CHANGE_REVERTED,
            GenericEventPayload(
                data={
                    "change_id": str(reverse.id),
                    "reverts_change_id": str(original.id),
                    "path": original.path,
                }
            ),
        )
        return reverse

    async def _save_snapshot(
        self,
        content: bytes | None,
        path: str,
        side: str,
    ):
        if content is None:
            return None
        return await self._artifacts.save(
            content,
            media_type="application/octet-stream",
            suggested_name=f"{Path(path).name}.{side}",
        )

    async def _current_bytes(
        self,
        workspace: Path,
        path: str,
        lease: CapabilityLease,
    ) -> bytes | None:
        resolver = WorkspacePathResolver(workspace)
        target = resolver.resolve_write(
            path,
            allowed_scopes=lease.filesystem_write,
            must_exist=False,
        )
        if not await asyncio.to_thread(target.exists):
            return None
        return await asyncio.to_thread(target.read_bytes)

    async def _set_status(self, change: FileChange, status: FileChangeStatus) -> None:
        await self._save(
            change.model_copy(
                update={
                    "status": status,
                    "completed_at": datetime.now(UTC),
                }
            )
        )

    async def _save(self, change: FileChange) -> None:
        async with self._database.transaction() as session:
            await FileChangeRepository(session).save(change)


def _sha256(content: bytes | None) -> str | None:
    return None if content is None else hashlib.sha256(content).hexdigest()


def _unified_diff(path: str, before: bytes | None, after: bytes | None) -> bytes:
    previous = "" if before is None else before.decode("utf-8")
    updated = "" if after is None else after.decode("utf-8")
    value = "".join(
        difflib.unified_diff(
            previous.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    if not value:
        value = f"--- a/{path}\n+++ b/{path}\n(no textual line changes)\n"
    return value.encode("utf-8")
