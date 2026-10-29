"""One HTTP transport for every JSON API connector.

Every SaaS connector in this product would otherwise grow its own ``urllib`` block, and each
would get a different subset of these wrong: ``429`` backoff, ``Retry-After``, token in a query
string vs a header, and the provider's own error text (which is the only part a customer can act
on). This module is the single implementation.

Design rules:

* **Bounded.** At most :data:`MAX_ATTEMPTS` attempts, each with a timeout, and a total retry
  budget. A retrying call must never hang the agent loop.
* **Provider text preserved.** ``invalid_grant``, ``channel_not_found``, ``AUTHENTICATION_FAILED``
  are surfaced verbatim; replacing them with "request failed" is what makes support impossible.
* **401 is not retried.** A rejected credential will be rejected again. It raises
  :class:`AuthError` immediately so the UI can send the customer to the right settings field.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from .base import ConnectorError

MAX_ATTEMPTS = 3
RETRY_STATUS = {429, 500, 502, 503, 504}
DEFAULT_TIMEOUT = 30.0
USER_AGENT = "ZYNTHARO/1.0 (+desktop connector)"


class HttpError(ConnectorError):
    """A service answered with a non-2xx status and its own explanation."""

    def __init__(self, status: int, message: str, body: Any = None, retry_after: float = 0.0):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = body
        self.retry_after = retry_after


class AuthError(ConnectorError):
    """The credential was rejected (401) or lacks the permission (403)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _provider_message(body: Any, fallback: str) -> str:
    """Pull the human sentence out of the many shapes providers use."""
    if isinstance(body, dict):
        for key in ("error_description", "error", "message", "detail", "errorMessage", "errors"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, list) and value:
                first = value[0]
                if isinstance(first, dict):
                    for inner in ("message", "detail", "errorMessage"):
                        if isinstance(first.get(inner), str):
                            return str(first[inner])
                elif isinstance(first, str):
                    return first
            if isinstance(value, dict):
                for inner in ("message", "detail", "value"):
                    if isinstance(value.get(inner), str):
                        return str(value[inner])
    if isinstance(body, str) and body.strip():
        return body.strip()[:300]
    return fallback


def _retry_after(headers: Mapping[str, str]) -> float:
    raw = str(headers.get("Retry-After", "") or "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, min(float(raw), 60.0))
    except ValueError:
        return 0.0


def request_json(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    body: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
    attempts: int = MAX_ATTEMPTS,
    accept: str = "application/json",
) -> Any:
    """Perform one JSON request, with bounded retries for rate limits and server errors."""
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean:
            separator = "&" if urllib.parse.urlparse(url).query else "?"
            url = f"{url}{separator}{urllib.parse.urlencode(clean, doseq=True)}"
    payload: bytes | None = None
    request_headers = {"Accept": accept, "User-Agent": USER_AGENT}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request_headers.update({k: v for k, v in (headers or {}).items() if v})

    last_error: Exception | None = None
    derived_attempts = 1 if method.upper() in {"POST", "PATCH", "PUT", "DELETE"} else max(1, int(attempts))
    for attempt in range(1, derived_attempts + 1):
        request = urllib.request.Request(url, data=payload, headers=request_headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                if not raw:
                    return {}
                try:
                    return json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except OSError:
                raw = b""
            parsed: Any = None
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else None
            except (ValueError, UnicodeDecodeError):
                parsed = raw.decode("utf-8", "replace") if raw else None
            message = _provider_message(parsed, f"HTTP {exc.code}")
            if exc.code in (401, 403):
                raise AuthError(
                    exc.code,
                    f"the service rejected this credential ({exc.code}): {message}",
                ) from exc
            if exc.code in RETRY_STATUS and attempt < derived_attempts:
                wait = _retry_after(exc.headers or {}) or min(2.0 * attempt, 8.0)
                last_error = HttpError(exc.code, message, parsed, wait)
                time.sleep(wait)
                continue
            raise HttpError(exc.code, message, parsed) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < derived_attempts:
                time.sleep(min(2.0 * attempt, 5.0))
                continue
            raise ConnectorError(f"could not reach {urllib.parse.urlparse(url).netloc}: {exc}") from exc
    raise ConnectorError(f"request failed after {derived_attempts} attempt(s): {last_error}")


def fill_path(template: str, values: Mapping[str, Any]) -> str:
    """Substitute ``{name}`` placeholders, quoting each value for a URL path."""
    out = template
    for key, value in values.items():
        token = "{" + key + "}"
        if token in out:
            out = out.replace(token, urllib.parse.quote(str(value), safe=""))
    if "{" in out:
        missing = out[out.index("{") : out.index("}") + 1]
        raise ConnectorError(f"this operation needs a value for {missing}")
    return out
