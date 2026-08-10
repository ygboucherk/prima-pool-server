"""Shared test fixtures for the demo model registry."""
from __future__ import annotations

import pytest

from prima_pool_server.config import ModelDef

# A single registered model with a fixed GGUF hash.
DEMO_HASH = "a" * 64


@pytest.fixture()
def demo_models() -> dict[str, ModelDef]:
    return {
        "demo-model": ModelDef(
            slug="demo-model",
            gguf_sha256=DEMO_HASH,
            required_memory_mb=4096,
        )
    }