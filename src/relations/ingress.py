# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""The `ingress` requirer endpoint: the gateway's address outside the cluster.

Only host-based ingress URLs are usable: Trino clients and the `trino_catalog` interface
(`trino_url` as `host:port`) cannot carry a path prefix.
"""

import dataclasses
import logging
from urllib.parse import urlsplit

from charms.traefik_k8s.v2.ingress import DataValidationError, IngressPerAppRequirer

from exceptions import PathBasedIngressError

logger = logging.getLogger(__name__)

_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclasses.dataclass(frozen=True)
class IngressAddress:
    """A host-based ingress address.

    Attributes:
        scheme: `http` or `https`.
        host: The host name or IP address.
        port: The port, defaulted from the scheme when the URL has none.
    """

    scheme: str
    host: str
    port: int

    @property
    def authority(self) -> str:
        """The `host:port` form consumers are given."""
        return f"{self.host}:{self.port}"

    @property
    def url(self) -> str:
        """The base URL, without the port when it is the default of the scheme."""
        if self.port == _DEFAULT_PORTS[self.scheme]:
            return f"{self.scheme}://{self.host}"
        return f"{self.scheme}://{self.authority}"


def parse_url(url: str) -> IngressAddress:
    """Parse an ingress URL into a host-based address.

    Args:
        url: The URL the ingress provider gave.

    Returns:
        The address, with the default port of the scheme if the URL has none.

    Raises:
        PathBasedIngressError: if the URL has a path, with a message fit for status.
    """
    parsed = urlsplit(url)
    if parsed.path.strip("/"):
        raise PathBasedIngressError(
            f"ingress URL {url} is path-based; a host-based ingress is required"
        )
    scheme = parsed.scheme.lower()
    return IngressAddress(
        scheme=scheme,
        host=parsed.hostname or "",
        port=parsed.port or _DEFAULT_PORTS[scheme],
    )


def load(requirer: IngressPerAppRequirer) -> IngressAddress | None:
    """Read the ingress address the provider gave.

    Args:
        requirer: The library requirer for the endpoint.

    Returns:
        The address, or None if ingress is not related or the provider has not given a
        valid URL yet.

    Raises:
        PathBasedIngressError: if the URL has a path, with a message fit for status.
    """
    try:
        url = requirer.url
    except DataValidationError as e:
        logger.warning("Ignoring the invalid ingress provider data: %s", e)
        return None
    return parse_url(url) if url else None
