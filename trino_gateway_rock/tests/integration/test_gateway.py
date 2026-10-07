# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the Trino Gateway rock, deployed as plain Kubernetes objects."""

import json
import logging
import subprocess
import textwrap
import time
import urllib.request
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import jubilant
import pytest

logger = logging.getLogger(__name__)

GATEWAY = "trino-gateway"
GATEWAY_NODE_PORT = 31880
TRINO_NODE_PORT = 31881
TRINO_USER = "rock-test"
DATABASE_NAME = "trino_gateway"


def run_kubectl(namespace: str, *args: str, stdin: str | None = None) -> str:
    """Run kubectl in the namespace and return its output."""
    result = subprocess.run(
        ["kubectl", "-n", namespace, *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"kubectl {args[0]} failed: {result.stderr}")
    return result.stdout


def send_http_request(
    url: str, body: bytes | None = None, headers: dict[str, str] | None = None
) -> str:
    """Send a GET, or a POST when a body is given, and return the response body."""
    request = urllib.request.Request(url, data=body, headers=headers or {})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode()


def generate_gateway_manifests(image: str, credentials: dict[str, str]) -> dict[str, Any]:
    """Generate Kubernetes objects running the Gateway rock and exposing the Gateway and Trino."""
    config = textwrap.dedent(f"""\
        serverConfig:
          node.environment: test
          http-server.http.port: 8080
        dataStore:
          jdbcUrl: jdbc:postgresql://{credentials["endpoints"]}/{credentials["database"]}
          user: {credentials["username"]}
          password: ${{ENV:DB_PASSWORD}}
          driver: org.postgresql.Driver
        clusterStatsConfiguration:
          monitorType: INFO_API
        """)
    labels = {"app": GATEWAY}
    container = {
        "name": GATEWAY,
        "image": image,
        "ports": [{"containerPort": 8080}],
        "env": [
            {
                "name": "DB_PASSWORD",
                "valueFrom": {"secretKeyRef": {"name": GATEWAY, "key": "db-password"}},
            }
        ],
        "volumeMounts": [
            {
                "name": "config",
                "mountPath": "/etc/trino-gateway/config.yaml",
                "subPath": "config.yaml",
            }
        ],
        "readinessProbe": {"httpGet": {"path": "/trino-gateway/readyz", "port": 8080}},
    }
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": GATEWAY},
                "stringData": {"db-password": credentials["password"]},
            },
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": GATEWAY},
                "data": {"config.yaml": config},
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": GATEWAY, "labels": labels},
                "spec": {
                    "selector": {"matchLabels": labels},
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "containers": [container],
                            "volumes": [{"name": "config", "configMap": {"name": GATEWAY}}],
                        },
                    },
                },
            },
            generate_node_port_service(GATEWAY, labels, GATEWAY_NODE_PORT),
            generate_node_port_service(
                "trino-k8s-node-port", {"app.kubernetes.io/name": "trino-k8s"}, TRINO_NODE_PORT
            ),
        ],
    }


def generate_node_port_service(
    name: str, selector: dict[str, str], node_port: int
) -> dict[str, Any]:
    """Generate a NodePort Service forwarding port 8080 of the selected pods."""
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name},
        "spec": {
            "type": "NodePort",
            "selector": selector,
            "ports": [{"port": 8080, "targetPort": 8080, "nodePort": node_port}],
        },
    }


@pytest.fixture(scope="module", autouse=True)
def dump_diagnostics_on_failure(request: pytest.FixtureRequest, namespace: str) -> Iterator[None]:
    """Log Gateway and namespace diagnostics if any test failed."""
    yield
    if not request.session.testsfailed:
        return
    for args in (
        ("exec", f"deployment/{GATEWAY}", "--", "/usr/bin/pebble", "logs", "-n", "200"),
        ("describe", "pod", "-l", f"app={GATEWAY}"),
        ("get", "events", "--sort-by=.lastTimestamp"),
    ):
        result = subprocess.run(
            ["kubectl", "-n", namespace, *args], capture_output=True, text=True, check=False
        )
        logger.error("kubectl %s:\n%s%s", " ".join(args), result.stdout, result.stderr)


