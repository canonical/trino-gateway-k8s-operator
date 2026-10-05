# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Client for the Trino Gateway backend REST API."""

import json
import logging
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from config import BackendConfig
from workload import DEFAULT_ROUTING_GROUP

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5


class GatewayApiError(Exception):
    """The gateway API could not be reached or rejected a request."""


class GatewayApiClient:
    """Minimal client for the `/gateway/backend` endpoints."""

    def __init__(self, base_url: str):
        self._base_url = base_url.rstrip("/")

    def list_backends(self) -> list[dict[str, Any]]:
        """Return every backend registered with the gateway."""
        body = self._request("GET", "/gateway/backend/all")
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise GatewayApiError(f"invalid backend list: {e}") from e

    def add(self, backend: dict[str, Any]) -> None:
        """Register a backend."""
        self._request(
            "POST", "/gateway/backend/modify/add", json.dumps(backend), "application/json"
        )

    def update(self, backend: dict[str, Any]) -> None:
        """Update a registered backend, matched by name."""
        self._request(
            "POST", "/gateway/backend/modify/update", json.dumps(backend), "application/json"
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
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
                return response.read()
        except OSError as e:
            raise GatewayApiError(f"{method} {path} failed: {e}") from e


@dataclass
class SyncPlan:
    """Changes needed to make the gateway's backends match the desired set."""

    add: list[dict[str, Any]] = field(default_factory=list)
    update: list[dict[str, Any]] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)


def to_entry(backend: BackendConfig) -> dict[str, Any]:
    """Convert a configured backend to the gateway's API representation."""
    return {
        "name": backend.name,
        "proxyTo": backend.url,
        "active": True,
        "routingGroup": DEFAULT_ROUTING_GROUP,
        "externalUrl": backend.url,
    }


def plan_sync(desired: Iterable[BackendConfig], current: Iterable[dict[str, Any]]) -> SyncPlan:
    """Compute the changes that make `current` match `desired`.

    The charm owns the whole backend table, so anything not desired is deleted.

    Args:
        desired: Backends from the charm configuration.
        current: Backends as returned by the gateway.

    Returns:
        The backends to add, update and delete.
    """
    current_by_name = {entry["name"]: entry for entry in current}
    plan = SyncPlan()
    for entry in map(to_entry, desired):
        existing = current_by_name.pop(entry["name"], None)
        if existing is None:
            plan.add.append(entry)
        elif any(existing.get(key) != value for key, value in entry.items()):
            plan.update.append(entry)
    plan.delete = sorted(current_by_name)
    return plan


def sync_backends(client: GatewayApiClient, desired: Iterable[BackendConfig]) -> None:
    """Make the gateway's registered backends match `desired`.

    Args:
        client: Client for the gateway API.
        desired: Backends from the charm configuration.

    Raises:
        GatewayApiError: if the gateway cannot be reached or rejects a change.
    """
    plan = plan_sync(desired, client.list_backends())
    for name in plan.delete:
        logger.info("Deregistering backend %s", name)
        client.delete(name)
    for entry in plan.update:
        logger.info("Updating backend %s", entry["name"])
        client.update(entry)
    for entry in plan.add:
        logger.info("Registering backend %s", entry["name"])
        client.add(entry)
