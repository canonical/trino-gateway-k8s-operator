# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Registering upstreams and publishing `trino-catalog` data to consumers, end to end in a hook."""

import dataclasses
import json
from types import SimpleNamespace

import ops
import pytest
from conftest import FakeGateway
from ops import testing

import constants
from charm import TrinoGatewayK8SOperatorCharm
from core.users import username
from relations import trino_catalog

GATEWAY_APP = "trino-gateway-k8s"
MODEL = testing.Model(name="gw-model", uuid="f2b1c5a8-0d6e-4c1e-9a77-3e1b2c4d5e6f")
URL = f"{GATEWAY_APP}.{MODEL.name}.svc.cluster.local:8080"
PUBLISHED_KEYS = {"trino_url", "trino_catalogs", "trino_credentials_secret_id"}
ROCK_LAYER = ops.pebble.Layer(
    {
        "services": {
            constants.SERVICE_NAME: {
                "override": "replace",
                "command": "java -jar gateway.jar /etc/trino-gateway/config.yaml",
                "startup": "enabled",
            }
        }
    }
)


@pytest.fixture
def ctx() -> testing.Context:
    return testing.Context(TrinoGatewayK8SOperatorCharm)


@pytest.fixture(autouse=True)
def _fake_gateway(gateway: FakeGateway) -> FakeGateway:
    return gateway


def _consumer(app: str = "superset", app_name: str | None = None) -> testing.Relation:
    data = {"app_name": app_name or app}
    return testing.Relation(
        constants.TRINO_CATALOG_RELATION, remote_app_name=app, remote_app_data=data
    )


def _backend(app: str, catalogs: list[dict]) -> testing.Relation:
    return testing.Relation(
        constants.BACKEND_TRINO_CATALOG_RELATION,
        remote_app_name=app,
        remote_app_data={
            "trino_url": f"{app}:8080",
            "trino_catalogs": json.dumps(catalogs),
            "trino_credentials_secret_id": "secret:not-readable",
        },
    )


def _integration(app: str, registered=(), refused=None) -> testing.Relation:
    return testing.Relation(
        constants.TRINO_GATEWAY_INTEGRATION_RELATION,
        remote_app_name=app,
        remote_app_data={
            "trino_url": f"{app}:8080",
            "registered_users": json.dumps(sorted(registered)),
            "refused_users": json.dumps(refused or {}),
        },
    )


def _catalog(name: str, connector: str = "") -> dict:
    return {"name": name, "connector": connector, "description": ""}


def _state(*relations: testing.Relation, leader: bool = True, config=None) -> testing.State:
    """Build a state with a running workload and a ready database."""
    pg_secret = testing.Secret({"username": "gateway", "password": "pw"})
    postgresql = testing.Relation(
        constants.POSTGRESQL_RELATION,
        remote_app_data={
            "database": "trino_gateway",
            "endpoints": "pg-0:5432",
            "secret-user": pg_secret.id,
        },
    )
    container = testing.Container(
        constants.CONTAINER_NAME, can_connect=True, layers={"rock": ROCK_LAYER}
    )
    return testing.State(
        leader=leader,
        model=MODEL,
        config=config or {},
        containers={container},
        relations={postgresql, testing.PeerRelation(constants.PEER_RELATION), *relations},
        secrets={pg_secret},
    )


def _replace(state: testing.State, *relations: testing.Relation, **changes) -> testing.State:
    """Replace relations by ID, add new ones, and apply other changes to a state."""
    ids = {r.id for r in relations}
    kept = {r for r in state.relations if r.id not in ids}
    return dataclasses.replace(state, relations={*kept, *relations}, **changes)


def _username(relation: testing.Relation) -> str:
    return username(MODEL.uuid, GATEWAY_APP, relation.remote_app_name, relation.id)


def _published(state: testing.State, relation: testing.Relation) -> dict:
    return dict(state.get_relation(relation.id).local_app_data)


