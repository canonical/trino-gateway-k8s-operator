# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import dataclasses
import json
from unittest.mock import MagicMock

import ops
import pytest
from ops import testing

import constants
import gateway_api
from charm import TrinoGatewayK8SOperatorCharm

USER = {"username": "gateway", "password": "s3cr3t-pw"}
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


def _container(**kwargs) -> testing.Container:
    return testing.Container(
        constants.CONTAINER_NAME, can_connect=True, layers={"rock": ROCK_LAYER}, **kwargs
    )


def _secret(**kwargs) -> testing.Secret:
    kwargs.setdefault("tracked_content", USER)
    return testing.Secret(**kwargs)


def _relation(secret: testing.Secret, endpoints: str = "pg-0:5432") -> testing.Relation:
    return testing.Relation(
        "postgresql",
        remote_app_data={
            "database": "trino_gateway",
            "endpoints": endpoints,
            "secret-user": secret.id,
        },
    )


def _config_hash(state: testing.State) -> str:
    plan = state.get_container(constants.CONTAINER_NAME).plan
    return plan.services[constants.SERVICE_NAME].environment["CONFIG_HASH"]


def test_no_relation_blocks_and_leaves_workload_untouched(ctx: testing.Context):
    container = _container()
    state_out = ctx.run(ctx.on.pebble_ready(container), testing.State(containers={container}))

    assert state_out.unit_status == testing.BlockedStatus("missing required relation: postgresql")
    container_out = state_out.get_container(constants.CONTAINER_NAME)
    assert set(container_out.layers) == {"rock"}
    assert constants.SERVICE_NAME not in container_out.service_statuses
    config_path = container_out.get_filesystem(ctx) / constants.CONFIG_PATH.lstrip("/")
    assert not config_path.exists()
    assert not state_out.opened_ports


def test_container_unreachable_reports_maintenance(ctx: testing.Context):
    secret = _secret()
    container = testing.Container(constants.CONTAINER_NAME, can_connect=False)
    state = testing.State(containers={container}, relations={_relation(secret)}, secrets={secret})

    state_out = ctx.run(ctx.on.update_status(), state)

    assert state_out.unit_status == testing.MaintenanceStatus("waiting for Pebble")


def test_relation_without_data_waits(ctx: testing.Context):
    container = _container()
    relation = testing.Relation("postgresql")
    state = testing.State(containers={container}, relations={relation})

    state_out = ctx.run(ctx.on.relation_changed(relation), state)

    assert state_out.unit_status == testing.WaitingStatus("waiting for postgresql database")
    assert set(state_out.get_container(constants.CONTAINER_NAME).layers) == {"rock"}


def test_relation_with_unreadable_secret_waits(ctx: testing.Context):
    container = _container()
    relation = testing.Relation(
        "postgresql",
        remote_app_data={
            "database": "trino_gateway",
            "endpoints": "pg-0:5432",
            "secret-user": "secret:missing",
        },
    )
    state = testing.State(containers={container}, relations={relation})

    state_out = ctx.run(ctx.on.update_status(), state)

    assert state_out.unit_status == testing.WaitingStatus("waiting for postgresql database")


def test_relation_with_ungranted_secret_waits(ctx: testing.Context, monkeypatch):
    secret = _secret()
    state = testing.State(
        containers={_container()}, relations={_relation(secret)}, secrets={secret}
    )

    def deny(*args, **kwargs):
        raise ops.ModelError("permission denied")

    monkeypatch.setattr(ops.Model, "get_secret", deny)
    state_out = ctx.run(ctx.on.update_status(), state)

    assert state_out.unit_status == testing.WaitingStatus("waiting for postgresql database")


