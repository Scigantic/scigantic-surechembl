"""HTTP client for SureChEMBL's REST API (https://www.surechembl.org/api).

SureChEMBL replaced its Java REST API with a FastAPI service in late 2026
(spec at /api/openapi.json, Swagger at /api/docs). Every endpoint the 0.3
releases called now answers 404 or 405. What was verified live on
2026-11-09 against the new service and shaped the code here:

- There is no response envelope any more. A 2xx body is the payload
  itself (a list for /chemical/id, an object for searches and patents).
- Errors are FastAPI shapes: `{"detail": "..."}` for a 404 ("Patent not
  found: ..."), `{"detail": [{"msg": ...}, ...]}` for a 422 validation
  error (an unparseable SMILES, an unknown search type), and
  `{"error": "..."}` for some misses (`/chemical/id` with no known id is
  a 404 `{"error": "Chemical not found"}`). request() raises
  SureChEMBLError for every status >= 400 carrying the HTTP status, so
  each public function maps its own endpoint's miss to None/[] and lets
  everything else through.
- A Solr failure behind the search endpoints comes back as HTTP 503 with
  `{"error": "Solr responded with an error (HTTP 400): ..."}` (a query
  syntax error) or `(HTTP 500)` (a non-numeric chemical id). That 503 is
  deterministic, so it is raised at once rather than retried like a
  gateway 503 would be: retrying a syntax error five times would only
  turn a clear error into a slow one.
- There are still no rate-limit or throttle headers. SureChEMBL is a
  shared EMBL-EBI service with no published quota, so requests are paced
  through a token bucket (5/s, a conservative figure borrowed from
  PubChem's published limit), and a 429/502/503/504 that is not a Solr
  error is retried with backoff. A 500 is not retried.
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
# A dropped connection is retried fewer times than a 429/5xx: the case
# seen live (2026-09-08) was Solr closing the connection on a query it
# could not finish (a regex over every claim), which five retries would
# only have re-inflicted on the server, 60 s at a time.
_MAX_CONNECTION_RETRIES = 3
# Connect and read are bounded separately: `timeout` is the read budget a
# caller chooses per endpoint, while a TCP connect to an unreachable host
# should never consume it (measured: 5 attempts x a 60 s connect hang
# would be 5 minutes before the first error surfaced).
_CONNECT_TIMEOUT = 10.0


class SureChEMBLError(Exception):
    """A SureChEMBL API error: an HTTP status >= 400 (in `http_status`),
    a connection that failed after retries, a response that is not the
    JSON the endpoint is documented to return, or a call to something
    the late-2026 API no longer offers. `api_status` is kept for
    compatibility and is always None now: the new API has no envelope
    status to report."""

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
    """One paced, retried HTTP request. Returns the Response once it is
    not retryable (or retries are spent), whatever its status, so the
    caller decides what a 404 means for its endpoint; raises
    SureChEMBLError only when no response arrived at all. (UniChem,
    whose failures look nothing like SureChEMBL's, has its own loop in
    _unichem.py.)"""
    session = _get_session()
    last_exc: Exception | None = None
    retryable = _RETRY_STATUS_CODES
    for attempt in range(_MAX_RETRIES):
        _limiter.acquire()
        try:
            response = session.request(
                method,
                url,
                params=params,
                data=data,
                json=json_body,
                timeout=(_CONNECT_TIMEOUT, timeout),
                stream=stream,
            )
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= _MAX_CONNECTION_RETRIES - 1:
                break
            time.sleep(2**attempt)
            continue
        if response.status_code in retryable and attempt < _MAX_RETRIES - 1 and not _is_solr_error(response):
            retry_after = response.headers.get("Retry-After")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else float(2**attempt)
            warnings.warn(
                f"SureChEMBL returned {response.status_code} for {url}; retrying in {wait:.0f}s "
                f"(attempt {attempt + 1}/{_MAX_RETRIES})",
                stacklevel=3,
            )
            time.sleep(wait)
            continue
        return response
    raise SureChEMBLError(f"SureChEMBL request failed after {attempt + 1} attempts: {last_exc}")


def _body(response: requests.Response) -> Any:
    """The parsed JSON body, or None if it is not JSON.

    Falls back to json.loads(strict=False) before giving up: patent text
    is OCR output, and a full-document response can carry a raw ASCII
    control character inside a string, which the default strict decoder
    rejects outright (the same failure scigantic-pubchem hit on depositor
    free text). The relaxed parse only runs after the strict one fails,
    so a normal response pays nothing for it."""
    try:
        return response.json()
    except ValueError:
        try:
            return json.loads(response.text, strict=False)
        except ValueError:
            return None


def _error_message(response: requests.Response) -> str:
    """The API's own words for a failure: FastAPI's `detail` (a string,
    or a list of validation errors whose `msg` fields are joined), the
    service's `error`, or the start of the raw body."""
    body = _body(response)
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str) and detail:
            return detail
        if isinstance(detail, list) and detail:
            msgs = [str(d.get("msg")) for d in detail if isinstance(d, dict) and d.get("msg")]
            if msgs:
                return "; ".join(msgs)
        if body.get("error"):
            return str(body["error"])
    return response.text[:300] or "(empty body)"


