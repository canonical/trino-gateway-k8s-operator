# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest

from config import BackendConfig
from core.backends import BackendNameConflictError, GatewayBackend, merge_backends, plan_sync
from relations.backend_trino_catalog import RelatedBackend

A = BackendConfig(name="a", url="http://a:8080")
B = BackendConfig(name="b", url="http://b:8080")


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


def _related(name: str, url: str = "http://r:8080", relation_id: int = 1) -> RelatedBackend:
    """Build a related backend with no catalogs."""
    return RelatedBackend(
        name=name, url=url, catalogs=(), relation_id=relation_id, remote_app=name
    )


def test_from_config_uses_default_routing_group():
    """A configured backend is active in the adhoc group and redirects to its own URL."""
    assert GatewayBackend.from_config(A) == _backend("a", "http://a:8080")


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


def test_merge_backends_unions_configured_then_related():
    """Configured backends come first, in order, followed by the related ones."""
    merged = merge_backends([B, A], [_related("z", "http://z:80"), _related("c")])

    assert merged == [
        B,
        A,
        BackendConfig(name="z", url="http://z:80"),
        BackendConfig(name="c", url="http://r:8080"),
    ]


def test_merge_backends_with_nothing_is_empty():
    """Without any source there are no backends."""
    assert merge_backends([], []) == []


def test_merge_backends_rejects_config_and_relation_clash():
    """A related backend named like a configured one is a conflict."""
    with pytest.raises(BackendNameConflictError) as error:
        merge_backends([A, B], [_related("b")])

    assert error.value.names == ["b"]


def test_merge_backends_rejects_relation_clash_with_sorted_names():
    """Duplicates among related backends are reported once each, sorted."""
    with pytest.raises(BackendNameConflictError) as error:
        merge_backends([], [_related("y"), _related("x"), _related("y"), _related("x")])

    assert error.value.names == ["x", "y"]


def test_merge_backends_is_case_sensitive():
    """Names differing only in case do not conflict, like the gateway's primary key."""
    merged = merge_backends([A], [_related("A")])

    assert [backend.name for backend in merged] == ["a", "A"]
