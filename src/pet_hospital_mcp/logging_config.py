"""Structured logging setup.

Two responsibilities:

1. Configure structlog to emit JSON with the fields required for teaching
   observability: ``timestamp``, ``tool_name``, ``params``, ``status``,
   ``duration_ms``.
2. Recursively mask sensitive fields (``ownerPhone``, ``ownerAddr``, ``chipNo``
   and their snake_case variants) so logs never carry raw PII.

The logger is process-wide: tests and the MCP server pick it up by name
``pet_hospital_mcp``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

import structlog


_LOGGER_NAME = "pet_hospital_mcp"

# Keys (case-insensitive) that must be masked wherever they appear.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "ownerphone",
        "owner_phone",
        "owneraddr",
        "owner_addr",
        "chipno",
        "chip_no",
    }
)

_MASK = "***MASKED***"


def _mask_value(value: Any) -> Any:
    if isinstance(value, str):
        # Keep type code but scrub the content.
        return _MASK
    return _MASK


def _mask_dict(payload: dict[str, Any]) -> dict[str, Any]:
    masked: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(key, str) and key.lower() in SENSITIVE_KEYS:
            masked[key] = _mask_value(value)
        elif isinstance(value, dict):
            masked[key] = _mask_dict(value)
        elif isinstance(value, list):
            masked[key] = _mask_list(value)
        else:
            masked[key] = value
    return masked


def _mask_list(values: Iterable[Any]) -> list[Any]:
    out: list[Any] = []
    for value in values:
        if isinstance(value, dict):
            out.append(_mask_dict(value))
        elif isinstance(value, list):
            out.append(_mask_list(value))
        else:
            out.append(value)
    return out


def mask_sensitive(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy of ``payload`` with every sensitive key masked recursively."""
    if not payload:
        return {}
    return _mask_dict(payload)


def configure_logging(level: int = logging.INFO) -> structlog.stdlib.BoundLogger:
    """Initialise structlog and return the package logger.

    Safe to call more than once; subsequent calls only update the level.
    """
    logging.basicConfig(
        level=level,
        format="%(message)s",
        stream=None,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.stdlib.add_log_level,
            _mask_sensitive_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger(_LOGGER_NAME)


def _mask_sensitive_processor(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog processor: recursively mask sensitive keys in every event."""
    for key in ("params", "args", "kwargs", "request", "response", "details"):
        if key in event_dict and isinstance(event_dict[key], dict):
            event_dict[key] = _mask_dict(event_dict[key])
    return event_dict


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a child logger; configure_logging must have been called first."""
    return structlog.get_logger(name or _LOGGER_NAME)


__all__ = [
    "configure_logging",
    "get_logger",
    "mask_sensitive",
    "SENSITIVE_KEYS",
]
