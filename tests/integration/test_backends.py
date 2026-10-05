# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import textwrap
import time
import urllib.error
import urllib.request

import jubilant
import pytest
import trino

APP = "trino-gateway-k8s"
UNIT = f"{APP}/0"
POSTGRESQL = "postgresql-k8s"
TRINO = "trino-k8s"
BACKEND = "trino-a"


def _trino_url(juju: jubilant.Juju) -> str:
    return f"http://{TRINO}.{juju.model}.svc.cluster.local:8080"


def _gateway_request(juju: jubilant.Juju, path: str, body: dict | None = None) -> bytes:
    address = juju.status().apps[APP].units[UNIT].address
    request = urllib.request.Request(
        f"http://{address}:8080{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return response.read()


def _wait_for_backends(juju: jubilant.Juju, expected: list[dict], timeout: float = 600) -> None:
    """Poll the gateway until its backends match; the charm syncs on its next hook."""
    keys = ("name", "proxyTo", "active", "routingGroup")
    deadline = time.monotonic() + timeout
    while True:
        try:
            backends = json.loads(_gateway_request(juju, "/gateway/backend/all"))
            current = sorted(({k: b.get(k) for k in keys} for b in backends), key=str)
        except (urllib.error.URLError, ConnectionError):
            current = None
        if current == sorted(({k: b[k] for k in keys} for b in expected), key=str):
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"gateway backends {current} never matched {expected}")
        time.sleep(10)


def _query_through_gateway(juju: jubilant.Juju, query: str, timeout: float = 600) -> list:
    """Query through the gateway Service, retrying until the backend is reported healthy."""
    address = juju.status().apps[APP].address
    deadline = time.monotonic() + timeout
    while True:
        conn = trino.dbapi.connect(host=address, port=8080, user="trino", http_scheme="http")
        try:
            cursor = conn.cursor()
            cursor.execute(query)
            return cursor.fetchall()
        except (trino.exceptions.Error, trino.exceptions.HttpError):
            if time.monotonic() > deadline:
                raise
            time.sleep(10)
        finally:
            conn.close()


@pytest.mark.incremental
class TestBackends:
    def test_deploy(self, charm_path: str, resource_images: dict[str, str], juju: jubilant.Juju):
        juju.model_config({"update-status-hook-interval": "30s"})
        juju.deploy(charm_path, app=APP, resources=resource_images)
        juju.deploy(POSTGRESQL, channel="14/stable", trust=True)
        juju.deploy(TRINO, channel="latest/stable", trust=True)
        juju.integrate(APP, POSTGRESQL)
        status = juju.wait(jubilant.all_active, error=jubilant.any_error, timeout=30 * 60)

        assert status.apps[APP].units[UNIT].workload_status.message == "no backends configured"

    def test_configured_backend_is_registered(self, juju: jubilant.Juju):
        url = _trino_url(juju)
        backends = textwrap.dedent(
            f"""\
            - name: {BACKEND}
              url: {url}
            """
        )
        juju.config(APP, {"backends": backends})
        juju.wait(lambda s: jubilant.all_active(s, APP), error=jubilant.any_error)

        _wait_for_backends(
            juju, [{"name": BACKEND, "proxyTo": url, "active": True, "routingGroup": "adhoc"}]
        )

    def test_query_through_gateway_service(self, juju: jubilant.Juju):
        rows = _query_through_gateway(juju, "SELECT node_id FROM system.runtime.nodes")

        assert rows

    def test_backend_added_outside_charm_is_removed(self, juju: jubilant.Juju):
        url = _trino_url(juju)
        _gateway_request(
            juju,
            "/gateway/backend/modify/add",
            {
                "name": "manual",
                "proxyTo": "http://manual:8080",
                "active": True,
                "routingGroup": "adhoc",
            },
        )

        _wait_for_backends(
            juju, [{"name": BACKEND, "proxyTo": url, "active": True, "routingGroup": "adhoc"}]
        )

    def test_removed_backend_is_deregistered(self, juju: jubilant.Juju):
        juju.config(APP, reset="backends")
        status = juju.wait(lambda s: jubilant.all_active(s, APP), error=jubilant.any_error)

        assert status.apps[APP].units[UNIT].workload_status.message == "no backends configured"
        _wait_for_backends(juju, [])
