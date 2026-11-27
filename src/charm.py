#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm for Trino Gateway on Kubernetes."""

import logging
from collections.abc import Mapping

import ops
import pydantic
from charms.data_platform_libs.v0.data_interfaces import DatabaseRequires
from charms.hydra.v0.oauth import ClientConfigError, OAuthRequirer
from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer
from charms.trino_k8s.v0.trino_catalog import (
    TrinoCatalog,
    TrinoCatalogProvider,
    TrinoCatalogRequirer,
)

import constants
import gateway_api
import workload
from config import CharmConfig
from core import catalogs, policy, upstreams, users
from core.policy import Consumer, PolicyInputs, PolicyResult
from exceptions import InvalidConfigError, MalformedIssuedUsersError, PathBasedIngressError
from gateway_api import GatewayApiClient, GatewayApiError
from relations import (
    backend_trino_catalog,
    ingress,
    oauth,
    peer,
    trino_catalog,
    trino_gateway_integration,
)
from relations.backend_trino_catalog import RelatedBackends
from relations.ingress import IngressAddress
from workload import GatewayAuth, OAuthLogin, PostgresRelationModel

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
        self._trino_catalog_provider = TrinoCatalogProvider(self, constants.TRINO_CATALOG_RELATION)
        # Created before the reconcile observers, so the library has published the gateway's
        # port and read the provider's URL by the time a reconcile runs in the same hook.
        self._ingress = IngressPerAppRequirer(
            self, constants.INGRESS_RELATION, port=constants.HTTP_PORT, scheme="http"
        )
        self._oauth = OAuthRequirer(self, relation_name=constants.OAUTH_RELATION)

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
            self.on[constants.INGRESS_RELATION].relation_changed,
            self.on[constants.INGRESS_RELATION].relation_broken,
            self.on[constants.OAUTH_RELATION].relation_created,
            self.on[constants.OAUTH_RELATION].relation_changed,
            self.on[constants.OAUTH_RELATION].relation_broken,
            self.on[constants.PEER_RELATION].relation_created,
        ):
            framework.observe(event, self._reconcile)
        framework.observe(self.on.collect_unit_status, self._on_collect_unit_status)

    def _reconcile(self, _: ops.EventBase) -> None:
        """Converge the issued users, the workload and, on the leader, backends and consumers.

        Args:
            _: The event that triggered the reconcile, which does not affect the outcome.
        """
        # Issuing users and registering the OAuth client need neither Pebble nor the database.
        self._issue_users()
        self._publish_oauth_client()

        if not self._container.can_connect():
            return
        # The service is deliberately left running without the relation, so clients get
        # database errors from the gateway rather than refused connections.
        pg = self._load_postgresql()
        if pg is None:
            return

        api_key = gateway_api.ensure_api_key(self.model)
        oauth_related = self.model.get_relation(constants.OAUTH_RELATION) is not None
        if oauth_related and api_key is None:
            # A follower waits for the leader's key rather than opening the UI and API.
            return
        auth = GatewayAuth(login=self._load_oauth_login()) if oauth_related else None
        workload.apply(self._container, workload.render_config(pg, auth), api_key)
        self.unit.set_ports(constants.HTTP_PORT)

        if self.unit.is_leader():
            self._register_and_publish(api_key)

    def _register_and_publish(self, api_key: str | None) -> None:
        """On the leader, sync the admitted backends and then publish to consumers.

        Args:
            api_key: The key to sign the charm's API tokens with, if there is one.
        """
        try:
            exclusions = catalogs.parse_exclusions(self._load_config().catalog_exclusions)
        except InvalidConfigError as e:
            # Registration does not depend on exclusions, so only publication waits.
            logger.warning("Not publishing to consumers because the config is invalid: %s", e)
            exclusions = None

        client = _api_client(api_key)
        try:
            current = client.list_backends()
            inputs = self._policy_inputs(frozenset(b.name for b in current), exclusions or {})
            if inputs is None:
                return
            result = policy.evaluate(inputs)
            policy.log_details(inputs, result)
            gateway_api.sync_backends(client, result.admitted, current)
        except GatewayApiError as e:
            # The gateway may still be starting after a restart; a later hook converges.
            logger.warning("Backend sync failed, retrying on a later hook: %s", e)
            return

        # Publishing only after a successful sync keeps consumers from being told about
        # catalogs of a backend the gateway has not registered.
        if exclusions is not None:
            self._publish(result.publish, inputs.consumers)

    def _publish(
        self, publish: Mapping[int, list[TrinoCatalog]], consumers: Mapping[int, Consumer]
    ) -> None:
        """Write the complete connection data of each consumer to publish.

        Args:
            publish: The catalogs to publish, by consumer relation ID.
            consumers: The live consumers, by relation ID.
        """
        url = self._published_url()
        for relation in self.model.relations[constants.TRINO_CATALOG_RELATION]:
            if relation.id not in publish:
                continue
            secret = trino_catalog.ensure_user_secret(
                self.model, relation.id, consumers[relation.id].username
            )
            trino_catalog.publish(
                self._trino_catalog_provider, relation, secret, url, publish[relation.id]
            )

    def _publish_oauth_client(self) -> None:
        """On the leader, register the OAuth client for the gateway's HTTPS ingress address."""
        if not self.unit.is_leader() or self.model.get_relation(constants.OAUTH_RELATION) is None:
            return
        address = self._https_ingress()
        if address is None:
            return
        try:
            oauth.publish(self._oauth, address)
        except ClientConfigError as e:
            logger.error("Not registering the OAuth client: %s", e)

    def _load_oauth_login(self) -> OAuthLogin | None:
        """Load the OAuth login for the current ingress address, or None if it is not ready."""
        address = self._https_ingress()
        if address is None:
            return None
        client = oauth.load(self._oauth)
        return None if client is None else OAuthLogin(client, oauth.redirect_uri(address))

    def _https_ingress(self) -> IngressAddress | None:
        """Load the ingress address if it is host-based and HTTPS, as OAuth needs."""
        try:
            address = self._load_ingress()
        except PathBasedIngressError:
            return None
        return address if address is not None and address.scheme == "https" else None

    def _published_url(self) -> str:
        """Choose the `host:port` address published to consumers.

        Returns:
            The ingress address if the provider gave a host-based one, else the in-cluster one.
        """
        try:
            address = self._load_ingress()
        except PathBasedIngressError:
            address = None
        if address is None:
            return trino_catalog.gateway_url(self.app.name, self.model.name)
        return address.authority

    def _policy_inputs(
        self, registered: frozenset[str], exclusions: dict[str, frozenset[str]]
    ) -> PolicyInputs | None:
        """Gather the state the single-group policy decides from. Only the leader may call this.

        Args:
            registered: Names of the backends registered with the gateway.
            exclusions: Catalog names hidden from consumers, by `app_name`.

        Returns:
            The policy inputs, or None if the issued users cannot be read, since without them
            the published consumers are unknown.
        """
        peer_relation = self.model.get_relation(constants.PEER_RELATION)
        if peer_relation is None:
            return None
        try:
            issued = peer.load_issued_users(peer_relation)
        except MalformedIssuedUsersError:
            return None

        relations = [
            relation
            for relation in self.model.relations[constants.TRINO_CATALOG_RELATION]
            if relation.app is not None and relation.id in issued
        ]
        backends = self._load_related_backends().backends
        upstream_states = trino_gateway_integration.load(
            self.model.relations[constants.TRINO_GATEWAY_INTEGRATION_RELATION],
            set(issued.values()),
        )
        return PolicyInputs(
            backends=backends,
            matches=upstreams.match_backends(backends, upstream_states),
            upstreams={upstream.relation_id: upstream for upstream in upstream_states},
            consumers={
                relation.id: Consumer(
                    relation.id, trino_catalog.app_name(relation), issued[relation.id]
                )
                for relation in relations
            },
            published=frozenset(
                relation.id
                for relation in relations
                if trino_catalog.is_published(relation, self.app)
            ),
            registered=registered,
            exclusions=exclusions,
        )

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

        self._add_input_statuses(event)

        related = self._load_related_backends()
        messages = []
        if not related.backends and not related.skipped:
            messages.append("no backends related")
        if related.skipped:
            noun = "relation" if related.skipped == 1 else "relations"
            messages.append(
                f"{related.skipped} {constants.BACKEND_TRINO_CATALOG_RELATION} {noun} not ready"
            )
        if self.unit.is_leader():
            result = self._evaluate_policy_for_status()
            if result is not None:
                messages.extend(self._add_policy_statuses(event, result))
        event.add_status(ops.ActiveStatus("; ".join(messages)))

    def _add_input_statuses(self, event: ops.CollectStatusEvent) -> None:
        """Add the statuses of an invalid configuration, ingress address or OAuth login.

        Args:
            event: The event that collects the statuses to report.
        """
        try:
            self._load_config()
        except InvalidConfigError as e:
            event.add_status(ops.BlockedStatus(f"invalid catalog-exclusions: {e}"))

        try:
            self._load_ingress()
        except PathBasedIngressError as e:
            event.add_status(ops.BlockedStatus(str(e)))

        self._add_oauth_status(event)

    def _add_oauth_status(self, event: ops.CollectStatusEvent) -> None:
        """Add the status of the OAuth login, while it is related but not ready.

        Args:
            event: The event that collects the statuses to report.
        """
        if self.model.get_relation(constants.OAUTH_RELATION) is None:
            return
        try:
            address = self._load_ingress()
        except PathBasedIngressError:
            # Reported as the ingress status.
            address = None
        if address is None:
            event.add_status(ops.WaitingStatus("waiting for ingress URL for OAuth"))
        elif address.scheme != "https":
            event.add_status(ops.BlockedStatus("OAuth requires an HTTPS ingress URL"))
        else:
            try:
                oauth.client_config(address)
            except ClientConfigError as e:
                event.add_status(ops.BlockedStatus(f"invalid OAuth client configuration: {e}"))
                return
            if oauth.load(self._oauth) is None:
                event.add_status(ops.WaitingStatus("waiting for OAuth provider registration"))

    def _evaluate_policy_for_status(self) -> PolicyResult | None:
        """Evaluate the policy from the current state, or None if the state is unavailable."""
        try:
            current = _api_client(gateway_api.load_api_key(self.model)).list_backends()
        except GatewayApiError:
            return None
        inputs = self._policy_inputs(frozenset(b.name for b in current), {})
        return None if inputs is None else policy.evaluate(inputs)

    @staticmethod
    def _add_policy_statuses(event: ops.CollectStatusEvent, result: PolicyResult) -> list[str]:
        """Add the Blocked statuses of the policy result and list its Active messages.

        Args:
            event: The event that collects the statuses to report.
            result: The policy's decision.

        Returns:
            The non-zero waiting and gap counts, as status message parts.
        """
        if result.refusals:
            refused = len(set().union(*result.refusals.values()))
            event.add_status(
                ops.BlockedStatus(
                    f"{_count(refused, 'user')} refused by"
                    f" {_count(result.refusing_backends, 'upstream')}: "
                    + "; ".join(sorted(result.refusals))
                )
            )
        if result.conflicting_catalogs:
            event.add_status(
                ops.BlockedStatus(
                    f"{_count(len(result.conflicting_catalogs), 'catalog conflict')}"
                    " between upstreams"
                )
            )
        counts = [
            (result.pending_backends, "upstream", "waiting for registration"),
            (result.pending_consumers, "consumer", "waiting for publication"),
            (result.gaps, "confirmation gap", ""),
        ]
        return [f"{_count(n, noun)} {suffix}".rstrip() for n, noun, suffix in counts if n]

    def _load_config(self) -> CharmConfig:
        """Load the charm configuration.

        Returns:
            The validated configuration.

        Raises:
            InvalidConfigError: if an option is invalid, with a message fit for status.
        """
        try:
            return self.load_config(CharmConfig)
        except pydantic.ValidationError as e:
            reasons = (
                str(error.get("ctx", {}).get("error", error["msg"])) for error in e.errors()
            )
            raise InvalidConfigError("; ".join(reasons)) from e

    def _load_ingress(self) -> IngressAddress | None:
        """Load the ingress address, or None if ingress is not related or has no URL yet.

        Raises:
            PathBasedIngressError: if the provider gave a path-based URL.
        """
        return ingress.load(self._ingress)

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


def _api_client(api_key: str | None) -> GatewayApiClient:
    """Build a client for the gateway's API on the local unit."""
    return GatewayApiClient(f"http://localhost:{constants.HTTP_PORT}", api_key)


def _count(n: int, noun: str) -> str:
    """Format a count with its noun, pluralised with a trailing `s` unless it is one."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


if __name__ == "__main__":  # pragma: nocover
    ops.main(TrinoGatewayK8SOperatorCharm)
