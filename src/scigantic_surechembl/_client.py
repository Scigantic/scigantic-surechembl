"""HTTP client for SureChEMBL's REST API (https://www.surechembl.org/api).

What was verified live on 2026-09-08 and shaped the code here, none of it
in the OpenAPI spec at /api/v3/api-docs, which declares every response as
an opaque `SureChEmblApiResponse {status, data, timestamp, errorMessage}`:

- Every JSON response is an envelope. `status` is "OK" on success; on a
  failure it is "ERROR", "NOT_FOUND", "BAD_REQUEST" or
  "INTERNAL_SERVER_ERROR" with `error_message` set (note the snake_case,
  not the spec's `errorMessage`). request() unwraps `data` on OK and
  raises SureChEMBLError otherwise, so no caller re-checks the envelope.
- A miss is not consistent across endpoints. `/chemical/id/{n}` for an
  unknown id is 200 with `data: []`; `/chemical/name/{x}` for an unknown
  name is 400 with status ERROR; `/document/{id}/contents` for an
  unknown document is 404; `/document/{id}/family/members` for one is a
  500 ("SQL exception detected while reading family members"). Each
  public function maps its own endpoint's miss to None/[] and lets real
  errors through as SureChEMBLError.
- `POST /search/structure` needs its JSON body wrapped in the Java class
  name, `{"StructureSearchRequest": {...}}` (Jackson root-name wrapping);
  the bare object the spec shows is rejected with a 400.
- There are no rate-limit or throttle headers at all. SureChEMBL is a
  shared EMBL-EBI service with no published quota, so requests are paced
  through a token bucket (5/s, a conservative figure borrowed from
  PubChem's published limit) rather than fired as fast as a thread pool
  can go, and a 429/502/503/504 is retried with backoff. A 500 is NOT
  retried: every 500 seen during development was deterministic (a
  malformed id, a missing family), so retrying it would only turn a
  clear error into a slow one.
"""

from __future__ import annotations

import json
import threading
import time
import warnings
from importlib.metadata import PackageNotFoundError, version as _version
from typing import Any

import requests

from . import cache

BASE_URL = "https://www.surechembl.org/api"

try:
    __version__ = _version("scigantic-surechembl")
except PackageNotFoundError:
    __version__ = "0.0.0"

USER_AGENT = f"scigantic-surechembl/{__version__} (+https://scigantic.com; mailto:support@scigantic.com)"

_MAX_RETRIES = 5
_RETRY_STATUS_CODES = {429, 502, 503, 504}


class SureChEMBLError(Exception):
    """A SureChEMBL API error: a non-OK envelope status, an HTTP error
    after retries are exhausted, or a response that is not the JSON the
    endpoint is documented to return."""

    def __init__(self, message: str, http_status: int | None = None, api_status: str | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.api_status = api_status


class _RateLimiter:
    """Token bucket: `capacity` requests may go immediately, then
    `rate`/second. acquire() blocks until a token is free; safe from
    multiple threads."""

    def __init__(self, rate: float, capacity: float) -> None:
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self._rate
            time.sleep(wait)


_limiter = _RateLimiter(rate=5.0, capacity=5.0)

_session: requests.Session | None = None
_session_lock = threading.Lock()


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                _session = requests.Session()
                _session.headers["User-Agent"] = USER_AGENT
    return _session


def send(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    json_body: Any | None = None,
    timeout: float = 60.0,
    stream: bool = False,
) -> requests.Response:
    """One paced, retried HTTP request. Returns the Response for any
    status code below 500 that is not retryable, so the caller decides
    what a 400/404 means for its endpoint; raises SureChEMBLError for a
    500 or for retries exhausted."""
    session = _get_session()
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        _limiter.acquire()
        try:
            response = session.request(
                method, url, params=params, data=data, json=json_body, timeout=timeout, stream=stream
            )
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2**attempt)
            continue
        if response.status_code in _RETRY_STATUS_CODES and attempt < _MAX_RETRIES - 1:
            retry_after = response.headers.get("Retry-After")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else float(2**attempt)
            warnings.warn(
                f"SureChEMBL returned {response.status_code} for {url}; retrying in {wait:.0f}s "
                f"(attempt {attempt + 1}/{_MAX_RETRIES})",
                stacklevel=3,
            )
            time.sleep(wait)
            continue
        if response.status_code >= 500:
            raise SureChEMBLError(
                f"SureChEMBL {response.status_code} for {url}: {_error_message(response)}",
                http_status=response.status_code,
                api_status=_api_status(response),
            )
        return response
    raise SureChEMBLError(f"SureChEMBL request failed after {_MAX_RETRIES} attempts: {last_exc}")


