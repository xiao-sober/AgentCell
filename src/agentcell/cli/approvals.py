"""Explicit CLI approval interaction and non-interactive decision parsing."""

from __future__ import annotations

import json
from uuid import UUID

from pydantic import TypeAdapter
from rich.console import Console

from agentcell.cli.display import CliEventRenderer
from agentcell.events import JsonValue
from agentcell.policy import Approval, ApprovalDecision, ApprovalDecisionKind


def prompt_approval(
    approval: Approval,
    *,
    output: Console,
    renderer: CliEventRenderer | None = None,
) -> ApprovalDecision | None:
    """Suspend Live, show the complete bounded envelope, and require an explicit key."""

    if renderer is not None:
        renderer.suspend()
    output.print(
        f"[yellow]Approval required[/yellow] agent={approval.agent_name} "
        f"provider={approval.provider}/{approval.model}"
    )
    output.print(f"tool={approval.tool_name} risk={approval.risk.value}")
    output.print(f"impact: {approval.impact}")
    output.print(
        f"idempotent={approval.idempotent} timeout={approval.timeout_seconds}s "
        f"remaining_tool_calls={approval.remaining_budget.remaining.tool_calls}"
    )
    output.print_json(json.dumps(approval.arguments, ensure_ascii=False))
    if approval.diff:
        output.print(approval.diff, markup=False)
    while True:
        try:
            choice = (
                output.input(
                    "[yellow][a]pprove [t] approve same tool this Run "
                    "[m]odify [r]eject [q] leave pending > [/yellow]"
                )
                .strip()
                .casefold()
            )
        except EOFError:
            return None
        if choice in {"", "q", "quit", "pending"}:
            return None
        if choice in {"a", "approve", "y", "yes"}:
            return ApprovalDecision(kind=ApprovalDecisionKind.APPROVE)
        if choice in {"t", "temporary"}:
            return ApprovalDecision(kind=ApprovalDecisionKind.APPROVE, grant_same_tool=True)
        if choice in {"m", "modify"}:
            try:
                raw = output.input("[yellow]approved arguments as JSON> [/yellow]")
                value = TypeAdapter(dict[str, JsonValue]).validate_json(raw)
            except (EOFError, ValueError):
                output.print("[red]Arguments must be one valid JSON object.[/red]")
                continue
            return ApprovalDecision(kind=ApprovalDecisionKind.MODIFY, arguments=value)
        if choice in {"r", "reject", "n", "no"}:
            return ApprovalDecision(kind=ApprovalDecisionKind.REJECT)
        output.print("[red]Unknown choice; use a, t, m, r, or q.[/red]")


def resume_decision(
    *,
    approval_id: UUID | None,
    decision: ApprovalDecisionKind | None,
    arguments_json: str | None,
    grant_same_tool: bool,
) -> ApprovalDecision | None:
    """Build one validated non-interactive decision; no option defaults to approval."""

    if approval_id is None:
        if decision is not None or arguments_json is not None or grant_same_tool:
            raise ValueError("Approval decision options require --approval-id")
        return None
    if decision is None:
        raise ValueError("--approval-id requires an explicit --decision")
    arguments = None
    if arguments_json is not None:
        arguments = TypeAdapter(dict[str, JsonValue]).validate_json(arguments_json)
    if decision is ApprovalDecisionKind.MODIFY and arguments is None:
        raise ValueError("--decision modify requires --arguments-json")
    if decision is not ApprovalDecisionKind.MODIFY and arguments is not None:
        raise ValueError("--arguments-json is valid only with --decision modify")
    return ApprovalDecision(
        kind=decision,
        arguments=arguments,
        grant_same_tool=grant_same_tool,
    )
