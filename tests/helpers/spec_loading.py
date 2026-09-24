"""Parse the repository's YAML specifications with PyYAML's C loader.

The published OpenAPI documents are large enough that the pure-Python loader
spends about half a second on each parse, and several test modules parse them
while they are being collected. The C loader constructs the same objects in a
fraction of that time; ``tests/sage/test_spec_loading.py`` holds the two to the
same result. PyYAML ships the C loader only where libyaml was available when it
was built, so the pure-Python loader remains the fallback.

Each call parses afresh and returns a new object. Callers that cache do so in
their own module, so that one module mutating its copy cannot reach another's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

try:
    from yaml import CSafeLoader as SafeLoader
except ImportError:  # pragma: no cover - PyYAML built without libyaml
    from yaml import SafeLoader


def load_yaml(path: Path) -> Any:
    """The YAML document at ``path``, parsed with the safe loader."""
    return yaml.load(path.read_text(encoding="utf-8"), Loader=SafeLoader)
