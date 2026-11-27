# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""The gateway behind traefik in `subdomain` mode, and consumers given the ingress address.

Requests reach traefik at its unit address with the ingress host in the `Host` header, so the
test needs no DNS for the ingress host.
"""

import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import jubilant
import pytest
from conftest import DeployTrino
from helpers import (
    APP,
    CATALOG_ENDPOINT,
    CONSUMER,
    CONSUMER_ENDPOINT,
    PUBLISHED_KEYS,
    UNIT,
    consumer_credentials,
    consumer_data,
    relate_upstream,
    wait_for_consumer_data,
)

POSTGRESQL = "postgresql-k8s"
TRAEFIK = "traefik-k8s"
TRINO = "trino"
QUERY = "SELECT count(*) FROM system.runtime.nodes"


def _ingress_url(juju: jubilant.Juju, timeout: float = 600) -> str:
    """Wait for the URL traefik gives the gateway, as the gateway reads it.

    The relation data is read rather than traefik's `show-proxied-endpoints` action, which
    can report another URL in `subdomain` mode.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        unit_info = json.loads(juju.cli("show-unit", UNIT, "--format", "json"))[UNIT]
        for relation in unit_info["relation-info"]:
            if relation["endpoint"] == "ingress" and "ingress" in relation["application-data"]:
                return json.loads(relation["application-data"]["ingress"])["url"]
        time.sleep(10)
    raise AssertionError(f"no ingress URL within {timeout}s")


def _in_cluster_url(juju: jubilant.Juju) -> str:
    return f"{APP}.{juju.model}.svc.cluster.local:8080"


def _request(juju: jubilant.Juju, url: str, **kwargs) -> urllib.request.Request:
    """Build a request for a URL at the ingress host, sent to traefik's unit address."""
    parsed = urlsplit(url)
    traefik = juju.status().apps[TRAEFIK].units[f"{TRAEFIK}/0"].address
    headers = {"Host": parsed.netloc, **kwargs.pop("headers", {})}
    target = parsed._replace(netloc=f"{traefik}:{parsed.port or 80}").geturl()
    return urllib.request.Request(target, headers=headers, **kwargs)


def _query(juju: jubilant.Juju, base: str, username: str, timeout: float = 120) -> list:
    """Run a query through the ingress, following each `nextUri` as a client would.

    Raises:
        AssertionError: if the query fails, does not finish, or a `nextUri` leaves the ingress.
    """
    headers = {"X-Trino-User": username}
    request = _request(
        juju, f"{base}/v1/statement", data=QUERY.encode(), headers=headers, method="POST"
    )
    ingress = urlsplit(base)
    deadline = time.monotonic() + timeout
    rows = []
    while time.monotonic() < deadline:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            body = json.loads(response.read())
        if "error" in body:
            raise AssertionError(f"query failed: {body['error']}")
        rows.extend(body.get("data", []))
        next_uri = body.get("nextUri")
        if not next_uri:
            return rows
        follow_up = urlsplit(next_uri)
        assert (follow_up.scheme, follow_up.hostname, follow_up.port or 80) == (
            ingress.scheme,
            ingress.hostname,
            ingress.port or 80,
        ), f"nextUri {next_uri} is not at the ingress address {base}"
        request = _request(juju, next_uri, headers=headers)
        time.sleep(0.2)
    raise AssertionError(f"query did not finish within {timeout}s")


def _wait_for_query(juju: jubilant.Juju, base: str, username: str, timeout: float = 600):
    """Retry the query until the gateway has a healthy backend and the ingress routes to it."""
    deadline = time.monotonic() + timeout
    error = None
    while time.monotonic() < deadline:
        try:
            return _query(juju, base, username)
        except (urllib.error.URLError, ConnectionError, AssertionError) as e:
            if isinstance(e, urllib.error.HTTPError):
                e.close()
            error = e
            time.sleep(10)
    raise AssertionError(f"query never succeeded within {timeout}s") from error


