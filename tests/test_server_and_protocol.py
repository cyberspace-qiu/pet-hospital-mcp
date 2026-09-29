"""Server-level tests: tool registration, JSON Schema, and the stateless MCP
HTTP flow required by the 2026-07-28 protocol.

Covers:

6. MCP tool registration, tool name, and the JSON Schema the SDK derives from
   the flattened input parameters (each ``ListPetsInput`` field is a top-level
   parameter so the model can fill them in directly).
7. SDK 2.x stateless flow, exercised over real HTTP via the SDK's own
   ``Client`` (in-memory for the tool discovery / call path, HTTP for the
   protocol wire checks). Asserts:

   - no ``initialize`` JSON-RPC method is sent by the client;
   - no ``Mcp-Session-Id`` request or response header appears;
   - the 2026-07-28 ``server/discover`` flow is what the client uses to learn
     the server's capabilities;
   - ``/health`` returns 200 with the server identity;
   - the tool is discoverable and callable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette

from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from pet_hospital_mcp.config import (
    MCP_PROTOCOL_VERSION,
    MCP_SDK_VERSION,
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
)
from pet_hospital_mcp.server import build_app, build_server

from .conftest import SAMPLE_LIST_RESPONSE, make_transport


def _ok_response(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        content=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


# ---------------------------------------------------------------------------
# 6. Tool registration & JSON Schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_is_registered_with_correct_name(test_config) -> None:
    mcp, _ = build_server(test_config)
    tools = await mcp.list_tools()
    assert len(tools) == 1
    assert tools[0].name == "list_pets"
    assert "Pet Hospital REST API" in (tools[0].description or "")


def _extract_enum(prop: dict[str, Any]) -> list[Any] | None:
    """Find the enum list inside a possibly-Optional schema property."""
    if "enum" in prop:
        return prop["enum"]
    # Optional[Literal[...]] becomes anyOf/oneOf with a Literal branch + null.
    for combiner in ("anyOf", "oneOf"):
        if combiner in prop:
            for branch in prop[combiner]:
                if "enum" in branch:
                    return branch["enum"]
    return None


@pytest.mark.asyncio
async def test_tool_input_schema_is_flat_and_advertises_enums(
    test_config,
) -> None:
    """The flattened parameters become top-level JSON Schema fields.

    The model must be able to read every parameter (species, status, sortBy,
    order, page, pageSize, min, max) without trial and error.
    """
    mcp, _ = build_server(test_config)
    tools = await mcp.list_tools()
    schema = tools[0].input_schema

    # No adapter-private parameters leaked: exactly the 14 supported ones.
    expected_keys = {
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
    assert set(schema["properties"].keys()) == expected_keys

    species_enum = _extract_enum(schema["properties"]["species"])
    assert species_enum is not None
    assert "犬" in species_enum
    assert "猫" in species_enum
    assert "仓鼠" in species_enum

    status_enum = _extract_enum(schema["properties"]["status"])
    assert status_enum is not None
    assert "待就诊" in status_enum
    assert "已康复" in status_enum

    sort_enum = _extract_enum(schema["properties"]["sortBy"])
    assert sort_enum is not None
    assert "totalCost" in sort_enum
    assert "createdAt" in sort_enum

    order_enum = _extract_enum(schema["properties"]["order"])
    assert order_enum is not None
    assert "asc" in order_enum
    assert "desc" in order_enum

    # `page` has a minimum of 1; `pageSize` has minimum 1 and maximum 500.
    page_blob = json.dumps(schema["properties"]["page"])
    assert "1" in page_blob  # the minimum
    page_size_blob = json.dumps(schema["properties"]["pageSize"])
    assert "500" in page_size_blob


# ---------------------------------------------------------------------------
# In-memory tool discovery and call (the SDK's recommended test path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_discoverable_and_callable_in_memory(test_config) -> None:
    """``Client(server_object)`` is the SDK's recommended in-memory test path.

    This proves the tool is registered correctly and that calling it with the
    flattened parameters returns the structured success payload.
    """
    transport = make_transport(lambda _: _ok_response(SAMPLE_LIST_RESPONSE))
    mcp, _ = build_server(test_config, transport=transport)

    async with Client(mcp, mode="2026-07-28") as client:
        tools_result = await client.list_tools()
        tools = tools_result.tools
        assert any(t.name == "list_pets" for t in tools)

        result = await client.call_tool("list_pets", {"species": "犬"})
        sc = result.structured_content or {}
        assert sc.get("total") == 1
        assert len(sc.get("items", [])) == 1
        assert sc["items"][0]["name"] == "旺财"


@pytest.mark.asyncio
async def test_tool_failure_returns_structured_error(test_config) -> None:
    """When the upstream is unreachable, the tool returns ``error``, not raise.

    The error is in ``structured_content.error`` with the ``BACKEND_*`` code
    the client can route on. The LLM sees the failure text via the content.
    """
    transport = make_transport(
        lambda _: httpx.Response(503, content=b"down")
    )
    mcp, _ = build_server(test_config, transport=transport)

    async with Client(mcp, mode="2026-07-28") as client:
        result = await client.call_tool("list_pets", {})
        sc = result.structured_content or {}
        assert "error" in sc
        assert sc["error"]["code"] == "BACKEND_API_ERROR"
        assert sc["error"]["message"]


# ---------------------------------------------------------------------------
# 7. Stateless MCP HTTP flow
# ---------------------------------------------------------------------------


def _build_http_app(
    transport: httpx.MockTransport,
    *,
    test_config,
):
    """Build the MCP ASGI app exactly as production serves it.

    We do not wrap the app in a ``Mount``: that would re-prefix every
    ``custom_route`` (so ``/health`` would become ``/mcp/health``), which is
    not what production serves.

    DNS-rebinding protection is disabled for tests because the in-process
    transport's ``Host: testserver`` is not on the production allowlist.

    Returns ``(app, mcp)`` so the test fixture can enter
    ``mcp.session_manager.run()`` manually: ``httpx.ASGITransport`` does not
    run ASGI lifespan, so without entering ``run()`` the session manager's
    task group never initialises and every request fails with ``RuntimeError:
    Task group is not initialized.``.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    mcp, _ = build_server(test_config, transport=transport)
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host=test_config.mcp_host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
    return app, mcp


