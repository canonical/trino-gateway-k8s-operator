# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
from unittest.mock import MagicMock

import yaml

import workload
from constants import CONFIG_PATH
from workload import PostgresRelationModel

PG = PostgresRelationModel.model_validate(
    {
        "database": "trino_gateway",
        "endpoints": "pg-0:5432",
        "secret-user": {"username": "gateway", "password": "s3cr3t-pw"},
    }
)


def test_render_config():
    assert yaml.safe_load(workload.render_config(PG)) == {
        "serverConfig": {"node.environment": "production", "http-server.http.port": 8080},
        "dataStore": {
            "jdbcUrl": "jdbc:postgresql://pg-0:5432/trino_gateway",
            "user": "gateway",
            "password": "s3cr3t-pw",
            "driver": "org.postgresql.Driver",
        },
        "clusterStatsConfiguration": {"monitorType": "INFO_API"},
        "routing": {"defaultRoutingGroup": "adhoc"},
    }


def test_render_config_quotes_special_characters():
    pg = PG.model_copy(update={"secret_user": {"username": "gateway", "password": "a: '#b"}})

    assert yaml.safe_load(workload.render_config(pg))["dataStore"]["password"] == "a: '#b"


def test_apply_pushes_private_config_and_replans():
    container = MagicMock()

    workload.apply(container, "config")

    container.push.assert_called_once_with(
        CONFIG_PATH,
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
