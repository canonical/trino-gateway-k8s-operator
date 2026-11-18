# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""The `trino-catalog` provider endpoint: consumers that get a gateway-issued Trino user.

Each consumer gets its own user. Once published, a consumer's relation holds the gateway's
in-cluster URL, its catalogs and the ID of its credentials secret, always written together
through the `trino_catalog` charm library. The consumer's `app_name` selects its catalog
exclusions.
"""

import secrets

import ops
from charms.trino_k8s.v0.trino_catalog import TrinoCatalog, TrinoCatalogProvider

import constants

_APP_NAME_KEY = "app_name"
_SECRET_ID_KEY = "trino_credentials_secret_id"  # nosec B105 - a databag key, not a password

_PASSWORD_BYTES = 32


def _label(relation_id: int) -> str:
    return f"{constants.USER_SECRET_LABEL_PREFIX}{relation_id}"


def consumers(relations: list[ops.Relation]) -> dict[int, str]:
    """List the live consumer relations.

    Relations whose remote application is not known yet are left out until it is.

    Args:
        relations: The relations on the `trino-catalog` endpoint.

    Returns:
        The consumer application names, by relation ID.
    """
    return {relation.id: relation.app.name for relation in relations if relation.app is not None}


def ensure_user_secret(model: ops.Model, relation_id: int, username: str) -> ops.Secret:
    """Get the credentials secret of a consumer relation, creating it if it does not exist.

    An existing secret is returned unchanged, so the password is stable across reconciles and
    leader changes. Only the leader may call this.

    Args:
        model: The charm's model.
        relation_id: ID of the consumer relation.
        username: Username to store when the secret is created.

    Returns:
        The app-owned secret holding `username` and `password`.
    """
    try:
        return model.get_secret(label=_label(relation_id))
    except ops.SecretNotFoundError:
        content = {"username": username, "password": secrets.token_urlsafe(_PASSWORD_BYTES)}
        return model.app.add_secret(content, label=_label(relation_id))


def remove_user_secret(model: ops.Model, relation_id: int) -> None:
    """Remove the credentials secret of a consumer relation, if it exists.

    Only the leader may call this.

    Args:
        model: The charm's model.
        relation_id: ID of the consumer relation.
    """
    try:
        model.get_secret(label=_label(relation_id)).remove_all_revisions()
    except ops.SecretNotFoundError:
        pass


def app_name(relation: ops.Relation) -> str | None:
    """Read the `app_name` a consumer published.

    Args:
        relation: A `trino-catalog` relation.

    Returns:
        The consumer's `app_name`, or None if it is missing or empty.
    """
    if relation.app is None:
        return None
    return relation.data[relation.app].get(_APP_NAME_KEY) or None


def is_published(relation: ops.Relation, app: ops.Application) -> bool:
    """Tell whether the gateway has written connection data to a consumer relation.

    Only meaningful on the leader, which alone can read the gateway's application databag.

    Args:
        relation: A `trino-catalog` relation.
        app: The gateway application.

    Returns:
        True if the relation holds a credentials secret ID.
    """
    return bool(relation.data[app].get(_SECRET_ID_KEY))


def gateway_url(app: str, model: str) -> str:
    """Build the in-cluster address consumers reach the gateway at.

    Args:
        app: Name of the gateway application.
        model: Name of the gateway's model.

    Returns:
        The `host:port` address of the gateway's Kubernetes Service.
    """
    return f"{app}.{model}.svc.cluster.local:{constants.HTTP_PORT}"


def publish(
    provider: TrinoCatalogProvider,
    relation: ops.Relation,
    secret: ops.Secret,
    url: str,
    catalogs: list[TrinoCatalog],
) -> None:
    """Write a consumer's complete connection data. Only the leader may call this.

    Args:
        provider: The library provider for the endpoint.
        relation: The consumer's `trino-catalog` relation.
        secret: The consumer's credentials secret, granted to its relation only.
        url: The gateway's `host:port` address.
        catalogs: The catalogs the consumer may see.
    """
    # Granting also fills in the ID of a secret that was looked up by label.
    secret.grant(relation)
    if secret.id is None:  # pragma: nocover - granting always sets the ID
        raise ValueError("secret has no ID")
    provider.update_relation_data(relation, url, catalogs, secret.id)
