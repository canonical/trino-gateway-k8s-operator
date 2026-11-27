# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""OAuth login to the gateway's web UI and REST API, behind traefik with TLS.

`oauth-external-idp-integrator` registers a stub client with Google's endpoints, so no user
logs in; the test checks the gateway's configuration and what it refuses. Requests reach
traefik at its unit address with the ingress host as TLS server name and `Host` header, so the
test needs no DNS for the ingress host.
"""

import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

import jubilant
import pytest
import yaml
from conftest import DeployTrino
from helpers import (
    APP,
    CATALOG_ENDPOINT,
    CONSUMER,
    CONSUMER_ENDPOINT,
    UNIT,
    consumer_credentials,
    ingress_url,
    relate_upstream,
    wait_for_consumer_data,
)

POSTGRESQL = "postgresql-k8s"
TRAEFIK = "traefik-k8s"
CERTIFICATES = "self-signed-certificates"
OAUTH = "oauth-external-idp-integrator"
TRINO = "trino"
OAUTH_STUB_CONFIG = {
    "issuer_url": "https://accounts.google.com",
    "authorization_endpoint": "https://accounts.google.com/o/oauth2/auth",
    "token_endpoint": "https://oauth2.googleapis.com/token",  # nosec B105
    "introspection_endpoint": "https://oauth2.googleapis.com/tokeninfo",
    "userinfo_endpoint": "https://www.googleapis.com/oauth2/v1/userinfo",
    "jwks_endpoint": "https://www.googleapis.com/oauth2/v3/certs",
    "scope": "openid profile email",
    "client_id": "stub-client-id",
    "client_secret": "stub-client-secret",  # nosec B105
}
# Requests the web UI and REST API refuse without a login, as `(method, path)`.
PROTECTED = [
    ("POST", "/userinfo"),
    ("POST", "/webapp/getAllBackends"),
    ("POST", "/webapp/saveBackend"),
    ("GET", "/gateway/backend/all"),
    ("POST", "/gateway/backend/modify/add"),
]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """An HTTPS connection to a fixed address, sending the URL's host as TLS server name."""

    def __init__(self, host: str, address: str, context: ssl.SSLContext):
        super().__init__(host, 443, context=context, timeout=30)
        self._address = address
        self._context = context

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _https(
    juju: jubilant.Juju, url: str, method: str = "GET", body: str | None = None
) -> tuple[int, bytes]:
    """Send a request for an HTTPS ingress URL to traefik, trusting only the test's CA.

    Returns:
        The status code and the response body.
    """
    ca = juju.run(f"{CERTIFICATES}/0", "get-ca-certificate").results["ca-certificate"]
    traefik = juju.status().apps[TRAEFIK].units[f"{TRAEFIK}/0"].address
    parsed = urlsplit(url)
    assert parsed.hostname is not None
    context = ssl.create_default_context(cadata=ca)
    # Traefik's certificate names its `external_hostname` only, not the per-application hosts
    # of `subdomain` mode.
    context.check_hostname = False
    connection = _PinnedHTTPSConnection(parsed.hostname, traefik, context)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    try:
        connection.request(method, parsed.path or "/", body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _wait_for_tls(juju: jubilant.Juju, url: str, timeout: float = 600) -> None:
    """Wait until traefik serves a certificate issued by the test's CA."""
    deadline = time.monotonic() + timeout
    error = None
    while time.monotonic() < deadline:
        try:
            _https(juju, url)
            return
        except (ssl.SSLError, ConnectionError) as e:
            error = e
            time.sleep(10)
    raise AssertionError(f"no trusted certificate for {url} within {timeout}s") from error


def _gateway(juju: jubilant.Juju, method: str, path: str) -> int:
    """Send a request to the gateway unit's in-cluster address and return the status code."""
    address = juju.status().apps[APP].units[UNIT].address
    request = urllib.request.Request(
        f"http://{address}:8080{path}",
        data=b"{}" if method == "POST" else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status
    except urllib.error.HTTPError as e:
        e.close()
        return e.code


def _registered_redirect_uri(juju: jubilant.Juju, timeout: float = 600) -> str:
    """Wait for the redirect URI the gateway registers, as the OAuth provider sees it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = juju.cli("show-unit", f"{OAUTH}/0", "--format=yaml")
        for relation in yaml.safe_load(raw)[f"{OAUTH}/0"].get("relation-info", []):
            uri = relation.get("application-data", {}).get("redirect_uri")
            if relation.get("endpoint") == "oauth" and uri:
                return uri
        time.sleep(10)
    raise AssertionError(f"no redirect URI registered within {timeout}s")


def _wait_for_status(juju: jubilant.Juju, path: str, status: int, timeout: float = 600) -> None:
    """Poll a GET at the in-cluster address until it returns a status; restarts take time."""
    deadline = time.monotonic() + timeout
    code = None
    while time.monotonic() < deadline:
        try:
            code = _gateway(juju, "GET", path)
        except (urllib.error.URLError, ConnectionError):
            code = None
        if code == status:
            return
        time.sleep(10)
    raise AssertionError(f"GET {path} returned {code}, never {status}")


def _wait_for_public_backends(juju: jubilant.Juju, names: set[str], timeout: float = 600) -> None:
    """Poll the backends the gateway lists without a login until their names match."""
    deadline = time.monotonic() + timeout
    current = None
    while time.monotonic() < deadline:
        address = juju.status().apps[APP].units[UNIT].address
        url = f"http://{address}:8080/api/public/backends"
        try:
            with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
                current = {backend["name"] for backend in json.loads(response.read())}
        except (urllib.error.URLError, ConnectionError):
            current = None
        if current == names:
            return
        time.sleep(10)
    raise AssertionError(f"gateway backends {current} never matched {names}")


def _wait_for_query(juju: jubilant.Juju, username: str, timeout: float = 600) -> None:
    """Retry a query at the in-cluster address until Trino accepts it.

    Trino restarts to apply the users the gateway requests, so the first attempts can fail.
    """
    address = juju.status().apps[APP].units[UNIT].address
    deadline = time.monotonic() + timeout
    error = None
    while time.monotonic() < deadline:
        request = urllib.request.Request(
            f"http://{address}:8080/v1/statement",
            data=b"SELECT 1",
            headers={"X-Trino-User": username},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                body = json.loads(response.read())
            if "error" not in body:
                return
            error = body["error"]
        except (urllib.error.URLError, ConnectionError) as e:
            if isinstance(e, urllib.error.HTTPError):
                e.close()
            error = e
        time.sleep(10)
    raise AssertionError(f"query never succeeded within {timeout}s: {error}")


@pytest.mark.incremental
class TestOAuth:
    def test_deploy(
        self,
        charm_path: str,
        resource_images: dict[str, str],
        deploy_trino: DeployTrino,
        juju: jubilant.Juju,
    ):
        """The gateway, its consumer, PostgreSQL, Trino, traefik and the providers deploy."""
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
        juju.deploy(CERTIFICATES, channel="1/stable")
        juju.deploy(OAUTH, channel="latest/edge", config=OAUTH_STUB_CONFIG)
        deploy_trino(juju, TRINO)
        juju.integrate(APP, POSTGRESQL)
        juju.integrate(f"{APP}:ingress", f"{TRAEFIK}:ingress")
        juju.wait(
            lambda s: (
                jubilant.all_active(s, APP, POSTGRESQL, TRINO, TRAEFIK, CERTIFICATES)
                and jubilant.all_blocked(s, CONSUMER)
            ),
            error=lambda s: jubilant.any_error(s, APP, POSTGRESQL, TRINO, TRAEFIK, CERTIFICATES),
            timeout=30 * 60,
        )

    def test_oauth_requires_https_ingress(self, juju: jubilant.Juju):
        """Over an HTTP ingress, the unit is Blocked and the gateway lets only the charm in."""
        juju.integrate(f"{APP}:oauth", f"{OAUTH}:oauth")

        juju.wait(
            lambda s: (
                jubilant.all_blocked(s, APP)
                and s.apps[APP].units[UNIT].workload_status.message
                == "OAuth requires an HTTPS ingress URL"
            ),
            error=lambda s: jubilant.any_error(s, APP, OAUTH),
            timeout=10 * 60,
        )
        _wait_for_status(juju, "/gateway/backend/all", 403)
        assert _gateway(juju, "GET", "/trino-gateway") == 200

    def test_https_ingress_configures_the_oauth_login(self, juju: jubilant.Juju):
        """The client is registered at the gateway's callback, and the login sends users to it."""
        juju.integrate(f"{TRAEFIK}:certificates", f"{CERTIFICATES}:certificates")
        url = ingress_url(juju, scheme="https").rstrip("/")
        juju.wait(
            lambda s: jubilant.all_active(s, APP, TRAEFIK, CERTIFICATES, OAUTH),
            error=lambda s: jubilant.any_error(s, APP, TRAEFIK, CERTIFICATES, OAUTH),
            timeout=15 * 60,
        )
        _wait_for_tls(juju, f"{url}/trino-gateway")

        assert _registered_redirect_uri(juju) == f"{url}/oidc/callback"
        status, body = _https(juju, f"{url}/loginType", "POST", "{}")
        assert status == 200
        assert json.loads(body)["data"] == ["oauth"]
        status, body = _https(juju, f"{url}/sso", "POST", "{}")
        assert status == 200
        login = urlsplit(json.loads(body)["data"])
        query = parse_qs(login.query)
        assert login.geturl().startswith(OAUTH_STUB_CONFIG["authorization_endpoint"])
        assert query["client_id"] == [OAUTH_STUB_CONFIG["client_id"]]
        assert query["redirect_uri"] == [f"{url}/oidc/callback"]

    def test_unauthenticated_requests_are_refused(self, juju: jubilant.Juju):
        url = ingress_url(juju, scheme="https").rstrip("/")

        status, body = _https(juju, f"{url}/trino-gateway")
        assert status == 200, "the web UI's page loads, and shows the login"
        assert b"<html" in body.lower()
        for method, path in PROTECTED:
            status, _ = _https(juju, f"{url}{path}", method, "{}" if method == "POST" else None)
            assert status == 403, f"{method} {path} through the ingress returned {status}"
            assert _gateway(juju, method, path) == 403, f"{method} {path} in the cluster"

    def test_charm_still_manages_backends(self, juju: jubilant.Juju):
        """Trino is registered and the consumer is published, with the ingress address."""
        host = urlsplit(ingress_url(juju, scheme="https")).hostname
        relate_upstream(juju, APP, TRINO)
        juju.integrate(f"{CONSUMER}:{CONSUMER_ENDPOINT}", f"{APP}:{CATALOG_ENDPOINT}")

        _wait_for_public_backends(juju, {TRINO})
        data = wait_for_consumer_data(juju, lambda d: d.get("trino_url") == f"{host}:443")

        username, _ = consumer_credentials(juju, data)
        _wait_for_query(juju, username)

    def test_oauth_removal_opens_the_ui_and_api(self, juju: jubilant.Juju):
        juju.remove_relation(f"{APP}:oauth", f"{OAUTH}:oauth")

        _wait_for_status(juju, "/gateway/backend/all", 200)
        _wait_for_public_backends(juju, {TRINO})
        juju.wait(
            lambda s: jubilant.all_active(s, APP),
            error=lambda s: jubilant.any_error(s, APP),
            timeout=10 * 60,
        )
