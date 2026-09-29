# Stage 2 upgrade prompt

This document is the prompt for the next stage of the Pet Hospital MCP. Stage 1
shipped a single tool (`list_pets`) over a stateless Streamable HTTP service
built on the official Python SDK `mcp==2.0.0` and the 2026-07-28 protocol.
Stage 2 widens the surface to write operations and richer medical-history
queries without breaking the stage 1 contract.

> You are an expert Model Context Protocol and Python developer. Continue
> the work on the `pet-hospital-mcp/` service shipped in stage 1. Add the
> tools below while preserving every stage 1 guarantee (stateless 2026-07-28
> Streamable HTTP, no `initialize` handshake, no `Mcp-Session-Id`, unified
> structured error payload, JSON logging with sensitive-field masking,
> Pydantic-validated inputs, flat tool signatures whose JSON Schema
> advertises enum values and numeric constraints).

## Constraints inherited from stage 1

- Do not modify the upstream Go Pet Hospital REST API.
- The MCP service is a standalone Python process that talks to the Go service
  only over HTTP.
- Reuse the existing `PetHospitalClient` (`rest_client.py`), `errors.py`
  payload shape, `logging_config.py` masking, and `tools/` package layout.
- New tools must be added as new modules under `src/pet_hospital_mcp/tools/`
  and registered through a single `register_all` call in
  `src/pet_hospital_mcp/tools/__init__.py`. Do not fork the registration path
  per tool.
- Pin `mcp==2.0.0`; do not introduce SDK 1.x or FastMCP.
- Do not change the stateless transport contract: no `initialize`, no
  `Mcp-Session-Id`, no session storage, no resume.
- Do not stage 3 features (resources, prompts, subscriptions, sampling,
  elicitation). They are out of scope here.

## New tools

Add the following tools. Each mirrors an existing Go REST API endpoint; read
the upstream `internal/api/api.go` to confirm the exact path, method, and
request/response shape before coding.

1. **`get_pet`** — `GET /api/v1/pets/{id}`.
   Input: `id: str` (required, non-empty). Output: the same `PetItem` shape
   `list_pets` returns, but as a single object (not a list). Errors:
   `VALIDATION_ERROR` (empty id), `BACKEND_TIMEOUT`, `BACKEND_UNAVAILABLE`,
   `BACKEND_API_ERROR` (4xx/5xx, including 404 -> a stable "not found"
   `message`), `BACKEND_INVALID_RESPONSE`, `INTERNAL_ERROR`.

2. **`create_pet`** — `POST /api/v1/pets`.
   Input: every field the Go endpoint accepts in the JSON body. Validate
   `species` and `status` against the same `Literal` enums used by
   `list_pets`. Output: the created `PetItem` (with server-assigned `id`,
   `createdAt`, `updatedAt`). Errors: union with the standard codes;
   `VALIDATION_ERROR` for missing required fields.

3. **`update_pet`** — `PUT /api/v1/pets/{id}`.
   Input: `id` plus the same field set as `create_pet`, all optional (a
   partial update). Output: the updated `PetItem`. Errors: as above, with
   `BACKEND_API_ERROR` for 404 mapped to a "not found" message.

4. **`delete_pet`** — `DELETE /api/v1/pets/{id}`.
   Input: `id`. Output: `{"deleted": true, "id": "..."}`. Errors: as above.

5. **`list_medical_records`** — `GET /api/v1/pets/{id}/records`.
   Input: `id` plus optional `page`, `pageSize` (reuse the same constraints
   as `list_pets`). Output: a paginated `items`/`total`/`page`/... envelope
   mirroring the upstream `Result`. Errors: as above.

6. **`add_medical_record`** — `POST /api/v1/pets/{id}/records`.
   Input: `id` plus the medical-record fields (visitDate, doctor,
   diagnosis, symptoms, treatment, prescription, weightKg, temperature,
   followUp, charge). Output: the created `MedicalRecord`. Errors: as
   above.

7. **`list_charges`** — `GET /api/v1/pets/{id}/charges`.
   Input: `id` plus optional `page`, `pageSize`. Output: a paginated
   envelope of `Treatment` items. Errors: as above.

## Tool-description contract

Every new tool's `description` field must state, in plain text:

- the upstream endpoint (method + path) the tool forwards to;
- the input parameters and any enum / numeric constraints;
- the success `structured_content` shape;
- the stable `code` values returned on failure.

## Tests

Reuse the test scaffolding from stage 1: `tests/conftest.py` fixtures
(`make_transport`, `SAMPLE_PET`, `SAMPLE_LIST_RESPONSE`, `envelope`) and
the in-memory `Client(server, mode="2026-07-28")` pattern. Add at least:

1. **Normal path** for each tool: request path, method, body and every
   field forwarded verbatim; success `structured_content` matches the
   upstream `data` block.
2. **Input validation** for each tool: required fields missing, wrong
   enum values, wrong types, NaN/Infinity where applicable.
3. **Upstream 4xx / 5xx** mapped to `BACKEND_API_ERROR`; 404 surfaced
   with a stable "not found" message.
4. **Timeout / connect exception** mapped to `BACKEND_TIMEOUT` /
   `BACKEND_UNAVAILABLE`.
5. **Non-JSON / schema mismatch** mapped to `BACKEND_INVALID_RESPONSE`.
6. **Tool registration**: every new tool appears in
   `(await client.list_tools()).tools` with the correct name and a flat
   input JSON Schema.
7. **Stateless protocol**: re-run the stage 1 assertions (no `initialize`,
   no `Mcp-Session-Id`, `/health` unchanged) with the wider tool set to
   prove the transport contract is preserved.

Run command remains:

```bash
cd pet-hospital-mcp
pytest -q
```

All tests must pass.

## Documentation

Update `README.md` to add a per-tool section with an example call for each
new tool, and update the "Module structure" listing. Do not remove the
stage 1 sections. Update this file's heading to read "Stage 2 complete;
stage 3 prompt pending" when the work is done.

## Out of scope for stage 2

- Resources, prompts, subscriptions, sampling, elicitation, roots.
- Authentication, authorization, TLS, CORS, Origin validation.
- Caching, rate limiting, request queuing.
- Migrating away from `MCPServer` to any other server abstraction.
