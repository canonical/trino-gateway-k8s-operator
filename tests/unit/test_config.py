# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pydantic
import pytest

from config import BackendConfig, CharmConfig, describe_error


@pytest.mark.parametrize("raw", ["", "  \n", "[]"])
def test_empty_backends(raw: str):
    assert CharmConfig(backends=raw).backends == []  # type: ignore[arg-type]


def test_parses_backends_and_strips_trailing_slash():
    raw = """
    - name: trino-a
      url: http://trino-a.example:8080/
    - name: trino_b.2
      url: https://trino-b.example
    """

    assert CharmConfig(backends=raw).backends == [  # type: ignore[arg-type]
        BackendConfig(name="trino-a", url="http://trino-a.example:8080"),
        BackendConfig(name="trino_b.2", url="https://trino-b.example"),
    ]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("- name: [", "is not valid YAML"),
        ("name: a", "input should be a valid list"),
        ("- name: a", "[0].url: field required"),
        ("- {name: a, url: 'http://a', token: x}", "[0].token: extra inputs are not permitted"),
        ("- {name: -a, url: 'http://a'}", "[0].name: must match"),
        ("- {name: '', url: 'http://a'}", "[0].name: must match"),
        (f"- {{name: {'a' * 64}, url: 'http://a'}}", "[0].name: must match"),
        ("- {name: a, url: 'ftp://a'}", "[0].url: must be an http(s) URL with a host"),
        ("- {name: a, url: 'http://'}", "[0].url: must be an http(s) URL with a host"),
        ("- {name: a, url: 'http://a:99999'}", "[0].url: port out of range"),
        ("- {name: a, url: 'http://a/v1'}", "[0].url: must not have a path"),
        ("- {name: a, url: 'http://a?x=1'}", "[0].url: must not have a path"),
        ("- {name: a, url: 'http://a#x'}", "[0].url: must not have a path"),
        ("- {name: a, url: 'http://u:p@a'}", "[0].url: must not have a path"),
        (
            "- {name: a, url: 'http://a'}\n- {name: a, url: 'http://b'}",
            "duplicate names: a",
        ),
    ],
)
def test_invalid_backends(raw: str, message: str):
    with pytest.raises(pydantic.ValidationError) as excinfo:
        CharmConfig(backends=raw)  # type: ignore[arg-type]

    assert describe_error(excinfo.value).startswith(message)
