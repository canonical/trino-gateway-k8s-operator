# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

import pytest

from core.upstreams import UpstreamUsers, build, match_backends
from relations.backend_trino_catalog import RelatedBackend


def _upstream(relation_id: int, url: str | None) -> UpstreamUsers:
    return build(relation_id, f"trino-{relation_id}", url, set(), [], {})


def _backend(name: str, url: str) -> RelatedBackend:
    return RelatedBackend(name=name, url=url, catalogs=(), relation_id=99, remote_app=name)


def test_build_confirms_requested_registered_users():
    upstream = build(4, "trino", "trino:8080", {"a", "b", "c"}, ["a", "b"], {})

    assert upstream == UpstreamUsers(
        relation_id=4,
        remote_app="trino",
        url="http://trino:8080",
        confirmed=frozenset({"a", "b"}),
        refused={},
    )


def test_build_records_refusals_with_reasons():
    upstream = build(4, "trino", "trino:8080", {"a", "b"}, ["a"], {"b": "username taken"})

    assert upstream.confirmed == {"a"}
    assert dict(upstream.refused) == {"b": "username taken"}


def test_build_refusal_wins_over_registration():
    upstream = build(4, "trino", "trino:8080", {"a"}, ["a"], {"a": "conflict"})

    assert upstream.confirmed == frozenset()
    assert dict(upstream.refused) == {"a": "conflict"}


def test_build_ignores_unrequested_names():
    """Names the gateway never requested, or has withdrawn, count for nothing."""
    upstream = build(4, "trino", "trino:8080", {"a"}, ["a", "stale"], {"other": "no"})

    assert upstream.confirmed == {"a"}
    assert dict(upstream.refused) == {}


def test_build_refused_is_read_only():
    upstream = build(4, "trino", "trino:8080", {"a"}, [], {"a": "no"})

    with pytest.raises(TypeError):
        upstream.refused["a"] = "changed"  # type: ignore[index]


@pytest.mark.parametrize(
    ("raw_url", "expected"),
    [
        ("trino:8080", "http://trino:8080"),
        ("trino.example.com:443", "https://trino.example.com:443"),
        (None, None),
        ("", None),
        ("no-port", None),
        ("trino:0", None),
        ("a/v1:8080", None),
        ("user@a:8080", None),
    ],
)
def test_build_normalises_url_like_backends(raw_url: str | None, expected: str | None):
    """URLs are normalised as backend URLs are, and an invalid one becomes None."""
    assert _upstream(1, raw_url).url == expected


def test_match_by_identical_url():
    backends = [_backend("trino-a", "http://trino-a:8080"), _backend("trino-b", "http://b:443")]
    upstreams = [_upstream(10, "trino-a:8080"), _upstream(11, "trino-b:8080")]

    assert match_backends(backends, upstreams) == {"trino-a": 10, "trino-b": None}


def test_match_compares_normalised_urls():
    """A port-443 address matches the https URL its backend gets."""
    backends = [_backend("trino-a", "https://trino.example.com:443")]

    assert match_backends(backends, [_upstream(10, "trino.example.com:443")]) == {"trino-a": 10}


def test_match_without_upstreams_or_urls():
    backends = [_backend("trino-a", "http://trino-a:8080")]

    assert match_backends(backends, []) == {"trino-a": None}
    assert match_backends(backends, [_upstream(10, None), _upstream(11, "bad")]) == {
        "trino-a": None
    }


def test_match_ignores_duplicate_urls_and_warns(caplog: pytest.LogCaptureFixture):
    """A URL advertised by several upstreams matches none of them."""
    caplog.set_level(logging.WARNING)
    backends = [_backend("trino-a", "http://trino-a:8080")]
    upstreams = [_upstream(11, "trino-a:8080"), _upstream(10, "trino-a:8080")]

    assert match_backends(backends, upstreams) == {"trino-a": None}
    assert "'trino-a'" in caplog.text
    assert "[10, 11]" in caplog.text


def test_match_several_backends_to_one_upstream():
    backends = [
        _backend("trino-a", "http://trino:8080"),
        _backend("trino-a2", "http://trino:8080"),
    ]

    assert match_backends(backends, [_upstream(10, "trino:8080")]) == {
        "trino-a": 10,
        "trino-a2": 10,
    }


def test_unmatched_upstream_confirms_nothing_for_any_backend():
    """An upstream that confirmed users but advertises another URL matches no backend."""
    upstream = build(10, "trino", "other:8080", {"a"}, ["a"], {})
    backends = [_backend("trino-a", "http://trino-a:8080")]

    assert upstream.confirmed == {"a"}
    assert 10 not in match_backends(backends, [upstream]).values()
