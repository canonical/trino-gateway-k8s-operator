# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from charms.trino_k8s.v0.trino_catalog import TrinoCatalog

from relations.backend_trino_catalog import RelatedBackend, backend_name, backend_url, load


def _info(relation_id: int, app: str, url: str = "trino:8080", catalogs=("b", "a")) -> dict:
    """Build the per-relation dict the library returns for a complete relation."""
    return {
        "trino_url": url,
        "trino_catalogs": [
            c if isinstance(c, TrinoCatalog) else TrinoCatalog(c) for c in catalogs
        ],
        "trino_credentials_secret_id": "secret:abc",
        "relation_id": relation_id,
        "remote_app": app,
    }


def _load(infos: list[dict], relation_ids: list[int] | None = None):
    """Run `load` against a mocked requirer and return its result and the requirer."""
    requirer = MagicMock()
    requirer.get_all_trino_info.return_value = {info["relation_id"]: info for info in infos}
    ids = relation_ids if relation_ids is not None else [info["relation_id"] for info in infos]
    return load(requirer, [SimpleNamespace(id=i) for i in ids]), requirer  # type: ignore[arg-type]


def test_backend_name_passes_valid_names_through():
    """A name that is already valid is used as it is."""
    assert backend_name("trino-a") == "trino-a"
    assert backend_name("a" * 63) == "a" * 63


def test_backend_name_shortens_long_names_deterministically():
    """Long names are cut to 63 characters with a stable hash suffix that tells them apart."""
    long_a = "a" * 64
    long_b = "a" * 54 + "b" * 20

    name = backend_name(long_a)

    assert len(name) == 63
    assert name.startswith("a" * 54 + "-")
    assert backend_name(long_a) == name
    assert backend_name(long_b) != name
    assert len(backend_name(long_b)) == 63


@pytest.mark.parametrize(
    ("trino_url", "expected"),
    [
        ("trino.example.com:443", "https://trino.example.com:443"),
        ("trino.example.com:80", "http://trino.example.com:80"),
        (
            "trino-k8s.model.svc.cluster.local:8080",
            "http://trino-k8s.model.svc.cluster.local:8080",
        ),
    ],
)
def test_backend_url_infers_scheme_from_port(trino_url: str, expected: str):
    """Port 443 gives https and any other port gives http."""
    assert backend_url(trino_url) == expected


@pytest.mark.parametrize(
    "trino_url", ["", "host", ":8080", "host:", "host:http", "host:0", "host:65536", "host:-1"]
)
def test_backend_url_rejects_malformed_addresses(trino_url: str):
    """An address without a host or a valid port is rejected."""
    with pytest.raises(ValueError):
        backend_url(trino_url)


def test_load_without_relations():
    """No relations give no backends and nothing skipped."""
    result, _ = _load([])

    assert result.backends == []
    assert result.skipped == 0


def test_load_builds_backend_with_sorted_catalogs():
    """A complete relation becomes a backend that carries its catalogs, sorted by name."""
    hive = TrinoCatalog("b", connector="hive", description="warehouse")
    result, _ = _load([_info(7, "trino-a", "trino:8080", catalogs=(hive, "a"))])

    assert result.backends == [
        RelatedBackend(
            name="trino-a",
            url="http://trino:8080",
            catalogs=(TrinoCatalog("a"), hive),
            relation_id=7,
            remote_app="trino-a",
        )
    ]
    assert result.skipped == 0


def test_load_sorts_backends_by_name():
    """Backends are returned in name order regardless of relation order."""
    result, _ = _load([_info(1, "trino-b"), _info(2, "trino-a"), _info(3, "trino-c")])

    assert [backend.name for backend in result.backends] == ["trino-a", "trino-b", "trino-c"]


def test_load_accepts_empty_catalogs():
    """A Trino application with no catalogs is still a valid backend."""
    result, _ = _load([_info(1, "trino-a", catalogs=())])

    assert result.backends[0].catalogs == ()


def test_load_counts_relations_the_library_left_out():
    """Relations the library skipped as incomplete are counted."""
    result, _ = _load([_info(1, "trino-a")], relation_ids=[1, 2, 3])

    assert [backend.name for backend in result.backends] == ["trino-a"]
    assert result.skipped == 2


def test_load_skips_and_logs_malformed_url_among_valid_relations(
    caplog: pytest.LogCaptureFixture,
):
    """A relation with a malformed URL is logged and skipped without affecting the others."""
    caplog.set_level(logging.WARNING)

    result, _ = _load([_info(1, "trino-a"), _info(2, "trino-bad", "no-port"), _info(3, "trino-c")])

    assert [backend.name for backend in result.backends] == ["trino-a", "trino-c"]
    assert result.skipped == 1
    assert "2" in caplog.text
    assert "'trino-bad'" in caplog.text


def test_load_skips_name_that_stays_invalid_after_shortening(caplog: pytest.LogCaptureFixture):
    """A remote application name that cannot be made valid is logged and skipped."""
    caplog.set_level(logging.WARNING)

    result, _ = _load([_info(1, "bad name!")])

    assert result.backends == []
    assert result.skipped == 1
    assert "'bad name!'" in caplog.text


@pytest.mark.parametrize(
    "trino_url", ["?:8080", "a/v1:8080", "a?x=1:8080", "a#x:8080", "user@a:8080"]
)
def test_load_skips_address_that_does_not_make_a_valid_url(trino_url: str):
    """An address that would put a path, query, fragment or userinfo in the URL is skipped."""
    result, _ = _load([_info(1, "trino-a", trino_url)])

    assert result.backends == []
    assert result.skipped == 1


def test_load_skips_relation_with_non_string_catalog_names(caplog: pytest.LogCaptureFixture):
    """A relation advertising a non-string catalog name is logged and skipped, not raised on."""
    caplog.set_level(logging.WARNING)

    result, _ = _load([_info(1, "trino-a", catalogs=(None, "hive")), _info(2, "trino-b")])

    assert [backend.name for backend in result.backends] == ["trino-b"]
    assert result.skipped == 1
    assert "'trino-a'" in caplog.text


def test_load_skips_relation_with_non_string_connector(caplog: pytest.LogCaptureFixture):
    """A relation advertising a non-string connector is logged and skipped."""
    caplog.set_level(logging.WARNING)
    bad = TrinoCatalog("hive", connector=["postgresql"])  # type: ignore[arg-type]

    result, _ = _load([_info(1, "trino-a", catalogs=(bad,)), _info(2, "trino-b")])

    assert [backend.name for backend in result.backends] == ["trino-b"]
    assert result.skipped == 1


def test_load_never_reads_credentials(caplog: pytest.LogCaptureFixture):
    """Neither the credentials accessors nor the secret ID are ever used."""
    caplog.set_level(logging.DEBUG)

    _, requirer = _load([_info(1, "trino-a"), _info(2, "trino-bad", "no-port")])

    requirer.get_credentials.assert_not_called()
    requirer.get_all_credentials.assert_not_called()
    assert "secret:abc" not in caplog.text
