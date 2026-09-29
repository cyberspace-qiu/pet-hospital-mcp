# Pet Hospital MCP

A standalone Model Context Protocol (MCP) service that exposes the Go Pet
Hospital REST API to AI agents. Built on the official Python SDK 2.x with the
stateless Streamable HTTP transport from the 2026-07-28 protocol revision.

This is stage 1: a single `list_pets` tool that forwards every filter, sort,
and pagination parameter accepted by the upstream `GET /api/v1/pets`.

## Stage 1 scope

- **One tool**: `list_pets`.
- **Stage 2 tools (create/update/delete, medical records, etc.) are not
  implemented in this stage.** See `UPGRADE_PROMPT.md` for the stage 2 brief.

## Requirements

- Python 3.11+
- The Go Pet Hospital REST API running on `http://127.0.0.1:8080` (or any
  reachable base URL configured via `PET_HOSPITAL_BASE_URL`).

## Installation

```bash
cd pet-hospital-mcp
python -m pip install -e .
```

This installs the `pet_hospital_mcp` package and pins these dependencies:

- `mcp==2.0.0` — official MCP Python SDK
- `httpx` — HTTP client for the upstream Go REST API
- `pydantic` — input/output validation
- `structlog` — JSON logging with sensitive-field masking

## Starting the Go REST API first

The MCP service calls the Go Pet Hospital REST API; it must be running
before the MCP service starts.

```bash
# From the repository root (the Go project directory):
go run . serve --addr 127.0.0.1:8080 --seed
```

`--seed` populates the embedded database with sample data on first run. See
the upstream README for the full set of CLI flags.

## Configuration

All configuration is via environment variables. None are required; defaults
match a local dev setup.

| Variable                  | Default                  | Meaning                                              |
| ------------------------- | ------------------------ | ---------------------------------------------------- |
| `PET_HOSPITAL_BASE_URL`   | `http://127.0.0.1:8080`  | Upstream Go REST API base URL.                       |
| `MCP_HOST`                | `127.0.0.1`              | Bind address for the MCP HTTP service.               |
| `MCP_PORT`                | `8000`                   | Bind port for the MCP HTTP service.                  |
| `MCP_ENDPOINT`            | `/mcp`                   | Path the stateless Streamable HTTP endpoint serves. |
| `MCP_HEALTH_ENDPOINT`     | `/health`                | Path of the plain-JSON health probe.                |
| `BACKEND_TIMEOUT_SECONDS` | `5.0`                    | Per-request timeout for upstream calls.             |
| `BACKEND_RETRIES`         | `1`                      | Number of retries on upstream 5xx.                   |
| `BACKEND_BACKOFF_SECONDS`  | `0.5`                    | Backoff between retries.                            |

The MCP service listens on `127.0.0.1` only by default. Do not expose it on
a public interface without adding authentication, TLS, and Origin checks
(stage 2+ concern; stage 1 is for local / trusted-network use).

## Starting the MCP service

```bash
python -m pet_hospital_mcp
```

The CLI uses the configuration above; override any of them via the
environment. For example:

```bash
PET_HOSPITAL_BASE_URL=http://10.0.0.5:8080 \
MCP_HOST=0.0.0.0 \
MCP_PORT=9000 \
python -m pet_hospital_mcp
```

## Endpoint and protocol details

| Property              | Value                                           |
| --------------------- | ----------------------------------------------- |
| MCP endpoint          | `http://127.0.0.1:8000/mcp` (default host/port) |
| Health endpoint       | `http://127.0.0.1:8000/health`                  |
| MCP SDK               | `mcp==2.0.0`                                    |
| MCP protocol version  | `2026-07-28`                                    |
| Server class          | `mcp.server.MCPServer` (SDK 2.x)                |
| Transport             | Stateless Streamable HTTP                       |
| Session management    | **None.** No `initialize` handshake, no        |
|                       | `Mcp-Session-Id` header, no session storage,   |
|                       | no session expiry.                              |

