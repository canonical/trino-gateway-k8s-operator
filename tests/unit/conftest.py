# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared unit test fixtures."""

import ops
import pytest
from ops import testing
from scenario.mocking import _MockModelBackend

from core.backends import GatewayBackend
from gateway_api import GatewayApiClient, GatewayApiError


class FakeGateway:
    """An in-memory gateway backend table behind `GatewayApiClient`.

    Attributes:
        backends: The registered backends, by name.
        reachable: Whether API calls succeed.
        api_keys: The API key of the client behind each call, in order.
    """

    def __init__(self):
        self.backends: dict[str, GatewayBackend] = {}
        self.reachable = True
        self.api_keys: list[str | None] = []

    def register(self, *names: str) -> None:
        """Register backends by name, with placeholder values the charm will update."""
        for name in names:
            self.backends[name] = GatewayBackend(name, None, None, None, None)

    def _check(self, client: GatewayApiClient) -> None:
        self.api_keys.append(client._api_key)
        if not self.reachable:
            raise GatewayApiError("connection refused")


def latest_content(secret: testing.Secret) -> dict[str, str]:
    """Read the content of a secret's latest revision, which Scenario types as optional."""
    assert secret.latest_content is not None
    return secret.latest_content


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> FakeGateway:
    """Replace the gateway API with an in-memory backend table."""
    fake = FakeGateway()

    def list_backends(client: GatewayApiClient) -> list[GatewayBackend]:
        fake._check(client)
        return list(fake.backends.values())

    def put(client: GatewayApiClient, backend: GatewayBackend) -> None:
        fake._check(client)
        fake.backends[backend.name] = backend

    def delete(client: GatewayApiClient, name: str) -> None:
        fake._check(client)
        fake.backends.pop(name, None)

    monkeypatch.setattr(GatewayApiClient, "list_backends", list_backends)
    monkeypatch.setattr(GatewayApiClient, "add", put)
    monkeypatch.setattr(GatewayApiClient, "update", put)
    monkeypatch.setattr(GatewayApiClient, "delete", delete)
    return fake


@pytest.fixture
def unreadable_revisions(monkeypatch: pytest.MonkeyPatch) -> set[tuple[str, int]]:
    """Make Scenario's secrets fail as Juju's do after a partly saved secret.

    Reading a listed `(secret ID, revision)` raises `SecretNotFoundError`, and adding a secret
    under a label that is taken raises `ModelError`, as in Juju.

    Returns:
        The set of unreadable revisions, to fill in.
    """
    unreadable: set[tuple[str, int]] = set()
    original_get = _MockModelBackend.secret_get
    original_add = _MockModelBackend.secret_add

    def secret_get(self, *, id=None, label=None, refresh=False, peek=False):
        secret = self._get_secret(id, label)
        revision = secret._latest_revision if refresh or peek else secret._tracked_revision
        if (secret.id, revision) in unreadable:
            raise ops.SecretNotFoundError(f'secret "{secret.id}-{revision}" not found')
        return original_get(self, id=id, label=label, refresh=refresh, peek=peek)

    def secret_add(self, content, *, label=None, **kwargs):
        if label is not None and any(s.label == label for s in self._state.secrets):
            raise ops.ModelError(f'secret with label "{label}" already exists')
        return original_add(self, content, label=label, **kwargs)

    monkeypatch.setattr(_MockModelBackend, "secret_get", secret_get)
    monkeypatch.setattr(_MockModelBackend, "secret_add", secret_add)
    return unreadable
