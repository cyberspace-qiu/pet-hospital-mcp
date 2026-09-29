"""Structured error model used by every MCP tool.

The shape is intentionally identical across tools so an AI agent can rely on
one parsing rule to recover `code`, `message`, and `details` from any failure.

Wire form (returned as the tool's structured `error` field)::

    {
      "error": {
        "code": "BACKEND_TIMEOUT",
        "message": "Pet Hospital API timed out after 5.0s",
        "details": { "endpoint": "/api/v1/pets", "elapsed_ms": 5003 }
      }
    }

The Python stack (httpx, pydantic, the MCP SDK) is never surfaced to the
client; every upstream or internal failure is mapped to one of the codes below.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(str, Enum):
    """Stable error codes exposed to MCP clients."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    BACKEND_TIMEOUT = "BACKEND_TIMEOUT"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    BACKEND_API_ERROR = "BACKEND_API_ERROR"
    BACKEND_INVALID_RESPONSE = "BACKEND_INVALID_RESPONSE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorDetail(BaseModel):
    """A single structured error returned by a tool."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str = Field(..., description="Human-readable error message.")
    status: int | None = Field(
        default=None,
        description=(
            "Upstream HTTP status code when the failure came from the backend, "
            "or null when the failure originated locally."
        ),
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form context: endpoint, attempt, error_type, etc.",
    )


class ToolErrorPayload(BaseModel):
    """Wrapper that puts the error under a single `error` key.

    Used as the structured payload of a failed tool call so clients can read
    `result.structured_content["error"]` regardless of which tool failed.
    """

    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail


def build_error(
    code: ErrorCode,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> ToolErrorPayload:
    """Construct a `ToolErrorPayload` with normalised field order."""
    return ToolErrorPayload(error=ErrorDetail(code=code, message=message, details=details or {}))


__all__ = ["ErrorCode", "ErrorDetail", "ToolErrorPayload", "build_error"]
