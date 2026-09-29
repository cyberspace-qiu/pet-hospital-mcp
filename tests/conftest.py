"""Pytest fixtures shared by all test modules.

The fixtures here are deliberately tiny: they build the same ``MCPServer`` the
production code builds, but with an ``httpx.MockTransport`` so tests never
touch the real Go service. Each test that needs HTTP behaviour installs its
own handler on the transport.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from pet_hospital_mcp.config import Config
from pet_hospital_mcp.logging_config import configure_logging


# A canonical success response from the Go REST API. Tests build variants on
# top of this rather than redefining the whole shape every time.
SAMPLE_PET = {
    "id": "PET-000001",
    "name": "旺财",
    "species": "犬",
    "breed": "金毛",
    "gender": "公",
    "ageMonths": 36,
    "color": "金色",
    "chipNo": "CN-1234567890",
    "ownerName": "张三",
    "ownerPhone": "13800001111",
    "ownerAddr": "北京市朝阳区",
    "doctor": "李医生",
    "disease": "急性肠胃炎",
    "status": "待就诊",
    "allergy": "无",
    "note": "",
    "records": [
        {
            "id": "REC-1",
            "visitDate": "2026-01-01",
            "doctor": "李医生",
            "diagnosis": "急性肠胃炎",
            "symptoms": "呕吐腹泻",
            "treatment": "补液消炎",
            "prescription": ["阿莫西林"],
            "charge": 380.0,
            "createdAt": "2026-01-01T10:00:00+08:00",
        }
    ],
    "charges": [
        {
            "id": "CHG-1",
            "item": "血常规检查",
            "category": "检查",
            "amount": 180.0,
            "doctor": "李医生",
            "date": "2026-01-01",
        }
    ],
    "totalCost": 560.0,
    "visitCount": 1,
    "createdAt": "2026-01-01T10:00:00+08:00",
    "updatedAt": "2026-01-01T10:00:00+08:00",
}

SAMPLE_LIST_RESPONSE = {
    "code": 200,
    "message": "ok",
    "data": {
        "items": [SAMPLE_PET],
        "total": 1,
        "page": 1,
        "pageSize": 20,
        "totalPages": 1,
        "totalCost": 560.0,
    },
    "time": "2026-01-01T10:00:00+08:00",
}


def _envelope(data: Any, *, code: int = 200, message: str = "ok") -> dict[str, Any]:
    return {"code": code, "message": message, "data": data, "time": "2026-01-01T10:00:00+08:00"}


@pytest.fixture(autouse=True)
def _configure_logging_once() -> None:
    """Initialise structlog before any test imports the server."""
    configure_logging()


@pytest.fixture
def test_config() -> Config:
    """Config with a short timeout so timeout tests don't hang the suite."""
    return Config(
        pet_hospital_base_url="http://127.0.0.1:8080",
        mcp_host="127.0.0.1",
        mcp_port=8000,
        mcp_endpoint="/mcp",
        health_endpoint="/health",
        backend_timeout_seconds=1.0,
        backend_retries=1,
        backend_backoff_seconds=0.01,
    )


def make_transport(
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
) -> httpx.MockTransport:
    """Wrap a sync or async handler into an ``httpx.MockTransport``."""
    return httpx.MockTransport(handler)


@pytest.fixture
def sample_pet() -> dict[str, Any]:
    return SAMPLE_PET


@pytest.fixture
def sample_list_response() -> dict[str, Any]:
    return SAMPLE_LIST_RESPONSE


@pytest.fixture
def envelope():
    return _envelope


@pytest.fixture
def sample_pet_with_null_records() -> dict[str, Any]:
    """A pet whose `records` and `charges` arrive as `null`.

    Go's `omitempty` on a nil slice serialises to JSON `null` when the slice
    is nil (not empty). The MCP adapter must accept this without raising.
    """
    pet = dict(SAMPLE_PET)
    pet = {**pet, "records": None, "charges": None}
    return pet


__all__ = [
    "SAMPLE_PET",
    "SAMPLE_LIST_RESPONSE",
    "make_transport",
    "test_config",
    "sample_pet",
    "sample_list_response",
    "sample_pet_with_null_records",
    "envelope",
]
