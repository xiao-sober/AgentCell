"""Read-only, bounded Git workspace inspection used only as audit metadata."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from agentcell.changes.models import GitBaseline


class GitWorkspaceInspector:
    """Run fixed Git argv with pager, external diff, prompts, and optional locks disabled."""

    def __init__(self, *, timeout_seconds: float = 3.0, max_output_bytes: int = 256_000) -> None:
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes

    async def baseline(self, workspace: Path, path: str | None = None) -> GitBaseline | None:
        root = await self._run(workspace, "rev-parse", "--show-toplevel")
        if root is None:
            return None
        root_path = await asyncio.to_thread(Path(root.strip()).resolve)
        workspace_root = await asyncio.to_thread(workspace.resolve)
        try:
            relative_root = root_path.relative_to(workspace_root).as_posix() or "."
        except ValueError:
            relative_root = "."
        head_raw = await self._run(workspace, "rev-parse", "--verify", "HEAD")
        head = None if head_raw is None else head_raw.strip()
        if head and not all(character in "0123456789abcdef" for character in head.casefold()):
            head = None
        branch_raw = await self._run(workspace, "symbolic-ref", "--short", "-q", "HEAD")
        args = ["status", "--porcelain=v2", "-z", "--untracked-files=all"]
        if path is not None:
            args.extend(["--", path])
        status = await self._run(workspace, *args)
        return GitBaseline(
            repository_root=relative_root,
            head=head,
            branch=None if branch_raw is None else branch_raw.strip() or None,
            dirty=bool(status),
            path_status=status,
        )

    async def diff(self, workspace: Path, path: str) -> bytes | None:
        output = await self._run(
            workspace,
            "diff",
            "--no-ext-diff",
            "--no-color",
            "--binary",
            "--",
            path,
        )
        return None if output is None else output.encode("utf-8")

    async def _run(self, workspace: Path, *args: str) -> str | None:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "GIT_EXTERNAL_DIFF": "",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        command = (
            "git",
            "-c",
            "core.pager=cat",
            "-c",
            "diff.external=",
            "-C",
            str(workspace),
            *args,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
            )
            async with asyncio.timeout(self._timeout_seconds):
                stdout, _ = await process.communicate()
        except (FileNotFoundError, PermissionError, TimeoutError, OSError):
            return None
        if process.returncode != 0 or len(stdout) > self._max_output_bytes:
            return None
        return stdout.decode("utf-8", errors="replace")