def test_relation_ready_starts_gateway(ctx: testing.Context):
    """A ready database relation starts the gateway and opens its port."""
    secret = _secret()
    relation = _relation(secret)
    state = testing.State(containers={_container()}, relations={relation}, secrets={secret})

    state_out = ctx.run(ctx.on.relation_changed(relation), state)

    assert state_out.unit_status == testing.ActiveStatus("no backends related")
    container_out = state_out.get_container(constants.CONTAINER_NAME)
    service_status = container_out.service_statuses[constants.SERVICE_NAME]
    assert service_status == ops.pebble.ServiceStatus.ACTIVE
    assert state_out.opened_ports == {testing.TCPPort(constants.HTTP_PORT)}
    config_path = container_out.get_filesystem(ctx) / constants.CONFIG_PATH.lstrip("/")
    config = config_path.read_text()
    assert "jdbc:postgresql://pg-0:5432/trino_gateway" in config
    assert USER["password"] not in str(container_out.plan.to_dict())


def test_service_not_running_reports_maintenance(ctx: testing.Context):
    secret = _secret()
    state = testing.State(
        containers={_container()}, relations={_relation(secret)}, secrets={secret}
    )

    state_out = ctx.run(ctx.on.start(), state)

    assert state_out.unit_status == testing.MaintenanceStatus("gateway service is not running")


def test_relation_broken_blocks_and_keeps_service_running(ctx: testing.Context):
    secret = _secret()
    relation = _relation(secret)
    container = _container(
        service_statuses={constants.SERVICE_NAME: ops.pebble.ServiceStatus.ACTIVE}
    )
    state = testing.State(containers={container}, relations={relation}, secrets={secret})

    state_out = ctx.run(ctx.on.relation_broken(relation), state)

    assert state_out.unit_status == testing.BlockedStatus("missing required relation: postgresql")
    container_out = state_out.get_container(constants.CONTAINER_NAME)
    service_status = container_out.service_statuses[constants.SERVICE_NAME]
    assert service_status == ops.pebble.ServiceStatus.ACTIVE


def test_config_hash_is_stable_without_changes(ctx: testing.Context):
    secret = _secret()
    state = testing.State(
        containers={_container()}, relations={_relation(secret)}, secrets={secret}
    )

    first = ctx.run(ctx.on.update_status(), state)
    second = ctx.run(ctx.on.update_status(), first)

    assert _config_hash(first) == _config_hash(second)


def test_config_hash_changes_on_secret_rotation(ctx: testing.Context):
    secret = _secret()
    before = ctx.run(
        ctx.on.update_status(),
        testing.State(containers={_container()}, relations={_relation(secret)}, secrets={secret}),
    )

    rotated = _secret(id=secret.id, latest_content={**USER, "password": "rotated"})
    after = ctx.run(
        ctx.on.secret_changed(rotated),
        testing.State(
            containers={_container()}, relations={_relation(rotated)}, secrets={rotated}
        ),
    )

    assert _config_hash(before) != _config_hash(after)


def test_config_hash_changes_on_new_endpoint(ctx: testing.Context):
    secret = _secret()
    before = ctx.run(
        ctx.on.update_status(),
        testing.State(containers={_container()}, relations={_relation(secret)}, secrets={secret}),
    )

    moved = _relation(secret, endpoints="pg-1:5432")
    after = ctx.run(
        ctx.on.relation_changed(moved),
        testing.State(containers={_container()}, relations={moved}, secrets={secret}),
    )

    assert _config_hash(before) != _config_hash(after)


