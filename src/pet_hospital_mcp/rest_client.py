"""HTTP client for the upstream Go Pet Hospital REST API.

The client is the *only* code in this package that talks to the Go service.
Every tool reuses it; nothing else should import ``httpx`` directly.

Design goals:

* **Stateless and safe**: one ``AsyncClient`` is shared via an ``async`` context
  manager. No global state.
* **Bounded latency**: every call has a hard timeout; the caller decides
  whether a timeout is fatal.
* **Bounded retry**: retries are limited and only applied to transient errors
  (connect errors, read timeouts, 5xx). 4xx responses are never retried — they
  carry the server's verdict on the request itself.
* **No stack leaks**: the client returns ``RestError`` with one of the
  ``ErrorCode`` values. The caller never sees ``httpx.*`` or ``pydantic.*``
  exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import asyncio

import httpx

from pet_hospital_mcp.config import Config
from pet_hospital_mcp.errors import ErrorCode
from pet_hospital_mcp.logging_config import get_logger


@dataclass(frozen=True)
class RestError(Exception):
    """Normalised error raised by the REST client.

    ``code`` is one of :class:`ErrorCode` so callers can route it directly
    into the MCP structured error payload without translating.
    """

    code: ErrorCode
    message: str
    status: int | None = None
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        # Keep the exception text useful in tracebacks.
        super().__init__(self.message)


class PetHospitalClient:
    """Thin async wrapper around the Go Pet Hospital REST API.

    Lifetime: instantiate inside an ``async with`` block so the underlying
    ``httpx.AsyncClient`` is closed cleanly. Reuse one client across calls;
    do not create a new client per request.
    """

    def __init__(
        self,
        config: Config,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._log = get_logger("pet_hospital_mcp.rest_client")

    async def __aenter__(self) -> "PetHospitalClient":
        self._client = httpx.AsyncClient(
            base_url=self._config.pet_hospital_base_url,
            transport=self._transport,
            timeout=httpx.Timeout(self._config.backend_timeout_seconds),
            follow_redirects=False,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def base_url(self) -> str:
        return self._config.pet_hospital_base_url

    async def list_pets(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Call ``GET /api/v1/pets`` and return the parsed ``data`` payload.

        Raises :class:`RestError` on any failure (timeout, network, non-2xx,
        invalid JSON, schema mismatch). Callers must catch it and forward
        ``code`` / ``message`` / ``details`` to the MCP client.
        """
        if self._client is None:
            raise RestError(
                code=ErrorCode.INTERNAL_ERROR,
                message="PetHospitalClient used outside of 'async with' block",
            )

        # Drop None values; httpx would otherwise ship "None" strings.
        clean_params: dict[str, Any] = {
            key: value for key, value in params.items() if value is not None
        }
        endpoint = "/api/v1/pets"
        url = f"{self._config.pet_hospital_base_url}{endpoint}"

        attempts = self._config.backend_retries + 1
        last_error: RestError | None = None
        for attempt in range(1, attempts + 1):
            try:
                self._log.debug(
                    "http_call",
                    endpoint=endpoint,
                    params=clean_params,
                    attempt=attempt,
                )
                # httpx.AsyncClient already has a connect+read+write timeout
                # configured via httpx.Timeout at construction time. We don't
                # add a second wall here; tests rely on httpx raising
                # ReadTimeout/ConnectTimeout, which we map below.
                response = await self._client.get(endpoint, params=clean_params)
                self._log.debug(
                    "http_response",
                    endpoint=endpoint,
                    status=response.status_code,
                    bytes=len(response.content),
                )
            except httpx.TimeoutException as exc:
                last_error = RestError(
                    code=ErrorCode.BACKEND_TIMEOUT,
                    message=(
                        f"Pet Hospital API timed out after "
                        f"{self._config.backend_timeout_seconds:.1f}s"
                    ),
                    details={
                        "endpoint": endpoint,
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                    },
                )
                self._log.warning("http_timeout", endpoint=endpoint, attempt=attempt)
                await self._maybe_sleep(attempt)
                continue
            except httpx.ConnectError as exc:
                last_error = RestError(
                    code=ErrorCode.BACKEND_UNAVAILABLE,
                    message=(
                        f"Pet Hospital API unreachable at "
                        f"{self._config.pet_hospital_base_url}"
                    ),
                    details={
                        "endpoint": endpoint,
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                    },
                )
                self._log.warning("http_unavailable", endpoint=endpoint, attempt=attempt)
                await self._maybe_sleep(attempt)
                continue
            except httpx.HTTPError as exc:
                last_error = RestError(
                    code=ErrorCode.BACKEND_UNAVAILABLE,
                    message=f"Pet Hospital API transport error: {exc}",
                    details={
                        "endpoint": endpoint,
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                    },
                )
                self._log.warning("http_transport_error", endpoint=endpoint, attempt=attempt)
                await self._maybe_sleep(attempt)
                continue

            if 500 <= response.status_code < 600 and attempt < attempts:
                last_error = RestError(
                    code=ErrorCode.BACKEND_API_ERROR,
                    message=(
                        f"Pet Hospital API returned HTTP {response.status_code}"
                    ),
                    status=response.status_code,
                    details={"endpoint": endpoint, "attempt": attempt},
                )
                self._log.warning(
                    "http_5xx_retry",
                    endpoint=endpoint,
                    status=response.status_code,
                    attempt=attempt,
                )
                await self._maybe_sleep(attempt)
                continue

            return self._parse_response(response, endpoint)

        assert last_error is not None
        raise last_error

    async def _maybe_sleep(self, attempt: int) -> None:
        if attempt >= self._config.backend_retries + 1:
            return
        await asyncio.sleep(self._config.backend_backoff_seconds * attempt)

    def _parse_response(
        self, response: httpx.Response, endpoint: str
    ) -> dict[str, Any]:
        # Non-2xx is fatal; no retry past this point.
        if response.status_code >= 400:
            body_preview = response.text[:200] if response.text else ""
            raise RestError(
                code=ErrorCode.BACKEND_API_ERROR,
                message=f"Pet Hospital API returned HTTP {response.status_code}",
                status=response.status_code,
                details={
                    "endpoint": endpoint,
                    "body_preview": body_preview,
                },
            )

        try:
            payload = response.json()
        except Exception as exc:
            raise RestError(
                code=ErrorCode.BACKEND_INVALID_RESPONSE,
                message="Pet Hospital API returned non-JSON response",
                status=response.status_code,
                details={
                    "endpoint": endpoint,
                    "error_type": type(exc).__name__,
                    "body_preview": response.text[:200] if response.text else "",
                },
            ) from exc

        # The Go REST API wraps every response in:
        #   { "code": 200, "message": "ok", "data": {...}, "time": "..." }
        if not isinstance(payload, dict):
            raise RestError(
                code=ErrorCode.BACKEND_INVALID_RESPONSE,
                message="Pet Hospital API response is not a JSON object",
                status=response.status_code,
                details={"endpoint": endpoint, "shape": type(payload).__name__},
            )

        data = payload.get("data")
        # Go's `omitempty` on a nil slice yields `null` in JSON; the REST API
        # never returns null for /pets but we handle it defensively.
        if data is None:
            raise RestError(
                code=ErrorCode.BACKEND_INVALID_RESPONSE,
                message="Pet Hospital API response has no 'data' field",
                status=response.status_code,
                details={"endpoint": endpoint, "message": payload.get("message")},
            )
        if not isinstance(data, dict):
            raise RestError(
                code=ErrorCode.BACKEND_INVALID_RESPONSE,
                message="Pet Hospital API 'data' is not a JSON object",
                status=response.status_code,
                details={"endpoint": endpoint, "shape": type(data).__name__},
            )
        return data


__all__ = ["PetHospitalClient", "RestError"]
