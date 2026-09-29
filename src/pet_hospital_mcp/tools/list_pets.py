"""``list_pets`` tool.

The only tool in stage 1. It is a thin, strict adapter to the Go REST API's
``GET /api/v1/pets``: it forwards exactly the parameters the upstream service
accepts, validates them with Pydantic, and returns the upstream ``data`` block
as a typed structured output.

Design notes
------------

* Input model uses ``Literal`` for the four enum-like parameters so the JSON
  Schema advertises the allowed values to the model. Unknown values are
  rejected at the Pydantic layer before the upstream is touched.
* ``records`` and ``charges`` may legitimately arrive as ``null`` (Go's
  ``omitempty`` on a nil slice). The output model accepts both ``None`` and
  a list and normalises to an empty list so callers never need to defensively
  null-check.
* The tool function is annotated to return :class:`CallToolResult` directly.
  The SDK's auto-derived ``output_schema`` cannot represent a tagged union of
  ``ListPetsSuccessOutput | ListPetsErrorOutput`` cleanly, so we construct
  the ``structured_content`` ourselves: success carries the upstream ``data``
  block, failure carries the unified ``error`` payload, and the LLM-visible
  ``content`` carries a JSON rendering of the same. ``is_error=True`` flags
  the failure branch so SDK 2.x clients can route without parsing JSON.
* On any failure (validation, timeout, upstream 5xx, malformed JSON, etc.)
  the tool returns a :class:`ListPetsErrorOutput` payload, never raises to
  the MCP dispatcher: structured errors are LLM-visible, while raised
  exceptions become JSON-RPC errors that clients (including the SDK's own
  ``Client``) raise on, hiding the failure text from the model.
"""

from __future__ import annotations

import math
import time
from typing import Any, Literal, Union

from mcp.server.mcpserver.context import Context
from mcp_types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pet_hospital_mcp.errors import (
    ErrorCode,
    ErrorDetail,
    ToolErrorPayload,
    build_error,
)
from pet_hospital_mcp.logging_config import get_logger, mask_sensitive
from pet_hospital_mcp.rest_client import PetHospitalClient, RestError


# ---------------------------------------------------------------------------
# Enums (mirror the Go REST API's `ValidSpecies`/`ValidStatus`/`sortBy`/`order`)
# ---------------------------------------------------------------------------

Species = Literal["犬", "猫", "兔", "鸟", "仓鼠", "爬宠", "其他"]
Status = Literal["待就诊", "就诊中", "住院中", "已康复", "慢性病随访"]
SortBy = Literal[
    "id",
    "name",
    "ownerName",
    "species",
    "doctor",
    "disease",
    "status",
    "totalCost",
    "visitCount",
    "createdAt",
    "updatedAt",
]
Order = Literal["asc", "desc"]


# ---------------------------------------------------------------------------
# Input model
# ---------------------------------------------------------------------------


