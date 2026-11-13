# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import time
import urllib.error
import urllib.request

import jubilant

APP = "trino-gateway-k8s"
UNIT = f"{APP}/0"
CATALOG_ENDPOINT = "trino-catalog"
INTEGRATION_ENDPOINT = "trino-gateway-integration"


def relate_upstream(juju: jubilant.Juju, gateway: str, trino: str) -> None:
    """Relate a Trino application to the gateway over both endpoints registration needs."""
    juju.integrate(f"{gateway}:backend-trino-catalog", f"{trino}:{CATALOG_ENDPOINT}")
    juju.integrate(f"{gateway}:{INTEGRATION_ENDPOINT}", f"{trino}:{INTEGRATION_ENDPOINT}")


def gateway_request(juju: jubilant.Juju, path: str, body: dict | None = None) -> bytes:
    """Send a request to the gateway unit's REST API and return the response body."""
    address = juju.status().apps[APP].units[UNIT].address
    request = urllib.request.Request(
        f"http://{address}:8080{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return response.read()


def backend_names(juju: jubilant.Juju) -> set[str] | None:
    """List the names of the gateway's backends, or None if the gateway cannot be reached."""
    try:
        return {b["name"] for b in json.loads(gateway_request(juju, "/gateway/backend/all"))}
    except (urllib.error.URLError, ConnectionError):
        return None


def wait_for_backends(juju: jubilant.Juju, expected: list[dict], timeout: float = 600) -> None:
    """Poll the gateway until its backends match; the charm syncs on its next hook."""
    keys = ("name", "proxyTo", "active", "routingGroup")
    deadline = time.monotonic() + timeout
    current = None
    while time.monotonic() < deadline:
        try:
            backends = json.loads(gateway_request(juju, "/gateway/backend/all"))
            current = sorted(({k: b.get(k) for k in keys} for b in backends), key=str)
        except (urllib.error.URLError, ConnectionError):
            current = None
        if current == sorted(({k: b[k] for k in keys} for b in expected), key=str):
            return
        time.sleep(10)
    raise AssertionError(f"gateway backends {current} never matched {expected}")
