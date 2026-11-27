# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
import re
from unittest.mock import MagicMock

import yaml

import constants
import workload
from relations.oauth import OAuthClient
from workload import GatewayAuth, OAuthLogin, PostgresRelationModel

PG = PostgresRelationModel.model_validate(
    {
        "database": "trino_gateway",
        "endpoints": "pg-0:5432",
        "secret-user": {"username": "gateway", "password": "s3cr3t-pw"},
    }
)


def test_render_config():
    assert yaml.safe_load(workload.render_config(PG)) == {
        "serverConfig": {
            "node.environment": constants.NODE_ENVIRONMENT,
            "http-server.http.port": 8080,
            "http-server.process-forwarded": True,
        },
        "dataStore": {
            "jdbcUrl": "jdbc:postgresql://pg-0:5432/trino_gateway",
            "user": "gateway",
            "password": "s3cr3t-pw",
            "driver": "org.postgresql.Driver",
        },
        "clusterStatsConfiguration": {"monitorType": "INFO_API"},
        "routing": {"defaultRoutingGroup": "adhoc"},
    }


CLIENT = OAuthClient(
    issuer="https://idp.example.com",
    client_id="gw-client",
    client_secret="client-s3cret",
    authorization_endpoint="https://idp.example.com/oauth2/auth",
    token_endpoint="https://idp.example.com/oauth2/token",
    jwks_endpoint="https://idp.example.com/.well-known/jwks.json",
)
LOGIN = OAuthLogin(CLIENT, "https://gw.example.com/oidc/callback")


def _roles(authorization: dict, privileges: str) -> set[str]:
    """List the roles the gateway gives privileges, matching its regexes as Java does."""
    return {
        role
        for role in ("admin", "user", "api")
        if re.fullmatch(authorization[role], privileges) is not None
    }


def test_oauth_users_get_read_only_access_and_the_charm_the_api():
    """OAuth users only read the UI; only the charm's user calls the management API."""
    config = yaml.safe_load(workload.render_config(PG, GatewayAuth(LOGIN)))

    authorization = config["authorization"]
    assert authorization["enableDefaultPrivilege"] is True
    assert _roles(authorization, authorization["defaultPrivilege"]) == {"user"}
    assert config["presetUsers"].keys() == {constants.API_USER}
    assert _roles(authorization, config["presetUsers"][constants.API_USER]["privileges"]) == {
        "api"
    }


def test_oauth_login_is_the_only_login_offered():
    config = yaml.safe_load(workload.render_config(PG, GatewayAuth(LOGIN)))

    authentication = config["authentication"]
    assert authentication["defaultType"][0] == "oauth"
    assert authentication["showFirstTypeOnly"] is True
    assert "form" not in authentication
    oauth = authentication["oauth"]
    assert oauth["clientSecret"] == "client-s3cret"
    assert oauth["redirectUrl"] == "https://gw.example.com/oidc/callback"
    assert oauth["audiences"] == ["gw-client"]


def test_auth_without_login_only_lets_the_charm_in():
    config = yaml.safe_load(workload.render_config(PG, GatewayAuth()))

    assert config["authentication"]["defaultType"] == ["jwt"]
    assert "oauth" not in config["authentication"]
    assert config["presetUsers"].keys() == {constants.API_USER}


def test_render_config_quotes_special_characters():
    pg = PG.model_copy(update={"secret_user": {"username": "gateway", "password": "a: '#b"}})

    assert yaml.safe_load(workload.render_config(pg))["dataStore"]["password"] == "a: '#b"


def test_apply_pushes_private_config_and_replans():
    container = MagicMock()

    workload.apply(container, "config")

    container.push.assert_called_once_with(
        constants.CONFIG_PATH,
        "config",
        user="_daemon_",
        group="_daemon_",
        permissions=0o600,
        make_dirs=True,
    )
    layer = container.add_layer.call_args.args[1]
    assert layer["services"]["trino-gateway"]["environment"] == {
        "CONFIG_HASH": hashlib.sha256(b"config").hexdigest()
    }
    container.replan.assert_called_once_with()


def test_apply_pushes_private_api_key_and_restarts_on_key_change():
    container = MagicMock()

    workload.apply(container, "config", "key-1")
    first = container.add_layer.call_args.args[1]
    workload.apply(container, "config", "key-2")
    second = container.add_layer.call_args.args[1]

    container.push.assert_any_call(
        constants.API_KEY_PATH,
        "key-2",
        user="_daemon_",
        group="_daemon_",
        permissions=0o600,
        make_dirs=True,
    )
    environment = first["services"]["trino-gateway"]["environment"]
    assert environment != second["services"]["trino-gateway"]["environment"]
    assert "key-1" not in str(environment)
