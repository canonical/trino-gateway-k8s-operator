# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""The single-routing-group policy for registering backends and publishing consumers."""

import logging
from collections.abc import Iterable, Mapping
from types import MappingProxyType

import pytest
from charms.trino_k8s.v0.trino_catalog import TrinoCatalog

from core.policy import Consumer, PolicyInputs, evaluate, log_details
from core.upstreams import UpstreamUsers
from relations.backend_trino_catalog import RelatedBackend

SUPERSET = Consumer(1, "superset", "gw-superset")
DATAHUB = Consumer(2, "datahub", "gw-datahub")


def _backend(name: str, *catalogs: TrinoCatalog | str) -> RelatedBackend:
    parsed = [c if isinstance(c, TrinoCatalog) else TrinoCatalog(c) for c in catalogs]
    return RelatedBackend(
        name=name,
        url=f"http://{name}:8080",
        catalogs=tuple(sorted(parsed, key=lambda c: c.name)),
        relation_id=100,
        remote_app=name,
    )


def _inputs(
    backends: Iterable[RelatedBackend],
    confirmed: Mapping[str, Iterable[Consumer] | None],
    consumers: Iterable[Consumer] = (),
    published: Iterable[Consumer] = (),
    registered: Iterable[str] = (),
    exclusions: Mapping[str, frozenset[str]] | None = None,
    refused: Mapping[str, Mapping[str, str]] | None = None,
) -> PolicyInputs:
    """Build policy inputs where each backend has its own upstream.

    `confirmed` maps a backend name to the consumers its upstream confirms, or to None if the
    backend is not matched to any upstream.
    """
    backends = list(backends)
    matches: dict[str, int | None] = {}
    upstreams = {}
    for index, backend in enumerate(backends):
        users = confirmed.get(backend.name)
        if users is None:
            matches[backend.name] = None
            continue
        matches[backend.name] = index
        upstreams[index] = UpstreamUsers(
            relation_id=index,
            remote_app=backend.name,
            url=backend.url,
            confirmed=frozenset(c.username for c in users),
            refused=MappingProxyType(dict((refused or {}).get(backend.name, {}))),
        )
    return PolicyInputs(
        backends=backends,
        matches=matches,
        upstreams=upstreams,
        consumers={c.relation_id: c for c in [*consumers, *published]},
        published=frozenset(c.relation_id for c in published),
        registered=frozenset(registered),
        exclusions=exclusions or {},
    )


def _names(backends: Iterable[RelatedBackend]) -> list[str]:
    return [b.name for b in backends]


def _catalog_names(catalogs: Iterable[TrinoCatalog]) -> list[str]:
    return [c.name for c in catalogs]


def test_without_consumers_every_matched_backend_is_admitted():
    backends = [_backend("b"), _backend("a"), _backend("unmatched")]

    result = evaluate(_inputs(backends, {"a": [], "b": []}))

    assert _names(result.admitted) == ["a", "b"]
    assert result.pending_backends == 1
    assert result.publish == {}


def test_unmatched_registered_backend_is_deregistered():
    result = evaluate(_inputs([_backend("a")], {"a": None}, registered=["a"]))

    assert result.admitted == []
    assert result.pending_backends == 1


def test_registered_names_without_a_live_backend_are_not_desired():
    result = evaluate(_inputs([_backend("a")], {"a": []}, registered=["a", "gone"]))

    assert _names(result.admitted) == ["a"]


def test_consumer_is_published_once_every_admitted_backend_confirms_it():
    backends = [_backend("a", "hive"), _backend("b", "sales")]

    partial = evaluate(_inputs(backends, {"a": [SUPERSET], "b": []}, consumers=[SUPERSET]))
    full = evaluate(_inputs(backends, {"a": [SUPERSET], "b": [SUPERSET]}, consumers=[SUPERSET]))

    assert partial.publish == {}
    assert partial.pending_consumers == 1
    assert _catalog_names(full.publish[SUPERSET.relation_id]) == ["hive", "sales"]
    assert full.pending_consumers == 0


