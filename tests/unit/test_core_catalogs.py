# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from charms.trino_k8s.v0.trino_catalog import TrinoCatalog

import constants
from config import CharmConfig
from core.catalogs import conflicts, merge, parse_exclusions


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, {}),
        ("", {}),
        ("superset: [a, b]\nranger: [c]", {"superset": {"a", "b"}, "ranger": {"c"}}),
        ("{unclosed", {}),
        ("[a, b]", {}),
        ("just a string", {}),
        ("superset: a\nranger: [c]", {"ranger": {"c"}}),
        ("superset: [a, {b: c}, 1]", {"superset": {"a"}}),
        ("1: [a]\nsuperset: []", {"superset": set()}),
    ],
)
def test_parse_exclusions_ignores_malformed_shapes_and_entries(raw, expected):
    """Like the Trino charm, malformed values are ignored rather than rejected."""
    assert parse_exclusions(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "superset: [a]", "[a]", "just a string"])
def test_config_accepts_parseable_yaml(raw):
    assert CharmConfig(catalog_exclusions=raw).catalog_exclusions == raw


def test_config_rejects_unparsable_yaml():
    with pytest.raises(ValueError, match="not valid YAML"):
        CharmConfig(catalog_exclusions="superset: [a")


@pytest.mark.parametrize(
    ("a", "b", "flag", "expected"),
    [
        ("postgresql", "postgresql", True, False),
        ("postgresql", "mysql", True, True),
        ("", "mysql", True, False),
        ("postgresql", "", True, False),
        ("", "", True, False),
        ("", "mysql", False, True),
        ("", "", False, False),
        ("postgresql", "mysql", False, True),
    ],
)
def test_conflicts_compares_connectors_only(
    monkeypatch: pytest.MonkeyPatch, a: str, b: str, flag: bool, expected: bool
):
    monkeypatch.setattr(constants, "EMPTY_CONNECTOR_NEVER_CONFLICTS", flag)

    first = TrinoCatalog("sales", connector=a, description="one")
    second = TrinoCatalog("sales", connector=b, description="two")

    assert conflicts(first, second) is expected


def test_merge_takes_first_known_connector_in_backend_name_order():
    merged = merge(
        [
            ("trino-c", [TrinoCatalog("sales", "mysql")]),
            ("trino-b", [TrinoCatalog("sales", "postgresql"), TrinoCatalog("hive")]),
            ("trino-a", [TrinoCatalog("sales"), TrinoCatalog("hive", description="x")]),
        ]
    )

    assert merged.catalogs == {
        "hive": TrinoCatalog("hive", description="x"),
        "sales": TrinoCatalog("sales", "postgresql"),
    }
    assert list(merged.catalogs) == ["hive", "sales"]
    assert merged.conflicting == {"sales"}


def test_merge_without_conflicts():
    merged = merge([("a", [TrinoCatalog("x", "pg")]), ("b", [TrinoCatalog("x", "pg")]), ("c", [])])

    assert merged.catalogs == {"x": TrinoCatalog("x", "pg")}
    assert merged.conflicting == frozenset()


def test_merge_of_nothing_is_empty():
    assert merge([]).catalogs == {}
