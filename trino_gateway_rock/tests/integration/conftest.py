# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for the Trino Gateway rock integration tests."""

from pathlib import Path

import jubilant
import pytest
from opcli.models.artifacts_build import ArtifactsGenerated
from opcli.pytest_plugin import artifacts_root_from_yaml_path, build_rock_images


@pytest.fixture(scope="session")
def gateway_image(opcli_artifacts: ArtifactsGenerated, opcli_build_yaml_path: Path) -> str:
    """Image reference of the Trino Gateway rock under test."""
    root = artifacts_root_from_yaml_path(opcli_build_yaml_path)
    image = build_rock_images(opcli_artifacts, root)["trino-gateway"]
    if image.endswith(".rock"):
        pytest.fail("Push the rock first: opcli artifacts push-images --missing-registry deploy")
    return image


@pytest.fixture(scope="module")
def namespace(juju: jubilant.Juju) -> str:
    """Kubernetes namespace of the test model."""
    assert juju.model
    return juju.model.rpartition(":")[2]
