# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""The charm's configuration options."""

import pydantic
import yaml


class CharmConfig(pydantic.BaseModel):
    """The validated charm configuration.

    Attributes:
        catalog_exclusions: YAML mapping of consumer `app_name`s to the catalogs hidden from them.
    """

    catalog_exclusions: str | None = None

    @pydantic.field_validator("catalog_exclusions")
    @classmethod
    def _validate_catalog_exclusions(cls, value: str | None) -> str | None:
        """Check that `catalog-exclusions` parses as YAML, as the Trino charm does.

        Args:
            value: The raw option value.

        Returns:
            The value, unchanged.

        Raises:
            ValueError: if the value is not valid YAML.
        """
        if not value:
            return value
        try:
            yaml.safe_load(value)
        except yaml.YAMLError as e:
            problem = getattr(e, "problem", None)
            raise ValueError(
                f"not valid YAML: {problem}" if problem else "not valid YAML"
            ) from None
        return value
