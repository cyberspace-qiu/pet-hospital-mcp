"""Runtime configuration for the Pet Hospital MCP service.

All settings are environment-driven so the same code runs in dev, CI, and prod
without code changes. The Go REST API base URL, the MCP bind address, and
upstream HTTP behaviour are all here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    """Resolved configuration loaded from environment variables.

    Defaults target the local teaching setup: the Go REST API on
    ``http://127.0.0.1:8080`` and the MCP service on ``127.0.0.1:8000``.
    """

    # Upstream Go REST API.
    pet_hospital_base_url: str
    # MCP Streamable HTTP service.
    mcp_host: str
    mcp_port: int
    mcp_endpoint: str
    health_endpoint: str
    # Upstream HTTP behaviour.
    backend_timeout_seconds: float
    backend_retries: int
    backend_backoff_seconds: float

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            pet_hospital_base_url=_env_str(
                "PET_HOSPITAL_BASE_URL", "http://127.0.0.1:8080"
            ).rstrip("/"),
            mcp_host=_env_str("MCP_HOST", "127.0.0.1"),
            mcp_port=_env_int("MCP_PORT", 8000),
            mcp_endpoint=_env_str("MCP_ENDPOINT", "/mcp"),
            health_endpoint=_env_str("MCP_HEALTH_ENDPOINT", "/health"),
            backend_timeout_seconds=_env_float("BACKEND_TIMEOUT_SECONDS", 5.0),
            backend_retries=_env_int("BACKEND_RETRIES", 2),
            backend_backoff_seconds=_env_float("BACKEND_BACKOFF_SECONDS", 0.25),
        )


# Protocol / SDK identity exposed by the MCP service.
MCP_PROTOCOL_VERSION: Final[str] = "2026-07-28"
MCP_SDK_VERSION: Final[str] = "2.0.0"
MCP_SERVER_NAME: Final[str] = "pet-hospital-mcp"
MCP_SERVER_VERSION: Final[str] = "0.1.0"
