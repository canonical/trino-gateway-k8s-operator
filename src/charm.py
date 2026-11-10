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
from core import users
from exceptions import MalformedIssuedUsersError
from gateway_api import GatewayApiClient, GatewayApiError
from relations import backend_trino_catalog, peer, trino_catalog, trino_gateway_integration
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
            self.on[constants.TRINO_CATALOG_RELATION].relation_created,
            self.on[constants.TRINO_CATALOG_RELATION].relation_changed,
            self.on[constants.TRINO_CATALOG_RELATION].relation_broken,
            self.on[constants.TRINO_GATEWAY_INTEGRATION_RELATION].relation_created,
            self.on[constants.TRINO_GATEWAY_INTEGRATION_RELATION].relation_changed,
            self.on[constants.TRINO_GATEWAY_INTEGRATION_RELATION].relation_broken,
            self.on[constants.PEER_RELATION].relation_created,
        ):
            framework.observe(event, self._reconcile)
        framework.observe(self.on.collect_unit_status, self._on_collect_unit_status)

    def _reconcile(self, _: ops.EventBase) -> None:
        """Converge the issued users, the workload and, on the leader, the gateway's backends.

        Args:
            _: The event that triggered the reconcile, which does not affect the outcome.
        """
        # Issuing users needs neither Pebble nor the database.
        self._issue_users()

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
            gateway_api.sync_backends(
                GatewayApiClient(f"http://localhost:{constants.HTTP_PORT}"),
                self._load_related_backends().backends,
            )
        except GatewayApiError as e:
            # The gateway may still be starting after a restart; a later hook converges.
            logger.warning("Backend sync failed, retrying on a later hook: %s", e)

    def _issue_users(self) -> None:
        """On the leader, issue one user per consumer and request every user from each upstream."""
        peer_relation = self.model.get_relation(constants.PEER_RELATION)
        if not self.unit.is_leader() or peer_relation is None:
            return
        try:
            issued = peer.load_issued_users(peer_relation)
        except MalformedIssuedUsersError as e:
            logger.warning("Not issuing users because the issued-user index is malformed: %s", e)
            return

        plan = users.plan_issuance(
            issued,
            trino_catalog.consumers(self.model.relations[constants.TRINO_CATALOG_RELATION]),
            model_uuid=self.model.uuid,
            gateway_app=self.app.name,
        )
        secrets_by_username = {
            user.username: trino_catalog.ensure_user_secret(
                self.model, user.relation_id, user.username
            )
            for user in plan.users
        }
        for relation_id in plan.remove:
            trino_catalog.remove_user_secret(self.model, relation_id)
        peer.save_issued_users(
            peer_relation, self.app, {user.relation_id: user.username for user in plan.users}
        )
        for relation_id, username in plan.issue.items():
            logger.info("Issued user %r to trino-catalog relation %r", username, relation_id)
        for relation_id in plan.remove:
            logger.info(
                "Removed user %r of trino-catalog relation %r", issued[relation_id], relation_id
            )

        for relation in self.model.relations[constants.TRINO_GATEWAY_INTEGRATION_RELATION]:
            trino_gateway_integration.publish(relation, self.app, secrets_by_username)

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

        related = self._load_related_backends()
        messages = []
        if not related.backends and not related.skipped:
            messages.append("no backends related")
        if related.skipped:
            noun = "relation" if related.skipped == 1 else "relations"
            messages.append(
                f"{related.skipped} {constants.BACKEND_TRINO_CATALOG_RELATION} {noun} not ready"
            )
        event.add_status(ops.ActiveStatus("; ".join(messages)))

    def _load_related_backends(self) -> RelatedBackends:
        """Read the backends of the Trino applications related over `backend-trino-catalog`.

        Returns:
            The backends the gateway should have, and the count of relations that yielded none.
        """
        return backend_trino_catalog.load(
            self._trino_catalog, self.model.relations[constants.BACKEND_TRINO_CATALOG_RELATION]
        )

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
