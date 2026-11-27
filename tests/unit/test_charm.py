# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import dataclasses
import json
from unittest.mock import MagicMock

import ops
import pytest
import yaml
from conftest import FakeGateway
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


@pytest.fixture(autouse=True)
def _fake_gateway(gateway: FakeGateway) -> FakeGateway:
    return gateway


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
        relations={_relation(secret), testing.PeerRelation(constants.PEER_RELATION)},
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


def _integration_for(relation: testing.Relation) -> testing.Relation:
    """Build the `trino-gateway-integration` relation of a Trino application."""
    return testing.Relation(
        constants.TRINO_GATEWAY_INTEGRATION_RELATION,
        remote_app_name=relation.remote_app_name,
        remote_app_data={"trino_url": relation.remote_app_data.get("trino_url", "")},
    )


def _state_with(*relations: testing.Relation, leader: bool = True):
    """Build a ready state with Trino applications related over both of their endpoints."""
    state = _ready_state(leader=leader)
    integrations = [_integration_for(r) for r in relations if r.remote_app_data.get("trino_url")]
    return dataclasses.replace(state, relations={*state.relations, *relations, *integrations})


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


PROVIDER_DATA = {
    "issuer_url": "https://idp.example.com",
    "authorization_endpoint": "https://idp.example.com/oauth2/auth",
    "token_endpoint": "https://idp.example.com/oauth2/token",
    "introspection_endpoint": "https://idp.example.com/admin/oauth2/introspect",
    "userinfo_endpoint": "https://idp.example.com/userinfo",
    "jwks_endpoint": "https://idp.example.com/.well-known/jwks.json",
    "scope": "openid profile email",
}


def _ingress(url: str | None) -> testing.Relation:
    data = {} if url is None else {"ingress": json.dumps({"url": url})}
    return testing.Relation(
        constants.INGRESS_RELATION, remote_app_name="traefik", remote_app_data=data
    )


def _client_secret(value: str = "client-s3cret") -> testing.Secret:
    return testing.Secret({"secret": value})


def _oauth(client_secret: testing.Secret | None = None) -> testing.Relation:
    """Build an `oauth` relation, with the registered client if a secret is given."""
    data = dict(PROVIDER_DATA)
    if client_secret is not None:
        data.update(client_id="gw-client", client_secret_id=client_secret.id)
    return testing.Relation(
        constants.OAUTH_RELATION, remote_app_name="hydra", remote_app_data=data
    )


def _oauth_state(*relations: testing.Relation, secrets=(), leader: bool = True):
    state = _ready_state(leader=leader)
    return dataclasses.replace(
        state, relations={*state.relations, *relations}, secrets={*state.secrets, *secrets}
    )


def _gateway_config(ctx: testing.Context, state: testing.State) -> dict:
    container = state.get_container(constants.CONTAINER_NAME)
    path = container.get_filesystem(ctx) / constants.CONFIG_PATH.lstrip("/")
    return yaml.safe_load(path.read_text())


def _registered_redirect(state: testing.State, relation: testing.Relation) -> str | None:
    return state.get_relation(relation.id).local_app_data.get("redirect_uri")


@pytest.mark.parametrize(
    ("ingress", "status"),
    [
        (None, testing.WaitingStatus("waiting for ingress URL for OAuth")),
        (_ingress(None), testing.WaitingStatus("waiting for ingress URL for OAuth")),
        (
            _ingress("http://gw.example.com"),
            testing.BlockedStatus("OAuth requires an HTTPS ingress URL"),
        ),
    ],
)
def test_oauth_without_https_ingress_lets_only_the_charm_in(
    ctx: testing.Context, ingress: testing.Relation | None, status: ops.StatusBase
):
    """Without an HTTPS address, no client is registered and no user can log in."""
    secret = _client_secret()
    relation = _oauth(secret)
    relations = [relation] if ingress is None else [relation, ingress]

    state_out = ctx.run(ctx.on.config_changed(), _oauth_state(*relations, secrets=[secret]))

    assert state_out.unit_status == status
    assert _registered_redirect(state_out, relation) is None
    authentication = _gateway_config(ctx, state_out)["authentication"]
    assert "oauth" not in authentication
    service = state_out.get_container(constants.CONTAINER_NAME).service_statuses
    assert service[constants.SERVICE_NAME] == ops.pebble.ServiceStatus.ACTIVE


def test_https_ingress_registers_the_gateway_callback(ctx: testing.Context):
    relation = _oauth()
    state = _oauth_state(relation, _ingress("https://gw.example.com:8443/"))

    state_out = ctx.run(ctx.on.relation_changed(relation), state)

    assert _registered_redirect(state_out, relation) == "https://gw.example.com:8443/oidc/callback"
    assert state_out.unit_status == testing.WaitingStatus(
        "waiting for OAuth provider registration"
    )


