# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import jubilant
import pytest
from helpers import APP, UNIT, wait_for_backends

POSTGRESQL = "postgresql-k8s"
TRINO = "trino-k8s"
TRINO_APPS = ("trino-a", "trino-b")
CATALOG_ENDPOINT = "trino-catalog"
NO_BACKENDS = "no backends configured or related"


def _expected(juju: jubilant.Juju, apps: tuple[str, ...]) -> list[dict]:
    """Build the backends the gateway should list for the given Trino applications."""
    return [
        {
            "name": app,
            "proxyTo": f"http://{app}.{juju.model}.svc.cluster.local:8080",
            "active": True,
            "routingGroup": "adhoc",
        }
        for app in apps
    ]


@pytest.mark.incremental
class TestBackendTrinoCatalog:
    """Trino applications related over `backend-trino-catalog` are registered as backends."""

    def test_deploy(self, charm_path: str, resource_images: dict[str, str], juju: jubilant.Juju):
        """The gateway, PostgreSQL and two Trino applications deploy and settle."""
        juju.model_config({"update-status-hook-interval": "30s"})
        juju.deploy(charm_path, app=APP, resources=resource_images)
        juju.deploy(POSTGRESQL, channel="14/stable", trust=True, config={"profile": "testing"})
        for app in TRINO_APPS:
            juju.deploy(
                TRINO, app=app, channel="latest/edge", trust=True, config={"charm-function": "all"}
            )
        juju.integrate(APP, POSTGRESQL)
        status = juju.wait(jubilant.all_active, error=jubilant.any_error, timeout=30 * 60)

        assert status.apps[APP].units[UNIT].workload_status.message == NO_BACKENDS

    def test_related_trino_applications_are_registered(self, juju: jubilant.Juju):
        """Both related Trino applications appear as backends in the adhoc group."""
        for app in TRINO_APPS:
            juju.integrate(f"{APP}:backend-trino-catalog", f"{app}:{CATALOG_ENDPOINT}")
        juju.wait(lambda s: jubilant.all_active(s, APP), error=jubilant.any_error)

        wait_for_backends(juju, _expected(juju, TRINO_APPS))

    def test_removed_relation_deregisters_backend(self, juju: jubilant.Juju):
        """Removing one relation deregisters only that application's backend."""
        juju.remove_relation(f"{APP}:backend-trino-catalog", f"trino-b:{CATALOG_ENDPOINT}")
        juju.wait(lambda s: jubilant.all_active(s, APP), error=jubilant.any_error)

        wait_for_backends(juju, _expected(juju, TRINO_APPS[:1]))
