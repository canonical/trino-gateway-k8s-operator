# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import time

import jubilant
import pytest
import trino
from conftest import DeployTrino
from helpers import (
    APP,
    CATALOG_ENDPOINT,
    UNIT,
    gateway_request,
    relate_upstream,
    wait_for_backends,
)

POSTGRESQL = "postgresql-k8s"
TRINO = "trino-k8s"
NO_BACKENDS = "no backends related"


def _expected_backend(juju: jubilant.Juju) -> dict:
    """Build the backend the gateway should list for the related Trino application."""
    return {
        "name": TRINO,
        "proxyTo": f"http://{TRINO}.{juju.model}.svc.cluster.local:8080",
        "active": True,
        "routingGroup": "adhoc",
    }


def _query_through_gateway(juju: jubilant.Juju, query: str, timeout: float = 600) -> list:
    """Query through the gateway Service, retrying until the backend is reported healthy."""
    address = juju.status().apps[APP].address
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        conn = trino.dbapi.connect(host=address, port=8080, user="trino", http_scheme="http")
        try:
            cursor = conn.cursor()
            cursor.execute(query)
            return cursor.fetchall()
        except (trino.exceptions.Error, trino.exceptions.HttpError) as e:
            last_error = e
            time.sleep(10)
        finally:
            conn.close()
    raise AssertionError(f"query {query!r} never succeeded through the gateway") from last_error


@pytest.mark.incremental
class TestBackends:
    def test_deploy(
        self,
        charm_path: str,
        resource_images: dict[str, str],
        deploy_trino: DeployTrino,
        juju: jubilant.Juju,
    ):
        """The gateway, PostgreSQL and Trino deploy and the gateway reports no backends."""
        juju.model_config({"update-status-hook-interval": "30s"})
        juju.deploy(charm_path, app=APP, resources=resource_images)
        juju.deploy(POSTGRESQL, channel="14/stable", trust=True)
        deploy_trino(juju, TRINO)
        juju.integrate(APP, POSTGRESQL)
        status = juju.wait(jubilant.all_active, error=jubilant.any_error, timeout=30 * 60)

        assert status.apps[APP].units[UNIT].workload_status.message == NO_BACKENDS

    def test_related_backend_is_registered(self, juju: jubilant.Juju):
        """A Trino application related over both endpoints is registered."""
        relate_upstream(juju, APP, TRINO)
        juju.wait(lambda s: jubilant.all_active(s, APP), error=jubilant.any_error)

        wait_for_backends(juju, [_expected_backend(juju)])

    def test_query_through_gateway_service(self, juju: jubilant.Juju):
        rows = _query_through_gateway(juju, "SELECT node_id FROM system.runtime.nodes")

        assert rows

    def test_backend_added_outside_charm_is_removed(self, juju: jubilant.Juju):
        """A backend added through the gateway's API is removed again by the charm."""
        gateway_request(
            juju,
            "/gateway/backend/modify/add",
            {
                "name": "manual",
                "proxyTo": "http://manual:8080",
                "active": True,
                "routingGroup": "adhoc",
            },
        )

        wait_for_backends(juju, [_expected_backend(juju)])

    def test_removed_relation_deregisters_backend(self, juju: jubilant.Juju):
        """Removing the relation deregisters the backend."""
        juju.remove_relation(f"{APP}:backend-trino-catalog", f"{TRINO}:{CATALOG_ENDPOINT}")
        status = juju.wait(lambda s: jubilant.all_active(s, APP), error=jubilant.any_error)

        assert status.apps[APP].units[UNIT].workload_status.message == NO_BACKENDS
        wait_for_backends(juju, [])
