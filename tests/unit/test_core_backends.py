# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest

from core.backends import GatewayBackend, plan_sync
from relations.backend_trino_catalog import RelatedBackend


def _related(name: str, url: str) -> RelatedBackend:
    """Build a related backend with no catalogs."""
    return RelatedBackend(name=name, url=url, catalogs=(), relation_id=1, remote_app=name)


A = _related("a", "http://a:8080")
B = _related("b", "http://b:8080")


def _backend(name: str, url: str, **overrides) -> GatewayBackend:
    """Build the backend the gateway should have for a URL, with optional field overrides."""
    fields = {
        "name": name,
        "proxy_to": url,
        "active": True,
        "routing_group": "adhoc",
        "external_url": url,
        **overrides,
    }
    return GatewayBackend(**fields)


def test_from_related_uses_default_routing_group():
    """A related backend is active in the adhoc group and redirects to its own URL."""
    assert GatewayBackend.from_related(A) == _backend("a", "http://a:8080")


def test_api_round_trip():
    """A backend survives conversion to the gateway's JSON and back."""
    backend = _backend("a", "http://a:8080")

    assert backend.to_api() == {
        "name": "a",
        "proxyTo": "http://a:8080",
        "active": True,
        "routingGroup": "adhoc",
        "externalUrl": "http://a:8080",
    }
    assert GatewayBackend.from_api(backend.to_api()) == backend


def test_from_api_ignores_extra_and_tolerates_missing_fields():
    """Only the name is required; extra keys are dropped and unset fields become None."""
    backend = GatewayBackend.from_api({"name": "a", "proxyTo": "http://a", "other": 1})

    assert backend == GatewayBackend("a", "http://a", None, None, None)


@pytest.mark.parametrize("data", [None, [], "a", {}, {"name": None}, {"name": 1}])
def test_from_api_requires_an_object_with_a_name(data):
    """An entry that is not an object with a string name is rejected."""
    with pytest.raises(ValueError):
        GatewayBackend.from_api(data)


def test_plan_sync_noop_when_in_sync():
    """Nothing changes when the registered backends equal the desired ones."""
    plan = plan_sync([A], [_backend("a", "http://a:8080")])

    assert (plan.add, plan.update, plan.delete) == ([], [], [])


def test_plan_sync_adds_updates_and_deletes():
    """Missing backends are added, different ones updated and unknown ones deleted."""
    current = [
        _backend("a", "http://old:8080"),
        _backend("manual", "http://manual:8080"),
    ]

    plan = plan_sync([A, B], current)

    assert plan.add == [_backend("b", "http://b:8080")]
    assert plan.update == [_backend("a", "http://a:8080")]
    assert plan.delete == ["manual"]


@pytest.mark.parametrize(
    "drift",
    [
        {"active": False},
        {"routing_group": "etl"},
        {"external_url": "http://x"},
        {"routing_group": None},
    ],
)
def test_plan_sync_overwrites_drift(drift: dict):
    """A backend that differs in any field is updated, including one with unset fields."""
    plan = plan_sync([A], [_backend("a", "http://a:8080", **drift)])

    assert plan.update == [_backend("a", "http://a:8080")]
