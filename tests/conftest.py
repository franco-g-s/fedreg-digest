"""Offline test helpers: a fake requests session that serves recorded API responses."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, status_code=200, body: bytes | str = b"", json_data=None):
        self.status_code = status_code
        if json_data is not None:
            body = json.dumps(json_data)
        self.content = body.encode() if isinstance(body, str) else body
        self.text = self.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.content)


class FakeSession:
    """Routes GET requests to handlers; records every call for assertions."""

    def __init__(self):
        self.routes = []
        self.calls = []
        self.request_timeout = 5

    def add(self, match, response):
        """match(url, params) -> bool; response is a FakeResponse or a callable."""
        self.routes.append((match, response))

    def get(self, url, params=None, timeout=None):
        merged = dict(params or {})
        for key, values in parse_qs(urlparse(url).query).items():
            merged.setdefault(key, values if len(values) > 1 else values[0])
        self.calls.append((url, merged))
        for match, response in self.routes:
            if match(url, merged):
                return response(url, merged) if callable(response) else response
        return FakeResponse(404, f"no fake route for {url} {merged}")


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str):
    return json.loads(fixture_bytes(name))


@pytest.fixture
def session():
    return FakeSession()
