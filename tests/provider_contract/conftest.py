"""Provider contract safety guard: online model requests are opt-in only."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic_ai import models


@pytest.fixture(autouse=True)
def disable_online_model_requests() -> Iterator[None]:
    previous = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = False
    try:
        yield
    finally:
        models.ALLOW_MODEL_REQUESTS = previous
