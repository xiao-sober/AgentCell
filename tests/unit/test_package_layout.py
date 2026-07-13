from __future__ import annotations

import importlib

import agentcell


def test_package_exposes_version() -> None:
    assert agentcell.__version__ == "0.1.0"


def test_architecture_boundary_packages_are_importable() -> None:
    package_names = (
        "agents",
        "api",
        "budgets",
        "cli",
        "events",
        "kernel",
        "memory",
        "policy",
        "providers",
        "storage",
        "telemetry",
        "tools",
    )

    imported = [importlib.import_module(f"agentcell.{name}") for name in package_names]

    assert len(imported) == len(package_names)