The stateless 2026-07-28 transport means any process replica behind a
round-robin load balancer can answer any request. Clients use
`server/discover` to learn the server's capabilities; the legacy
`initialize` JSON-RPC method is not sent.

## Verifying with the SDK 2.x client

```python
import asyncio
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


async def main() -> None:
    # Pass the transport *context manager itself* (not the (read, write)
    # streams it yields) to Client; Client enters it during __aenter__.
    transport_cm = streamable_http_client("http://127.0.0.1:8000/mcp")
    async with Client(transport_cm, mode="2026-07-28") as client:
        tools = (await client.list_tools()).tools
        print("tools:", [t.name for t in tools])

        result = await client.call_tool("list_pets", {"species": "犬"})
        sc = result.structured_content or {}
        print("items:", len(sc.get("items", [])), "total:", sc.get("total"))


asyncio.run(main())
```

`mode="2026-07-28"` pins the modern protocol so the client uses
`server/discover` instead of `initialize`.

## Verifying with MCP Inspector

```bash
npx @modelcontextprotocol/inspector \
    --endpoint http://127.0.0.1:8000/mcp \
    --protocol 2026-07-28
```

In Inspector: connect, list tools, call `list_pets` with `{"species": "犬"}`,
and inspect the structured `items` / `total` / `totalCost` payload.

## `list_pets` call examples

A bare call (no filters, default page 1, default page size 20):

```json
{}
```

A filtered call (every supported parameter):

```json
{
  "q": "肠胃炎",
  "name": "旺财",
  "ownerName": "张三",
  "ownerPhone": "13800001111",
  "species": "犬",
  "doctor": "李医生",
  "disease": "肠胃炎",
  "status": "待就诊",
  "min": 100,
  "max": 1000,
  "sortBy": "totalCost",
  "order": "desc",
  "page": 2,
  "pageSize": 15
}
```

The accepted enum values mirror the upstream Go REST API:

- `species`: `犬`, `猫`, `兔`, `鸟`, `仓鼠`, `爬宠`, `其他`
- `status`: `待就诊`, `就诊中`, `住院中`, `已康复`, `慢性病随访`
- `sortBy`: `id`, `name`, `ownerName`, `species`, `doctor`, `disease`,
  `status`, `totalCost`, `visitCount`, `createdAt`, `updatedAt`
- `order`: `asc`, `desc`

Numeric constraints:

- `page >= 1`
- `1 <= pageSize <= 500`
- `min, max >= 0` and finite (no `NaN`, no `Infinity`)
- `min <= max`

Unknown fields, type-mismatched inputs, NaN/Infinity, and `min > max` are
rejected at the Pydantic layer before the upstream is touched. The tool
returns the unified `error` payload (see below) with code
`VALIDATION_ERROR`.

### Success payload

On success the tool returns a `CallToolResult` whose `structured_content`
mirrors the upstream `data` block:

```json
{
  "items": [
    {
      "id": "PET-000001",
      "name": "旺财",
      "species": "犬",
      "records": [{"diagnosis": "急性肠胃炎", "prescription": ["阿莫西林"]}],
      "charges": [{"item": "血常规检查", "amount": 180.0}],
      "totalCost": 560.0,
      "visitCount": 1
    }
  ],
  "total": 1,
  "page": 1,
  "pageSize": 20,
  "totalPages": 1,
  "totalCost": 560.0
}
```

`records` and `charges` may arrive as `null` from the Go service (Go's
`omitempty` on a nil slice); the adapter normalises both to an empty list
so callers never need to null-check.

### Error payload

On any failure (validation, timeout, upstream 4xx/5xx, malformed response,
unexpected internal error) the tool returns a `CallToolResult` with
`is_error: true` and a `structured_content` of the unified error shape:

