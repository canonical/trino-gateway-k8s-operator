# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import re

from core.users import IssuedUser, plan_issuance, username

UUID = "f2b1c5a8-0d6e-4c1e-9a77-3e1b2c4d5e6f"


def test_username_shape():
    """A username is the gw- prefix, a 16-hex-digit gateway ID, the consumer and relation ID."""
    name = username(UUID, "trino-gateway-k8s", "superset", 7)

    assert re.fullmatch(r"gw-[0-9a-f]{16}-superset-7", name)
    assert not name.startswith("app-")
    assert ":" not in name


def test_username_is_deterministic():
    assert username(UUID, "gw", "superset", 7) == username(UUID, "gw", "superset", 7)


def test_username_differs_between_models_and_gateways():
    """Gateways in other models, or other gateways in the same model, issue other usernames."""
    names = {
        username(UUID, "gw", "superset", 7),
        username("00000000-0000-0000-0000-000000000000", "gw", "superset", 7),
        username(UUID, "gw-other", "superset", 7),
    }

    assert len(names) == 3


def test_username_differs_between_relations_of_one_gateway():
    """Relation IDs keep usernames apart even where consumer names and IDs share digits."""
    names = {
        username(UUID, "gw", "superset", 7),
        username(UUID, "gw", "superset", 8),
        username(UUID, "gw", "app-1", 23),
        username(UUID, "gw", "app-1-2", 3),
        username(UUID, "gw", "app", 12),
    }

    assert len(names) == 5


def test_username_truncation_keeps_uniqueness_and_bounds_length():
    """Consumer names are cut to 63 characters; the relation ID still tells users apart."""
    long_app = "a" * 100

    first = username(UUID, "gw", long_app, 1)
    second = username(UUID, "gw", long_app + "b", 2)

    assert first != second
    assert f"-{'a' * 63}-1" in first
    assert len(username(UUID, "gw", long_app, 2**31)) <= 100


def test_plan_issues_users_to_new_consumers():
    plan = plan_issuance({}, {3: "superset", 1: "datahub"}, model_uuid=UUID, gateway_app="gw")

    assert plan.issue == {
        3: username(UUID, "gw", "superset", 3),
        1: username(UUID, "gw", "datahub", 1),
    }
    assert plan.remove == []
    assert plan.users == [
        IssuedUser(1, username(UUID, "gw", "datahub", 1)),
        IssuedUser(3, username(UUID, "gw", "superset", 3)),
    ]


def test_plan_keeps_existing_usernames_without_deriving_them_again():
    """An issued username is kept even if the inputs it was derived from have changed."""
    plan = plan_issuance({1: "gw-old-name-1"}, {1: "renamed"}, model_uuid=UUID, gateway_app="gw")

    assert plan.issue == {}
    assert plan.remove == []
    assert plan.users == [IssuedUser(1, "gw-old-name-1")]


def test_plan_removes_users_of_departed_consumers():
    issued = {1: "gw-x-a-1", 2: "gw-x-b-2", 5: "gw-x-c-5"}

    plan = plan_issuance(issued, {2: "b"}, model_uuid=UUID, gateway_app="gw")

    assert plan.issue == {}
    assert plan.remove == [1, 5]
    assert plan.users == [IssuedUser(2, "gw-x-b-2")]


def test_plan_without_consumers_or_users_is_empty():
    plan = plan_issuance({}, {}, model_uuid=UUID, gateway_app="gw")

    assert (plan.issue, plan.remove, plan.users) == ({}, [], [])