def test_pending_consumer_never_changes_admitted_backends():
    backends = [_backend("a"), _backend("b")]
    confirmed = {"a": [SUPERSET, DATAHUB], "b": [SUPERSET]}

    without = evaluate(_inputs(backends, confirmed, published=[SUPERSET], registered=["a"]))
    with_pending = evaluate(
        _inputs(backends, confirmed, consumers=[DATAHUB], published=[SUPERSET], registered=["a"])
    )

    assert _names(with_pending.admitted) == _names(without.admitted) == ["a", "b"]
    assert DATAHUB.relation_id not in with_pending.publish
    assert with_pending.pending_consumers == 1


def test_consumer_without_app_name_waits():
    anonymous = Consumer(3, None, "gw-anonymous")

    result = evaluate(_inputs([_backend("a")], {"a": [anonymous]}, consumers=[anonymous]))

    assert result.publish == {}
    assert result.pending_consumers == 1


def test_without_backends_consumers_are_published_with_no_catalogs():
    result = evaluate(_inputs([], {}, consumers=[SUPERSET]))

    assert result.publish == {SUPERSET.relation_id: []}


def test_new_backend_is_admitted_only_once_it_confirms_every_published_consumer():
    backends = [_backend("a", "hive"), _backend("b", "sales")]

    partial = evaluate(
        _inputs(
            backends,
            {"a": [SUPERSET, DATAHUB], "b": [SUPERSET]},
            published=[SUPERSET, DATAHUB],
            registered=["a"],
        )
    )
    full = evaluate(
        _inputs(
            backends,
            {"a": [SUPERSET, DATAHUB], "b": [SUPERSET, DATAHUB]},
            published=[SUPERSET, DATAHUB],
            registered=["a"],
        )
    )

    assert _names(partial.admitted) == ["a"]
    assert partial.pending_backends == 1
    assert _catalog_names(partial.publish[SUPERSET.relation_id]) == ["hive"]
    assert _catalog_names(partial.publish[DATAHUB.relation_id]) == ["hive"]
    assert _names(full.admitted) == ["a", "b"]
    assert _catalog_names(full.publish[SUPERSET.relation_id]) == ["hive", "sales"]


def test_withdrawn_confirmation_keeps_backend_while_it_confirms_another_consumer():
    backends = [_backend("a", "hive"), _backend("b", "sales")]

    result = evaluate(
        _inputs(
            backends,
            {"a": [SUPERSET, DATAHUB], "b": [SUPERSET]},
            published=[SUPERSET, DATAHUB],
            registered=["a", "b"],
        )
    )

    assert _names(result.admitted) == ["a", "b"]
    assert result.gaps == 1
    assert _catalog_names(result.publish[SUPERSET.relation_id]) == ["hive", "sales"]
    assert _catalog_names(result.publish[DATAHUB.relation_id]) == ["hive"]


def test_backend_that_confirms_no_published_consumer_is_deregistered():
    backends = [_backend("a", "hive"), _backend("b", "sales")]

    result = evaluate(
        _inputs(backends, {"a": [SUPERSET], "b": []}, published=[SUPERSET], registered=["a", "b"])
    )

    assert _names(result.admitted) == ["a"]
    assert result.gaps == 0


def test_published_consumer_stays_published_with_no_catalogs_when_unconfirmed():
    result = evaluate(
        _inputs([_backend("a", "hive")], {"a": []}, published=[SUPERSET], registered=["a"])
    )

    assert result.admitted == []
    assert result.publish == {SUPERSET.relation_id: []}


def test_exclusions_hide_catalogs_by_app_name():
    backends = [_backend("a", "hive", "sales", "debug")]

    result = evaluate(
        _inputs(
            backends,
            {"a": [SUPERSET, DATAHUB]},
            consumers=[SUPERSET, DATAHUB],
            exclusions={"superset": frozenset({"debug", "missing"})},
        )
    )

    assert _catalog_names(result.publish[SUPERSET.relation_id]) == ["hive", "sales"]
    assert _catalog_names(result.publish[DATAHUB.relation_id]) == ["debug", "hive", "sales"]