def _catalogs(state: testing.State, relation: testing.Relation) -> list[str]:
    return [c["name"] for c in json.loads(_published(state, relation)["trino_catalogs"])]


def _confirm(integration: testing.Relation, *consumers: testing.Relation, refused=None):
    data = {
        **integration.remote_app_data,
        "registered_users": json.dumps(sorted(_username(c) for c in consumers)),
        "refused_users": json.dumps(refused or {}),
    }
    return dataclasses.replace(integration, remote_app_data=data)


def _published_consumer(ctx, gateway, catalogs=None):
    """Run hooks until one consumer is published by one registered upstream."""
    consumer = _consumer()
    backend = _backend("trino-a", catalogs or [_catalog("hive"), _catalog("sales")])
    integration = _integration("trino-a")
    state = ctx.run(ctx.on.update_status(), _state(consumer, backend, integration))
    integration = _confirm(integration, consumer)
    state = ctx.run(ctx.on.relation_changed(integration), _replace(state, integration))
    assert set(gateway.backends) == {"trino-a"}
    assert set(_published(state, consumer)) == PUBLISHED_KEYS
    return state, consumer, backend, integration


def test_consumer_waits_until_the_registered_upstream_confirms_it(
    ctx: testing.Context, gateway: FakeGateway
):
    consumer = _consumer()
    backend, integration = _backend("trino-a", [_catalog("hive")]), _integration("trino-a")

    state = ctx.run(ctx.on.update_status(), _state(consumer, backend, integration))

    assert set(gateway.backends) == {"trino-a"}
    assert _published(state, consumer) == {}
    assert state.unit_status == testing.ActiveStatus("1 consumer waiting for publication")


def test_confirmed_consumer_receives_complete_data_with_its_own_secret(
    ctx: testing.Context, gateway: FakeGateway
):
    state, consumer, *_ = _published_consumer(ctx, gateway)

    data = _published(state, consumer)
    secret = state.get_secret(label=f"{constants.USER_SECRET_LABEL_PREFIX}{consumer.id}")
    assert data["trino_url"] == URL
    assert _catalogs(state, consumer) == ["hive", "sales"]
    assert data["trino_credentials_secret_id"] == secret.id
    assert secret.remote_grants[consumer.id] == {consumer.remote_app_name}
    assert secret.tracked_content["username"] == _username(consumer)
    assert state.unit_status == testing.ActiveStatus()


def test_without_upstreams_consumer_is_published_with_no_catalogs(ctx: testing.Context):
    consumer = _consumer()

    state = ctx.run(ctx.on.relation_changed(consumer), _state(consumer))

    assert set(_published(state, consumer)) == PUBLISHED_KEYS
    assert json.loads(_published(state, consumer)["trino_catalogs"]) == []


def test_consumer_without_app_name_is_not_published(ctx: testing.Context):
    consumer = testing.Relation(constants.TRINO_CATALOG_RELATION, remote_app_name="superset")

    state = ctx.run(ctx.on.relation_changed(consumer), _state(consumer))

    assert _published(state, consumer) == {}
    assert state.unit_status == testing.ActiveStatus(
        "no backends related; 1 consumer waiting for publication"
    )


def test_upstream_without_integration_is_never_registered(
    ctx: testing.Context, gateway: FakeGateway
):
    state, consumer, *_ = _published_consumer(ctx, gateway)
    unmatched = _backend("trino-c", [_catalog("hive")])

    state = ctx.run(ctx.on.relation_changed(unmatched), _replace(state, unmatched))

    assert set(gateway.backends) == {"trino-a"}
    assert state.unit_status == testing.ActiveStatus("1 upstream waiting for registration")