@pytest.fixture
def sync_backends(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr(gateway_api, "sync_backends", mock)
    return mock


def _ready_state(leader: bool = True) -> testing.State:
    secret = _secret()
    return testing.State(
        leader=leader,
        containers={_container()},
        relations={_relation(secret)},
        secrets={secret},
    )


def _synced(sync_backends: MagicMock) -> list[tuple[str, str]]:
    """List the name and URL of each backend the charm last asked to sync."""
    return [(b.name, b.url) for b in sync_backends.call_args.args[1]]


def _trino_relation(app: str = "trino-b", url: str = "trino-b:8080", **data) -> testing.Relation:
    """Build a `backend-trino-catalog` relation with complete provider data."""
    remote_app_data = {
        "trino_url": url,
        "trino_catalogs": json.dumps([{"name": "hive", "connector": "", "description": ""}]),
        "trino_credentials_secret_id": "secret:not-readable",
        **data,
    }
    return testing.Relation(
        constants.BACKEND_TRINO_CATALOG_RELATION,
        remote_app_name=app,
        remote_app_data=remote_app_data,
    )


def _state_with(*relations: testing.Relation, leader: bool = True):
    """Build a ready state with extra relations added."""
    state = _ready_state(leader=leader)
    return dataclasses.replace(state, relations={*state.relations, *relations})


def test_leader_syncs_related_backends(ctx: testing.Context, sync_backends: MagicMock):
    """The leader syncs one backend per related Trino application."""
    relations = (_trino_relation("trino-a", "trino-a:8080"), _trino_relation())

    state_out = ctx.run(ctx.on.config_changed(), _state_with(*relations))

    assert state_out.unit_status == testing.ActiveStatus()
    sync_backends.assert_called_once()
    assert _synced(sync_backends) == [
        ("trino-a", "http://trino-a:8080"),
        ("trino-b", "http://trino-b:8080"),
    ]


def test_https_url_for_port_443(ctx: testing.Context, sync_backends: MagicMock):
    """A related backend on port 443 is synced with an https URL."""
    state = _state_with(_trino_relation(url="trino.example.com:443"))

    state_out = ctx.run(ctx.on.config_changed(), state)

    assert state_out.unit_status == testing.ActiveStatus()
    assert _synced(sync_backends) == [("trino-b", "https://trino.example.com:443")]


def test_no_relations_report_no_backends_related(ctx: testing.Context, sync_backends: MagicMock):
    """Without relations the unit is active, says so and syncs an empty backend list."""
    state_out = ctx.run(ctx.on.config_changed(), _ready_state())

    assert state_out.unit_status == testing.ActiveStatus("no backends related")
    assert _synced(sync_backends) == []


def test_sync_errors_are_swallowed(ctx: testing.Context, sync_backends: MagicMock):
    sync_backends.side_effect = gateway_api.GatewayApiError("connection refused")

    state_out = ctx.run(ctx.on.update_status(), _state_with(_trino_relation()))

    assert state_out.unit_status == testing.ActiveStatus()


def test_non_leader_does_not_sync(ctx: testing.Context, sync_backends: MagicMock):
    ctx.run(ctx.on.config_changed(), _state_with(_trino_relation(), leader=False))

    sync_backends.assert_not_called()


def test_follower_reports_same_status_without_syncing(
    ctx: testing.Context, sync_backends: MagicMock
):
    """A follower reports the status the leader would, but writes nothing."""
    state_out = ctx.run(
        ctx.on.config_changed(), _state_with(_trino_relation(url="no-port"), leader=False)
    )

    assert state_out.unit_status == testing.ActiveStatus(
        "1 backend-trino-catalog relation not ready"
    )
    sync_backends.assert_not_called()


def test_url_change_keeps_name(ctx: testing.Context, sync_backends: MagicMock):
    """A new advertised address changes the URL of the backend but not its name."""
    relation = _trino_relation(url="new-host:8080")

    ctx.run(ctx.on.relation_changed(relation), _state_with(relation))

    assert _synced(sync_backends) == [("trino-b", "http://new-host:8080")]


def test_relation_broken_drops_backend(ctx: testing.Context, sync_backends: MagicMock):
    """Removing the relation removes its backend from the desired set."""
    kept = _trino_relation("trino-a", "trino-a:8080")
    removed = _trino_relation()

    state_out = ctx.run(ctx.on.relation_broken(removed), _state_with(kept, removed))

    assert _synced(sync_backends) == [("trino-a", "http://trino-a:8080")]
    assert state_out.unit_status == testing.ActiveStatus()


def test_last_relation_broken_reports_no_backends_related(
    ctx: testing.Context, sync_backends: MagicMock
):
    """Removing the only relation leaves no backends and says so."""
    relation = _trino_relation()

    state_out = ctx.run(ctx.on.relation_broken(relation), _state_with(relation))

    assert _synced(sync_backends) == []
    assert state_out.unit_status == testing.ActiveStatus("no backends related")


def test_incomplete_relation_does_not_block_or_stop_sync(
    ctx: testing.Context, sync_backends: MagicMock
):
    """A relation without data neither blocks the unit nor stops other backends syncing."""
    incomplete = testing.Relation(
        constants.BACKEND_TRINO_CATALOG_RELATION, remote_app_name="trino-c"
    )

    state_out = ctx.run(ctx.on.config_changed(), _state_with(_trino_relation(), incomplete))

    assert state_out.unit_status == testing.ActiveStatus(
        "1 backend-trino-catalog relation not ready"
    )
    assert [name for name, _ in _synced(sync_backends)] == ["trino-b"]


def test_only_unready_relations_are_reported_as_not_ready(
    ctx: testing.Context, sync_backends: MagicMock
):
    """With only unready relations the status reports them, not an absence of relations."""
    incomplete = testing.Relation(
        constants.BACKEND_TRINO_CATALOG_RELATION, remote_app_name="trino-c"
    )

    state_out = ctx.run(ctx.on.config_changed(), _state_with(incomplete))

    assert state_out.unit_status == testing.ActiveStatus(
        "1 backend-trino-catalog relation not ready"
    )
    assert _synced(sync_backends) == []


def test_malformed_relations_are_counted(ctx: testing.Context, sync_backends: MagicMock):
    """Relations with a bad address or bad catalogs are counted in the status message."""
    relations = [
        _trino_relation("trino-c", url="no-port"),
        _trino_relation("trino-d", trino_catalogs="not json"),
    ]

    state_out = ctx.run(ctx.on.config_changed(), _state_with(*relations))

    assert state_out.unit_status == testing.ActiveStatus(
        "2 backend-trino-catalog relations not ready"
    )
    assert _synced(sync_backends) == []


def test_non_string_catalog_names_are_counted(ctx: testing.Context, sync_backends: MagicMock):
    """A relation with a non-string catalog name is not ready and does not fail the hook."""
    catalogs = json.dumps([{"name": None}, {"name": "hive"}])
    relation = _trino_relation("trino-c", trino_catalogs=catalogs)

    state_out = ctx.run(ctx.on.config_changed(), _state_with(relation))

    assert state_out.unit_status == testing.ActiveStatus(
        "1 backend-trino-catalog relation not ready"
    )
    assert _synced(sync_backends) == []


def test_relation_created_publishes_app_name(ctx: testing.Context, sync_backends: MagicMock):
    """The leader publishes its application name for the Trino provider to use."""
    relation = testing.Relation(constants.BACKEND_TRINO_CATALOG_RELATION)

    state_out = ctx.run(ctx.on.relation_created(relation), _state_with(relation))

    assert state_out.get_relation(relation.id).local_app_data == {"app_name": "trino-gateway-k8s"}


def test_trino_credentials_secret_is_not_read(
    ctx: testing.Context, sync_backends: MagicMock, monkeypatch: pytest.MonkeyPatch
):
    """The Trino credentials secret is never requested."""
    requested = []
    get_secret = ops.Model.get_secret

    def recording_get_secret(self, *, id=None, label=None):
        """Record the requested secret ID before delegating."""
        requested.append(id)
        return get_secret(self, id=id, label=label)

    monkeypatch.setattr(ops.Model, "get_secret", recording_get_secret)
    state = _state_with(_trino_relation(trino_credentials_secret_id="secret:trino-user"))

    ctx.run(ctx.on.config_changed(), state)

    assert "secret:trino-user" not in requested