```json
{
  "error": {
    "code": "BACKEND_API_ERROR",
    "message": "Pet Hospital API returned HTTP 503",
    "status": 503,
    "details": {"endpoint": "/api/v1/pets", "attempt": 2}
  }
}
```

The stable `code` values:

- `VALIDATION_ERROR` — input rejected by the Pydantic layer.
- `BACKEND_TIMEOUT` — upstream call timed out.
- `BACKEND_UNAVAILABLE` — could not connect to the upstream.
- `BACKEND_API_ERROR` — upstream returned 4xx or 5xx after retries.
- `BACKEND_INVALID_RESPONSE` — upstream returned non-JSON, missing
  `data`, or a `data` block that fails schema validation.
- `INTERNAL_ERROR` — unexpected exception inside the tool.

The Python stack (httpx, pydantic, the MCP SDK) is never surfaced to the
client; every upstream or internal failure is mapped to one of the codes
above.

## `/health` example

```bash
curl http://127.0.0.1:8000/health
```

```json
{
  "status": "ok",
  "service": "pet-hospital-mcp",
  "version": "0.1.0",
  "mcp_sdk": "2.0.0",
  "protocol_version": "2026-07-28",
  "upstream": "http://127.0.0.1:8080"
}
```

A `/health` probe does not call the upstream Go service.

## Logging

Logs are JSON-formatted (via `structlog`) and include at least:

- `timestamp` (ISO 8601, UTC)
- `level`
- `event` (e.g. `tool_call`, `tool_success`, `tool_failed`)
- `tool_name`
- `params` (sensitive fields masked)
- `status` (the error code on failure, `"ok"` on success)
- `duration_ms`

Sensitive fields (`ownerPhone`, `ownerAddr`, `chipNo` and their snake_case
variants) are recursively replaced with `***REDACTED***` before logging.

## Unit tests

```bash
cd pet-hospital-mcp
pytest -q
```

Expected result: **43 passed**. Tests never touch the real Go service;
they use `httpx.MockTransport` for the upstream REST calls and an in-process
ASGI transport for the MCP HTTP wire.

Coverage:

1. Normal call — request path and every filter/sort/pagination parameter
   forwarded verbatim; `None` parameters omitted; `records`/`charges` as
   `null` normalised to `[]`.
2. Input validation — unknown fields, invalid enums, page boundaries,
   pageSize boundaries, negative `min`/`max`, `min > max`, NaN/Infinity,
   wrong types.
3. Upstream 4xx / 5xx — mapped to `BACKEND_API_ERROR`, with HTTP status
   preserved; 5xx retried then succeeds; persistent 5xx surfaces the error.
4. Timeout / connect exception — mapped to `BACKEND_TIMEOUT` /
   `BACKEND_UNAVAILABLE`.
5. Non-JSON / missing `data` / wrong `data` shape — mapped to
   `BACKEND_INVALID_RESPONSE`.
6. Tool registration, tool name, and JSON Schema flatness (each input field
   appears at the top level, enum values advertised, `page >= 1` and
   `pageSize <= 500` constraints preserved).
7. SDK 2.x stateless flow — no `initialize` JSON-RPC method sent, no
   `Mcp-Session-Id` request header, `/health` returns 200, tool discoverable
   and callable over HTTP and in-memory.

## Module structure

```text
pet-hospital-mcp/
├── pyproject.toml
├── README.md
├── UPGRADE_PROMPT.md
├── src/
│   └── pet_hospital_mcp/
│       ├── __init__.py
│       ├── __main__.py
│       ├── config.py
│       ├── server.py
│       ├── rest_client.py
│       ├── errors.py
│       ├── logging_config.py
│       └── tools/
│           ├── __init__.py
│           └── list_pets.py
└── tests/
    ├── conftest.py
    ├── test_input_validation.py
    ├── test_list_pets_tool.py
    └── test_server_and_protocol.py
```

To add a stage 2 tool, drop a new module in `tools/` and add a single
`register_all` line; the REST client, logging, and error conventions are
shared.
