# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm configuration model."""

import re
from typing import Any
from urllib.parse import urlsplit

import pydantic
import yaml

# Up to 63 characters, starting with a letter or digit.
BACKEND_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$"


class BackendConfig(pydantic.BaseModel):
    """A Trino cluster to register with the gateway."""

    model_config = pydantic.ConfigDict(extra="forbid")

    name: str
    url: str

    @pydantic.field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not re.fullmatch(BACKEND_NAME_PATTERN, value):
            raise ValueError(f"must match {BACKEND_NAME_PATTERN}")
        return value

    @pydantic.field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        parts = urlsplit(value)
        # Reading .port raises ValueError for a malformed port.
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.port == 0:
            raise ValueError("must be an http(s) URL with a host")
        if parts.path not in ("", "/") or parts.query or parts.fragment or "@" in parts.netloc:
            raise ValueError("must not have a path, query, fragment or userinfo")
        # Stored without a trailing slash so it compares equal to what the gateway returns.
        return value.rstrip("/")


class CharmConfig(pydantic.BaseModel):
    """Validated charm configuration."""

    backends: list[BackendConfig] = []

    @pydantic.field_validator("backends", mode="before")
    @classmethod
    def _parse_backends(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            parsed = yaml.safe_load(value)
        except yaml.YAMLError:
            raise ValueError("is not valid YAML") from None
        return [] if parsed is None else parsed

    @pydantic.field_validator("backends")
    @classmethod
    def _validate_unique_names(cls, value: list[BackendConfig]) -> list[BackendConfig]:
        names = [backend.name for backend in value]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate names: {', '.join(duplicates)}")
        return value


def describe_error(error: pydantic.ValidationError) -> str:
    """Summarise the first validation error of the backends option for a status message.

    Args:
        error: The error raised while loading `CharmConfig`.

    Returns:
        A short, lowercase description such as `[0].url: field required`.
    """
    first = error.errors()[0]
    location = "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}" for part in first["loc"][1:]
    ).lstrip(".")
    message = first["msg"].removeprefix("Value error, ")
    message = message[:1].lower() + message[1:]
    return f"{location}: {message}" if location else message
