# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import shlex

import jubilant
import pytest
from helpers import APP, UNIT, gateway_request, wait_for_backends

POSTGRESQL = "postgresql-k8s"
TRINO = "trino-k8s"
CATALOG_ENDPOINT = "trino-catalog"
GATEWAY_ENDPOINT = f"{APP}:backend-trino-catalog"
NO_BACKENDS = "no backends related"
# The application, offer and alias differ so the test shows which one names the backend.
OFFER = "trino-offer"
ALIAS = "trino-remote"
OTHER_ALIAS = "trino-other"
FETCH_SCRIPT = (
    "import sys, urllib.request; "
    "print(urllib.request.urlopen(sys.argv[1], timeout=10).read().decode())"
)


def _backend(name: str, url: str) -> dict:
    """Build a backend as the gateway lists it in the default routing group."""
    return {"name": name, "proxyTo": url, "active": True, "routingGroup": "adhoc"}


def _trino_backend(trino_juju: jubilant.Juju, name: str) -> dict:
    """Build the backend the Gateway should list for the Trino application under `name`."""
    return _backend(name, f"http://{TRINO}.{trino_juju.model}.svc.cluster.local:8080")


def _consume(juju: jubilant.Juju, trino_juju: jubilant.Juju, alias: str) -> None:
    """Consume the Trino offer into the Gateway's model under an alias."""
    juju.consume(f"{trino_juju.model}.{OFFER}", alias)


@pytest.fixture(scope="module")
def trino_juju(juju_factory) -> jubilant.Juju:
    """Create the model that hosts the Trino application and its offer."""
    return juju_factory.get_juju("trino")


@pytest.mark.incremental
class TestBackendTrinoCatalogCrossModel:
    """Trino applications related across models are registered under their consumed alias."""

    def test_deploy(
        self,
        charm_path: str,
        resource_images: dict[str, str],
        juju: jubilant.Juju,
        trino_juju: jubilant.Juju,
    ):
        """The gateway, PostgreSQL and a Trino deploy in one model and another Trino in another."""
        juju.model_config({"update-status-hook-interval": "30s"})
        juju.deploy(charm_path, app=APP, resources=resource_images)
        juju.deploy(POSTGRESQL, channel="14/stable", trust=True, config={"profile": "testing"})
        juju.integrate(APP, POSTGRESQL)
        for model in (juju, trino_juju):
            model.deploy(
                TRINO, channel="latest/edge", trust=True, config={"charm-function": "all"}
            )
        trino_juju.wait(jubilant.all_active, error=jubilant.any_error, timeout=30 * 60)
        status = juju.wait(jubilant.all_active, error=jubilant.any_error, timeout=30 * 60)

        assert status.apps[APP].units[UNIT].workload_status.message == NO_BACKENDS

    def test_backend_is_named_after_the_alias(
        self, juju: jubilant.Juju, trino_juju: jubilant.Juju
    ):
        """The alias of the consumed offer names the backend, not the application or offer."""
        trino_juju.offer(TRINO, endpoint=CATALOG_ENDPOINT, name=OFFER)
        _consume(juju, trino_juju, ALIAS)
        juju.integrate(GATEWAY_ENDPOINT, f"{ALIAS}:{CATALOG_ENDPOINT}")
        juju.wait(lambda s: jubilant.all_active(s, APP), error=jubilant.any_error)

        wait_for_backends(juju, [_trino_backend(trino_juju, ALIAS)])

    def test_advertised_url_is_reachable_from_the_gateway(self, juju: jubilant.Juju):
        """The Trino health endpoint answers at the registered URL from inside the gateway pod."""
        backends = json.loads(gateway_request(juju, "/gateway/backend/all"))
        url = next(b["proxyTo"] for b in backends if b["name"] == ALIAS)

        task = juju.exec(
            f"python3 -c {shlex.quote(FETCH_SCRIPT)} {shlex.quote(url + '/v1/info')}", unit=UNIT
        )

        assert "nodeVersion" in json.loads(task.stdout)

    def test_removed_relation_deregisters_backend(
        self, juju: jubilant.Juju, trino_juju: jubilant.Juju
    ):
        """Removing the relation deregisters the backend registered under the alias."""
        juju.remove_relation(GATEWAY_ENDPOINT, f"{ALIAS}:{CATALOG_ENDPOINT}")
        juju.wait(
            lambda s: jubilant.all_active(s, APP) and not s.app_endpoints[ALIAS].relations,
            error=jubilant.any_error,
        )
        trino_juju.wait(
            lambda s: CATALOG_ENDPOINT not in s.apps[TRINO].relations, error=jubilant.any_error
        )
        wait_for_backends(juju, [])

        juju.cli("remove-saas", ALIAS)
        juju.wait(lambda s: ALIAS not in s.app_endpoints, error=jubilant.any_error)

    def test_another_alias_names_the_backend(self, juju: jubilant.Juju, trino_juju: jubilant.Juju):
        """Consuming the offer under another alias registers a backend under that alias."""
        _consume(juju, trino_juju, OTHER_ALIAS)
        juju.integrate(GATEWAY_ENDPOINT, f"{OTHER_ALIAS}:{CATALOG_ENDPOINT}")
        juju.wait(lambda s: jubilant.all_active(s, APP), error=jubilant.any_error)

        wait_for_backends(juju, [_trino_backend(trino_juju, OTHER_ALIAS)])

    def test_local_and_remote_applications_with_the_same_name_coexist(
        self, juju: jubilant.Juju, trino_juju: jubilant.Juju
    ):
        """Juju requires an alias for a consumed offer named like a local application."""
        with pytest.raises(jubilant.CLIError):
            _consume(juju, trino_juju, TRINO)
        juju.integrate(GATEWAY_ENDPOINT, f"{TRINO}:{CATALOG_ENDPOINT}")
        juju.wait(lambda s: jubilant.all_active(s, APP), error=jubilant.any_error)

        wait_for_backends(
            juju, [_trino_backend(trino_juju, OTHER_ALIAS), _trino_backend(juju, TRINO)]
        )
