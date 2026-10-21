# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
#
"""Integration test configuration.

The pytest-opcli plugin provides the session-scoped charm_path and resource_images fixtures.
"""

from collections.abc import Callable
from pathlib import Path

import jubilant
import pytest

TRINO_CHARM = "trino-k8s"
TRINO_CHANNEL = "latest/edge"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--trino-charm",
        default=TRINO_CHARM,
        help=f"Trino charm to deploy: a Charmhub name ({TRINO_CHANNEL}) or a local .charm path.",
    )
    parser.addoption(
        "--trino-image",
        default=None,
        help="OCI image for the trino-image resource; required with a local --trino-charm.",
    )


DeployTrino = Callable[..., None]


@pytest.fixture(scope="session")
def deploy_trino(request: pytest.FixtureRequest) -> DeployTrino:
    """Return a function that deploys Trino from Charmhub or from a local charm."""
    charm: str = request.config.getoption("--trino-charm")
    image: str | None = request.config.getoption("--trino-image")
    local = charm.endswith(".charm")
    if local and not image:
        raise pytest.UsageError("--trino-image is required with a local --trino-charm")

    def deploy(juju: jubilant.Juju, app: str, config: dict | None = None) -> None:
        config = {"charm-function": "all", **(config or {})}
        if local:
            juju.deploy(
                Path(charm).resolve(),
                app=app,
                trust=True,
                config=config,
                resources={"trino-image": image or ""},
            )
        else:
            juju.deploy(charm, app=app, channel=TRINO_CHANNEL, trust=True, config=config)

    return deploy


# Test class node ID -> name of its first failed test.
_FAILED_TESTS: dict[str, str] = {}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "incremental: xfail remaining tests in a class once one of them fails"
    )


def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> None:
    if call.excinfo is not None and item.get_closest_marker("incremental"):
        _FAILED_TESTS.setdefault(item.nodeid.rsplit("::", 1)[0], item.name)


def pytest_runtest_setup(item: pytest.Item) -> None:
    if not item.get_closest_marker("incremental"):
        return
    failed = _FAILED_TESTS.get(item.nodeid.rsplit("::", 1)[0])
    if failed is not None:
        pytest.xfail(f"previous test failed: {failed}")
