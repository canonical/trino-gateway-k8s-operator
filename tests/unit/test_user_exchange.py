# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Issuing users to `trino-catalog` consumers and requesting them over the integration."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from ops import testing

import constants
import gateway_api
from charm import TrinoGatewayK8SOperatorCharm
from core.users import username
from relations import trino_catalog, trino_gateway_integration
from relations.peer import load_issued_users

GATEWAY_APP = "trino-gateway-k8s"
MODEL = testing.Model(name="gw-model", uuid="f2b1c5a8-0d6e-4c1e-9a77-3e1b2c4d5e6f")


@pytest.fixture
def ctx() -> testing.Context:
    return testing.Context(TrinoGatewayK8SOperatorCharm)


@pytest.fixture(autouse=True)
def sync_backends(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr(gateway_api, "sync_backends", mock)
    return mock


def _peer(issued: dict[int, str] | None = None) -> testing.PeerRelation:
    data = {} if issued is None else {constants.ISSUED_USERS_KEY: json.dumps(issued)}
    return testing.PeerRelation(constants.PEER_RELATION, local_app_data=data)


def _consumer(app: str = "superset") -> testing.Relation:
    return testing.Relation(constants.TRINO_CATALOG_RELATION, remote_app_name=app)


def _upstream(app: str = "trino", **data: str) -> testing.Relation:
    return testing.Relation(
        constants.TRINO_GATEWAY_INTEGRATION_RELATION, remote_app_name=app, remote_app_data=data
    )


def _state(*relations, leader: bool = True, secrets=(), can_connect: bool = False):
    """Build a state; without Pebble only the user exchange runs."""
    container = testing.Container(constants.CONTAINER_NAME, can_connect=can_connect)
    return testing.State(
        leader=leader,
        model=MODEL,
        containers={container},
        relations=set(relations),
        secrets=set(secrets),
    )


def _username(relation: testing.Relation) -> str:
    return username(MODEL.uuid, GATEWAY_APP, relation.remote_app_name, relation.id)


def _user_secret(state: testing.State, relation_id: int) -> testing.Secret:
    return state.get_secret(label=f"{constants.USER_SECRET_LABEL_PREFIX}{relation_id}")


def _index(state: testing.State) -> dict:
    raw = state.get_relations(constants.PEER_RELATION)[0].local_app_data
    return json.loads(raw[constants.ISSUED_USERS_KEY])


def _requested(state: testing.State, relation: testing.Relation) -> dict:
    data = state.get_relation(relation.id).local_app_data
    return json.loads(data[constants.REQUESTED_USERS_KEY])


def test_leader_issues_one_user_per_consumer(ctx: testing.Context):
    """Each consumer gets an app-owned secret with its username and a random password."""
    superset, datahub = _consumer("superset"), _consumer("datahub")

    state_out = ctx.run(ctx.on.relation_created(superset), _state(_peer(), superset, datahub))

    assert _index(state_out) == {
        str(superset.id): _username(superset),
        str(datahub.id): _username(datahub),
    }
    for relation in (superset, datahub):
        secret = _user_secret(state_out, relation.id)
        assert secret.owner == "app"
        assert secret.tracked_content["username"] == _username(relation)
        assert len(secret.tracked_content["password"]) >= 32
    passwords = {
        _user_secret(state_out, r.id).tracked_content["password"] for r in (superset, datahub)
    }
    assert len(passwords) == 2


def test_nothing_is_written_to_consumer_relations(ctx: testing.Context):
    consumer = _consumer()

    state_out = ctx.run(ctx.on.relation_created(consumer), _state(_peer(), consumer))

    relation_out = state_out.get_relation(consumer.id)
    assert relation_out.local_app_data == {}
    assert _user_secret(state_out, consumer.id).remote_grants == {}


def test_users_are_stable_across_reconciles(ctx: testing.Context):
    consumer = _consumer()
    first = ctx.run(ctx.on.relation_created(consumer), _state(_peer(), consumer))

    second = ctx.run(ctx.on.update_status(), first)

    assert _index(second) == _index(first)
    assert _user_secret(second, consumer.id) == _user_secret(first, consumer.id)
    assert len(list(second.secrets)) == 1


def test_new_leader_reuses_existing_user(ctx: testing.Context):
    """A new leader finds the secret by label and keeps the username and password."""
    consumer = _consumer()
    secret = testing.Secret(
        {"username": "gw-existing-superset-1", "password": "kept"},
        owner="app",
        label=f"{constants.USER_SECRET_LABEL_PREFIX}{consumer.id}",
    )
    state = _state(_peer({consumer.id: "gw-existing-superset-1"}), consumer, secrets=[secret])

    state_out = ctx.run(ctx.on.leader_elected(), state)

    assert _index(state_out) == {str(consumer.id): "gw-existing-superset-1"}
    assert len(list(state_out.secrets)) == 1
    assert _user_secret(state_out, consumer.id).tracked_content["password"] == "kept"


def test_lost_secret_is_recreated_under_the_same_username(ctx: testing.Context):
    consumer = _consumer()
    state = _state(_peer({consumer.id: "gw-existing-superset-1"}), consumer)

    state_out = ctx.run(ctx.on.update_status(), state)

    secret = _user_secret(state_out, consumer.id)
    assert secret.tracked_content["username"] == "gw-existing-superset-1"
    assert secret.tracked_content["password"]


def test_removed_consumer_loses_its_user_everywhere(ctx: testing.Context):
    """Breaking a consumer relation removes its secret, index entry and request."""
    kept, removed = _consumer("superset"), _consumer("datahub")
    upstream = _upstream()
    before = ctx.run(ctx.on.relation_created(kept), _state(_peer(), kept, removed, upstream))
    kept_secret = _user_secret(before, kept.id)

    state_out = ctx.run(ctx.on.relation_broken(before.get_relation(removed.id)), before)

    assert _index(state_out) == {str(kept.id): _username(kept)}
    assert [s.label for s in state_out.secrets] == [kept_secret.label]
    assert _requested(state_out, upstream) == {_username(kept): kept_secret.id}


def test_users_of_consumers_gone_while_not_leader_are_removed(ctx: testing.Context):
    """The index lets a leader clean up users whose relation broke under another leader."""
    secret = testing.Secret(
        {"username": "gw-x-old-9", "password": "pw"},
        owner="app",
        label=f"{constants.USER_SECRET_LABEL_PREFIX}9",
    )

    state_out = ctx.run(
        ctx.on.leader_elected(), _state(_peer({9: "gw-x-old-9"}), secrets=[secret])
    )

    assert _index(state_out) == {}
    assert not state_out.secrets


def test_every_user_is_requested_from_every_upstream(ctx: testing.Context):
    consumers = [_consumer("superset"), _consumer("datahub")]
    upstreams = [_upstream("trino-a"), _upstream("trino-b")]

    state_out = ctx.run(ctx.on.update_status(), _state(_peer(), *consumers, *upstreams))

    secrets = {_username(c): _user_secret(state_out, c.id) for c in consumers}
    expected = {name: secret.id for name, secret in secrets.items()}
    for upstream in upstreams:
        assert _requested(state_out, upstream) == expected
    for upstream, secret in [(u, s) for u in upstreams for s in secrets.values()]:
        assert secret.remote_grants[upstream.id] == {upstream.remote_app_name}


def test_requested_users_never_contain_passwords(ctx: testing.Context):
    upstream = _upstream()
    state_out = ctx.run(ctx.on.update_status(), _state(_peer(), _consumer(), upstream))

    password = next(iter(state_out.secrets)).tracked_content["password"]
    assert password not in json.dumps(state_out.get_relation(upstream.id).local_app_data)
    assert password not in json.dumps(_index(state_out))


def test_new_upstream_gets_existing_users(ctx: testing.Context):
    consumer = _consumer()
    before = ctx.run(ctx.on.update_status(), _state(_peer(), consumer))
    upstream = _upstream()
    state = testing.State(
        leader=True,
        model=MODEL,
        containers=before.containers,
        relations={*before.relations, upstream},
        secrets=before.secrets,
    )

    state_out = ctx.run(ctx.on.relation_created(upstream), state)

    secret = _user_secret(state_out, consumer.id)
    assert _requested(state_out, upstream) == {_username(consumer): secret.id}
    assert upstream.id in secret.remote_grants


def test_no_consumers_request_nothing(ctx: testing.Context):
    upstream = _upstream()

    state_out = ctx.run(ctx.on.relation_created(upstream), _state(_peer(), upstream))

    assert _requested(state_out, upstream) == {}


def test_broken_upstream_leaves_other_requests_alone(ctx: testing.Context):
    consumer, kept, removed = _consumer(), _upstream("trino-a"), _upstream("trino-b")
    before = ctx.run(ctx.on.update_status(), _state(_peer(), consumer, kept, removed))

    state_out = ctx.run(ctx.on.relation_broken(before.get_relation(removed.id)), before)

    secret = _user_secret(state_out, consumer.id)
    assert _requested(state_out, kept) == {_username(consumer): secret.id}
    assert _index(state_out) == _index(before)


def test_follower_issues_nothing(ctx: testing.Context):
    consumer, upstream = _consumer(), _upstream()

    state_out = ctx.run(ctx.on.update_status(), _state(_peer(), consumer, upstream, leader=False))

    assert not state_out.secrets
    assert state_out.get_relations(constants.PEER_RELATION)[0].local_app_data == {}
    assert state_out.get_relation(upstream.id).local_app_data == {}


def test_without_peer_relation_nothing_is_issued(ctx: testing.Context):
    consumer, upstream = _consumer(), _upstream()

    state_out = ctx.run(ctx.on.relation_created(consumer), _state(consumer, upstream))

    assert not state_out.secrets
    assert state_out.get_relation(upstream.id).local_app_data == {}


def test_malformed_index_is_left_alone(ctx: testing.Context, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING)
    peer = testing.PeerRelation(
        constants.PEER_RELATION, local_app_data={constants.ISSUED_USERS_KEY: "not json"}
    )
    upstream = _upstream()

    state_out = ctx.run(ctx.on.update_status(), _state(peer, _consumer(), upstream))

    assert not state_out.secrets
    assert state_out.get_relations(constants.PEER_RELATION)[0].local_app_data == {
        constants.ISSUED_USERS_KEY: "not json"
    }
    assert state_out.get_relation(upstream.id).local_app_data == {}
    assert "issued-user index is malformed" in caplog.text


def test_users_are_issued_with_a_ready_workload(ctx: testing.Context, sync_backends: MagicMock):
    """The user exchange does not get in the way of the workload or backend sync."""
    secret = testing.Secret({"username": "gateway", "password": "pw"})
    postgresql = testing.Relation(
        constants.POSTGRESQL_RELATION,
        remote_app_data={
            "database": "trino_gateway",
            "endpoints": "pg-0:5432",
            "secret-user": secret.id,
        },
    )
    consumer = _consumer()
    state = _state(_peer(), consumer, postgresql, secrets=[secret], can_connect=True)

    state_out = ctx.run(ctx.on.relation_created(consumer), state)

    assert _index(state_out) == {str(consumer.id): _username(consumer)}
    sync_backends.assert_called_once()


def test_consumers_skip_relations_without_remote_app():
    relations = [
        SimpleNamespace(id=1, app=SimpleNamespace(name="superset")),
        SimpleNamespace(id=2, app=None),
    ]

    assert trino_catalog.consumers(relations) == {1: "superset"}  # type: ignore[arg-type]


def test_peer_index_defaults_to_empty(ctx: testing.Context):
    peer = _peer()
    with ctx(ctx.on.update_status(), _state(peer, leader=False)) as manager:
        relation = manager.charm.model.get_relation(constants.PEER_RELATION)
        assert relation is not None
        assert load_issued_users(relation) == {}


def _load_upstreams(ctx: testing.Context, relations, requested, leader: bool = False):
    """Load the upstream state as a unit would during a hook."""
    with ctx(ctx.on.update_status(), _state(*relations, leader=leader)) as manager:
        model_relations = manager.charm.model.relations[
            constants.TRINO_GATEWAY_INTEGRATION_RELATION
        ]
        return trino_gateway_integration.load(model_relations, requested)


def test_load_reads_confirmations_and_refusals_with_reasons(ctx: testing.Context):
    """A follower reads each upstream's reply into its confirmation state."""
    upstream = _upstream(
        "trino-a",
        trino_url="trino-a:8080",
        registered_users=json.dumps(["gw-a", "gw-b"]),
        refused_users=json.dumps({"gw-c": "username belongs to another user"}),
    )

    [state] = _load_upstreams(ctx, [upstream], {"gw-a", "gw-b", "gw-c"})

    assert state.relation_id == upstream.id
    assert state.remote_app == "trino-a"
    assert state.url == "http://trino-a:8080"
    assert state.confirmed == {"gw-a", "gw-b"}
    assert dict(state.refused) == {"gw-c": "username belongs to another user"}


def test_load_tracks_each_upstream_separately(ctx: testing.Context):
    upstreams = [
        _upstream("trino-a", trino_url="trino-a:8080", registered_users='["gw-a"]'),
        _upstream("trino-b", trino_url="trino-b:8080", refused_users='{"gw-a": "taken"}'),
        _upstream("trino-c"),
    ]

    result = _load_upstreams(ctx, upstreams, {"gw-a"})

    assert [(u.remote_app, u.url, u.confirmed, dict(u.refused)) for u in result] == [
        ("trino-a", "http://trino-a:8080", {"gw-a"}, {}),
        ("trino-b", "http://trino-b:8080", frozenset(), {"gw-a": "taken"}),
        ("trino-c", None, frozenset(), {}),
    ]


@pytest.mark.parametrize(
    "data",
    [
        {"trino_url": "trino:8080", "registered_users": "not json"},
        {"trino_url": "trino:8080", "registered_users": '{"gw-a": true}'},
        {"trino_url": "trino:8080", "refused_users": '["gw-a"]'},
        {"trino_url": "8080", "registered_users": '["gw-a"]'},
    ],
)
def test_load_degrades_malformed_data_to_nothing_confirmed(
    ctx: testing.Context, caplog: pytest.LogCaptureFixture, data: dict
):
    caplog.set_level(logging.WARNING)

    [state] = _load_upstreams(ctx, [_upstream(**data)], {"gw-a"})

    assert (state.url, state.confirmed, dict(state.refused)) == (None, frozenset(), {})
    assert "malformed" in caplog.text


def test_departed_consumer_without_secret_is_dropped_from_index(ctx: testing.Context):
    state_out = ctx.run(ctx.on.update_status(), _state(_peer({9: "gw-x-old-9"})))

    assert _index(state_out) == {}


def test_load_skips_relations_without_remote_app():
    relation = SimpleNamespace(id=1, app=None)

    assert trino_gateway_integration.load([relation], {"gw-a"}) == []  # type: ignore[list-item]
