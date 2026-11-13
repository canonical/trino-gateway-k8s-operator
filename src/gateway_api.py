# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Client for the Trino Gateway backend REST API."""

import json
import logging
import urllib.request
from collections.abc import Iterable

from core.backends import GatewayBackend, plan_sync
from relations.backend_trino_catalog import RelatedBackend

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5


class GatewayApiError(Exception):
    """The gateway API could not be reached or rejected a request."""


class GatewayApiClient:
    """Minimal client for the `/gateway/backend` endpoints."""

    def __init__(self, base_url: str):
        self._base_url = base_url.rstrip("/")

    def list_backends(self) -> list[GatewayBackend]:
        """Return every backend registered with the gateway.

        Returns:
            The registered backends.

        Raises:
            GatewayApiError: if the gateway cannot be reached or returns an unexpected response.
        """
        body = self._request("GET", "/gateway/backend/all")
        try:
            entries = json.loads(body)
            if not isinstance(entries, list):
                raise ValueError("expected a list")
            return [GatewayBackend.from_api(entry) for entry in entries]
        except ValueError as e:
            raise GatewayApiError(f"invalid backend list: {e}") from e

    def add(self, backend: GatewayBackend) -> None:
        """Register a backend.

        Args:
            backend: The backend to register.

        Raises:
            GatewayApiError: if the gateway cannot be reached or rejects the request.
        """
        self._request(
            "POST",
            "/gateway/backend/modify/add",
            json.dumps(backend.to_api()),
            "application/json",
        )

    def update(self, backend: GatewayBackend) -> None:
        """Update a registered backend, matched by name.

        Args:
            backend: The backend with its new values.

        Raises:
            GatewayApiError: if the gateway cannot be reached or rejects the request.
        """
        self._request(
            "POST",
            "/gateway/backend/modify/update",
            json.dumps(backend.to_api()),
            "application/json",
        )

    def delete(self, name: str) -> None:
        """Deregister a backend by name."""
        self._request("POST", "/gateway/backend/modify/delete", name, "text/plain")

    def _request(
        self, method: str, path: str, body: str | None = None, content_type: str | None = None
    ) -> bytes:
        request = urllib.request.Request(
            self._base_url + path,
            data=None if body is None else body.encode(),
            method=method,
        )
        if content_type:
            request.add_header("Content-Type", content_type)
        try:
            # The base URL is a fixed http URL chosen by the charm, not user input.
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # nosec B310
                return response.read()
        except OSError as e:
            raise GatewayApiError(f"{method} {path} failed: {e}") from e


def sync_backends(
    client: GatewayApiClient,
    desired: Iterable[RelatedBackend],
    current: Iterable[GatewayBackend],
) -> None:
    """Make the gateway's registered backends match `desired`.

    Args:
        client: Client for the gateway API.
        desired: The backends the gateway should have.
        current: The backends the gateway has, as listed by `client.list_backends()`.

    Raises:
        GatewayApiError: if the gateway cannot be reached or rejects a change.
    """
    plan = plan_sync(desired, current)
    for name in plan.delete:
        logger.info("Deregistering backend %r", name)
        client.delete(name)
    for backend in plan.update:
        logger.info("Updating backend %r", backend.name)
        client.update(backend)
    for backend in plan.add:
        logger.info("Registering backend %r", backend.name)
        client.add(backend)
