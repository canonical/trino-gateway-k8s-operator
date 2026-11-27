# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""The `oauth` requirer endpoint: the OAuth client for logins to the gateway's web UI.

The endpoint serves the gateway's own login only. Logins to the Trino backends through the
gateway would need a client of their own, shared by all backends.
"""

import dataclasses
import logging

import ops
from charms.hydra.v0.oauth import ClientConfig, DataValidationError, OAuthRequirer

import constants
from relations.ingress import IngressAddress

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class OAuthClient:
    """The OAuth client the provider registered for the gateway.

    Attributes:
        issuer: The provider's issuer URL.
        client_id: The client ID.
        client_secret: The client secret, from the latest revision of its Juju secret.
        authorization_endpoint: Where users are sent to log in.
        token_endpoint: Where the gateway exchanges the authorization code for tokens.
        jwks_endpoint: Where the gateway reads the keys that sign ID tokens.
    """

    issuer: str
    client_id: str
    client_secret: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_endpoint: str


def redirect_uri(address: IngressAddress) -> str:
    """Build the gateway's OIDC callback URL at an ingress address."""
    return f"{address.url}{constants.OIDC_CALLBACK_PATH}"


def client_config(address: IngressAddress) -> ClientConfig:
    """Build the client configuration to register for an ingress address.

    Args:
        address: The gateway's HTTPS ingress address.

    Returns:
        The validated client configuration.

    Raises:
        ClientConfigError: if the library rejects the configuration, such as the redirect URI.
    """
    config = ClientConfig(
        redirect_uri=redirect_uri(address),
        scope=" ".join(constants.OAUTH_SCOPES),
        grant_types=["authorization_code"],
    )
    config.validate()
    return config


def publish(requirer: OAuthRequirer, address: IngressAddress) -> None:
    """On the leader, register or update the client for an ingress address.

    Args:
        requirer: The library requirer for the endpoint.
        address: The gateway's HTTPS ingress address.

    Raises:
        ClientConfigError: if the library rejects the configuration.
    """
    requirer.update_client_config(client_config(address))


def load(requirer: OAuthRequirer) -> OAuthClient | None:
    """Read the registered client.

    Args:
        requirer: The library requirer for the endpoint.

    Returns:
        The client, or None if the provider has not registered it yet, its data is invalid,
        or its secret cannot be read yet.
    """
    if not requirer.is_client_created():
        return None
    try:
        provider = requirer.get_provider_info()
    except DataValidationError as e:
        logger.warning("Ignoring the invalid OAuth provider data: %s", e)
        return None
    except (ops.ModelError, KeyError) as e:
        logger.info("The OAuth client secret cannot be read yet: %s", e)
        return None
    if provider is None or not provider.client_id or not provider.client_secret:
        return None
    return OAuthClient(
        issuer=provider.issuer_url,
        client_id=provider.client_id,
        client_secret=provider.client_secret,
        authorization_endpoint=provider.authorization_endpoint,
        token_endpoint=provider.token_endpoint,
        jwks_endpoint=provider.jwks_endpoint,
    )
