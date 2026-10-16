# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""The `peer` relation: application state shared by all gateway units."""

import json
from collections.abc import Mapping

import ops
import pydantic

import constants
from exceptions import MalformedIssuedUsersError


class _PeerAppData(pydantic.BaseModel):
    """The parts of the peer application databag this charm reads."""

    issued_users: dict[int, str] = pydantic.Field(
        default_factory=dict, alias=constants.ISSUED_USERS_KEY
    )


def load_issued_users(relation: ops.Relation) -> dict[int, str]:
    """Read the usernames issued to consumers, by `trino-catalog` relation ID.

    Args:
        relation: The peer relation.

    Returns:
        The issued usernames, empty if none were recorded yet.

    Raises:
        MalformedIssuedUsersError: if the recorded index cannot be parsed.
    """
    try:
        return relation.load(_PeerAppData, relation.app).issued_users
    except ValueError as e:
        raise MalformedIssuedUsersError(str(e)) from e


def save_issued_users(relation: ops.Relation, app: ops.Application, issued: Mapping[int, str]):
    """Record the usernames issued to consumers. Only the leader may call this.

    Args:
        relation: The peer relation.
        app: The gateway application.
        issued: The issued usernames, by `trino-catalog` relation ID.
    """
    relation.data[app][constants.ISSUED_USERS_KEY] = json.dumps(
        {str(relation_id): name for relation_id, name in issued.items()}, sort_keys=True
    )
