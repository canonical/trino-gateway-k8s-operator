#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm for Trino Gateway on Kubernetes."""

import logging

import ops
import pydantic
from charms.data_platform_libs.v0.data_interfaces import DatabaseRequires

import gateway_api
import workload
from config import CharmConfig, describe_error
from constants import CONTAINER_NAME, DATABASE_NAME, HTTP_PORT, POSTGRESQL_RELATION
from gateway_api import GatewayApiClient, GatewayApiError
from workload import PostgresRelationModel

logger = logging.getLogger(__name__)


class TrinoGatewayK8SOperatorCharm(ops.CharmBase):
    """Charm for Trino Gateway on Kubernetes."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self._container = self.unit.get_container(CONTAINER_NAME)
        self._postgresql = DatabaseRequires(self, POSTGRESQL_RELATION, database_name=DATABASE_NAME)

        for event in (
            self.on[CONTAINER_NAME].pebble_ready,
            self.on.config_changed,
            self.on.upgrade_charm,
            self.on.update_status,
            self.on.leader_elected,
            self.on.secret_changed,
            self.on[POSTGRESQL_RELATION].relation_changed,
            self.on[POSTGRESQL_RELATION].relation_broken,
        ):
            framework.observe(event, self._reconcile)
        framework.observe(self.on.collect_unit_status, self._on_collect_unit_status)

    def _reconcile(self, _: ops.EventBase) -> None:
        """Converge the workload on the state described by the relation."""
        if not self._container.can_connect():
            return
        # The service is deliberately left running without the relation, so clients get
        # database errors from the gateway rather than refused connections.
        pg = self._load_postgresql()
        if pg is None:
            return

        workload.apply(self._container, workload.render_config(pg))
        self.unit.set_ports(HTTP_PORT)

        if not self.unit.is_leader():
            return
        try:
            config = self.load_config(CharmConfig, errors="raise")
        except pydantic.ValidationError:
            return
        try:
            gateway_api.sync_backends(
                GatewayApiClient(f"http://localhost:{HTTP_PORT}"), config.backends
            )
        except GatewayApiError as e:
            # The gateway may still be starting after a restart; a later hook converges.
            logger.warning("Backend sync failed, retrying on a later hook: %s", e)

    def _on_collect_unit_status(self, event: ops.CollectStatusEvent) -> None:
        """Derive the unit status from observed state."""
        if not self._container.can_connect():
            event.add_status(ops.MaintenanceStatus("waiting for Pebble"))

        if self.model.get_relation(POSTGRESQL_RELATION) is None:
            event.add_status(
                ops.BlockedStatus(f"missing required relation: {POSTGRESQL_RELATION}")
            )
        elif self._load_postgresql() is None:
            event.add_status(ops.WaitingStatus("waiting for postgresql database"))
        elif self._container.can_connect() and not workload.is_running(self._container):
            event.add_status(ops.MaintenanceStatus("gateway service is not running"))

        try:
            config = self.load_config(CharmConfig, errors="raise")
        except pydantic.ValidationError as e:
            event.add_status(ops.BlockedStatus(f"invalid backends config: {describe_error(e)}"))
            return
        event.add_status(ops.ActiveStatus("" if config.backends else "no backends configured"))

    def _load_postgresql(self) -> PostgresRelationModel | None:
        """Load the PostgreSQL relation data, or None if it is absent or incomplete."""
        relation = self.model.get_relation(POSTGRESQL_RELATION)
        if relation is None:
            return None
        try:
            return relation.load(
                PostgresRelationModel,
                relation.app,
                decoder=PostgresRelationModel.decode(self),
            )
        except (pydantic.ValidationError, ops.SecretNotFoundError):
            return None


if __name__ == "__main__":  # pragma: nocover
    ops.main(TrinoGatewayK8SOperatorCharm)