class ListPetsInput(BaseModel):
    """Input schema for the ``list_pets`` tool.

    Exactly mirrors the query parameters accepted by the Go REST API's
    ``GET /api/v1/pets`` endpoint. No adapter-private fields are added.
    """

    # strict=True forbids implicit coercion (e.g. "1" -> 1) so a string
    # for `page` is a real validation error rather than a silent accept.
    model_config = ConfigDict(extra="forbid", strict=True)

    q: str | None = Field(
        default=None,
        description=(
            "Optional full-text search across pet, owner, and medical-history "
            "fields. Multiple terms are AND-ed."
        ),
    )
    name: str | None = Field(
        default=None, description="Filter by pet name (fuzzy match)."
    )
    ownerName: str | None = Field(
        default=None, description="Filter by owner name (fuzzy match)."
    )
    ownerPhone: str | None = Field(
        default=None, description="Filter by owner phone (fuzzy match)."
    )
    species: Species | None = Field(
        default=None,
        description=(
            "Filter by species. Must be one of: 犬, 猫, 兔, 鸟, 仓鼠, 爬宠, 其他."
        ),
    )
    doctor: str | None = Field(
        default=None, description="Filter by doctor name (fuzzy match)."
    )
    disease: str | None = Field(
        default=None, description="Filter by disease (fuzzy match)."
    )
    status: Status | None = Field(
        default=None,
        description=(
            "Filter by visit status. Must be one of: 待就诊, 就诊中, 住院中, "
            "已康复, 慢性病随访."
        ),
    )
    min: float | None = Field(
        default=None,
        ge=0,
        description="Lower bound on total cost (inclusive). Must be >= 0.",
    )
    max: float | None = Field(
        default=None,
        ge=0,
        description="Upper bound on total cost (inclusive). Must be >= 0.",
    )
    sortBy: SortBy | None = Field(
        default=None,
        description=(
            "Sort field. Must be one of: id, name, ownerName, species, doctor, "
            "disease, status, totalCost, visitCount, createdAt, updatedAt."
        ),
    )
    order: Order | None = Field(
        default=None, description="Sort direction: asc or desc."
    )
    page: int | None = Field(
        default=None,
        ge=1,
        description="Page number, 1-indexed. Must be >= 1.",
    )
    pageSize: int | None = Field(
        default=None,
        ge=1,
        le=500,
        description="Page size. Must be between 1 and 500 inclusive.",
    )

    @field_validator("min", "max")
    @classmethod
    def _reject_nan_inf(cls, v: float | None) -> float | None:
        if v is None:
            return None
        if math.isnan(v) or math.isinf(v):
            raise ValueError("must be a finite number, NaN/Infinity are not allowed")
        return v

    @model_validator(mode="after")
    def _check_min_max(self) -> "ListPetsInput":
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min must be <= max")
        return self


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class MedicalRecord(BaseModel):
    """A single historical visit. Mirrors the Go `MedicalRecord` struct."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    visitDate: str | None = None
    doctor: str | None = None
    diagnosis: str | None = None
    symptoms: str | None = None
    treatment: str | None = None
    prescription: list[str] | None = None
    weightKg: float | None = None
    temperature: float | None = None
    followUp: str | None = None
    charge: float | None = None
    createdAt: str | None = None


class Treatment(BaseModel):
    """A single charge line item. Mirrors the Go `Treatment` struct."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    item: str | None = None
    category: str | None = None
    amount: float | None = None
    doctor: str | None = None
    date: str | None = None
    note: str | None = None


class PetItem(BaseModel):
    """A single pet in the items list. Mirrors the Go `Pet` struct.

    `records` and `charges` may arrive as ``null`` (Go's `omitempty` on a
    nil slice); both are normalised to empty lists.
    """

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    name: str | None = None
    species: str | None = None
    breed: str | None = None
    gender: str | None = None
    ageMonths: int | None = None
    color: str | None = None
    chipNo: str | None = None
    ownerName: str | None = None
    ownerPhone: str | None = None
    ownerAddr: str | None = None
    doctor: str | None = None
    disease: str | None = None
    status: str | None = None
    allergy: str | None = None
    note: str | None = None
    records: list[MedicalRecord] = Field(default_factory=list)
    charges: list[Treatment] = Field(default_factory=list)
    totalCost: float | None = None
    visitCount: int | None = None
    createdAt: str | None = None
    updatedAt: str | None = None

    @field_validator("records", "charges", mode="before")
    @classmethod
    def _null_to_empty(cls, v: Any) -> Any:
        return v if v is not None else []


