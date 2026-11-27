# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Typed exceptions raised at the charm's edges."""


class MalformedIssuedUsersError(Exception):
    """The issued-user index in the peer relation cannot be parsed."""


class InvalidConfigError(Exception):
    """The charm configuration is invalid."""


class PathBasedIngressError(Exception):
    """The ingress provider gave a URL with a path, which Trino clients cannot use."""