def test_conflicting_catalog_holds_back_new_backend():
    incumbent = _backend("b", TrinoCatalog("sales", "postgresql"))
    newcomer = _backend("a", TrinoCatalog("sales", "mysql"), "hive")

    result = evaluate(
        _inputs(
            [incumbent, newcomer],
            {"a": [SUPERSET], "b": [SUPERSET]},
            published=[SUPERSET],
            registered=["b"],
        )
    )

    assert _names(result.admitted) == ["b"]
    assert result.pending_backends == 1
    assert result.conflicting_catalogs == {"sales"}
    assert result.publish[SUPERSET.relation_id] == [TrinoCatalog("sales", "postgresql")]


def test_unknown_connector_does_not_hold_back_and_known_one_represents():
    incumbent = _backend("b", TrinoCatalog("sales"))
    newcomer = _backend("a", TrinoCatalog("sales", "mysql"))

    result = evaluate(
        _inputs([incumbent, newcomer], {"a": [], "b": []}, consumers=[SUPERSET], registered=["b"])
    )

    assert _names(result.admitted) == ["a", "b"]
    assert result.conflicting_catalogs == frozenset()


def test_conflicts_between_registered_backends_keep_both_and_are_reported():
    backends = [_backend("a", TrinoCatalog("x", "pg")), _backend("b", TrinoCatalog("x", "my"))]

    result = evaluate(_inputs(backends, {"a": [], "b": []}, registered=["a", "b"]))

    assert _names(result.admitted) == ["a", "b"]
    assert result.conflicting_catalogs == {"x"}


def test_refusals_are_collected_by_reason_across_matched_backends():
    backends = [_backend("a"), _backend("b"), _backend("c")]
    refused = {
        "a": {"gw-superset": "username already exists on this Trino"},
        "b": {
            "gw-superset": "username already exists on this Trino",
            "gw-datahub": "invalid username",
        },
    }

    result = evaluate(
        _inputs(backends, {"a": [], "b": [], "c": []}, consumers=[SUPERSET], refused=refused)
    )

    assert result.refusals == {
        "username already exists on this Trino": {"gw-superset"},
        "invalid username": {"gw-datahub"},
    }
    assert result.refusing_backends == 2


def test_log_details_names_pending_items_and_problems(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)
    anonymous = Consumer(3, None, "gw-anonymous")
    backends = [
        _backend("a", TrinoCatalog("x", "pg")),
        _backend("b", TrinoCatalog("x", "my")),
        _backend("c", TrinoCatalog("x", "other")),
        _backend("d"),
        _backend("unmatched"),
    ]
    inputs = _inputs(
        backends,
        {"a": [SUPERSET], "b": [], "c": [SUPERSET, DATAHUB], "d": []},
        consumers=[DATAHUB, anonymous],
        published=[SUPERSET],
        registered=["a", "b"],
        refused={"b": {"gw-superset": "invalid username"}},
    )

    log_details(inputs, evaluate(inputs))

    assert "Backend 'unmatched' is waiting for registration: it is not matched" in caplog.text
    assert "Backend 'd' is waiting for registration: it has not confirmed ['gw-superset']" in (
        caplog.text
    )
    assert "Backend 'c' is waiting for registration: its catalogs conflict" in caplog.text
    assert "Consumer relation 3 is waiting for publication: it has not published" in caplog.text
    assert "Consumer relation 2 is waiting for publication: backends ['a']" in caplog.text
    assert "Backend 'b' refused user 'gw-superset': invalid username" in caplog.text
    assert "conflicting connectors between backends: ['x']" in caplog.text


def test_log_details_reports_gaps(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING)
    inputs = _inputs(
        [_backend("a")], {"a": [SUPERSET]}, published=[SUPERSET, DATAHUB], registered=["a"]
    )

    log_details(inputs, evaluate(inputs))

    assert "Registered backend 'a' has not confirmed published user 'gw-datahub'" in caplog.text