def _is_solr_error(response: requests.Response) -> bool:
    """A 503 that is Solr rejecting the query, not the service being down
    (see module docstring)."""
    if response.status_code != 503:
        return False
    body = _body(response)
    return isinstance(body, dict) and str(body.get("error", "")).startswith("Solr responded with an error")


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
    """Call a SureChEMBL API path and return its parsed JSON body.

    Raises SureChEMBLError for any HTTP status >= 400, carrying the
    status (`http_status`) so a caller can recognize its endpoint's miss
    (a 404 from /patent/get_patent, a 404 from /chemical/id) without
    string-matching, and for a 2xx body that is not JSON.

    Cached on (method, path, params, data, json_body) when caching is on
    and cacheable=True. POST is how SureChEMBL spells structure search,
    a pure read, so the method alone doesn't decide cacheability; the
    caller does.
    """
    cache_key = cache.key("request", method, path, params, data, json_body)
    if cacheable:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    url = f"{BASE_URL}{path}"
    response = send(method, url, params=params, data=data, json_body=json_body, timeout=timeout)
    if response.status_code >= 400:
        raise SureChEMBLError(
            f"SureChEMBL {response.status_code} for {url}: {_error_message(response)}",
            http_status=response.status_code,
        )
    payload = _body(response)
    if payload is None:
        raise SureChEMBLError(
            f"SureChEMBL {response.status_code} for {url}: response is not JSON: {response.text[:200]!r}",
            http_status=response.status_code,
        )
    if cacheable and payload:
        # An empty payload is not pinned: SureChEMBL has answered a valid
        # lookup with an empty result under load, and it spells a genuine
        # miss the same way. Caching the blank would turn a transient
        # hiccup into a miss for the cache TTL; re-asking on a real miss
        # costs one cheap request.
        cache.put(cache_key, payload)
    return payload


def request_bytes(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any | None = None,
    timeout: float = 120.0,
) -> bytes:
    """Call a path whose success response is a binary body (a PNG from
    /chemical/image, a PDF from /patent/pdf) and return it. A status
    >= 400, or a JSON body on a 2xx, is raised as SureChEMBLError."""
    url = f"{BASE_URL}{path}"
    response = send(method, url, params=params, json_body=json_body, timeout=timeout)
    content_type = response.headers.get("Content-Type", "")
    if response.status_code >= 400 or content_type.startswith("application/json"):
        raise SureChEMBLError(
            f"SureChEMBL {response.status_code} for {url}: {_error_message(response)}",
            http_status=response.status_code,
        )
    return response.content
