# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Desired gateway backends and the diff against the registered ones."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Self

import constants
from config import BackendConfig
from relations.backend_trino_catalog import RelatedBackend


class BackendNameConflictError(Exception):
    """Two desired backends share a name.

    Attributes:
        names: The names that occur more than once, sorted.
    """

    def __init__(self, names: list[str]):
        """Initialise the error.

        Args:
            names: The names that occur more than once, sorted.
        """
        super().__init__(f"duplicate backend names: {', '.join(names)}")
        self.names = names


@dataclass(frozen=True)
class GatewayBackend:
    """A backend as the gateway's REST API represents it.

    Attributes:
        name: Unique name of the backend, the gateway's primary key.
        proxy_to: URL that queries are proxied to.
        active: Whether the gateway routes queries to the backend.
        routing_group: Routing group the backend belongs to.
        external_url: URL that clients are redirected to.
    """

    name: str
    proxy_to: str | None
    active: bool | None
    routing_group: str | None
    external_url: str | None

    @classmethod
    def from_config(cls, backend: BackendConfig) -> Self:
        """Build the backend the gateway should have for a configured one.

        Args:
            backend: The backend from the charm configuration or a relation.

        Returns:
            The backend in the default routing group.
        """
        return cls(
            name=backend.name,
            proxy_to=backend.url,
            active=True,
            routing_group=constants.DEFAULT_ROUTING_GROUP,
            external_url=backend.url,
        )

    @classmethod
    def from_api(cls, data: Any) -> Self:
        """Parse a backend returned by the gateway.

        Only the name is required. A field the gateway leaves unset, as for a backend edited by
        hand, becomes None, so the backend compares as different and gets updated.

        Args:
            data: One entry of the gateway's backend list.

        Returns:
            The parsed backend.

        Raises:
            ValueError: if the entry is not an object with a string name.
        """
        if not isinstance(data, dict) or not isinstance(data.get("name"), str):
            raise ValueError("backend entry must be an object with a name")
        return cls(
            name=data["name"],
            proxy_to=data.get("proxyTo"),
            active=data.get("active"),
            routing_group=data.get("routingGroup"),
            external_url=data.get("externalUrl"),
        )

    def to_api(self) -> dict[str, Any]:
        """Convert the backend to the gateway's JSON representation.

        Returns:
            The request body that registers or updates the backend.
        """
        return {
            "name": self.name,
            "proxyTo": self.proxy_to,
            "active": self.active,
            "routingGroup": self.routing_group,
            "externalUrl": self.external_url,
        }


@dataclass
class SyncPlan:
    """Changes needed to make the gateway's backends match the desired set.

    Attributes:
        add: Backends to register.
        update: Registered backends that differ from the desired ones.
        delete: Names of registered backends that are not desired.
    """

    add: list[GatewayBackend] = field(default_factory=list)
    update: list[GatewayBackend] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)


def plan_sync(desired: Iterable[BackendConfig], current: Iterable[GatewayBackend]) -> SyncPlan:
    """Compute the changes that make `current` match `desired`.

    The charm owns the whole backend table, so anything not desired is deleted.

    Args:
        desired: The backends the gateway should have.
        current: Backends registered with the gateway.

    Returns:
        The backends to add, update and delete.
    """
    current_by_name = {backend.name: backend for backend in current}
    plan = SyncPlan()
    for backend in map(GatewayBackend.from_config, desired):
        existing = current_by_name.pop(backend.name, None)
        if existing is None:
            plan.add.append(backend)
        elif existing != backend:
            plan.update.append(backend)
    plan.delete = sorted(current_by_name)
    return plan


def merge_backends(
    configured: list[BackendConfig], related: Iterable[RelatedBackend]
) -> list[BackendConfig]:
    """Union the configured and related backends.

    Names are compared case-sensitively, as the gateway's primary key is.

    Args:
        configured: Backends from the charm configuration.
        related: Backends from related Trino applications.

    Returns:
        The configured backends followed by the related ones.

    Raises:
        BackendNameConflictError: if any name occurs more than once.
    """
    merged = [*configured, *(BackendConfig(name=r.name, url=r.url) for r in related)]
    names = [backend.name for backend in merged]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise BackendNameConflictError(duplicates)
    return merged
