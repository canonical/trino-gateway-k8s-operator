# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Lookup of the secrets the application owns."""

import ops


def find(model: ops.Model, label: str) -> ops.Secret | None:
    """Look up an owned secret by label without reading its content.

    `Model.get_secret` reads the content and raises `SecretNotFoundError` both when no secret
    has the label and when the content of the tracked revision cannot be read, as Juju leaves
    behind when it saves a secret only partly. ops has no public lookup by label that only
    reads metadata, hence the private backend.
    """
    secret = ops.Secret(model._backend, label=label)
    try:
        secret.get_info()
    except ops.SecretNotFoundError:
        return None
    return secret