@pytest.mark.incremental
class TestIngress:
    def test_deploy(
        self,
        charm_path: str,
        resource_images: dict[str, str],
        deploy_trino: DeployTrino,
        juju: jubilant.Juju,
    ):
        """The gateway, its consumer, PostgreSQL, Trino and traefik deploy."""
        juju.model_config({"update-status-hook-interval": "30s"})
        juju.deploy(charm_path, app=APP, resources=resource_images)
        juju.deploy(charm_path, app=CONSUMER, resources=resource_images)
        juju.deploy(POSTGRESQL, channel="14/stable", trust=True, config={"profile": "testing"})
        juju.deploy(
            TRAEFIK,
            channel="latest/stable",
            trust=True,
            config={"routing_mode": "subdomain", "external_hostname": "example.com"},
        )
        deploy_trino(juju, TRINO)
        juju.integrate(APP, POSTGRESQL)
        juju.wait(
            lambda s: (
                jubilant.all_active(s, APP, POSTGRESQL, TRINO, TRAEFIK)
                and jubilant.all_blocked(s, CONSUMER)
            ),
            error=lambda s: jubilant.any_error(s, APP, POSTGRESQL, TRINO, TRAEFIK),
            timeout=30 * 60,
        )

    def test_consumer_receives_the_ingress_address(self, juju: jubilant.Juju):
        relate_upstream(juju, APP, TRINO)
        juju.integrate(f"{CONSUMER}:{CONSUMER_ENDPOINT}", f"{APP}:{CATALOG_ENDPOINT}")
        juju.integrate(f"{APP}:ingress", f"{TRAEFIK}:ingress")
        ingress = urlsplit(_ingress_url(juju))

        data = wait_for_consumer_data(
            juju, lambda d: d.get("trino_url") == f"{ingress.hostname}:80"
        )

        assert ingress.scheme == "http"
        assert ingress.path.strip("/") == ""
        assert PUBLISHED_KEYS <= data.keys()
        assert jubilant.all_active(juju.status(), APP)

    def test_query_through_ingress_succeeds(self, juju: jubilant.Juju):
        username, _ = consumer_credentials(juju, consumer_data(juju))

        rows = _wait_for_query(juju, _ingress_url(juju).rstrip("/"), username)

        assert rows == [[1]]

    def test_web_ui_loads_through_ingress(self, juju: jubilant.Juju):
        request = _request(juju, _ingress_url(juju).rstrip("/") + "/trino-gateway")

        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            body = response.read().decode()

        assert response.status == 200
        assert "<html" in body.lower()

    def test_in_cluster_address_keeps_working(self, juju: jubilant.Juju):
        address = juju.status().apps[APP].units[UNIT].address
        username, _ = consumer_credentials(juju, consumer_data(juju))
        request = urllib.request.Request(
            f"http://{address}:8080/v1/statement",
            data=QUERY.encode(),
            headers={"X-Trino-User": username},
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            body = json.loads(response.read())

        assert "error" not in body

    def test_path_based_ingress_blocks_and_restores_in_cluster_address(self, juju: jubilant.Juju):
        juju.config(TRAEFIK, {"routing_mode": "path"})
        juju.wait(
            lambda s: (
                jubilant.all_blocked(s, APP)
                and "path-based" in s.apps[APP].units[UNIT].workload_status.message
            ),
            error=lambda s: jubilant.any_error(s, APP, TRAEFIK),
            timeout=10 * 60,
        )

        wait_for_consumer_data(juju, lambda d: d.get("trino_url") == _in_cluster_url(juju))

    def test_removed_ingress_restores_in_cluster_address(self, juju: jubilant.Juju):
        juju.config(TRAEFIK, {"routing_mode": "subdomain"})
        wait_for_consumer_data(juju, lambda d: d.get("trino_url") != _in_cluster_url(juju))

        juju.remove_relation(f"{APP}:ingress", f"{TRAEFIK}:ingress")

        wait_for_consumer_data(juju, lambda d: d.get("trino_url") == _in_cluster_url(juju))
        juju.wait(
            lambda s: jubilant.all_active(s, APP),
            error=lambda s: jubilant.any_error(s, APP),
            timeout=10 * 60,
        )