def test_registered_client_configures_the_oauth_login(ctx: testing.Context, gateway: FakeGateway):
    secret = _client_secret()
    relation = _oauth(secret)
    state = _oauth_state(relation, _ingress("https://gw.example.com"), secrets=[secret])

    state_out = ctx.run(ctx.on.relation_changed(relation), state)

    oauth = _gateway_config(ctx, state_out)["authentication"]["oauth"]
    assert oauth["clientId"] == "gw-client"
    assert oauth["clientSecret"] == "client-s3cret"
    assert oauth["redirectUrl"] == "https://gw.example.com/oidc/callback"
    assert oauth["tokenEndpoint"] == PROVIDER_DATA["token_endpoint"]
    container = state_out.get_container(constants.CONTAINER_NAME)
    assert "client-s3cret" not in str(container.plan.to_dict())
    api_key = (container.get_filesystem(ctx) / constants.API_KEY_PATH.lstrip("/")).read_text()
    assert api_key not in str(container.plan.to_dict())
    assert gateway.api_keys
    assert set(gateway.api_keys) == {api_key}
    assert state_out.unit_status == testing.ActiveStatus("no backends related")


def test_ingress_address_change_updates_the_client(ctx: testing.Context):
    secret = _client_secret()
    relation, ingress = _oauth(secret), _ingress("https://gw.example.com")
    state = ctx.run(
        ctx.on.relation_changed(relation), _oauth_state(relation, ingress, secrets=[secret])
    )

    moved = dataclasses.replace(
        ingress, remote_app_data={"ingress": json.dumps({"url": "https://new.example.com"})}
    )
    relations = {r for r in state.relations if r.id != ingress.id} | {moved}
    state_out = ctx.run(
        ctx.on.relation_changed(moved), dataclasses.replace(state, relations=relations)
    )

    redirect = "https://new.example.com/oidc/callback"
    assert _registered_redirect(state_out, relation) == redirect
    assert _gateway_config(ctx, state_out)["authentication"]["oauth"]["redirectUrl"] == redirect


def test_client_secret_rotation_restarts_with_the_new_secret(ctx: testing.Context):
    secret = _client_secret()
    relation, ingress = _oauth(secret), _ingress("https://gw.example.com")
    before = ctx.run(ctx.on.update_status(), _oauth_state(relation, ingress, secrets=[secret]))

    rotated = dataclasses.replace(secret, latest_content={"secret": "rotated"})
    secrets = {s for s in before.secrets if s.id != secret.id} | {rotated}
    after = ctx.run(ctx.on.secret_changed(rotated), dataclasses.replace(before, secrets=secrets))

    assert _gateway_config(ctx, after)["authentication"]["oauth"]["clientSecret"] == "rotated"
    assert _config_hash(before) != _config_hash(after)


def test_oauth_removal_opens_the_ui_and_api(ctx: testing.Context, gateway: FakeGateway):
    secret = _client_secret()
    relation = _oauth(secret)
    state = ctx.run(
        ctx.on.update_status(),
        _oauth_state(relation, _ingress("https://gw.example.com"), secrets=[secret]),
    )
    gateway.register("stale")

    state_out = ctx.run(ctx.on.relation_broken(state.get_relation(relation.id)), state)

    config = _gateway_config(ctx, state_out)
    assert "authentication" not in config
    assert "authorization" not in config
    assert gateway.backends == {}
    assert state_out.unit_status == testing.ActiveStatus("no backends related")


def test_follower_without_api_key_keeps_the_workload_untouched(ctx: testing.Context):
    """A follower does not open the UI and API while it waits for the leader's key."""
    state_out = ctx.run(ctx.on.config_changed(), _oauth_state(_oauth(), leader=False))

    assert set(state_out.get_container(constants.CONTAINER_NAME).layers) == {"rock"}


def test_redirect_uri_the_library_rejects_blocks(ctx: testing.Context):
    relation = _oauth()

    state_out = ctx.run(
        ctx.on.relation_changed(relation), _oauth_state(relation, _ingress("https://gateway"))
    )

    assert _registered_redirect(state_out, relation) is None
    assert isinstance(state_out.unit_status, testing.BlockedStatus)
    assert state_out.unit_status.message.startswith("invalid OAuth client configuration")


def test_unreadable_api_key_is_replaced_once(
    ctx: testing.Context, gateway: FakeGateway, unreadable_revisions: set[tuple[str, int]]
):
    """A key secret Juju saved only partly gets a new key, which later hooks keep."""
    stale = testing.Secret({"key": "lost"}, owner="app", label=constants.API_KEY_SECRET_LABEL)
    unreadable_revisions.add((stale.id, 1))
    state = _ready_state()
    state = dataclasses.replace(state, secrets={*state.secrets, stale})

    first = ctx.run(ctx.on.update_status(), state)
    second = ctx.run(ctx.on.update_status(), first)

    key = second.get_secret(label=constants.API_KEY_SECRET_LABEL).latest_content
    assert key is not None and key["key"] != "lost"
    assert _config_hash(first) == _config_hash(second)
    assert set(gateway.api_keys) == {key["key"]}
