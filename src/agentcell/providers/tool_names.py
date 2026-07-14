"""Provider-portable tool naming shared by real and deterministic model adapters."""

from __future__ import annotations

import re

_UNSUPPORTED_TOOL_NAME_CHARACTER = re.compile(r"[^A-Za-z0-9_-]")


def portable_tool_name(name: str) -> str:
    """Map a stable domain tool name to the common Provider function-name subset."""

    return _UNSUPPORTED_TOOL_NAME_CHARACTER.sub("_", name)[:64]
