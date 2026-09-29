"""CLI entry point.

Run with ``python -m pet_hospital_mcp`` or, after install, with the
``pet-hospital-mcp`` console script. Delegates to :func:`mcp.run` after
constructing the server and config from environment variables.
"""

from __future__ import annotations

import sys

from pet_hospital_mcp.config import Config
from pet_hospital_mcp.logging_config import configure_logging, get_logger
from pet_hospital_mcp.server import build_server


def main() -> None:
    """Start the MCP Streamable HTTP service."""
    config = Config.from_env()
    configure_logging()
    log = get_logger("pet_hospital_mcp.__main__")

    mcp, config = build_server(config)
    log.info(
        "startup",
        host=config.mcp_host,
        port=config.mcp_port,
        mcp_endpoint=config.mcp_endpoint,
        health_endpoint=config.health_endpoint,
        upstream=config.pet_hospital_base_url,
    )
    sys.stdout.write(
        f"Pet Hospital MCP listening on http://{config.mcp_host}:{config.mcp_port}"
        f"{config.mcp_endpoint} (health: {config.health_endpoint})\n"
        f"Upstream Go REST API: {config.pet_hospital_base_url}\n"
    )
    sys.stdout.flush()

    # mcp.run() builds and serves the Starlette app via uvicorn.
    # stateless_http / json_response match the test app in build_app.
    mcp.run(
        transport="streamable-http",
        host=config.mcp_host,
        port=config.mcp_port,
        streamable_http_path=config.mcp_endpoint,
        stateless_http=True,
        json_response=True,
    )


if __name__ == "__main__":
    main()
