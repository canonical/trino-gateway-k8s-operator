#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm for Trino Gateway on Kubernetes."""

import logging

import ops
import pydantic
from charms.data_platform_libs.v0.data_interfaces import DatabaseRequires
from charms.trino_k8s.v0.trino_catalog import TrinoCatalogRequirer

import constants
import gateway_api
import workload
from config import BackendConfig, CharmConfig, describe_error
from core.backends import BackendNameConflictError, merge_backends
from gateway_api import GatewayApiClient, GatewayApiError
from relations import backend_trino_catalog
from relations.backend_trino_catalog import RelatedBackends
from workload import PostgresRelationModel

logger = logging.getLogger(__name__)


class TrinoGatewayK8SOperatorCharm(ops.CharmBase):
    """Charm for Trino Gateway on Kubernetes."""

    def __init__(self, framework: ops.Framework):
        """Initialise the charm and observe the events that trigger a reconcile.

        Args:
            framework: The Ops framework.
        """
        super().__init__(framework)
        self._container = self.unit.get_container(constants.CONTAINER_NAME)
        self._postgresql = DatabaseRequires(
            self, constants.POSTGRESQL_RELATION, database_name=constants.DATABASE_NAME
        )
        self._trino_catalog = TrinoCatalogRequirer(self, constants.BACKEND_TRINO_CATALOG_RELATION)

        for event in (
            self.on[constants.CONTAINER_NAME].pebble_ready,
            self.on.config_changed,
            self.on.upgrade_charm,
            self.on.update_status,
            self.on.leader_elected,
            self.on.secret_changed,
            self.on[constants.POSTGRESQL_RELATION].relation_changed,
            self.on[constants.POSTGRESQL_RELATION].relation_broken,
            self.on[constants.BACKEND_TRINO_CATALOG_RELATION].relation_changed,
            self.on[constants.BACKEND_TRINO_CATALOG_RELATION].relation_broken,
        ):
            framework.observe(event, self._reconcile)
        framework.observe(self.on.collect_unit_status, self._on_collect_unit_status)

    def _reconcile(self, _: ops.EventBase) -> None:
        """Converge the workload and, on the leader, the gateway's backends.

        Args:
            _: The event that triggered the reconcile, which does not affect the outcome.
        """
        if not self._container.can_connect():
            return
        # The service is deliberately left running without the relation, so clients get
        # database errors from the gateway rather than refused connections.
        pg = self._load_postgresql()
        if pg is None:
            return

        workload.apply(self._container, workload.render_config(pg))
        self.unit.set_ports(constants.HTTP_PORT)

        if not self.unit.is_leader():
            return
        try:
            config = self.load_config(CharmConfig, errors="raise")
        except pydantic.ValidationError:
            return
        try:
            desired, _ = self._load_backends(config)
        except BackendNameConflictError as e:
            logger.error("Backend sync stopped: %s", e)
            return
        try:
            gateway_api.sync_backends(
                GatewayApiClient(f"http://localhost:{constants.HTTP_PORT}"), desired
            )
        except GatewayApiError as e:
            # The gateway may still be starting after a restart; a later hook converges.
            logger.warning("Backend sync failed, retrying on a later hook: %s", e)

    def _on_collect_unit_status(self, event: ops.CollectStatusEvent) -> None:
        """Derive the unit status from observed state.

        Args:
            event: The event that collects the statuses to report.
        """
        if not self._container.can_connect():
            event.add_status(ops.MaintenanceStatus("waiting for Pebble"))

        if self.model.get_relation(constants.POSTGRESQL_RELATION) is None:
            event.add_status(
                ops.BlockedStatus(f"missing required relation: {constants.POSTGRESQL_RELATION}")
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
        try:
            desired, related = self._load_backends(config)
        except BackendNameConflictError as e:
            event.add_status(ops.BlockedStatus(f"backend name conflict: {', '.join(e.names)}"))
            return
        messages = []
        if not desired:
            messages.append("no backends configured or related")
        if related.skipped:
            noun = "relation" if related.skipped == 1 else "relations"
            messages.append(
                f"{related.skipped} {constants.BACKEND_TRINO_CATALOG_RELATION} {noun} not ready"
            )
        event.add_status(ops.ActiveStatus("; ".join(messages)))

    def _load_backends(self, config: CharmConfig) -> tuple[list[BackendConfig], RelatedBackends]:
        """Union the configured backends with those of related Trino applications.

        Args:
            config: The validated charm configuration.

        Returns:
            The backends the gateway should have, and the related backends the union was built
            from, whose `skipped` count the caller can report.

        Raises:
            BackendNameConflictError: if a name occurs more than once.
        """
        related = backend_trino_catalog.load(
            self._trino_catalog, self.model.relations[constants.BACKEND_TRINO_CATALOG_RELATION]
        )
        return merge_backends(config.backends, related.backends), related

    def _load_postgresql(self) -> PostgresRelationModel | None:
        """Load the PostgreSQL relation data, or None if it is absent or incomplete."""
        relation = self.model.get_relation(constants.POSTGRESQL_RELATION)
        if relation is None:
            return None
        try:
            return relation.load(
                PostgresRelationModel,
                relation.app,
                decoder=PostgresRelationModel.decode(self),
            )
        except (pydantic.ValidationError, ops.ModelError):
            return None


if __name__ == "__main__":  # pragma: nocover
    ops.main(TrinoGatewayK8SOperatorCharm)
