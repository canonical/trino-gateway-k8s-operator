# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Trino applications related over `backend-trino-catalog`, as gateway backend candidates."""

import hashlib
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import ops
import pydantic
from charms.trino_k8s.v0.trino_catalog import TrinoCatalog, TrinoCatalogRequirer

import constants

logger = logging.getLogger(__name__)

_HTTPS_PORT = 443
_MAX_PORT = 65535
# 54 characters of the name, a dash and 8 hex digits fill the 63-character name limit.
_NAME_PREFIX_LENGTH = 54
_NAME_HASH_LENGTH = 8


class _BackendFields(pydantic.BaseModel):
    """The name and URL of a backend, checked against what the gateway and the charm accept."""

    model_config = pydantic.ConfigDict(extra="forbid")

    name: str
    url: str

    @pydantic.field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        """Check that the name is one the gateway and the charm accept.

        Args:
            value: The proposed backend name.

        Returns:
            The name, unchanged.

        Raises:
            ValueError: if the name does not match the backend name pattern.
        """
        if not re.fullmatch(constants.BACKEND_NAME_PATTERN, value):
            raise ValueError(f"must match {constants.BACKEND_NAME_PATTERN}")
        return value

    @pydantic.field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return _check_url(value)


def _check_url(value: str) -> str:
    """Check that a URL is one the gateway accepts as a backend URL.

    Args:
        value: The proposed URL.

    Returns:
        The URL without a trailing slash, so it compares equal to what the gateway returns.

    Raises:
        ValueError: if the URL is not an http(s) URL with only a scheme, host and port.
    """
    parts = urlsplit(value)
    # Reading .port raises ValueError for a malformed port.
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.port == 0:
        raise ValueError("must be an http(s) URL with a host")
    if parts.path not in ("", "/") or parts.query or parts.fragment or "@" in parts.netloc:
        raise ValueError("must not have a path, query, fragment or userinfo")
    return value.rstrip("/")


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
        The name itself if it is a valid backend name, otherwise its first 54 characters followed
        by a hash of the full name.
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


def normalise_url(trino_url: str) -> str:
    """Turn an advertised `host:port` address into the URL a backend for it would have.

    Args:
        trino_url: The advertised `host:port` address.

    Returns:
        The validated backend URL, comparable with `RelatedBackend.url`.

    Raises:
        ValueError: if the address does not make a valid backend URL.
    """
    return _check_url(backend_url(trino_url))


def _catalog_names(catalogs: list[TrinoCatalog]) -> tuple[str, ...]:
    """Collect the sorted names of the advertised catalogs.

    Args:
        catalogs: The catalogs advertised by the remote application.

    Returns:
        The catalog names in sorted order.

    Raises:
        ValueError: if a catalog name is not a string.
    """
    names = [catalog.name for catalog in catalogs]
    if not all(isinstance(name, str) for name in names):
        raise ValueError("catalog names must be strings")
    return tuple(sorted(names))


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
            validated = _BackendFields(
                name=backend_name(remote_app), url=backend_url(info["trino_url"])
            )
            catalogs = _catalog_names(info["trino_catalogs"])
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
                catalogs=catalogs,
                relation_id=relation_id,
                remote_app=remote_app,
            )
        )
    return RelatedBackends(sorted(backends, key=lambda backend: backend.name), skipped)