class ListPetsSuccessOutput(BaseModel):
    """Structured success payload mirroring the Go `Result` envelope."""

    model_config = ConfigDict(extra="ignore")

    items: list[PetItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    pageSize: int = 20
    totalPages: int = 0
    totalCost: float = 0.0

    @field_validator("items", mode="before")
    @classmethod
    def _items_null_to_empty(cls, v: Any) -> Any:
        return v if v is not None else []


class ListPetsErrorOutput(ToolErrorPayload):
    """Structured failure payload.

    Inherits the ``error`` shape from :class:`ToolErrorPayload` so every tool
    in this package returns the same failure shape, regardless of stage.
    """


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


ListPetsResult = Union[ListPetsSuccessOutput, ListPetsErrorOutput]


TOOL_NAME = "list_pets"
TOOL_DESCRIPTION = """List pet records from the Pet Hospital REST API.

Forwards filter, sort, and pagination parameters to the upstream Go REST API's
`GET /api/v1/pets` endpoint. Use this tool when an agent needs to browse pet
records, find specific pets by name, owner, species, doctor, disease, status,
or cost range, or paginate through the full archive.

Returns a structured payload with the page's `items` (each with `records` and
`charges` history), `total`, `page`, `pageSize`, `totalPages`, and `totalCost`
of the matched set. On any failure (validation, timeout, upstream error,
malformed response) returns an `error` object with a stable `code`:
`VALIDATION_ERROR`, `BACKEND_TIMEOUT`, `BACKEND_UNAVAILABLE`,
`BACKEND_API_ERROR`, `BACKEND_INVALID_RESPONSE`, or `INTERNAL_ERROR`.
"""


async def list_pets(
    client: PetHospitalClient, params: ListPetsInput
) -> ListPetsResult:
    """Call the Go REST API with the validated parameters.

    Pure data-in / model-out function. Returns either the validated success
    payload or a structured error payload; never raises (except for truly
    unexpected internal failures, which are converted to INTERNAL_ERROR).
    Wiring onto an ``MCPServer`` is done in :mod:`pet_hospital_mcp.server`
    via :func:`register_all`, which wraps this function's output into a
    :class:`CallToolResult`.
    """
    log = get_logger("pet_hospital_mcp.tools.list_pets")
    masked_params = mask_sensitive(params.model_dump(exclude_none=True))
    log.info("tool_call", tool_name=TOOL_NAME, params=masked_params)
    started = time.perf_counter()

    query: dict[str, Any] = {}
    for key, value in params.model_dump(exclude_none=True).items():
        query[key] = value

    try:
        data = await client.list_pets(query)
    except RestError as exc:
        duration_ms = (time.perf_counter() - started) * 1000
        log.warning(
            "tool_failed",
            tool_name=TOOL_NAME,
            params=masked_params,
            status=exc.code.value,
            duration_ms=round(duration_ms, 2),
            error=str(exc.message),
        )
        return ListPetsErrorOutput(
            error=ErrorDetail(
                code=exc.code,
                message=exc.message,
                status=exc.status,
                details=exc.details or {},
            )
        )
    except Exception as exc:  # pragma: no cover - defensive
        duration_ms = (time.perf_counter() - started) * 1000
        log.exception(
            "tool_internal_error",
            tool_name=TOOL_NAME,
            params=masked_params,
            duration_ms=round(duration_ms, 2),
        )
        err = build_error(
            ErrorCode.INTERNAL_ERROR,
            f"Unexpected error: {exc}",
            details={"error_type": type(exc).__name__},
        )
        return ListPetsErrorOutput(error=err.error)

    try:
        validated = ListPetsSuccessOutput.model_validate(data)
    except Exception as exc:
        duration_ms = (time.perf_counter() - started) * 1000
        log.warning(
            "tool_invalid_response",
            tool_name=TOOL_NAME,
            params=masked_params,
            duration_ms=round(duration_ms, 2),
            error=str(exc),
        )
        err = build_error(
            ErrorCode.BACKEND_INVALID_RESPONSE,
            "Pet Hospital API response did not match the expected schema",
            details={"error_type": type(exc).__name__, "error": str(exc)},
        )
        return ListPetsErrorOutput(error=err.error)

    duration_ms = (time.perf_counter() - started) * 1000
    log.info(
        "tool_success",
        tool_name=TOOL_NAME,
        params=masked_params,
        status="ok",
        duration_ms=round(duration_ms, 2),
        returned_items=len(validated.items),
        total=validated.total,
    )
    return validated


def _to_call_result(result: ListPetsResult) -> CallToolResult:
    """Wrap a :class:`ListPetsResult` into a transport-ready ``CallToolResult``.

    The SDK's auto-derived ``output_schema`` cannot represent a tagged union of
    ``ListPetsSuccessOutput | ListPetsErrorOutput`` cleanly, so the tool
    function registered with the server returns :class:`CallToolResult`
    directly and builds ``structured_content`` itself. Success carries the
    upstream ``data`` block; failure carries the unified ``error`` payload;
    the LLM-visible ``content`` is a JSON rendering of the same; ``is_error``
    flags the failure branch so SDK 2.x clients can route without parsing.
    """
    if isinstance(result, ListPetsErrorOutput):
        return CallToolResult(
            content=[
                TextContent(type="text", text=result.model_dump_json(indent=2))
            ],
            structured_content=result.model_dump(mode="json"),
            is_error=True,
        )
    return CallToolResult(
        content=[TextContent(type="text", text=result.model_dump_json(indent=2))],
        structured_content=result.model_dump(mode="json"),
        is_error=False,
    )


def register_all(mcp_server) -> None:
    """Wire every tool in this package onto an ``MCPServer`` instance.

    The server module calls this once during construction. New tools are added
    by creating a sibling module and appending a registration call here.

    The ``_list_pets`` wrapper flattens the :class:`ListPetsInput` schema to
    top-level parameters so the JSON Schema the MCP SDK derives from the
    function signature has ``species``, ``page``, etc. as direct fields
    rather than nested under ``params``. The wrapper reassembles them into a
    :class:`ListPetsInput` for the pure-data implementation in
    :func:`list_pets`.

    A ``ctx: Context`` parameter is included so the SDK injects the request
    :class:`Context` (it is detected by annotation and excluded from the
    input JSON Schema). The REST client is read from the server lifespan
    state — ``ctx.request_context.lifespan_context["client"]`` — which is the
    only mechanism that reaches stateless request tasks under uvicorn.

    The default of each input ``inspect.Parameter`` is set to the
    corresponding :class:`pydantic.fields.FieldInfo` from
    :class:`ListPetsInput` so the SDK's auto-derived JSON Schema preserves
    every constraint (``ge``, ``le``, ``description``) declared on the
    canonical model. A bare ``None`` default would lose those constraints
    because the SDK rebuilds a Pydantic model from the function signature.
    """
    import inspect

    # Delayed import to avoid the server <-> tools package import cycle
    # (server imports register_all at module load, before client_from_context
    # is defined).
    from pet_hospital_mcp.server import client_from_context

    async def _list_pets(ctx: Context, **kwargs: Any) -> CallToolResult:
        """Forward the flattened arguments to :func:`list_pets` after
        validating them against :class:`ListPetsInput`, then wrap the
        :class:`ListPetsResult` into a :class:`CallToolResult` so the
        SDK sends a single, non-wrapped ``structured_content`` object.

        ``ctx`` is injected by the SDK (not part of the advertised JSON
        Schema) and used to reach the lifespan-scoped REST client.
        """
        params = ListPetsInput.model_validate(kwargs)
        client = client_from_context(ctx)
        result = await list_pets(client, params)
        return _to_call_result(result)

    # Build the flattened signature: every input field becomes a top-level
    # keyword argument whose default is the FieldInfo from ListPetsInput
    # (preserves ge/le/description), followed by the SDK-injected Context.
    input_params = [
        inspect.Parameter(
            name=field_name,
            kind=inspect.Parameter.KEYWORD_ONLY,
            default=field_info,
            annotation=field_info.annotation,
        )
        for field_name, field_info in ListPetsInput.model_fields.items()
    ]
    ctx_param = inspect.Parameter(
        name="ctx",
        kind=inspect.Parameter.KEYWORD_ONLY,
        annotation=Context,
    )
    _list_pets.__signature__ = inspect.Signature(
        parameters=input_params + [ctx_param]
    )
    # `get_type_hints` reads __annotations__ to discover the Context
    # parameter; the real class object (not a string) keeps resolution
    # trivial for this nested closure.
    _list_pets.__annotations__ = {
        **{
            name: field.annotation
            for name, field in ListPetsInput.model_fields.items()
        },
        "ctx": Context,
        "return": CallToolResult,
    }

    mcp_server.add_tool(
        _list_pets,
        name=TOOL_NAME,
        description=TOOL_DESCRIPTION,
    )


__all__ = [
    "ListPetsInput",
    "ListPetsSuccessOutput",
    "ListPetsErrorOutput",
    "ListPetsResult",
    "MedicalRecord",
    "Treatment",
    "PetItem",
    "Species",
    "Status",
    "SortBy",
    "Order",
    "TOOL_NAME",
    "TOOL_DESCRIPTION",
    "list_pets",
    "register_all",
]
