"""A stand-in for the network, so tests never leave the machine."""

import json
from urllib.parse import parse_qs, urlsplit

GITHUB = "https://api.github.com"
GITLAB = "https://gitlab.example.com"


class FakeTransport:
    """Answers API calls from a table.

    ``routes`` maps a path (relative to the API root, without its query string) to a
    reply: JSON-able data, a string for raw file contents, ``("error", status)``, or a
    callable ``(method, path, body) -> reply``. A ``(method, path)`` key wins over a
    bare path. List replies are served in pages, like the real APIs.
    """

    def __init__(self, routes=None, page_cap=None):
        self.routes = dict(routes or {})
        self.page_cap = page_cap  # serve at most this many items per page
        self.calls = []  # (method, path, body)
        self.headers = []

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        path = parts.path.lstrip("/")
        if path.startswith("api/v4/"):
            path = path[len("api/v4/") :]
        query = parse_qs(parts.query)
        payload = json.loads(body) if body else None
        self.calls.append((method, path, payload))
        self.headers.append(headers)

        reply = self.routes.get((method, path), self.routes.get(path))
        if callable(reply):
            reply = reply(method, path, payload)
        if reply is None and method != "GET":
            reply = {}
        if reply is None:
            return 404, json.dumps({"message": "Not Found"})
        if isinstance(reply, tuple) and reply[0] == "error":
            return reply[1], json.dumps({"message": f"failed with {reply[1]}"})
        if isinstance(reply, str):
            return 200, reply

        if "page" in query:
            page = int(query["page"][0])
            size = int(query.get("per_page", ["30"])[0])
            if self.page_cap:
                size = min(size, self.page_cap)
            window = slice((page - 1) * size, page * size)
            if isinstance(reply, list):
                reply = reply[window]
            elif isinstance(reply, dict):
                reply = {
                    key: (value[window] if isinstance(value, list) else value)
                    for key, value in reply.items()
                }
        return 200, json.dumps(reply)
