# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
#
import jubilant


def test_deploy(charm_path: str, resource_images: dict[str, str], juju: jubilant.Juju):
    """Deploy the charm under test."""
    juju.deploy(charm_path, app="trino-gateway-k8s", resources=resource_images)
    juju.wait(jubilant.all_active)
