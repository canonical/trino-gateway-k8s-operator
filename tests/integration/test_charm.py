# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import time
import urllib.error
import urllib.request

import jubilant
import psycopg
import pytest

APP = "trino-gateway-k8s"
UNIT = f"{APP}/0"
POSTGRESQL = "postgresql-k8s"
BLOCKED_MESSAGE = "missing required relation: postgresql"
# The rock is bare (no shell), so Pebble is driven from the charm container.
PEBBLE = "PEBBLE_SOCKET=/charm/containers/trino-gateway/pebble.socket /charm/bin/pebble"


def _service_state(juju: jubilant.Juju, timeout: float = 120) -> str:
    deadline = time.monotonic() + timeout
    while True:
        try:
            output = juju.ssh(UNIT, f"{PEBBLE} services trino-gateway")
            return output.splitlines()[1].split()[2]
        except (jubilant.CLIError, IndexError):
            if time.monotonic() > deadline:
                raise
            time.sleep(5)


def _relation_user(juju: jubilant.Juju) -> dict[str, str]:
    unit_info = json.loads(juju.cli("show-unit", UNIT, "--format", "json"))[UNIT]
    relation = next(r for r in unit_info["relation-info"] if r["endpoint"] == "postgresql")
    secret_id = relation["application-data"]["secret-user"]
    return dict(juju.show_secret(secret_id, reveal=True).content)


def _wait_for_http_ok(url: str, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310
                assert response.status == 200
                return
        except (urllib.error.URLError, ConnectionError):
            if time.monotonic() > deadline:
                raise
            time.sleep(5)


@pytest.mark.incremental
class TestCharm:
    def test_deploy_without_postgresql_blocks(
        self, charm_path: str, resource_images: dict[str, str], juju: jubilant.Juju
    ):
        juju.deploy(charm_path, app=APP, resources=resource_images)
        status = juju.wait(lambda s: jubilant.all_blocked(s, APP), error=jubilant.any_error)

        assert status.apps[APP].units[UNIT].workload_status.message == BLOCKED_MESSAGE
        assert _service_state(juju) == "inactive"

    def test_relate_postgresql_serves_http(self, juju: jubilant.Juju):
        juju.deploy(POSTGRESQL, channel="14/stable", trust=True)
        juju.integrate(APP, POSTGRESQL)
        status = juju.wait(jubilant.all_active, error=jubilant.any_error, timeout=20 * 60)

        _wait_for_http_ok(f"http://{status.apps[APP].units[UNIT].address}:8080/")

    def test_schema_created(self, juju: jubilant.Juju):
        user = _relation_user(juju)
        address = juju.status().apps[POSTGRESQL].units[f"{POSTGRESQL}/0"].address

        with psycopg.connect(
            host=address,
            dbname="trino_gateway",
            user=user["username"],
            password=user["password"],
            connect_timeout=10,
        ) as conn:
            row = conn.execute("SELECT to_regclass('flyway_schema_history')").fetchone()

        assert row is not None and row[0] is not None

    def test_credentials_not_logged(self, juju: jubilant.Juju):
        password = _relation_user(juju)["password"]
        debug_log = juju.cli("debug-log", "--replay", "--no-tail", "--level", "TRACE")
        workload_log = juju.ssh(UNIT, f"{PEBBLE} logs -n all trino-gateway")

        # Booleans keep pytest's assertion rewriting from echoing the password.
        leaked_in_debug_log = password in debug_log
        leaked_in_workload_log = password in workload_log
        assert not leaked_in_debug_log
        assert not leaked_in_workload_log

    def test_remove_postgresql_blocks_and_keeps_service(self, juju: jubilant.Juju):
        juju.remove_relation(APP, POSTGRESQL)
        status = juju.wait(lambda s: jubilant.all_blocked(s, APP), error=jubilant.any_error)

        assert status.apps[APP].units[UNIT].workload_status.message == BLOCKED_MESSAGE
        assert _service_state(juju) == "active"
