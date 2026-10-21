# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Consumers related over `trino-catalog` get connection data and query through the gateway.

The consumer is a second gateway application without PostgreSQL: it requires `trino-catalog`
through the same charm library as real consumers and publishes its `app_name`, while its own
workload is never needed. The Trino applications are identical, so which upstream a query
reaches never matters.
"""

import json
import textwrap
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import jubilant
import pytest
from conftest import DeployTrino
from helpers import APP, CATALOG_ENDPOINT, UNIT, backend_names, relate_upstream

POSTGRESQL = "postgresql-k8s"
CONSUMER = "consumer"
CONSUMER_UNIT = f"{CONSUMER}/0"
CONSUMER_ENDPOINT = "backend-trino-catalog"
TRINO_A, TRINO_B, TRINO_C = "trino-a", "trino-b", "trino-c"
QUERY = "SELECT count(*) FROM system.runtime.nodes"
# Hive catalogs need no secrets, and Trino starts without reaching their metastore.
CATALOG_CONFIG = textwrap.dedent("""\
    catalogs:
      sales:
        backend: dummy
      marketing:
        backend: dummy
    backends:
      dummy:
        connector: hive
        url: thrift://metastore.invalid:9083
""")
CATALOGS = ["marketing", "sales"]
PUBLISHED_KEYS = {"trino_url", "trino_catalogs", "trino_credentials_secret_id"}


def _consumer_data(juju: jubilant.Juju) -> dict[str, str]:
    """Read the data the gateway published to the consumer, as the consumer sees it."""
    unit_info = json.loads(juju.cli("show-unit", CONSUMER_UNIT, "--format", "json"))
    relation = next(
        r for r in unit_info[CONSUMER_UNIT]["relation-info"] if r["endpoint"] == CONSUMER_ENDPOINT
    )
    return relation.get("application-data", {})


def _catalog_names(data: dict[str, str]) -> list[str]:
    return sorted(c["name"] for c in json.loads(data["trino_catalogs"]))


def _wait_for_consumer_data(juju: jubilant.Juju, check, timeout: float = 600) -> dict[str, str]:
    """Poll the consumer's relation data until `check` accepts it."""
    deadline = time.monotonic() + timeout
    data: dict[str, str] = {}
    while time.monotonic() < deadline:
        data = _consumer_data(juju)
        if check(data):
            return data
        time.sleep(10)
    raise AssertionError(f"consumer data never matched: {data}")


def _credentials(juju: jubilant.Juju, data: dict[str, str]) -> tuple[str, str]:
    content = juju.show_secret(data["trino_credentials_secret_id"], reveal=True).content
    return content["username"], content["password"]


def _query(juju: jubilant.Juju, credentials: tuple[str, str]) -> list:
    """Run a query through the gateway Service as the consumer's user.

    Over plain HTTP, Trino refuses passwords and trusts the username, and the gateway rejects
    `X-Forwarded-Proto`, so only the username can be checked end to end. Follow-up URLs are sent
    to the gateway's own address.
    """
    base = f"http://{juju.status().apps[APP].address}:8080"
    headers = {"X-Trino-User": credentials[0]}
    request = urllib.request.Request(
        f"{base}/v1/statement", data=QUERY.encode(), headers=headers, method="POST"
    )
    rows = []
    while True:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            body = json.loads(response.read())
        if "error" in body:
            raise AssertionError(f"query failed: {body['error']}")
        rows.extend(body.get("data", []))
        next_uri = body.get("nextUri")
        if not next_uri:
            return rows
        request = urllib.request.Request(base + urlsplit(next_uri).path, headers=headers)
        time.sleep(0.2)


