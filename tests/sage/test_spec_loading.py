"""The C YAML loader reads the published specifications exactly as the safe loader does.

The application and several test modules parse the OpenAPI documents with
PyYAML's C loader for speed. That is only a speed-up if the objects it builds
are the ones the pure-Python safe loader builds, so each published document is
parsed both ways here and the results compared.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import sage.app
from tests.helpers import spec_loading

_DOCS_FS = Path(__file__).resolve().parents[2] / "docs" / "fs"
_SPECS = sorted(_DOCS_FS.glob("**/*.openapi.yaml"))


def test_the_published_specifications_are_found() -> None:
    names = {path.name for path in _SPECS}
    assert {"sage_core_api.openapi.yaml", "cas_app_api.openapi.yaml"} <= names, names


@pytest.mark.parametrize("spec", _SPECS, ids=lambda path: path.name)
def test_c_loader_parses_the_published_specs_identically(spec: Path) -> None:
    reference = yaml.load(spec.read_text(encoding="utf-8"), Loader=yaml.SafeLoader)
    assert reference, f"{spec.name} parsed to nothing; the comparison would be vacuous"
    assert spec_loading.load_yaml(spec) == reference


@pytest.mark.skipif(not yaml.__with_libyaml__, reason="PyYAML built without libyaml")
def test_the_c_loader_is_the_one_in_use() -> None:
    """Where libyaml is present, both parsers resolve to the C safe loader.

    Without this, a fallback taken by mistake would leave every comparison
    above passing -- the pure-Python loader agrees with itself.
    """
    assert spec_loading.SafeLoader is yaml.CSafeLoader
    assert sage.app._SpecLoader is yaml.CSafeLoader