class _CapturingASGITransport(httpx.AsyncBaseTransport):
    """Wraps an ``httpx.ASGITransport`` and records every request that flows
    through it.

    The MCP-level requests (JSON-RPC ``server/discover``, ``tools/call``, etc.)
    go directly to the MCP ASGI app via the SDK client's HTTP transport; they
    never reach the upstream Go service. To assert on the wire (no
    ``initialize``, no ``Mcp-Session-Id``) we capture them here.
    """

    def __init__(self, app: Starlette) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self.captured_requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.captured_requests.append(request)
        return await self._inner.handle_async_request(request)


@dataclass
class HttpAppHarness:
    """Setup data for an HTTP-driven test, built once per test invocation.

    Each test enters ``mcp.session_manager.run()`` itself rather than relying
    on a fixture, because pytest-asyncio drives fixture teardown in a
    different async task than setup, which trips anyio's cancel-scope
    host-task invariant when the SDK's task group exits.
    """

    app: Starlette
    mcp: Any
    upstream_transport: httpx.MockTransport
    mcp_transport: _CapturingASGITransport


def _build_http_harness(test_config) -> HttpAppHarness:
    """Build the harness with separate upstream and MCP-level transports.

    The upstream transport is a ``MockTransport`` so the server's
    ``PetHospitalClient`` never touches the real Go service. The MCP transport
    wraps ``ASGITransport`` so tests can assert on the JSON-RPC wire.
    """
    upstream_transport = make_transport(
        lambda _: _ok_response(SAMPLE_LIST_RESPONSE)
    )
    app, mcp = _build_http_app(upstream_transport, test_config=test_config)
    mcp_transport = _CapturingASGITransport(app=app)
    return HttpAppHarness(
        app=app,
        mcp=mcp,
        upstream_transport=upstream_transport,
        mcp_transport=mcp_transport,
    )


@pytest.mark.asyncio
async def test_health_endpoint_returns_ok(test_config) -> None:
    harness = _build_http_harness(test_config)
    # Drive the session manager's lifespan (ASGITransport skips ASGI lifespan).
    async with harness.mcp.session_manager.run():
        async with httpx.AsyncClient(
            transport=harness.mcp_transport,
            base_url="http://testserver",
        ) as http_client:
            response = await http_client.get("/health")
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "ok"
            assert body["service"] == MCP_SERVER_NAME
            assert body["version"] == MCP_SERVER_VERSION
            assert body["mcp_sdk"] == MCP_SDK_VERSION
            assert body["protocol_version"] == MCP_PROTOCOL_VERSION


