# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Trino applications related over `backend-trino-catalog`, as gateway backend candidates."""

import hashlib
import logging
import re
from dataclasses import dataclass

import ops
from charms.trino_k8s.v0.trino_catalog import TrinoCatalogRequirer

import constants
from config import BackendConfig

logger = logging.getLogger(__name__)

_HTTPS_PORT = 443
_MAX_PORT = 65535
# 54 characters of the name, a dash and 8 hex digits fill the 63-character name limit.
_NAME_PREFIX_LENGTH = 54
_NAME_HASH_LENGTH = 8


@dataclass(frozen=True)
class RelatedBackend:
    """A backend advertised by a related Trino application.

    Attributes:
        name: Backend name derived from the remote application name.
        url: URL of the Trino application, with the scheme inferred from its port.
        catalogs: Sorted names of the catalogs the application advertises.
        relation_id: ID of the relation the data came from.
        remote_app: Name of the remote application.
    """

    name: str
    url: str
    catalogs: tuple[str, ...]
    relation_id: int
    remote_app: str


@dataclass(frozen=True)
class RelatedBackends:
    """The usable related backends and the number of relations left out.

    Attributes:
        backends: The valid backends, sorted by name.
        skipped: Number of relations that yielded no backend because their data was missing
            or invalid.
    """

    backends: list[RelatedBackend]
    skipped: int


def backend_name(app_name: str) -> str:
    """Derive a gateway backend name from a remote application name.

    Args:
        app_name: Name of the remote application.

    Returns:
        The name itself, or a shortened name with a hash suffix if it is not a valid backend
        name.
    """
    if re.fullmatch(constants.BACKEND_NAME_PATTERN, app_name):
        return app_name
    digest = hashlib.sha256(app_name.encode()).hexdigest()[:_NAME_HASH_LENGTH]
    return f"{app_name[:_NAME_PREFIX_LENGTH]}-{digest}"


def backend_url(trino_url: str) -> str:
    """Infer the backend URL from the `host:port` address a Trino application advertises.

    Args:
        trino_url: The advertised `host:port` address.

    Returns:
        An https URL for port 443, an http URL otherwise.

    Raises:
        ValueError: if the host is missing or the port is missing or invalid.
    """
    host, separator, port = trino_url.rpartition(":")
    if not separator or not host:
        raise ValueError("trino_url must be host:port")
    if not (port.isascii() and port.isdigit()) or not 0 < int(port) <= _MAX_PORT:
        raise ValueError("trino_url has an invalid port")
    scheme = "https" if int(port) == _HTTPS_PORT else "http"
    return f"{scheme}://{host}:{port}"


def load(requirer: TrinoCatalogRequirer, relations: list[ops.Relation]) -> RelatedBackends:
    """Read the related Trino applications as validated backends.

    Relations with missing or malformed data are skipped, not raised on. Credentials are
    never read.

    Args:
        requirer: The library requirer for the endpoint.
        relations: The relations on the endpoint.

    Returns:
        The backends sorted by name and the number of relations that yielded none.
    """
    info_by_relation = requirer.get_all_trino_info()
    skipped = len({relation.id for relation in relations} - info_by_relation.keys())
    backends = []
    for relation_id, info in info_by_relation.items():
        remote_app = info["remote_app"]
        try:
            validated = BackendConfig(
                name=backend_name(remote_app), url=backend_url(info["trino_url"])
            )
        except ValueError as e:
            logger.warning(
                "Skipping backend-trino-catalog relation %r (%r): %r", relation_id, remote_app, e
            )
            skipped += 1
            continue
        backends.append(
            RelatedBackend(
                name=validated.name,
                url=validated.url,
                catalogs=tuple(sorted(catalog.name for catalog in info["trino_catalogs"])),
                relation_id=relation_id,
                remote_app=remote_app,
            )
        )
    return RelatedBackends(sorted(backends, key=lambda backend: backend.name), skipped)
