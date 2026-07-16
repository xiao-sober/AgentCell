"""Approval-gated subprocess tools with command, cwd, environment, and output bounds."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentcell.errors import (
    ShellCommandDeniedError,
    ShellCommandLeaseMismatchError,
    ShellOutputTooLargeError,
)
from agentcell.policy import Capability, RiskLevel, ToolPolicy
from agentcell.tools.models import ToolApprovalPreview, ToolDefinition, ToolExecutionContext
from agentcell.tools.registry import ToolRegistry
from agentcell.tools.workspace import WorkspacePathResolver

_ENVIRONMENT_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "VIRTUAL_ENV",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
)
_PYTEST_COLLECTION_FLAGS = frozenset({"--collect-only", "--co"})
_PYTEST_EXECUTION_SUMMARY = re.compile(
    r"\b\d+\s+(?:passed|failed|skipped|xfailed|xpassed|errors?)\b",
    re.IGNORECASE,
)


class ShellRunParams(BaseModel):
    """One argv-based subprocess request; no command string is interpreted by a shell."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.+-]+$")
    args: tuple[str, ...] = Field(default=(), max_length=128)
    cwd: str = Field(default=".", min_length=1, max_length=1024)
    max_output_bytes: int = Field(
        default=1024 * 1024,
        ge=1024,
        le=4 * 1024 * 1024,
        strict=True,
    )

    @field_validator("args")
    @classmethod
    def validate_args(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(len(argument) > 4096 or "\x00" in argument for argument in value):
            raise ValueError("shell arguments exceed length or contain NUL")
        return value


class ShellRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command: tuple[str, ...]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    output_bytes: int = Field(ge=0)


class TestExecutionEvidence(BaseModel):
    """Conservative structured evidence that a test command executed rather than inspected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    framework: str | None = None
    executed: bool = False
    successful: bool = False
    collected_only: bool = False
    summary: str | None = Field(default=None, max_length=500)


class ShellTestResult(ShellRunResult):
    test_execution: TestExecutionEvidence


@dataclass(slots=True)
class _CaptureBudget:
    limit: int
    used: int = 0

    def consume(self, size: int) -> None:
        self.used += size
        if self.used > self.limit:
            raise ShellOutputTooLargeError(self.limit)


async def shell_run(
    params: ShellRunParams,
    context: ToolExecutionContext,
) -> ShellRunResult:
    resolver, cwd, environment, executable = _prepare_shell(params, context)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = await asyncio.create_subprocess_exec(
        executable,
        *params.args,
        cwd=cwd,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
    )
    budget = _CaptureBudget(params.max_output_bytes)
    stdout_task = asyncio.create_task(_read_stream(process.stdout, budget))
    stderr_task = asyncio.create_task(_read_stream(process.stderr, budget))
    try:
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        exit_code = await process.wait()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise
    return ShellRunResult(
        command=(params.command, *params.args),
        cwd=resolver.relative_name(cwd),
        exit_code=exit_code,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        output_bytes=len(stdout) + len(stderr),
    )


async def shell_test(
    params: ShellRunParams,
    context: ToolExecutionContext,
) -> ShellTestResult:
    """Run a test command and attach conservative execution evidence for orchestration."""

    result = await shell_run(params, context)
    return ShellTestResult(
        **result.model_dump(),
        test_execution=assess_test_execution(result),
    )


def assess_test_execution(result: ShellRunResult) -> TestExecutionEvidence:
    """Recognize an actually executed pytest run; unknown formats never enable fast finalization."""

    command = tuple(part.casefold() for part in result.command)
    if not _is_pytest_command(command):
        return TestExecutionEvidence()
    pytest_args = command[1:] if command[0] in {"pytest", "pytest.exe"} else command[3:]
    collected_only = any(
        argument in _PYTEST_COLLECTION_FLAGS
        or any(argument.startswith(f"{flag}=") for flag in _PYTEST_COLLECTION_FLAGS)
        for argument in pytest_args
    )
    summary = _execution_summary(f"{result.stdout}\n{result.stderr}")
    executed = not collected_only and summary is not None
    return TestExecutionEvidence(
        framework="pytest",
        executed=executed,
        successful=executed and result.exit_code == 0,
        collected_only=collected_only,
        summary=summary,
    )


def is_successful_test_result(value: object) -> bool:
    """Return true only for internally structured evidence of an executed successful test run."""

    try:
        result = ShellTestResult.model_validate(value)
    except (TypeError, ValueError):
        return False
    evidence = result.test_execution
    return (
        result.exit_code == 0
        and evidence.executed
        and evidence.successful
        and not evidence.collected_only
    )


def _is_pytest_command(command: tuple[str, ...]) -> bool:
    if not command:
        return False
    if command[0] in {"pytest", "pytest.exe"}:
        return True
    return (
        len(command) >= 3
        and command[0] in {"python", "python.exe", "python3", "python3.exe"}
        and command[1:3] == ("-m", "pytest")
    )


def _execution_summary(output: str) -> str | None:
    for line in reversed(output.splitlines()):
        normalized = line.strip()
        if _PYTEST_EXECUTION_SUMMARY.search(normalized):
            return normalized[:500]
    return None


async def preflight_shell_run(
    params: ShellRunParams,
    context: ToolExecutionContext,
) -> ToolApprovalPreview:
    resolver, cwd, _environment, _executable = _prepare_shell(params, context)
    rendered = " ".join((params.command, *params.args))
    return ToolApprovalPreview(impact=f"Run {rendered[:1000]} in {resolver.relative_name(cwd)}")


def _prepare_shell(
    params: ShellRunParams,
    context: ToolExecutionContext,
) -> tuple[WorkspacePathResolver, Path, dict[str, str], str]:
    command = params.command.casefold()
    if command not in context.lease.commands:
        raise ShellCommandLeaseMismatchError(params.command)
    resolver = WorkspacePathResolver(context.workspace)
    cwd = resolver.resolve_read(
        params.cwd,
        allowed_scopes=context.lease.filesystem_read,
        expected="directory",
    )
    environment = {name: os.environ[name] for name in _ENVIRONMENT_ALLOWLIST if name in os.environ}
    environment.setdefault("PYTHONUTF8", "1")
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    environment["PATH"] = _sanitized_path(environment.get("PATH", ""))
    if not environment["PATH"]:
        raise ShellCommandDeniedError(params.command)
    executable = shutil.which(params.command, path=environment["PATH"])
    if executable is None:
        raise ShellCommandDeniedError(params.command)
    return resolver, cwd, environment, executable


def _sanitized_path(value: str) -> str:
    entries: list[str] = []
    for item in value.split(os.pathsep):
        if not item:
            continue
        path = Path(item).expanduser()
        if not path.is_absolute():
            continue
        entries.append(str(path.resolve(strict=False)))
    return os.pathsep.join(entries)


async def _read_stream(
    stream: asyncio.StreamReader | None,
    budget: _CaptureBudget,
) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    while chunk := await stream.read(64 * 1024):
        budget.consume(len(chunk))
        chunks.append(chunk)
    return b"".join(chunks)


def register_shell_tools(registry: ToolRegistry) -> None:
    """Register argv-only shell tools; both remain disabled without explicit leases."""

    capabilities = frozenset(
        {
            Capability.SHELL_EXECUTE,
            Capability.FILESYSTEM_READ,
            Capability.FILESYSTEM_WRITE,
        }
    )
    run_policy = ToolPolicy(
        risk=RiskLevel.DANGEROUS,
        requires_approval=True,
        idempotent=False,
        timeout_seconds=120,
        max_output_bytes=64 * 1024,
        capabilities=capabilities,
    )
    registry.register(
        ToolDefinition(
            name="shell.run",
            description=(
                "Run one approved argv-based command in the workspace with a minimal environment."
            ),
            params_model=ShellRunParams,
            policy=run_policy,
            handler=shell_run,
            preflight=preflight_shell_run,
        )
    )
    registry.register(
        ToolDefinition(
            name="shell.test",
            description=(
                "Run one approved test command in the workspace with bounded output and timeout."
            ),
            params_model=ShellRunParams,
            policy=run_policy,
            handler=shell_test,
            preflight=preflight_shell_run,
        )
    )