def test_new_upstream_waits_for_published_consumers_and_leaves_their_data_alone(
    ctx: testing.Context, gateway: FakeGateway
):
    state, consumer, *_ = _published_consumer(ctx, gateway)
    before = _published(state, consumer)
    backend_b = _backend("trino-b", [_catalog("hive"), _catalog("extra")])
    integration_b = _integration("trino-b")

    state = ctx.run(ctx.on.update_status(), _replace(state, backend_b, integration_b))

    assert set(gateway.backends) == {"trino-a"}
    assert _published(state, consumer) == before
    assert state.unit_status == testing.ActiveStatus("1 upstream waiting for registration")

    integration_b = _confirm(integration_b, consumer)
    state = ctx.run(ctx.on.relation_changed(integration_b), _replace(state, integration_b))

    assert set(gateway.backends) == {"trino-a", "trino-b"}
    assert _catalogs(state, consumer) == ["extra", "hive", "sales"]
    assert (
        _published(state, consumer)["trino_credentials_secret_id"]
        == (before["trino_credentials_secret_id"])
    )


def test_withdrawn_confirmation_keeps_upstream_while_it_confirms_another_consumer(
    ctx: testing.Context, gateway: FakeGateway
):
    state, superset, _, integration = _published_consumer(ctx, gateway)
    datahub = _consumer("datahub")
    state = ctx.run(ctx.on.relation_changed(datahub), _replace(state, datahub))
    integration = _confirm(integration, superset, datahub)
    state = ctx.run(ctx.on.relation_changed(integration), _replace(state, integration))
    assert _catalogs(state, datahub) == ["hive", "sales"]

    integration = _confirm(integration, datahub)
    state = ctx.run(ctx.on.relation_changed(integration), _replace(state, integration))

    assert set(gateway.backends) == {"trino-a"}
    assert _catalogs(state, superset) == []
    assert _catalogs(state, datahub) == ["hive", "sales"]
    assert state.unit_status == testing.ActiveStatus("1 confirmation gap")


def test_published_consumer_stays_published_when_its_only_upstream_withdraws(
    ctx: testing.Context, gateway: FakeGateway
):
    state, consumer, _, integration = _published_consumer(ctx, gateway)

    integration = _confirm(integration)
    state = ctx.run(ctx.on.relation_changed(integration), _replace(state, integration))

    assert gateway.backends == {}
    assert set(_published(state, consumer)) == PUBLISHED_KEYS
    assert _catalogs(state, consumer) == []


def test_catalog_changes_are_published(ctx: testing.Context, gateway: FakeGateway):
    state, consumer, backend, _ = _published_consumer(ctx, gateway)

    data = {**backend.remote_app_data, "trino_catalogs": json.dumps([_catalog("new")])}
    backend = dataclasses.replace(backend, remote_app_data=data)
    state = ctx.run(ctx.on.relation_changed(backend), _replace(state, backend))

    assert _catalogs(state, consumer) == ["new"]


def test_exclusions_hide_catalogs_by_app_name(ctx: testing.Context, gateway: FakeGateway):
    state, consumer, *_ = _published_consumer(ctx, gateway)

    state = ctx.run(
        ctx.on.config_changed(),
        _replace(state, config={"catalog-exclusions": "superset: [sales]\nother: [hive]"}),
    )

    assert _catalogs(state, consumer) == ["hive"]


def test_exclusions_use_the_published_app_name_not_the_remote_app(ctx: testing.Context):
    consumer = _consumer("superset-alias", app_name="superset")
    backend, integration = _backend("trino-a", [_catalog("hive")]), _integration("trino-a")
    state = _state(
        consumer, backend, integration, config={"catalog-exclusions": "superset: [hive]"}
    )
    state = ctx.run(ctx.on.update_status(), state)
    integration = _confirm(integration, consumer)

    state = ctx.run(ctx.on.relation_changed(integration), _replace(state, integration))

    assert _catalogs(state, consumer) == []


