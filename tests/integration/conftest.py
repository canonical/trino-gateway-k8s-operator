# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
#
"""Integration test configuration.

The pytest-opcli plugin provides the session-scoped charm_path and resource_images fixtures.
"""

import pytest

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
