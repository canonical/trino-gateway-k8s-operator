# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import io
import json
import urllib.error
from unittest.mock import MagicMock, call

import pytest

import gateway_api
from config import BackendConfig
from gateway_api import GatewayApiClient, GatewayApiError, plan_sync, sync_backends

A = BackendConfig(name="a", url="http://a:8080")
B = BackendConfig(name="b", url="http://b:8080")


def _entry(name: str, url: str, **overrides) -> dict:
    return {
        "name": name,
        "proxyTo": url,
        "active": True,
        "routingGroup": "adhoc",
        "externalUrl": url,
        **overrides,
    }


def test_plan_sync_noop_when_in_sync():
    plan = plan_sync([A], [_entry("a", "http://a:8080")])

    assert (plan.add, plan.update, plan.delete) == ([], [], [])


def test_plan_sync_adds_updates_and_deletes():
    current = [
        _entry("a", "http://old:8080"),
        _entry("manual", "http://manual:8080"),
    ]

    plan = plan_sync([A, B], current)

    assert plan.add == [_entry("b", "http://b:8080")]
    assert plan.update == [_entry("a", "http://a:8080")]
    assert plan.delete == ["manual"]


@pytest.mark.parametrize(
    "drift", [{"active": False}, {"routingGroup": "etl"}, {"externalUrl": "http://x"}]
)
def test_plan_sync_overwrites_drift(drift: dict):
    plan = plan_sync([A], [_entry("a", "http://a:8080", **drift)])

    assert plan.update == [_entry("a", "http://a:8080")]


def test_sync_backends_applies_plan():
    client = MagicMock(spec=GatewayApiClient)
    client.list_backends.return_value = [
        _entry("a", "http://old:8080"),
        _entry("gone", "http://gone:8080"),
    ]

    sync_backends(client, [A, B])

    assert client.method_calls == [
        call.list_backends(),
        call.delete("gone"),
        call.update(_entry("a", "http://a:8080")),
        call.add(_entry("b", "http://b:8080")),
    ]


def test_client_requests(monkeypatch: pytest.MonkeyPatch):
    urlopen = MagicMock()
    urlopen.return_value.__enter__.return_value.read.return_value = b"[]"
    monkeypatch.setattr(gateway_api.urllib.request, "urlopen", urlopen)
    client = GatewayApiClient("http://localhost:8080/")

    assert client.list_backends() == []
    client.add(_entry("a", "http://a"))
    client.update(_entry("a", "http://a"))
    client.delete("a")

    requests = [c.args[0] for c in urlopen.call_args_list]
    assert [(r.get_method(), r.full_url) for r in requests] == [
        ("GET", "http://localhost:8080/gateway/backend/all"),
        ("POST", "http://localhost:8080/gateway/backend/modify/add"),
        ("POST", "http://localhost:8080/gateway/backend/modify/update"),
        ("POST", "http://localhost:8080/gateway/backend/modify/delete"),
    ]
    assert json.loads(requests[1].data) == _entry("a", "http://a")
    assert requests[1].get_header("Content-type") == "application/json"
    assert requests[3].data == b"a"
    assert all(c.kwargs["timeout"] == gateway_api.TIMEOUT_SECONDS for c in urlopen.call_args_list)


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