def test_invalid_exclusions_block_and_skip_publication_but_not_registration(
    ctx: testing.Context, gateway: FakeGateway
):
    state, consumer, *_ = _published_consumer(ctx, gateway)
    before = _published(state, consumer)
    backend_b, integration_b = _backend("trino-b", [_catalog("extra")]), _integration("trino-b")
    integration_b = _confirm(integration_b, consumer)
    newcomer = _consumer("datahub")

    state = ctx.run(
        ctx.on.config_changed(),
        _replace(
            state, backend_b, integration_b, newcomer, config={"catalog-exclusions": "a: [b"}
        ),
    )

    assert set(gateway.backends) == {"trino-a", "trino-b"}
    assert _published(state, consumer) == before
    assert _published(state, newcomer) == {}
    assert isinstance(state.unit_status, testing.BlockedStatus)
    assert state.unit_status.message.startswith("invalid catalog-exclusions: not valid YAML")


def test_follower_reports_invalid_exclusions_and_publishes_nothing(ctx: testing.Context):
    consumer = _consumer()

    state = ctx.run(
        ctx.on.config_changed(),
        _state(consumer, leader=False, config={"catalog-exclusions": "{"}),
    )

    assert _published(state, consumer) == {}
    assert isinstance(state.unit_status, testing.BlockedStatus)


def test_unreachable_gateway_publishes_nothing_and_omits_policy_counts(
    ctx: testing.Context, gateway: FakeGateway
):
    gateway.reachable = False
    consumer = _consumer()

    state = ctx.run(ctx.on.relation_changed(consumer), _state(consumer))

    assert _published(state, consumer) == {}
    assert state.unit_status == testing.ActiveStatus("no backends related")


def test_refusals_block_with_distinct_reasons(ctx: testing.Context, gateway: FakeGateway):
    state, consumer, _, integration = _published_consumer(ctx, gateway)
    datahub = _consumer("datahub")
    state = ctx.run(ctx.on.relation_changed(datahub), _replace(state, datahub))
    refused = {
        _username(consumer): "username already exists on this Trino",
        _username(datahub): "invalid username",
    }

    integration = _confirm(integration, refused=refused)
    state = ctx.run(ctx.on.relation_changed(integration), _replace(state, integration))

    assert state.unit_status == testing.BlockedStatus(
        "2 users refused by 1 upstream: invalid username; username already exists on this Trino"
    )


def test_catalog_conflict_holds_back_upstream_and_blocks(
    ctx: testing.Context, gateway: FakeGateway
):
    state, consumer, *_ = _published_consumer(ctx, gateway, [_catalog("sales", "postgresql")])
    backend_b = _backend("trino-b", [_catalog("sales", "mysql")])
    integration_b = _confirm(_integration("trino-b"), consumer)

    state = ctx.run(ctx.on.update_status(), _replace(state, backend_b, integration_b))

    assert set(gateway.backends) == {"trino-a"}
    assert state.unit_status == testing.BlockedStatus("1 catalog conflict between upstreams")


def test_follower_neither_registers_nor_publishes(ctx: testing.Context, gateway: FakeGateway):
    consumer = _consumer()
    backend, integration = _backend("trino-a", [_catalog("hive")]), _integration("trino-a")

    state = ctx.run(ctx.on.update_status(), _state(consumer, backend, integration, leader=False))

    assert gateway.backends == {}
    assert _published(state, consumer) == {}
    assert state.unit_status == testing.ActiveStatus()


def test_without_peer_relation_nothing_is_registered(ctx: testing.Context, gateway: FakeGateway):
    backend, integration = _backend("trino-a", [_catalog("hive")]), _integration("trino-a")
    state = _state(backend, integration)
    peer = state.get_relations(constants.PEER_RELATION)[0]
    state = dataclasses.replace(state, relations=set(state.relations) - {peer})

    ctx.run(ctx.on.update_status(), state)

    assert gateway.backends == {}


def test_app_name_is_none_without_remote_app():
    relation = SimpleNamespace(id=1, app=None)

    assert trino_catalog.app_name(relation) is None  # type: ignore[arg-type]
