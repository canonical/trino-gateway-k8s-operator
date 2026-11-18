# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Catalog exclusions, connector conflicts and merging catalogs from several backends."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

import yaml
from charms.trino_k8s.v0.trino_catalog import TrinoCatalog

import constants


@dataclass(frozen=True)
class MergedCatalogs:
    """The catalogs of several backends, one per name.

    Attributes:
        catalogs: The representative catalog, by name.
        conflicting: Names whose catalogs conflict between backends.
    """

    catalogs: dict[str, TrinoCatalog]
    conflicting: frozenset[str]


def parse_exclusions(raw: str | None) -> dict[str, frozenset[str]]:
    """Parse `catalog-exclusions` the way the Trino charm does.

    Args:
        raw: The option value, a YAML mapping of `app_name`s to lists of catalog names.

    Returns:
        The excluded catalog names, by `app_name`. Empty for an empty value, unparsable YAML or
        a value that is not a mapping. Entries whose value is not a list are ignored.
    """
    if not raw:
        return {}
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        app_name: frozenset(name for name in catalogs if isinstance(name, str))
        for app_name, catalogs in parsed.items()
        if isinstance(app_name, str) and isinstance(catalogs, list)
    }


def conflicts(a: TrinoCatalog, b: TrinoCatalog) -> bool:
    """Tell whether two catalogs with the same name describe different data sources.

    Args:
        a: A catalog.
        b: Another catalog with the same name.

    Returns:
        True if their connectors differ, unless one is empty and empty connectors never
        conflict. Descriptions are not compared.
    """
    if constants.EMPTY_CONNECTOR_NEVER_CONFLICTS and (not a.connector or not b.connector):
        return False
    return a.connector != b.connector


def merge(sources: Iterable[tuple[str, Iterable[TrinoCatalog]]]) -> MergedCatalogs:
    """Merge the catalogs of several backends by name.

    Args:
        sources: The catalogs of each backend, as `(backend_name, catalogs)` pairs.

    Returns:
        For each name, the first catalog with a connector in backend name order, or the first
        catalog if none has one; and the names whose catalogs conflict.
    """
    by_name: dict[str, list[TrinoCatalog]] = defaultdict(list)
    for _, catalogs in sorted(sources, key=lambda source: source[0]):
        for catalog in catalogs:
            by_name[catalog.name].append(catalog)

    merged = {}
    conflicting = set()
    for name, candidates in by_name.items():
        merged[name] = next((c for c in candidates if c.connector), candidates[0])
        if any(conflicts(a, b) for i, a in enumerate(candidates) for b in candidates[i + 1 :]):
            conflicting.add(name)
    return MergedCatalogs(
        catalogs=dict(sorted(merged.items())), conflicting=frozenset(conflicting)
    )
