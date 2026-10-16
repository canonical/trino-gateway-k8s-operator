# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Trino users the gateway issues to its `trino-catalog` consumers."""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

import constants

_GATEWAY_ID_LENGTH = 16
_CONSUMER_APP_LENGTH = 63


@dataclass(frozen=True)
class IssuedUser:
    """A Trino user issued to one consumer relation.

    Attributes:
        relation_id: ID of the `trino-catalog` relation the user belongs to.
        username: The issued username.
    """

    relation_id: int
    username: str


@dataclass(frozen=True)
class IssuancePlan:
    """Changes that make the issued users match the live consumer relations.

    Attributes:
        issue: Usernames to issue, by consumer relation ID.
        remove: IDs of relations whose users should be removed, sorted.
        users: Every user issued once the plan is applied, sorted by relation ID.
    """

    issue: dict[int, str]
    remove: list[int]
    users: list[IssuedUser]


def username(model_uuid: str, gateway_app: str, consumer_app: str, relation_id: int) -> str:
    """Derive the username for a consumer relation.

    The hash of the model UUID and gateway application name tells gateways apart, and the
    relation ID tells consumers of one gateway apart. The consumer name only aids readability.

    Args:
        model_uuid: UUID of the gateway's model.
        gateway_app: Name of the gateway application.
        consumer_app: Name of the consumer application.
        relation_id: ID of the consumer relation.

    Returns:
        A username of at most about 90 characters, starting with `gw-`.
    """
    gateway_id = hashlib.sha256(f"{model_uuid}/{gateway_app}".encode()).hexdigest()
    return (
        f"{constants.USERNAME_PREFIX}{gateway_id[:_GATEWAY_ID_LENGTH]}"
        f"-{consumer_app[:_CONSUMER_APP_LENGTH]}-{relation_id}"
    )


def plan_issuance(
    issued: Mapping[int, str],
    consumers: Mapping[int, str],
    *,
    model_uuid: str,
    gateway_app: str,
) -> IssuancePlan:
    """Diff the issued users against the live consumer relations.

    An issued username is kept for the relation's lifetime and never derived again.

    Args:
        issued: Usernames already issued, by consumer relation ID.
        consumers: Consumer application names, by relation ID.
        model_uuid: UUID of the gateway's model.
        gateway_app: Name of the gateway application.

    Returns:
        The users to issue and remove, and the resulting set of users.
    """
    issue = {
        relation_id: username(model_uuid, gateway_app, consumer_app, relation_id)
        for relation_id, consumer_app in consumers.items()
        if relation_id not in issued
    }
    remove = sorted(relation_id for relation_id in issued if relation_id not in consumers)
    usernames = {**issued, **issue}
    users = [IssuedUser(relation_id, usernames[relation_id]) for relation_id in sorted(consumers)]
    return IssuancePlan(issue=issue, remove=remove, users=users)
