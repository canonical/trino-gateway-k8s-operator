# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Trino Gateway workload: configuration rendering and Pebble management."""

import dataclasses
import hashlib
import json
from collections.abc import Callable
from typing import Any

import ops
import pydantic
import yaml

import constants
from relations.oauth import OAuthClient


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


@dataclasses.dataclass(frozen=True)
class OAuthLogin:
    """The OAuth login of users to the gateway's web UI.

    Attributes:
        client: The OAuth client the provider registered.
        redirect_uri: The gateway's OIDC callback URL registered with the client.
    """

    client: OAuthClient
    redirect_uri: str


@dataclasses.dataclass(frozen=True)
class GatewayAuth:
    """The authentication of the gateway's web UI and REST API.

    Attributes:
        login: The OAuth login of users, or None to let no user log in.
    """

    login: OAuthLogin | None = None


def render_config(pg: PostgresRelationModel, auth: GatewayAuth | None = None) -> str:
    """Render the Trino Gateway configuration file.

    Args:
        pg: PostgreSQL connection details.
        auth: The authentication to configure, or None to leave the UI and REST API open.

    Returns:
        The configuration file content.
    """
    config = {
        "serverConfig": {
            "node.environment": constants.NODE_ENVIRONMENT,
            "http-server.http.port": constants.HTTP_PORT,
            # Without it the gateway refuses requests carrying the ingress's X-Forwarded-*
            # headers. With it, the gateway passes the client's scheme, host and port on to
            # the backends, which build `nextUri` from them.
            "http-server.process-forwarded": True,
        },
        "dataStore": {
            "jdbcUrl": f"jdbc:postgresql://{pg.endpoints}/{pg.database}",
            "user": pg.username,
            "password": pg.password,
            "driver": "org.postgresql.Driver",
        },
        "clusterStatsConfiguration": {"monitorType": "INFO_API"},
        "routing": {"defaultRoutingGroup": constants.DEFAULT_ROUTING_GROUP},
    }
    if auth is not None:
        config.update(_render_auth(auth))
    return yaml.safe_dump(config, sort_keys=False)


def _render_auth(auth: GatewayAuth) -> dict[str, Any]:
    """Render the authentication and authorization sections.

    The charm authenticates with a JWT and holds only the API role. OAuth users who log in get
    the USER role, which reads the web UI but does not edit it or call the REST API.
    """
    jwt = {
        "hmacSecretFile": constants.API_KEY_PATH,
        "requiredIssuer": constants.API_JWT_ISSUER,
        "requiredAudience": constants.API_JWT_AUDIENCE,
    }
    authentication: dict[str, Any] = {"defaultType": ["jwt"], "jwt": jwt}
    if auth.login is not None:
        client = auth.login.client
        authentication = {
            "defaultType": ["oauth", "jwt"],
            # The login page offers OAuth only; the API still accepts the charm's JWT.
            "showFirstTypeOnly": True,
            "oauth": {
                "issuer": client.issuer,
                "clientId": client.client_id,
                "clientSecret": client.client_secret,
                "authorizationEndpoint": client.authorization_endpoint,
                "tokenEndpoint": client.token_endpoint,
                "jwkEndpoint": client.jwks_endpoint,
                "redirectUrl": auth.login.redirect_uri,
                "userIdField": constants.OAUTH_USER_ID_CLAIM,
                "scopes": list(constants.OAUTH_SCOPES),
                # Without it, the gateway accepts ID tokens the provider issued to any client.
                "audiences": [client.client_id],
            },
            "jwt": jwt,
        }
    return {
        "authentication": authentication,
        "authorization": {
            "admin": "(.*)ADMIN(.*)",
            "user": "(.*)USER(.*)",
            "api": "(.*)API(.*)",
            "enableDefaultPrivilege": True,
            "defaultPrivilege": "USER",
        },
        "presetUsers": {constants.API_USER: {"privileges": "API"}},
    }


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
            constants.SERVICE_NAME: {
                "override": "merge",
                # Pebble restarts the service on replan only when its definition changes.
                "environment": {"CONFIG_HASH": config_hash},
            }
        },
    }


def apply(container: ops.Container, config_text: str, api_key: str | None = None) -> None:
    """Write the configuration and replan, restarting the gateway only if it changed.

    Args:
        container: The workload container.
        config_text: The rendered configuration file content.
        api_key: The key the gateway verifies the charm's API tokens with, if there is one.
    """
    _push_private(container, constants.CONFIG_PATH, config_text)
    digest = hashlib.sha256(config_text.encode())
    if api_key is not None:
        _push_private(container, constants.API_KEY_PATH, api_key)
        # The gateway reads the key only at startup.
        digest.update(b"\0" + api_key.encode())
    container.add_layer(constants.SERVICE_NAME, pebble_layer(digest.hexdigest()), combine=True)
    container.replan()


def _push_private(container: ops.Container, path: str, content: str) -> None:
    """Write a file only the workload user can read."""
    container.push(
        path,
        content,
        user=constants.WORKLOAD_USER,
        group=constants.WORKLOAD_USER,
        permissions=0o600,
        make_dirs=True,
    )


def is_running(container: ops.Container) -> bool:
    """Report whether the gateway service is running.

    Args:
        container: The workload container.

    Returns:
        True if the Pebble service is active.
    """
    try:
        return container.get_service(constants.SERVICE_NAME).is_running()
    except ops.ModelError:
        return False
