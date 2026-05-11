"""Schema validation helper for CAS contract tests.

Discovers all JSON Schema files under docs/fs/, builds a referencing.Registry
for $ref resolution, and provides validation convenience methods.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import referencing
import referencing.jsonschema

# Project root: two levels up from tests/helpers/
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
FORMAL_SUBSTRATE_DIR = PROJECT_ROOT / "docs" / "fs"


def _build_registry() -> referencing.Registry:
    """Build a Registry mapping $id URIs and relative paths to schema resources."""
    resources: list[tuple[str, referencing.Resource]] = []

    for schema_path in FORMAL_SUBSTRATE_DIR.rglob("*.schema.json"):
        schema = json.loads(schema_path.read_text())
        resource = referencing.Resource.from_contents(
            schema, default_specification=referencing.jsonschema.DRAFT202012
        )

        # Register under $id if present
        schema_id = schema.get("$id")
        if schema_id:
            resources.append((schema_id, resource))

        # Register under relative path from docs/fs/ (e.g., "sage/lifecycle.schema.json")
        rel_path = schema_path.relative_to(FORMAL_SUBSTRATE_DIR).as_posix()
        resources.append((rel_path, resource))

        # Register under bare filename (e.g., "lifecycle.schema.json")
        resources.append((schema_path.name, resource))

    return referencing.Registry().with_resources(resources)


class SchemaValidator:
    """Validates instances against CAS formal substrate schemas."""

    def __init__(self) -> None:
        self._registry = _build_registry()

    def _get_schema(self, schema_path: str | Path) -> dict[str, Any]:
        """Load a schema file by path (absolute or relative to docs/fs/)."""
        path = Path(schema_path)
        if not path.is_absolute():
            path = FORMAL_SUBSTRATE_DIR / path
        return json.loads(path.read_text())

    def _make_validator(self, schema: dict[str, Any]) -> jsonschema.Draft202012Validator:
        """Create a validator with the shared registry for $ref resolution."""
        return jsonschema.Draft202012Validator(
            schema,
            registry=self._registry,
        )

    def validate(self, schema_path: str | Path, instance: Any) -> None:
        """Validate instance against schema. Raises ValidationError on failure."""
        schema = self._get_schema(schema_path)
        validator = self._make_validator(schema)
        validator.validate(instance)

    def is_valid(self, schema_path: str | Path, instance: Any) -> bool:
        """Return True if instance validates against schema."""
        schema = self._get_schema(schema_path)
        validator = self._make_validator(schema)
        return validator.is_valid(instance)

    def validation_errors(
        self, schema_path: str | Path, instance: Any
    ) -> list[jsonschema.ValidationError]:
        """Return all validation errors for instance against schema."""
        schema = self._get_schema(schema_path)
        validator = self._make_validator(schema)
        return list(validator.iter_errors(instance))

    def validate_sub_schema(
        self, schema_path: str | Path, definition_name: str, instance: Any
    ) -> None:
        """Validate instance against a named $defs entry within a schema file.

        Useful for schemas like interrupt.schema.json that define multiple
        types via $defs without a top-level validation target.
        """
        schema = self._get_schema(schema_path)
        defs = schema.get("$defs", {})
        if definition_name not in defs:
            raise KeyError(
                f"Definition '{definition_name}' not found in {schema_path}. "
                f"Available: {list(defs.keys())}"
            )
        sub_schema = defs[definition_name]
        validator = self._make_validator(sub_schema)
        validator.validate(instance)

    def is_sub_schema_valid(
        self, schema_path: str | Path, definition_name: str, instance: Any
    ) -> bool:
        """Return True if instance validates against a named $defs entry."""
        try:
            self.validate_sub_schema(schema_path, definition_name, instance)
            return True
        except (jsonschema.ValidationError, KeyError):
            return False
