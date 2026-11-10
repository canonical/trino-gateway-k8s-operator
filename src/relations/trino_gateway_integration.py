# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""The `trino-gateway-integration` provider endpoint: users requested from upstream Trino.

The relation has no charm library; this module defines its shape. All fields are in the
application databags.

Gateway (provider) to upstream Trino (requirer):
    `requested_users`: JSON object mapping each requested username to the ID of a Juju secret
    granted to the relation. The secret holds `username` and `password`. Every user the gateway
    issued is requested; a user is withdrawn by removing its key, and its secret is removed.

Upstream Trino to gateway:
    `trino_url`: the bare `host:port` address the upstream also advertises on `trino-catalog`.
    `registered_users`: JSON list of the requested usernames the upstream registered.
    `refused_users`: JSON object mapping each requested username the upstream refused to a
    human-readable reason.
"""

import json
import logging
from collections.abc import Collection, Mapping
from typing import Any

import ops
import pydantic

import constants
from core import upstreams
from core.upstreams import UpstreamUsers

logger = logging.getLogger(__name__)


class _UpstreamAppData(pydantic.BaseModel):
    """The reply of an upstream Trino application."""

    trino_url: str | None = None
    registered_users: list[str] = []
    refused_users: dict[str, str] = {}


def _decode(value: str) -> Any:
    """Decode a databag value as JSON, falling back to the raw string as for `trino_url`."""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def publish(
    relation: ops.Relation, app: ops.Application, secrets_by_username: Mapping[str, ops.Secret]
) -> None:
    """Request every issued user from an upstream. Only the leader may call this.

    Args:
        relation: A `trino-gateway-integration` relation.
        app: The gateway application.
        secrets_by_username: The credentials secret of every issued user, by username.
    """
    for secret in secrets_by_username.values():
        # Granting fills in the ID of a secret that was looked up by label.
        secret.grant(relation)
    relation.data[app][constants.REQUESTED_USERS_KEY] = json.dumps(
        {username: secret.id for username, secret in secrets_by_username.items()},
        sort_keys=True,
    )


def load(relations: list[ops.Relation], requested: Collection[str]) -> list[UpstreamUsers]:
    """Read the reply of each upstream as its confirmation state.

    The requested set is passed in rather than read back from the relations, because only the
    leader can read the gateway's own application databag.

    Args:
        relations: The relations on the `trino-gateway-integration` endpoint.
        requested: The usernames the gateway requests from every upstream.

    Returns:
        The state of each relation whose remote application is known, sorted by relation ID.
        A relation with malformed data has no URL and confirms or refuses nothing.
    """
    result = []
    for relation in sorted(relations, key=lambda relation: relation.id):
        if relation.app is None:
            continue
        try:
            data = relation.load(_UpstreamAppData, relation.app, decoder=_decode)
        except ValueError as e:
            logger.warning(
                "Ignoring malformed trino-gateway-integration data in relation %r (%r): %s",
                relation.id,
                relation.app.name,
                e,
            )
            data = _UpstreamAppData()
        result.append(
            upstreams.build(
                relation_id=relation.id,
                remote_app=relation.app.name,
                raw_url=data.trino_url,
                requested=requested,
                registered=data.registered_users,
                refused=data.refused_users,
            )
        )
    return result
