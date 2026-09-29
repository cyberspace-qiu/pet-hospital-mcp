"""Tool-level tests for ``list_pets``.

Covers:

1. Normal call: the request path and every filter/sort/pagination parameter
   is forwarded verbatim to the upstream Go REST API.
2. Upstream 4xx and 5xx responses are mapped to ``BACKEND_API_ERROR``.
3. Timeout and connect exceptions are mapped to ``BACKEND_TIMEOUT`` /
   ``BACKEND_UNAVAILABLE`` and retried as configured.
4. Non-JSON responses and schema mismatches map to ``BACKEND_INVALID_RESPONSE``.
5. ``records``/``charges`` arriving as ``null`` (Go's nil-slice omitempty)
   is normalised to empty lists, not surfaced as a validation error.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pet_hospital_mcp.errors import ErrorCode
from pet_hospital_mcp.rest_client import PetHospitalClient, RestError
from pet_hospital_mcp.tools.list_pets import (
    ListPetsErrorOutput,
    ListPetsInput,
    ListPetsSuccessOutput,
    list_pets,
)

from .conftest import SAMPLE_LIST_RESPONSE, make_transport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_response(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        content=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


async def _run_tool_with_transport(
    transport: httpx.MockTransport,
    params: ListPetsInput,
    *,
    base_url: str = "http://127.0.0.1:8080",
) -> Any:
    """Drive the tool end-to-end against a mock transport."""
    from pet_hospital_mcp.config import Config

    config = Config(
        pet_hospital_base_url=base_url,
        mcp_host="127.0.0.1",
        mcp_port=8000,
        mcp_endpoint="/mcp",
        health_endpoint="/health",
        backend_timeout_seconds=1.0,
        backend_retries=1,
        backend_backoff_seconds=0.01,
    )
    async with PetHospitalClient(config, transport=transport) as client:
        return await list_pets(client, params)


# ---------------------------------------------------------------------------
# 1. Normal call: path + every parameter forwarded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_call_forwards_all_params() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _ok_response(SAMPLE_LIST_RESPONSE)

    transport = make_transport(handler)
    params = ListPetsInput(
        q="肠胃炎",
        name="旺财",
        ownerName="张三",
        ownerPhone="13800001111",
        species="犬",
        doctor="李医生",
        disease="肠胃炎",
        status="待就诊",
        min=100,
        max=1000,
        sortBy="totalCost",
        order="desc",
        page=2,
        pageSize=15,
    )

    result = await _run_tool_with_transport(transport, params)

    # Single request was made.
    assert len(captured) == 1
    request = captured[0]

    # Path.
    assert request.url.path == "/api/v1/pets"

    # Method.
    assert request.method == "GET"

    # Every parameter is forwarded verbatim.
    query = {pair[0]: pair[1] for pair in request.url.params.multi_items()}
    assert query["q"] == "肠胃炎"
    assert query["name"] == "旺财"
    assert query["ownerName"] == "张三"
    assert query["ownerPhone"] == "13800001111"
    assert query["species"] == "犬"
    assert query["doctor"] == "李医生"
    assert query["disease"] == "肠胃炎"
    assert query["status"] == "待就诊"
    assert query["min"] == "100.0"
    assert query["max"] == "1000.0"
    assert query["sortBy"] == "totalCost"
    assert query["order"] == "desc"
    assert query["page"] == "2"
    assert query["pageSize"] == "15"

    # No adapter-private parameters leaked into the request.
    assert set(query.keys()) == {
        "q",
        "name",
        "ownerName",
        "ownerPhone",
        "species",
        "doctor",
        "disease",
        "status",
        "min",
        "max",
        "sortBy",
        "order",
        "page",
        "pageSize",
    }

    # Success payload mirrors the upstream `data` block.
    assert isinstance(result, ListPetsSuccessOutput)
    assert result.total == 1
    assert result.page == 1
    assert result.pageSize == 20
    assert result.totalPages == 1
    assert result.totalCost == 560.0
    assert len(result.items) == 1
    pet = result.items[0]
    assert pet.id == "PET-000001"
    assert pet.name == "旺财"
    assert pet.species == "犬"
    assert pet.totalCost == 560.0
    assert len(pet.records) == 1
    assert pet.records[0].diagnosis == "急性肠胃炎"
    assert pet.records[0].prescription == ["阿莫西林"]
    assert len(pet.charges) == 1
    assert pet.charges[0].amount == 180.0


@pytest.mark.asyncio
async def test_normal_call_omits_none_params() -> None:
    """When an optional parameter is None, it must not appear in the request."""

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _ok_response(SAMPLE_LIST_RESPONSE)

    transport = make_transport(handler)
    params = ListPetsInput(species="猫")
    result = await _run_tool_with_transport(transport, params)
    assert isinstance(result, ListPetsSuccessOutput)
    assert len(captured) == 1
    query = {pair[0]: pair[1] for pair in captured[0].url.params.multi_items()}
    # Only the one set parameter made it through.
    assert set(query.keys()) == {"species"}
    assert query["species"] == "猫"


# ---------------------------------------------------------------------------
# 5. `records` / `charges` arriving as null
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_records_and_charges_are_normalised_to_empty(
    sample_pet: dict[str, Any],
    sample_pet_with_null_records: dict[str, Any],
    envelope,
) -> None:
    """Go's omitempty on a nil slice yields JSON `null`; we accept it."""
    body = envelope(
        {
            "items": [sample_pet_with_null_records],
            "total": 1,
            "page": 1,
            "pageSize": 20,
            "totalPages": 1,
            "totalCost": 0.0,
        }
    )
    transport = make_transport(lambda _: _ok_response(body))
    result = await _run_tool_with_transport(transport, ListPetsInput())
    assert isinstance(result, ListPetsSuccessOutput)
    assert result.items[0].records == []
    assert result.items[0].charges == []


