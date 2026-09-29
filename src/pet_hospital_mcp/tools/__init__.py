"""Tool package.

Each tool lives in its own module to keep the per-tool surface tiny and make
future tools (stage 2) a one-file addition. ``register_all`` is the single
hook the server uses to wire every tool onto an ``MCPServer`` instance.
"""

from pet_hospital_mcp.tools.list_pets import ListPetsErrorOutput, ListPetsInput, ListPetsSuccessOutput, register_all

__all__ = [
    "ListPetsInput",
    "ListPetsSuccessOutput",
    "ListPetsErrorOutput",
    "register_all",
]
