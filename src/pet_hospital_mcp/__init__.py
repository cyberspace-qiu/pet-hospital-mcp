"""Pet Hospital MCP.

A stateless Streamable HTTP MCP service that exposes the Go Pet Hospital REST API
to AI agents. Built on the official MCP Python SDK 2.x (`MCPServer`) and the
2026-07-28 protocol revision.
"""

from pet_hospital_mcp.config import Config

__all__ = ["Config", "__version__"]

__version__ = "0.1.0"
