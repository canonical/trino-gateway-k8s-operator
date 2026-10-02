#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm the application."""

import ops

SERVICE_NAME = "trino-gateway"
CONFIG_PATH = "/etc/trino-gateway/config.yaml"
PLACEHOLDER_CONFIG = """\
requestRouter:
  port: 8080
  name: trinoRouter
  historySize: 1000
dataStore:
  jdbcUrl: jdbc:h2:/tmp/trino-gateway
  user: sa
  password: sa
  driver: org.h2.Driver
server:
  applicationConnectors:
    - type: http
      port: 8081
  adminConnectors:
    - type: http
      port: 8082
"""


class TrinoGatewayK8SOperatorCharm(ops.CharmBase):
    """Charm the application."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        framework.observe(self.on["trino_gateway"].pebble_ready, self._on_pebble_ready)
        self.container = self.unit.get_container("trino-gateway")

    def _on_pebble_ready(self, event: ops.PebbleReadyEvent) -> None:
        """Configure and start the workload service."""
        layer: ops.pebble.LayerDict = {
            "services": {
                SERVICE_NAME: {
                    "override": "merge",
                    "startup": "enabled",
                }
            }
        }
        self.container.add_layer("charm", layer, combine=True)
        # TODO: remove once the charm renders the real config
        self.container.push(CONFIG_PATH, PLACEHOLDER_CONFIG, make_dirs=True)
        self.container.replan()
        self.unit.status = ops.ActiveStatus()


if __name__ == "__main__":  # pragma: nocover
    ops.main(TrinoGatewayK8SOperatorCharm)
