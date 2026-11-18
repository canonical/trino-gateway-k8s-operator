# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import io
import json
import urllib.error
from unittest.mock import MagicMock, call

import pytest

import gateway_api
from core.backends import GatewayBackend
from gateway_api import GatewayApiClient, GatewayApiError, sync_backends
from relations.backend_trino_catalog import RelatedBackend

A = RelatedBackend(name="a", url="http://a:8080", catalogs=(), relation_id=1, remote_app="a")
B = RelatedBackend(name="b", url="http://b:8080", catalogs=(), relation_id=2, remote_app="b")


def _backend(name: str, url: str) -> GatewayBackend:
    """Build the backend the gateway should have for a URL."""
    return GatewayBackend(name, url, True, "adhoc", url)


def _api(name: str, url: str) -> dict:
    """Build the JSON the gateway uses for a backend."""
    return _backend(name, url).to_api()


def test_sync_backends_applies_plan():
    """The plan is applied as deletes, then updates, then adds."""
    client = MagicMock(spec=GatewayApiClient)
    current = [_backend("a", "http://old:8080"), _backend("gone", "http://gone:8080")]

    sync_backends(client, [A, B], current)

    assert client.method_calls == [
        call.delete("gone"),
        call.update(_backend("a", "http://a:8080")),
        call.add(_backend("b", "http://b:8080")),
    ]


def test_client_requests(monkeypatch: pytest.MonkeyPatch):
    """Each client method sends the expected HTTP request."""
    urlopen = MagicMock()
    urlopen.return_value.__enter__.return_value.read.return_value = b"[]"
    monkeypatch.setattr(gateway_api.urllib.request, "urlopen", urlopen)
    client = GatewayApiClient("http://localhost:8080/")

    assert client.list_backends() == []
    client.add(_backend("a", "http://a"))
    client.update(_backend("a", "http://a"))
    client.delete("a")

    requests = [c.args[0] for c in urlopen.call_args_list]
    assert [(r.get_method(), r.full_url) for r in requests] == [
        ("GET", "http://localhost:8080/gateway/backend/all"),
        ("POST", "http://localhost:8080/gateway/backend/modify/add"),
        ("POST", "http://localhost:8080/gateway/backend/modify/update"),
        ("POST", "http://localhost:8080/gateway/backend/modify/delete"),
    ]
    assert json.loads(requests[1].data) == _api("a", "http://a")
    assert requests[1].get_header("Content-type") == "application/json"
    assert requests[3].data == b"a"
    assert all(c.kwargs["timeout"] == gateway_api._TIMEOUT_SECONDS for c in urlopen.call_args_list)


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("connection refused"),
        urllib.error.HTTPError("http://x", 500, "boom", {}, io.BytesIO()),  # type: ignore[arg-type]
        TimeoutError(),
    ],
)
def test_client_wraps_transport_errors(monkeypatch: pytest.MonkeyPatch, error: Exception):
    monkeypatch.setattr(gateway_api.urllib.request, "urlopen", MagicMock(side_effect=error))

    with pytest.raises(GatewayApiError):
        GatewayApiClient("http://localhost:8080").delete("a")


def test_client_rejects_invalid_json(monkeypatch: pytest.MonkeyPatch):
    urlopen = MagicMock()
    urlopen.return_value.__enter__.return_value.read.return_value = b"<html>"
    monkeypatch.setattr(gateway_api.urllib.request, "urlopen", urlopen)

    with pytest.raises(GatewayApiError):
        GatewayApiClient("http://localhost:8080").list_backends()


def _respond_with(monkeypatch: pytest.MonkeyPatch, body: bytes) -> GatewayApiClient:
    """Make every request to the gateway return a fixed body."""
    urlopen = MagicMock()
    urlopen.return_value.__enter__.return_value.read.return_value = body
    monkeypatch.setattr(gateway_api.urllib.request, "urlopen", urlopen)
    return GatewayApiClient("http://localhost:8080")


@pytest.mark.parametrize(
    "body", [b'{"name": "a"}', b'["a"]', b'[{"proxyTo": "http://a"}]', b"null"]
)
def test_client_rejects_unexpected_backend_list(monkeypatch: pytest.MonkeyPatch, body: bytes):
    """A response that is not a list of named backends is reported as an API error."""
    client = _respond_with(monkeypatch, body)

    with pytest.raises(GatewayApiError):
        client.list_backends()


def test_client_parses_backends(monkeypatch: pytest.MonkeyPatch):
    """Backends are parsed, tolerating unset fields and ignoring unknown ones."""
    body = json.dumps([_api("a", "http://a"), {"name": "manual", "extra": 1}]).encode()
    client = _respond_with(monkeypatch, body)

    assert client.list_backends() == [
        _backend("a", "http://a"),
        GatewayBackend("manual", None, None, None, None),
    ]
