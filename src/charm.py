#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm for Trino Gateway on Kubernetes."""

import ops
import pydantic
from charms.data_platform_libs.v0.data_interfaces import DatabaseRequires

import workload
from workload import PostgresRelationModel

CONTAINER_NAME = "trino-gateway"
POSTGRESQL_RELATION = "postgresql"
DATABASE_NAME = "trino_gateway"


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
        self.unit.set_ports(workload.HTTP_PORT)

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

        event.add_status(ops.ActiveStatus())

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
