"""Default-deny policy for durable memory writes and sensitive candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass

from agentcell.errors import MemoryApprovalRequiredError, MemoryPolicyRejectedError
from agentcell.memory.models import MemoryCandidate, MemoryKind

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_ -]?key|password|authorization)\s*[:=]"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
)


@dataclass(frozen=True, slots=True)
class MemoryPolicyDecision:
    sensitive: bool
    requires_approval: bool


class MemoryPolicy:
    """Reject credentials and require approval for stable user semantics."""

    def evaluate(
        self,
        candidate: MemoryCandidate,
        *,
        approval_granted: bool = False,
    ) -> MemoryPolicyDecision:
        if any(pattern.search(candidate.content) for pattern in _SECRET_PATTERNS):
            raise MemoryPolicyRejectedError("Memory contains credential-like material")
        requires_approval = candidate.kind is MemoryKind.SEMANTIC
        if requires_approval and not approval_granted:
            raise MemoryApprovalRequiredError("Semantic memory requires explicit approval")
        return MemoryPolicyDecision(sensitive=False, requires_approval=requires_approval)
