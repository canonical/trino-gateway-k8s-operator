# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Trino Gateway workload: configuration rendering and Pebble management."""

import hashlib
import json
from collections.abc import Callable
from typing import Any

import ops
import pydantic
import yaml

SERVICE_NAME = "trino-gateway"
CONFIG_PATH = "/etc/trino-gateway/config.yaml"
HTTP_PORT = 8080
WORKLOAD_USER = "_daemon_"
DEFAULT_ROUTING_GROUP = "adhoc"


class PostgresRelationModel(pydantic.BaseModel):
    """PostgreSQL connection details provided over the `postgresql_client` interface."""

    database: str
    endpoints: str
    secret_user: dict[str, str] = pydantic.Field(alias="secret-user")
    secret_tls: dict[str, str] | None = pydantic.Field(default=None, alias="secret-tls")

    @property
    def username(self) -> str:
        """Username of the relation user."""
        return self.secret_user["username"]

    @property
    def password(self) -> str:
        """Password of the relation user."""
        return self.secret_user["password"]

    @classmethod
    def decode(cls, charm: ops.CharmBase) -> Callable[[str], Any]:
        """Build a databag decoder that parses JSON values and resolves Juju secret IDs.

        Args:
            charm: The charm used to look up secrets.

        Returns:
            A function decoding a single raw databag value.
        """

        def wrapped(value: str) -> Any:
            if value.startswith("secret:"):
                return charm.model.get_secret(id=value).get_content(refresh=True)
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value

        return wrapped


def render_config(pg: PostgresRelationModel) -> str:
    """Render the Trino Gateway configuration file.

    Args:
        pg: PostgreSQL connection details.

    Returns:
        The configuration file content.
    """
    config = {
        "serverConfig": {
            "node.environment": "production",
            "http-server.http.port": HTTP_PORT,
        },
        "dataStore": {
            "jdbcUrl": f"jdbc:postgresql://{pg.endpoints}/{pg.database}",
            "user": pg.username,
            "password": pg.password,
            "driver": "org.postgresql.Driver",
        },
        "clusterStatsConfiguration": {"monitorType": "INFO_API"},
        "routing": {"defaultRoutingGroup": DEFAULT_ROUTING_GROUP},
    }
    return yaml.safe_dump(config, sort_keys=False)


def pebble_layer(config_hash: str) -> ops.pebble.LayerDict:
    """Build the Pebble layer that overlays the rock's service definition.

    Args:
        config_hash: Hash of the rendered configuration file.

    Returns:
        The Pebble layer.
    """
    return {
        "summary": "Trino Gateway layer",
        "services": {
            SERVICE_NAME: {
                "override": "merge",
                # Pebble restarts the service on replan only when its definition changes.
                "environment": {"CONFIG_HASH": config_hash},
            }
        },
    }


def apply(container: ops.Container, config_text: str) -> None:
    """Write the configuration and replan, restarting the gateway only if it changed.

    Args:
        container: The workload container.
        config_text: The rendered configuration file content.
    """
    container.push(
        CONFIG_PATH,
        config_text,
        user=WORKLOAD_USER,
        group=WORKLOAD_USER,
        permissions=0o600,
        make_dirs=True,
    )
    config_hash = hashlib.sha256(config_text.encode()).hexdigest()
    container.add_layer(SERVICE_NAME, pebble_layer(config_hash), combine=True)
    container.replan()


def is_running(container: ops.Container) -> bool:
    """Report whether the gateway service is running.

    Args:
        container: The workload container.

    Returns:
        True if the Pebble service is active.
    """
    try:
        return container.get_service(SERVICE_NAME).is_running()
    except ops.ModelError:
        return False
