# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import textwrap
from unittest.mock import MagicMock

import ops
import pytest
from ops import testing

import constants
import gateway_api
from charm import TrinoGatewayK8SOperatorCharm
from config import BackendConfig

USER = {"username": "gateway", "password": "s3cr3t-pw"}
BACKENDS = textwrap.dedent("""\
    - name: trino-a
      url: http://trino-a:8080
""")
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


def test_relation_ready_starts_gateway(ctx: testing.Context):
    secret = _secret()
    relation = _relation(secret)
    state = testing.State(containers={_container()}, relations={relation}, secrets={secret})

    state_out = ctx.run(ctx.on.relation_changed(relation), state)

    assert state_out.unit_status == testing.ActiveStatus("no backends configured")
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


def _ready_state(leader: bool = True, backends: str = BACKENDS) -> testing.State:
    secret = _secret()
    return testing.State(
        leader=leader,
        config={"backends": backends},
        containers={_container()},
        relations={_relation(secret)},
        secrets={secret},
    )


def test_leader_syncs_configured_backends(ctx: testing.Context, sync_backends: MagicMock):
    state_out = ctx.run(ctx.on.config_changed(), _ready_state())

    assert state_out.unit_status == testing.ActiveStatus()
    sync_backends.assert_called_once()
    assert sync_backends.call_args.args[1] == [
        BackendConfig(name="trino-a", url="http://trino-a:8080")
    ]


def test_sync_errors_are_swallowed(ctx: testing.Context, sync_backends: MagicMock):
    sync_backends.side_effect = gateway_api.GatewayApiError("connection refused")

    state_out = ctx.run(ctx.on.update_status(), _ready_state())

    assert state_out.unit_status == testing.ActiveStatus()


def test_non_leader_does_not_sync(ctx: testing.Context, sync_backends: MagicMock):
    ctx.run(ctx.on.config_changed(), _ready_state(leader=False))

    sync_backends.assert_not_called()


def test_invalid_backends_block_without_sync(ctx: testing.Context, sync_backends: MagicMock):
    state_out = ctx.run(ctx.on.config_changed(), _ready_state(backends="- name: a"))

    assert state_out.unit_status == testing.BlockedStatus(
        "invalid backends config: [0].url: field required"
    )
    sync_backends.assert_not_called()
    container_out = state_out.get_container(constants.CONTAINER_NAME)
    service_status = container_out.service_statuses[constants.SERVICE_NAME]
    assert service_status == ops.pebble.ServiceStatus.ACTIVE


def test_empty_backends_are_active(ctx: testing.Context, sync_backends: MagicMock):
    state_out = ctx.run(ctx.on.config_changed(), _ready_state(backends=""))

    assert state_out.unit_status == testing.ActiveStatus("no backends configured")
    assert sync_backends.call_args.args[1] == []
