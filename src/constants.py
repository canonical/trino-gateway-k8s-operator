# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Project-wide constants."""

CONTAINER_NAME = "trino-gateway"
SERVICE_NAME = "trino-gateway"
# Set by the rock's service command.
CONFIG_PATH = "/etc/trino-gateway/config.yaml"
WORKLOAD_USER = "_daemon_"
HTTP_PORT = 8080
DEFAULT_ROUTING_GROUP = "adhoc"
# Identifies this deployment in telemetry; must match [a-z0-9][_a-z0-9]*.
NODE_ENVIRONMENT = "trino_gateway_k8s"

POSTGRESQL_RELATION = "postgresql"
BACKEND_TRINO_CATALOG_RELATION = "backend-trino-catalog"
# Up to 63 characters, starting with a letter or digit.
BACKEND_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$"
DATABASE_NAME = "trino_gateway"