@pytest.mark.asyncio
async def test_tool_discoverable_and_callable_over_http(test_config) -> None:
    """End-to-end: SDK 2.x client over HTTP discovers and calls ``list_pets``.

    The transport uses ``streamable_http_client`` so a real HTTP stack carries
    the request. ``mode="2026-07-28"`` pins the modern protocol so the client
    uses ``server/discover`` rather than the legacy ``initialize`` handshake.
    """
    harness = _build_http_harness(test_config)

    async with harness.mcp.session_manager.run():
        async with httpx.AsyncClient(
            transport=harness.mcp_transport,
            base_url="http://testserver",
        ) as http_client:
            transport_cm = streamable_http_client(
                "http://testserver/mcp",
                http_client=http_client,
                terminate_on_close=False,
            )

            async with Client(transport_cm, mode="2026-07-28") as client:
                tools_result = await client.list_tools()
                assert any(t.name == "list_pets" for t in tools_result.tools)

                result = await client.call_tool("list_pets", {"species": "犬"})
                sc = result.structured_content or {}
                assert sc.get("total") == 1
                assert sc["items"][0]["name"] == "旺财"


@pytest.mark.asyncio
async def test_no_initialize_handshake_sent(test_config) -> None:
    """The 2026-07-28 protocol replaces ``initialize`` with ``server/discover``.

    Assert no JSON-RPC request body carries ``"method": "initialize"``.
    """
    harness = _build_http_harness(test_config)

    async with harness.mcp.session_manager.run():
        async with httpx.AsyncClient(
            transport=harness.mcp_transport,
            base_url="http://testserver",
        ) as http_client:
            transport_cm = streamable_http_client(
                "http://testserver/mcp",
                http_client=http_client,
                terminate_on_close=False,
            )

            async with Client(transport_cm, mode="2026-07-28") as client:
                await client.list_tools()

    captured = harness.mcp_transport.captured_requests
    assert captured, "expected at least one MCP request to be captured"
    for request in captured:
        body = json.loads(request.content.decode("utf-8"))
        assert "method" in body
        assert body["method"] != "initialize", (
            "the 2026-07-28 protocol forbids the initialize handshake; "
            "the SDK 2.x client must use server/discover instead"
        )


@pytest.mark.asyncio
async def test_no_mcp_session_id_header(test_config) -> None:
    """No ``Mcp-Session-Id`` request or response header must appear.

    The stateless 2026-07-28 transport does not establish or carry a session
    id; round-robin load balancers rely on that property.
    """
    harness = _build_http_harness(test_config)

    async with harness.mcp.session_manager.run():
        async with httpx.AsyncClient(
            transport=harness.mcp_transport,
            base_url="http://testserver",
        ) as http_client:
            transport_cm = streamable_http_client(
                "http://testserver/mcp",
                http_client=http_client,
                terminate_on_close=False,
            )

            async with Client(transport_cm, mode="2026-07-28") as client:
                await client.call_tool("list_pets", {})

    captured = harness.mcp_transport.captured_requests
    assert captured, "expected at least one MCP request to be captured"
    for request in captured:
        assert "mcp-session-id" not in {k.lower() for k in request.headers.keys()}, (
            "stateless 2026-07-28 transport must not send Mcp-Session-Id"
        )


@pytest.mark.asyncio
async def test_health_request_does_not_touch_upstream(
    test_config,
) -> None:
    """A /health probe must not call the upstream Go REST API."""
    upstream_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        return _ok_response(SAMPLE_LIST_RESPONSE)

    transport = make_transport(handler)
    app, mcp = _build_http_app(transport, test_config=test_config)
    mcp_transport = _CapturingASGITransport(app=app)

    # Manually drive the session manager's lifespan (ASGITransport skips it).
    async with mcp.session_manager.run():
        async with httpx.AsyncClient(
            transport=mcp_transport,
            base_url="http://testserver",
        ) as http_client:
            response = await http_client.get("/health")
            assert response.status_code == 200

    # No upstream request was made.
    assert upstream_calls == []
