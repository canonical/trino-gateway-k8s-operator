# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Which users each upstream Trino confirmed or refused, and which backend it corresponds to."""

import logging
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from relations.backend_trino_catalog import RelatedBackend, normalise_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UpstreamUsers:
    """The state of the requested users on one `trino-gateway-integration` relation.

    Attributes:
        relation_id: ID of the relation.
        remote_app: Name of the upstream application.
        url: Backend URL the upstream advertises, or None if it advertises no valid one.
        confirmed: Requested usernames the upstream registered and did not refuse.
        refused: Reasons the upstream gave for refusing requested users, by username.
    """

    relation_id: int
    remote_app: str
    url: str | None
    confirmed: frozenset[str]
    refused: Mapping[str, str]


def build(
    relation_id: int,
    remote_app: str,
    raw_url: str | None,
    requested: Collection[str],
    registered: Iterable[str],
    refused: Mapping[str, str],
) -> UpstreamUsers:
    """Derive the confirmation state of one upstream from its reply.

    Names that were not requested are ignored. A user that is both registered and refused
    counts as refused.

    Args:
        relation_id: ID of the `trino-gateway-integration` relation.
        remote_app: Name of the upstream application.
        raw_url: The `host:port` address the upstream advertises, if any.
        requested: Usernames the gateway requested.
        registered: Usernames the upstream reports as registered.
        refused: Reasons the upstream gives for refusing users, by username.

    Returns:
        The upstream's normalised URL and its confirmed and refused users.
    """
    url = None
    if raw_url is not None:
        try:
            url = normalise_url(raw_url)
        except ValueError:
            url = None
    refused_requested = {name: reason for name, reason in refused.items() if name in requested}
    confirmed = frozenset(name for name in registered if name in requested).difference(
        refused_requested
    )
    return UpstreamUsers(
        relation_id=relation_id,
        remote_app=remote_app,
        url=url,
        confirmed=confirmed,
        refused=MappingProxyType(refused_requested),
    )


def match_backends(
    backends: Iterable[RelatedBackend], upstreams: Iterable[UpstreamUsers]
) -> dict[str, int | None]:
    """Match each backend to the `trino-gateway-integration` relation advertising its URL.

    A backend matches only when exactly one upstream advertises the same URL. Several backends
    may match the same upstream.

    Args:
        backends: The backends of the `backend-trino-catalog` relations.
        upstreams: The state of the `trino-gateway-integration` relations.

    Returns:
        The matched relation ID, or None if there is no unambiguous match, by backend name.
    """
    relation_ids_by_url: dict[str, list[int]] = defaultdict(list)
    for upstream in upstreams:
        if upstream.url is not None:
            relation_ids_by_url[upstream.url].append(upstream.relation_id)

    matches: dict[str, int | None] = {}
    for backend in backends:
        relation_ids = relation_ids_by_url.get(backend.url, [])
        if len(relation_ids) > 1:
            logger.warning(
                "Backend %r matches several trino-gateway-integration relations %r by URL;"
                " matching none",
                backend.name,
                sorted(relation_ids),
            )
        matches[backend.name] = relation_ids[0] if len(relation_ids) == 1 else None
    return matches