# ---------------------------------------------------------------------------
# 3. Timeout and connect exceptions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_returns_backend_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated upstream timeout")

    transport = make_transport(handler)
    result = await _run_tool_with_transport(transport, ListPetsInput())
    assert isinstance(result, ListPetsErrorOutput)
    assert result.error.code == ErrorCode.BACKEND_TIMEOUT
    assert "timed out" in result.error.message.lower()


@pytest.mark.asyncio
async def test_connect_error_returns_backend_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection refused")

    transport = make_transport(handler)
    result = await _run_tool_with_transport(transport, ListPetsInput())
    assert isinstance(result, ListPetsErrorOutput)
    assert result.error.code == ErrorCode.BACKEND_UNAVAILABLE
    assert "unreachable" in result.error.message.lower()


# ---------------------------------------------------------------------------
# 2. 4xx / 5xx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_4xx_returns_backend_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            content=json.dumps({"code": 400, "message": "bad request", "data": None}).encode(),
            headers={"content-type": "application/json"},
        )

    transport = make_transport(handler)
    result = await _run_tool_with_transport(transport, ListPetsInput())
    assert isinstance(result, ListPetsErrorOutput)
    assert result.error.code == ErrorCode.BACKEND_API_ERROR
    assert result.error.details.get("endpoint") == "/api/v1/pets"


@pytest.mark.asyncio
async def test_500_then_success_retries_then_succeeds() -> None:
    """5xx is retried; a successful retry returns the success payload."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(500, content=b"internal error")
        return _ok_response(SAMPLE_LIST_RESPONSE)

    transport = make_transport(handler)
    result = await _run_tool_with_transport(transport, ListPetsInput())
    assert isinstance(result, ListPetsSuccessOutput)
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_500_persistently_returns_backend_api_error() -> None:
    """After retries are exhausted, a persistent 5xx surfaces as API error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"down")

    transport = make_transport(handler)
    result = await _run_tool_with_transport(transport, ListPetsInput())
    assert isinstance(result, ListPetsErrorOutput)
    assert result.error.code == ErrorCode.BACKEND_API_ERROR
    assert result.error.status == 503


# ---------------------------------------------------------------------------
# 4. Non-JSON / schema mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_json_returns_backend_invalid_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html>not json</html>",
            headers={"content-type": "text/html"},
        )

    transport = make_transport(handler)
    result = await _run_tool_with_transport(transport, ListPetsInput())
    assert isinstance(result, ListPetsErrorOutput)
    assert result.error.code == ErrorCode.BACKEND_INVALID_RESPONSE
    assert "non-json" in result.error.message.lower()


@pytest.mark.asyncio
async def test_json_without_data_returns_backend_invalid_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response({"code": 200, "message": "ok", "data": None})

    transport = make_transport(handler)
    result = await _run_tool_with_transport(transport, ListPetsInput())
    assert isinstance(result, ListPetsErrorOutput)
    assert result.error.code == ErrorCode.BACKEND_INVALID_RESPONSE


@pytest.mark.asyncio
async def test_data_not_object_returns_backend_invalid_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response({"code": 200, "message": "ok", "data": [1, 2, 3]})

    transport = make_transport(handler)
    result = await _run_tool_with_transport(transport, ListPetsInput())
    assert isinstance(result, ListPetsErrorOutput)
    assert result.error.code == ErrorCode.BACKEND_INVALID_RESPONSE


@pytest.mark.asyncio
async def test_response_missing_required_fields_is_invalid() -> None:
    """`data` exists but does not match the Result schema (missing items)."""
    body = {
        "code": 200,
        "message": "ok",
        "data": {"total": "not_an_int"},
        "time": "2026-01-01T10:00:00+08:00",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(body)

    transport = make_transport(handler)
    result = await _run_tool_with_transport(transport, ListPetsInput())
    # `total` is declared as int; a string fails validation. Because items
    # defaults to [] but total/page/etc. must coerce, this should land as
    # BACKEND_INVALID_RESPONSE. Pydantic's coercion may accept the string
    # "not_an_int" by failing — assert at least the error code is the schema
    # one.
    assert isinstance(result, ListPetsErrorOutput)
    assert result.error.code == ErrorCode.BACKEND_INVALID_RESPONSE


# ---------------------------------------------------------------------------
# RestError propagation when client is misused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_used_outside_context_returns_internal_error() -> None:
    """Direct RestError (not raised through the tool) is converted to a payload."""
    # Use a transport we never enter: instantiate client but skip __aenter__.
    from pet_hospital_mcp.config import Config

    config = Config(
        pet_hospital_base_url="http://127.0.0.1:8080",
        mcp_host="127.0.0.1",
        mcp_port=8000,
        mcp_endpoint="/mcp",
        health_endpoint="/health",
        backend_timeout_seconds=1.0,
        backend_retries=1,
        backend_backoff_seconds=0.01,
    )
    # Pass a transport so __init__ does not require a real network stack.
    transport = make_transport(lambda _: _ok_response(SAMPLE_LIST_RESPONSE))
    client = PetHospitalClient(config, transport=transport)
    # Without `async with`, list_pets raises RestError(INTERNAL_ERROR).
    with pytest.raises(RestError) as exc:
        await client.list_pets({})
    assert exc.value.code == ErrorCode.INTERNAL_ERROR