def _wait_for_query(juju: jubilant.Juju, credentials: tuple[str, str], timeout: float = 600):
    """Retry the query until the gateway reports a healthy backend for it."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return _query(juju, credentials)
        except (urllib.error.URLError, ConnectionError, AssertionError) as e:
            if isinstance(e, urllib.error.HTTPError):
                e.close()
            if time.monotonic() > deadline:
                raise
            time.sleep(10)


@pytest.mark.incremental
class TestTrinoCatalog:
    def test_deploy(
        self,
        charm_path: str,
        resource_images: dict[str, str],
        deploy_trino: DeployTrino,
        juju: jubilant.Juju,
    ):
        """The gateway, its consumer, PostgreSQL and three identical Trino applications deploy."""
        juju.model_config({"update-status-hook-interval": "30s"})
        juju.deploy(charm_path, app=APP, resources=resource_images)
        juju.deploy(charm_path, app=CONSUMER, resources=resource_images)
        juju.deploy(POSTGRESQL, channel="14/stable", trust=True, config={"profile": "testing"})
        for app in (TRINO_A, TRINO_B, TRINO_C):
            deploy_trino(juju, app, {"catalog-config": CATALOG_CONFIG})
        juju.integrate(APP, POSTGRESQL)
        juju.wait(
            lambda s: (
                jubilant.all_active(s, APP, POSTGRESQL, TRINO_A, TRINO_B, TRINO_C)
                and jubilant.all_blocked(s, CONSUMER)
            ),
            error=lambda s: jubilant.any_error(s, APP, POSTGRESQL, TRINO_A, TRINO_B, TRINO_C),
            timeout=30 * 60,
        )

    def test_consumer_receives_data(self, juju: jubilant.Juju):
        """A consumer gets the gateway URL, its catalogs and its secret together.

        The consumer may first be published with no catalogs, before the upstream confirms it
        and is registered.
        """
        relate_upstream(juju, APP, TRINO_A)
        juju.integrate(f"{CONSUMER}:{CONSUMER_ENDPOINT}", f"{APP}:{CATALOG_ENDPOINT}")

        data = _wait_for_consumer_data(
            juju, lambda d: PUBLISHED_KEYS <= d.keys() and _catalog_names(d) == CATALOGS
        )

        assert data["trino_url"] == f"{APP}.{juju.model}.svc.cluster.local:8080"
        assert _catalog_names(data) == CATALOGS
        assert backend_names(juju) == {TRINO_A}

    def test_query_through_gateway_succeeds(self, juju: jubilant.Juju):
        credentials = _credentials(juju, _consumer_data(juju))

        rows = _wait_for_query(juju, credentials)

        assert rows == [[1]]

    def test_second_upstream_does_not_interrupt_the_consumer(self, juju: jubilant.Juju):
        """Queries keep succeeding while a second upstream is added and registered."""
        before = _consumer_data(juju)
        credentials = _credentials(juju, before)

        relate_upstream(juju, APP, TRINO_B)
        deadline = time.monotonic() + 15 * 60
        queries = 0
        while backend_names(juju) != {TRINO_A, TRINO_B}:
            assert time.monotonic() < deadline, "trino-b was never registered"
            assert _query(juju, credentials) == [[1]]
            queries += 1
            time.sleep(5)
        for _ in range(10):
            assert _query(juju, credentials) == [[1]]

        assert queries > 0
        assert _consumer_data(juju) == before

    def test_upstream_without_integration_is_never_registered(self, juju: jubilant.Juju):
        juju.integrate(f"{APP}:backend-trino-catalog", f"{TRINO_C}:{CATALOG_ENDPOINT}")
        juju.wait(
            lambda s: (
                s.apps[APP].units[UNIT].workload_status.message
                == "1 upstream waiting for registration"
            ),
            error=lambda s: jubilant.any_error(s, APP, TRINO_C),
            timeout=10 * 60,
        )
        juju.wait(lambda s: jubilant.all_agents_idle(s, APP, TRINO_C), delay=5, successes=6)

        assert backend_names(juju) == {TRINO_A, TRINO_B}

    def test_excluded_catalog_is_missing(self, juju: jubilant.Juju):
        juju.config(APP, {"catalog-exclusions": f"{CONSUMER}: [sales]"})

        data = _wait_for_consumer_data(juju, lambda d: _catalog_names(d) == ["marketing"])

        assert PUBLISHED_KEYS <= data.keys()