@pytest.fixture(scope="module")
def node_ip(namespace: str) -> str:
    """Return the internal IP of the node serving the NodePort Services."""
    address = "{.items[0].status.addresses[?(@.type=='InternalIP')].address}"
    return run_kubectl(namespace, "get", "nodes", "-o", f"jsonpath={address}")


@pytest.fixture(scope="module")
def gateway_url(node_ip: str) -> str:
    """Return the Gateway base URL as seen from the test runner."""
    return f"http://{node_ip}:{GATEWAY_NODE_PORT}"


def test_deploy_dependencies(juju: jubilant.Juju):
    """Deploy PostgreSQL and Trino, which the Gateway needs before it starts."""
    juju.deploy("postgresql-k8s", channel="14/stable", trust=True)
    juju.deploy(
        "data-integrator", channel="latest/stable", config={"database-name": DATABASE_NAME}
    )
    juju.integrate("data-integrator:postgresql", "postgresql-k8s:database")
    juju.deploy("trino-k8s", channel="latest/edge", trust=True)
    juju.wait(jubilant.all_active, error=jubilant.any_error, timeout=30 * 60)


def test_gateway_ready(juju: jubilant.Juju, namespace: str, gateway_image: str, gateway_url: str):
    """The Gateway rock starts with its database and reports ready."""
    credentials = juju.run("data-integrator/0", "get-credentials").results["postgresql"]
    manifests = generate_gateway_manifests(gateway_image, credentials)
    run_kubectl(namespace, "apply", "-f", "-", stdin=json.dumps(manifests))
    run_kubectl(namespace, "rollout", "status", f"deployment/{GATEWAY}", "--timeout=10m")

    with urllib.request.urlopen(f"{gateway_url}/trino-gateway/readyz", timeout=30) as response:
        assert response.status == 200


def test_no_backends_registered(gateway_url: str):
    """A fresh Gateway has no Trino backends."""
    assert json.loads(send_http_request(f"{gateway_url}/gateway/backend/all")) == []


def test_register_trino_backend(namespace: str, gateway_url: str):
    """The Gateway marks a registered Trino backend as healthy."""
    backend = {
        "name": "trino-k8s",
        "proxyTo": f"http://trino-k8s.{namespace}.svc.cluster.local:8080",
        "active": True,
        "routingGroup": "adhoc",
    }
    send_http_request(
        f"{gateway_url}/gateway/backend/modify/add",
        body=json.dumps(backend).encode(),
        headers={"Content-Type": "application/json"},
    )

    state_url = f"{gateway_url}/api/public/backends/trino-k8s/state"
    deadline = time.monotonic() + 5 * 60
    while json.loads(send_http_request(state_url))["trinoStatus"] != "HEALTHY":
        assert time.monotonic() < deadline, "Trino backend did not become healthy"
        time.sleep(5)


def test_query_routed_through_gateway(node_ip: str, gateway_url: str):
    """A query sent to the Gateway runs on Trino and every follow-up goes through the Gateway."""
    headers = {"X-Trino-User": TRINO_USER}
    response = json.loads(
        send_http_request(
            f"{gateway_url}/v1/statement", body=b"SELECT 1, 'gateway'", headers=headers
        )
    )
    rows = []
    while "nextUri" in response:
        assert urlparse(response["nextUri"]).netloc == f"{node_ip}:{GATEWAY_NODE_PORT}"
        response = json.loads(send_http_request(response["nextUri"], headers=headers))
        rows += response.get("data", [])

    assert response["stats"]["state"] == "FINISHED"
    assert rows == [[1, "gateway"]]

    trino_url = f"http://{node_ip}:{TRINO_NODE_PORT}"
    query = json.loads(
        send_http_request(f"{trino_url}/v1/query/{response['id']}", headers=headers)
    )
    assert query["state"] == "FINISHED"
