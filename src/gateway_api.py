# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Client for the Trino Gateway backend REST API."""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.request
from collections.abc import Iterable

import ops

import constants
import owned_secrets
from core.backends import GatewayBackend, plan_sync
from relations.backend_trino_catalog import RelatedBackend

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5
_TOKEN_LIFETIME_SECONDS = 300
_API_KEY_FIELD = "key"


class GatewayApiError(Exception):
    """The gateway API could not be reached or rejected a request."""


def load_api_key(model: ops.Model) -> str | None:
    """Read the key the charm signs its API tokens with.

    Args:
        model: The Juju model.

    Returns:
        The key, or None if the leader has not created a readable one yet.
    """
    try:
        secret = model.get_secret(label=constants.API_KEY_SECRET_LABEL)
    except ops.SecretNotFoundError:
        if not model.unit.is_leader():
            return None
        # Owners track a revision too: after a replacement, the latest one is readable.
        secret = owned_secrets.find(model, constants.API_KEY_SECRET_LABEL)
        if secret is None:
            return None
    try:
        return secret.get_content(refresh=True)[_API_KEY_FIELD]
    except ops.SecretNotFoundError:
        return None


def ensure_api_key(model: ops.Model) -> str | None:
    """Read the key the charm signs its API tokens with, creating it on the leader.

    A secret whose label exists but whose content cannot be read, as Juju leaves behind when
    it saves a secret only partly, gets a new key. No token can have been signed with the
    unreadable one.

    Args:
        model: The Juju model.

    Returns:
        The key, or None on a follower before the leader has created it.
    """
    key = load_api_key(model)
    if key is not None or not model.unit.is_leader():
        return key
    # The gateway requires an HMAC key of at least 256 bits.
    key = secrets.token_hex(32)
    secret = owned_secrets.find(model, constants.API_KEY_SECRET_LABEL)
    if secret is None:
        model.app.add_secret({_API_KEY_FIELD: key}, label=constants.API_KEY_SECRET_LABEL)
    else:
        logger.warning("Replacing the unreadable gateway API key")
        secret.set_content({_API_KEY_FIELD: key})
    return key


def api_token(key: str, now: int) -> str:
    """Sign a short-lived HS256 JWT that authenticates the charm to the gateway's API.

    Args:
        key: The HMAC key the gateway verifies tokens with.
        now: The current time, in seconds since the epoch.

    Returns:
        The compact JWT.
    """
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "iss": constants.API_JWT_ISSUER,
        "aud": constants.API_JWT_AUDIENCE,
        "sub": constants.API_USER,
        "iat": now,
        "exp": now + _TOKEN_LIFETIME_SECONDS,
    }
    signing_input = f"{_b64url(json.dumps(header))}.{_b64url(json.dumps(claims))}"
    signature = hmac.new(key.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def _b64url(data: str | bytes) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class GatewayApiClient:
    """Minimal client for the `/gateway/backend` endpoints."""

    def __init__(self, base_url: str, api_key: str | None = None):
        """Initialise the client.

        Args:
            base_url: The gateway's base URL.
            api_key: The key to sign API tokens with, or None to send requests unauthenticated.
        """
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def list_backends(self) -> list[GatewayBackend]:
        """Return every backend registered with the gateway.

        Returns:
            The registered backends.

        Raises:
            GatewayApiError: if the gateway cannot be reached or returns an unexpected response.
        """
        body = self._request("GET", "/gateway/backend/all")
        try:
            entries = json.loads(body)
            if not isinstance(entries, list):
                raise ValueError("expected a list")
            return [GatewayBackend.from_api(entry) for entry in entries]
        except ValueError as e:
            raise GatewayApiError(f"invalid backend list: {e}") from e

    def add(self, backend: GatewayBackend) -> None:
        """Register a backend.

        Args:
            backend: The backend to register.

        Raises:
            GatewayApiError: if the gateway cannot be reached or rejects the request.
        """
        self._request(
            "POST",
            "/gateway/backend/modify/add",
            json.dumps(backend.to_api()),
            "application/json",
        )

    def update(self, backend: GatewayBackend) -> None:
        """Update a registered backend, matched by name.

        Args:
            backend: The backend with its new values.

        Raises:
            GatewayApiError: if the gateway cannot be reached or rejects the request.
        """
        self._request(
            "POST",
            "/gateway/backend/modify/update",
            json.dumps(backend.to_api()),
            "application/json",
        )

    def delete(self, name: str) -> None:
        """Deregister a backend by name."""
        self._request("POST", "/gateway/backend/modify/delete", name, "text/plain")

    def _request(
        self, method: str, path: str, body: str | None = None, content_type: str | None = None
    ) -> bytes:
        request = urllib.request.Request(
            self._base_url + path,
            data=None if body is None else body.encode(),
            method=method,
        )
        if content_type:
            request.add_header("Content-Type", content_type)
        if self._api_key is not None:
            token = api_token(self._api_key, int(time.time()))
            request.add_header("Authorization", f"Bearer {token}")
        try:
            # The base URL is a fixed http URL chosen by the charm, not user input.
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # nosec B310
                return response.read()
        except OSError as e:
            raise GatewayApiError(f"{method} {path} failed: {e}") from e


def sync_backends(
    client: GatewayApiClient,
    desired: Iterable[RelatedBackend],
    current: Iterable[GatewayBackend],
) -> None:
    """Make the gateway's registered backends match `desired`.

    Args:
        client: Client for the gateway API.
        desired: The backends the gateway should have.
        current: The backends the gateway has, as listed by `client.list_backends()`.

    Raises:
        GatewayApiError: if the gateway cannot be reached or rejects a change.
    """
    plan = plan_sync(desired, current)
    for name in plan.delete:
        logger.info("Deregistering backend %r", name)
        client.delete(name)
    for backend in plan.update:
        logger.info("Updating backend %r", backend.name)
        client.update(backend)
    for backend in plan.add:
        logger.info("Registering backend %r", backend.name)
        client.add(backend)
