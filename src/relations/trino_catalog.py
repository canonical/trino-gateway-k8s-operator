# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""The `trino-catalog` provider endpoint: consumers that get a gateway-issued Trino user.

Nothing is written to consumer relations yet; only their existence and application name are
used.
"""

import secrets

import ops

import constants

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
