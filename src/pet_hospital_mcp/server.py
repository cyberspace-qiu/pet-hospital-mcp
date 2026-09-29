"""MCP server wiring.

Builds an ``MCPServer`` from the official SDK 2.x, registers every tool in
:mod:`pet_hospital_mcp.tools`, and exposes the process-wide REST client via
the SDK's **lifespan state** so tools can fetch it from their injected
``Context`` without knowing about the server's lifespan.

Lifespan state (and not ``contextvars.ContextVar``) is used deliberately:
under stateless Streamable HTTP each request is handled in a fresh task
spawned inside the request's uvicorn task, which copies the *request*
context — a ``ContextVar`` set in the lifespan task is not visible there.
The SDK carries ``lifespan_state`` to every request explicitly (including
the 2026-07-28 stateless path), so ``ctx.request_context.lifespan_context``
works in production, in-process tests, and HTTP tests alike.

The transport is **Streamable HTTP** in **stateless mode**: every request
carries its own protocol version and capabilities, there is no ``initialize``
handshake, no ``Mcp-Session-Id``, no session storage, no expiry. Any process
replica behind a round-robin load balancer can answer any request.

A ``/health`` endpoint is mounted alongside ``/mcp`` so a load balancer or
container orchestrator can probe the service without speaking MCP.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp.server import MCPServer

from pet_hospital_mcp.config import (
    MCP_PROTOCOL_VERSION,
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
    MCP_SDK_VERSION,
    Config,
)
from pet_hospital_mcp.logging_config import configure_logging, get_logger
from pet_hospital_mcp.rest_client import PetHospitalClient
from pet_hospital_mcp.tools import register_all


# Key under which the REST client lives in the lifespan state dict.
CLIENT_STATE_KEY = "client"


def client_from_context(ctx: Any) -> PetHospitalClient:
    """Return the process-wide ``PetHospitalClient`` from a tool's ``Context``.

    The SDK injects the lifespan state (the dict our lifespan yields) into
    each request as ``ctx.request_context.lifespan_context``. Raises a
    clear error if the lifespan never ran, which means the server was
    started incorrectly (e.g. a custom ASGI mount that skipped the
    lifespan / ``session_manager.run()``).
    """
    lifespan_state = getattr(getattr(ctx, "request_context", None), "lifespan_context", None)
    client = lifespan_state.get(CLIENT_STATE_KEY) if isinstance(lifespan_state, dict) else None
    if not isinstance(client, PetHospitalClient):
        raise RuntimeError(
            "PetHospitalClient not found in lifespan state; the server "
            "lifespan did not run. If you mounted the MCP app, enter "
            "mcp.session_manager.run() (which runs the server lifespan) in "
            "the host app."
        )
    return client


# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------


def build_server(
    config: Config | None = None,
    *,
    transport: Any | None = None,
) -> tuple[MCPServer, Config]:
    """Construct the ``MCPServer`` and its config.

    Args:
        config: Resolved configuration; loaded from env if omitted.
        transport: Optional ``httpx.AsyncBaseTransport`` for tests. Production
            uses the default httpx transport.

    Returns:
        ``(server, config)``. The config is returned so callers (CLI, tests)
        can read the resolved bind address without re-parsing env vars.
    """
    if config is None:
        config = Config.from_env()
    configure_logging()
    log = get_logger("pet_hospital_mcp.server")
    log.info(
        "server_build",
        server_name=MCP_SERVER_NAME,
        server_version=MCP_SERVER_VERSION,
        sdk_version=MCP_SDK_VERSION,
        protocol_version=MCP_PROTOCOL_VERSION,
        upstream=config.pet_hospital_base_url,
        mcp_host=config.mcp_host,
        mcp_port=config.mcp_port,
        mcp_endpoint=config.mcp_endpoint,
        health_endpoint=config.health_endpoint,
    )

    @asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[dict[str, Any]]:
        # The REST client lifetime matches the server lifetime: one httpx
        # connection pool for the whole process, reused across requests. The
        # yielded dict is the SDK lifespan state; tools read the client from
        # `ctx.request_context.lifespan_context["client"]`.
        async with PetHospitalClient(config, transport=transport) as client:
            log.info("lifespan_started", upstream=config.pet_hospital_base_url)
            yield {CLIENT_STATE_KEY: client}
            log.info("lifespan_stopped")

    mcp = MCPServer(
        name=MCP_SERVER_NAME,
        version=MCP_SERVER_VERSION,
        instructions=(
            "Pet Hospital MCP: a stateless Streamable HTTP service exposing "
            "the Go Pet Hospital REST API. Tools return structured JSON; "
            "failures carry an `error` object with a stable `code`."
        ),
        log_level="INFO",
        lifespan=lifespan,
    )

    register_all(mcp)

    # Custom (non-MCP) HTTP routes: a plain health check.
    @mcp.custom_route(config.health_endpoint, methods=["GET"])
    async def health(request: Request) -> Response:
        # Health is intentionally simple: 200 if the process is alive and the
        # lifespan ran. A real deployment would also probe the upstream API.
        return JSONResponse(
            {
                "status": "ok",
                "service": MCP_SERVER_NAME,
                "version": MCP_SERVER_VERSION,
                "mcp_sdk": MCP_SDK_VERSION,
                "protocol_version": MCP_PROTOCOL_VERSION,
                "upstream": config.pet_hospital_base_url,
            }
        )

    return mcp, config


def build_app(config: Config | None = None, *, transport: Any | None = None):
    """Return the ASGI app, ready for any ASGI server (uvicorn, hypercorn).

    Used by the CLI entry point and by tests.
    """
    mcp, _ = build_server(config, transport=transport)
    return mcp.streamable_http_app(
        streamable_http_path=(
            config.mcp_endpoint if config else Config.from_env().mcp_endpoint
        ),
        # Stateless: no Mcp-Session-Id, no session store, no resume.
        # Every request is self-contained (2026-07-28 protocol semantics).
        stateless_http=True,
        # JSON responses (not SSE streams) for simpler client interop.
        json_response=True,
        host=(config.mcp_host if config else Config.from_env().mcp_host),
    )


__all__ = ["build_server", "build_app", "client_from_context", "CLIENT_STATE_KEY"]