def _envelope(response: requests.Response) -> dict[str, Any] | None:
    """The parsed JSON envelope, or None if the body is not a JSON object.

    Falls back to json.loads(strict=False) before giving up: patent text
    is OCR output, and a full-document response can carry a raw ASCII
    control character inside a string, which the default strict decoder
    rejects outright (the same failure scigantic-pubchem hit on depositor
    free text). The relaxed parse only runs after the strict one fails,
    so a normal response pays nothing for it."""
    try:
        body = response.json()
    except ValueError:
        try:
            body = json.loads(response.text, strict=False)
        except ValueError:
            return None
    return body if isinstance(body, dict) else None


def _error_message(response: requests.Response) -> str:
    env = _envelope(response)
    if env and env.get("error_message"):
        return str(env["error_message"])
    return response.text[:300]


def _api_status(response: requests.Response) -> str | None:
    env = _envelope(response)
    return str(env["status"]) if env and "status" in env else None


def request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    json_body: Any | None = None,
    cacheable: bool = True,
    timeout: float = 60.0,
) -> Any:
    """Call a SureChEMBL API path and return the envelope's `data`.

    Raises SureChEMBLError for any non-OK envelope, carrying the HTTP
    status and the API's own status string so a caller can recognize
    an endpoint-specific miss (a 400 ERROR from /chemical/name, a 404
    from /document/.../contents) without string-matching.

    Cached on (method, path, params, data, json_body) when caching is on
    and cacheable=True. POST here is how SureChEMBL spells several pure
    reads (batch id lookup, SMILES lookup, text search), so the method
    alone doesn't decide cacheability; the caller does, and passes
    cacheable=False for anything that creates or polls server state.
    """
    cache_key = cache.key("request", method, path, params, data, json_body)
    if cacheable:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    url = f"{BASE_URL}{path}"
    response = send(method, url, params=params, data=data, json_body=json_body, timeout=timeout)
    env = _envelope(response)
    if env is None:
        raise SureChEMBLError(
            f"SureChEMBL {response.status_code} for {url}: response is not JSON: {response.text[:200]!r}",
            http_status=response.status_code,
        )
    status = str(env.get("status", ""))
    if status != "OK":
        raise SureChEMBLError(
            f"SureChEMBL {status} for {url}: {env.get('error_message') or '(no message)'}",
            http_status=response.status_code,
            api_status=status,
        )
    payload = env.get("data")
    if cacheable:
        cache.put(cache_key, payload)
    return payload


def request_bytes(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 120.0,
) -> bytes:
    """Call a path whose success response is a binary body (a PNG from
    /service/chemical/image, a zip from /export/document-chemistry) and
    return it. A JSON body on a 2xx means the API reported an error in
    the envelope instead; raised as SureChEMBLError."""
    url = f"{BASE_URL}{path}"
    response = send(method, url, params=params, timeout=timeout)
    content_type = response.headers.get("Content-Type", "")
    if response.status_code >= 400 or content_type.startswith("application/json"):
        raise SureChEMBLError(
            f"SureChEMBL {response.status_code} for {url}: {_error_message(response)}",
            http_status=response.status_code,
            api_status=_api_status(response),
        )
    return response.content
