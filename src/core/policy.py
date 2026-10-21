# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""The temporary single-routing-group policy for registering backends and publishing consumers.

All backends share one routing group, so any query can reach any registered backend. Until
routing by eligible upstream sets replaces this module, the policy never takes away service
that already works:

- a consumer is published once every registered backend has confirmed its user;
- a backend is registered once it has confirmed every published consumer;
- a registered backend stays registered while it confirms at least one published consumer.

The policy only reads the per-pair confirmation state from `core.upstreams`.
"""

import logging
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from charms.trino_k8s.v0.trino_catalog import TrinoCatalog

from core import catalogs
from core.upstreams import UpstreamUsers
from relations.backend_trino_catalog import RelatedBackend

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Consumer:
    """A live `trino-catalog` consumer with an issued user.

    Attributes:
        relation_id: ID of the consumer relation.
        app_name: The `app_name` the consumer published, or None if it has not yet.
        username: The username issued to the consumer.
    """

    relation_id: int
    app_name: str | None
    username: str


@dataclass(frozen=True)
class PolicyInputs:
    """The observed state the policy decides from.

    Attributes:
        backends: The live backends of the `backend-trino-catalog` relations.
        matches: The matched `trino-gateway-integration` relation ID, by backend name.
        upstreams: The confirmation state of each `trino-gateway-integration` relation, by ID.
        consumers: The live consumers with an issued user, by relation ID.
        published: IDs of the consumer relations that already hold connection data.
        registered: Names of the backends registered with the gateway.
        exclusions: Catalog names hidden from consumers, by `app_name`.
    """

    backends: list[RelatedBackend]
    matches: Mapping[str, int | None]
    upstreams: Mapping[int, UpstreamUsers]
    consumers: Mapping[int, Consumer]
    published: frozenset[int]
    registered: frozenset[str]
    exclusions: Mapping[str, frozenset[str]]


@dataclass(frozen=True)
class PolicyResult:
    """What the gateway should register and publish, and the facts status reports.

    Attributes:
        admitted: The backends the gateway should have, sorted by name.
        publish: The catalogs to publish, sorted by name, by consumer relation ID.
        pending_backends: Number of live backends that are not admitted.
        pending_consumers: Number of unpublished consumers that are not published yet.
        gaps: Number of admitted backend and published consumer pairs without a confirmation.
        refusals: Usernames refused by matched backends, by reason.
        refusing_backends: Number of matched backends that refuse at least one user.
        conflicting_catalogs: Catalog names that conflict between admitted backends, or that
            hold back a pending backend.
    """

    admitted: list[RelatedBackend]
    publish: dict[int, list[TrinoCatalog]]
    pending_backends: int
    pending_consumers: int
    gaps: int
    refusals: dict[str, set[str]]
    refusing_backends: int
    conflicting_catalogs: frozenset[str]


class _Confirmations:
    """Answers whether a backend has confirmed a consumer, through its matched upstream."""

    def __init__(self, inputs: PolicyInputs):
        self._inputs = inputs

    def upstream(self, backend: RelatedBackend) -> UpstreamUsers | None:
        relation_id = self._inputs.matches.get(backend.name)
        return None if relation_id is None else self._inputs.upstreams.get(relation_id)

    def confirms(self, backend: RelatedBackend, consumer: Consumer) -> bool:
        upstream = self.upstream(backend)
        return upstream is not None and consumer.username in upstream.confirmed


def _published(inputs: PolicyInputs) -> list[Consumer]:
    """List the published consumers that are still live."""
    return [c for rid, c in sorted(inputs.consumers.items()) if rid in inputs.published]


def _clashes(backend: RelatedBackend, admitted: list[RelatedBackend]) -> set[str]:
    """Find the catalogs of a backend that conflict with those of admitted backends."""
    return {
        catalog.name
        for other in admitted
        for other_catalog in other.catalogs
        for catalog in backend.catalogs
        if catalog.name == other_catalog.name and catalogs.conflicts(catalog, other_catalog)
    }


def _admit(
    inputs: PolicyInputs, confirmations: _Confirmations, published: list[Consumer]
) -> tuple[list[RelatedBackend], set[str]]:
    """Keep the registered backends that still serve and admit pending ones that qualify.

    Returns:
        The admitted backends, sorted by name, and the catalogs that held a backend back.
    """
    kept = [
        backend
        for backend in inputs.backends
        if backend.name in inputs.registered
        and confirmations.upstream(backend) is not None
        and (not published or any(confirmations.confirms(backend, c) for c in published))
    ]
    kept_names = {backend.name for backend in kept}
    admitted = list(kept)
    held_back: set[str] = set()
    for backend in sorted(inputs.backends, key=lambda b: b.name):
        if backend.name in kept_names or confirmations.upstream(backend) is None:
            continue
        if not all(confirmations.confirms(backend, c) for c in published):
            continue
        clashes = _clashes(backend, admitted)
        if clashes:
            held_back |= clashes
            continue
        admitted.append(backend)
    return sorted(admitted, key=lambda b: b.name), held_back


def _catalogs_for(
    consumer: Consumer,
    admitted: list[RelatedBackend],
    confirmations: _Confirmations,
    exclusions: Mapping[str, frozenset[str]],
) -> list[TrinoCatalog]:
    """Merge the catalogs of the admitted backends that confirm a consumer, minus exclusions."""
    merged = catalogs.merge(
        (b.name, b.catalogs) for b in admitted if confirmations.confirms(b, consumer)
    )
    excluded = exclusions.get(consumer.app_name, frozenset()) if consumer.app_name else frozenset()
    return [catalog for name, catalog in merged.catalogs.items() if name not in excluded]


def _refusals(
    inputs: PolicyInputs, confirmations: _Confirmations
) -> tuple[dict[str, set[str]], int]:
    """Collect the users refused by matched backends, by reason, and count those backends."""
    refusals: dict[str, set[str]] = defaultdict(set)
    refusing = 0
    for backend in inputs.backends:
        upstream = confirmations.upstream(backend)
        if upstream is None or not upstream.refused:
            continue
        refusing += 1
        for username, reason in upstream.refused.items():
            refusals[reason].add(username)
    return dict(refusals), refusing


def evaluate(inputs: PolicyInputs) -> PolicyResult:
    """Decide which backends to register and which consumers to publish with what.

    Args:
        inputs: The observed state.

    Returns:
        The admitted backends, the catalogs of each consumer to publish and the status facts.
    """
    confirmations = _Confirmations(inputs)
    published = _published(inputs)
    admitted, held_back = _admit(inputs, confirmations, published)

    publish = {}
    for relation_id, consumer in sorted(inputs.consumers.items()):
        is_new = relation_id not in inputs.published
        if is_new and (
            consumer.app_name is None
            or not all(confirmations.confirms(b, consumer) for b in admitted)
        ):
            continue
        publish[relation_id] = _catalogs_for(consumer, admitted, confirmations, inputs.exclusions)

    refusals, refusing_backends = _refusals(inputs, confirmations)
    merged = catalogs.merge((b.name, b.catalogs) for b in admitted)
    return PolicyResult(
        admitted=admitted,
        publish=publish,
        pending_backends=len(inputs.backends) - len(admitted),
        pending_consumers=len(inputs.consumers) - len(publish),
        gaps=sum(not confirmations.confirms(b, c) for b in admitted for c in published),
        refusals=refusals,
        refusing_backends=refusing_backends,
        conflicting_catalogs=merged.conflicting | held_back,
    )


def _log_pending_backends(
    inputs: PolicyInputs, result: PolicyResult, confirmations: _Confirmations
) -> None:
    published = _published(inputs)
    admitted = {b.name for b in result.admitted}
    for backend in inputs.backends:
        if backend.name in admitted:
            continue
        if confirmations.upstream(backend) is None:
            reason = "it is not matched to a trino-gateway-integration relation"
        else:
            missing = sorted(
                c.username for c in published if not confirmations.confirms(backend, c)
            )
            reason = f"it has not confirmed {missing}" if missing else "its catalogs conflict"
        logger.info("Backend %r is waiting for registration: %s", backend.name, reason)


def _log_pending_consumers(
    inputs: PolicyInputs, result: PolicyResult, confirmations: _Confirmations
) -> None:
    for relation_id, consumer in sorted(inputs.consumers.items()):
        if relation_id in result.publish:
            continue
        if consumer.app_name is None:
            reason = "it has not published its app_name"
        else:
            missing = sorted(
                b.name for b in result.admitted if not confirmations.confirms(b, consumer)
            )
            reason = f"backends {missing} have not confirmed {consumer.username!r}"
        logger.info("Consumer relation %r is waiting for publication: %s", relation_id, reason)


def _log_problems(
    inputs: PolicyInputs, result: PolicyResult, confirmations: _Confirmations
) -> None:
    published = _published(inputs)
    for backend in result.admitted:
        for consumer in published:
            if not confirmations.confirms(backend, consumer):
                logger.warning(
                    "Registered backend %r has not confirmed published user %r",
                    backend.name,
                    consumer.username,
                )
    for backend in inputs.backends:
        upstream = confirmations.upstream(backend)
        for username, reason in sorted(upstream.refused.items() if upstream else ()):
            logger.warning("Backend %r refused user %r: %s", backend.name, username, reason)
    if result.conflicting_catalogs:
        logger.warning(
            "Catalogs with conflicting connectors between backends: %s",
            sorted(result.conflicting_catalogs),
        )


def log_details(inputs: PolicyInputs, result: PolicyResult) -> None:
    """Log the names behind the policy's status counts.

    Args:
        inputs: The observed state the policy decided from.
        result: The policy's decision.
    """
    confirmations = _Confirmations(inputs)
    _log_pending_backends(inputs, result, confirmations)
    _log_pending_consumers(inputs, result, confirmations)
    _log_problems(inputs, result, confirmations)
