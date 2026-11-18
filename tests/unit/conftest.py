# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared unit test fixtures."""

import pytest

from core.backends import GatewayBackend
from gateway_api import GatewayApiClient, GatewayApiError


class FakeGateway:
    """An in-memory gateway backend table behind `GatewayApiClient`.

    Attributes:
        backends: The registered backends, by name.
        reachable: Whether API calls succeed.
    """

    def __init__(self):
        self.backends: dict[str, GatewayBackend] = {}
        self.reachable = True

    def register(self, *names: str) -> None:
        """Register backends by name, with placeholder values the charm will update."""
        for name in names:
            self.backends[name] = GatewayBackend(name, None, None, None, None)

    def _check(self) -> None:
        if not self.reachable:
            raise GatewayApiError("connection refused")


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> FakeGateway:
    """Replace the gateway API with an in-memory backend table."""
    fake = FakeGateway()

    def list_backends(_: GatewayApiClient) -> list[GatewayBackend]:
        fake._check()
        return list(fake.backends.values())

    def put(_: GatewayApiClient, backend: GatewayBackend) -> None:
        fake._check()
        fake.backends[backend.name] = backend

    def delete(_: GatewayApiClient, name: str) -> None:
        fake._check()
        fake.backends.pop(name, None)

    monkeypatch.setattr(GatewayApiClient, "list_backends", list_backends)
    monkeypatch.setattr(GatewayApiClient, "add", put)
    monkeypatch.setattr(GatewayApiClient, "update", put)
    monkeypatch.setattr(GatewayApiClient, "delete", delete)
    return fake
