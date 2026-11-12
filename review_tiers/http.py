"""A small JSON-over-HTTPS client built on the standard library.

Both hosts are reached through this one class, so the whole tool needs no third-party
package. The base address and the token are handed in by the caller; nothing here
reads the environment.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

PER_PAGE = 100
TIMEOUT_SECONDS = 30
# Reads are retried on a dropped connection or a busy server. Writes never are.
RETRY_STATUSES = (429, 502, 503, 504)
RETRY_DELAYS = (1, 3)

# (method, url, headers, body) -> (status, response text)
Transport = Callable[[str, str, dict, bytes | None], tuple[int, str]]


class ApiError(RuntimeError):
    """An API call failed. ``status`` is the HTTP status, or None for no response."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def quote(value: str) -> str:
    """Percent-encode one path segment or query value, slashes included."""
    return urllib.parse.quote(str(value), safe="")


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise ApiError(f"{method} {url}: no response ({reason})") from error


class Api:
    """Calls one host's REST API. Pass ``transport`` to substitute the network."""

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str],
        transport: Transport = urllib_transport,
    ):
        if not base_url.lower().startswith("https://"):
            raise ApiError(f"refusing to send a token to a non-HTTPS address: {base_url}")
        self.base_url = base_url.rstrip("/")
        self._headers = dict(headers)
        self._transport = transport
        self.sleep = time.sleep

    def _send(self, method: str, url: str, headers: dict, payload: bytes | None):
        delays = RETRY_DELAYS if method == "GET" else ()
        for delay in (*delays, None):
            try:
                status, text = self._transport(method, url, headers, payload)
            except ApiError:
                if delay is None:
                    raise
            else:
                if delay is None or status not in RETRY_STATUSES:
                    return status, text
            self.sleep(delay)
        raise AssertionError("unreachable")

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        tolerate: tuple[int, ...] = (),
        raw: bool = False,
        accept: str | None = None,
    ):
        """Call the API and return decoded JSON, or None for an empty reply.

        ``tolerate`` lists HTTP error statuses that return None instead of raising.
        ``raw`` returns the response text undecoded, for file contents.
        """
        headers = dict(self._headers)
        if accept:
            headers["Accept"] = accept
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        url = f"{self.base_url}/{path.lstrip('/')}"
        status, text = self._send(method, url, headers, payload)
        if status >= 400:
            if status in tolerate:
                return None
            raise ApiError(f"{method} {path} failed: HTTP {status} {_detail(text)}", status)
        if raw:
            return text
        text = text.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError as error:
            raise ApiError(f"{method} {path}: the reply was not JSON", status) from error

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)

    def get_all(
        self,
        path: str,
        key: str | None = None,
        limit: int | None = None,
        until_empty: bool = False,
    ) -> list:
        """Fetch every page of a list endpoint that pages with page and per_page.

        ``key`` unwraps replies shaped like ``{"check_runs": [...]}``. ``limit`` stops
        once that many items are collected. A short page normally ends the listing;
        ``until_empty`` reads on until a page comes back empty, for hosts that may
        serve fewer items per page than asked for.
        """
        items: list = []
        joiner = "&" if "?" in path else "?"
        page = 1
        while True:
            data = self.get(f"{path}{joiner}per_page={PER_PAGE}&page={page}")
            chunk = (data or {}).get(key, []) if key else (data or [])
            items.extend(chunk)
            done = not chunk if until_empty else len(chunk) < PER_PAGE
            if done or (limit is not None and len(items) >= limit):
                break
            page += 1
        return items[:limit] if limit is not None else items


def _detail(text: str) -> str:
    """A short, single-line reason from an error reply."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        message = data.get("message") or data.get("error") or ""
        if not isinstance(message, str):
            message = json.dumps(message)
    else:
        message = text
    return " ".join(message.split())[:200]
