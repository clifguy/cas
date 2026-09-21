"""Derive the packaged error contract from the authoritative Core OpenAPI schema."""

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "sage/models/_error_contract.py"


def build_contract() -> dict:
    schemas = yaml.safe_load((ROOT / "docs/fs/sage/sage_core_api.openapi.yaml").read_text())[
        "components"
    ]["schemas"]
    selected = {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if ref:
                name = ref.rsplit("/", 1)[-1]
                if name not in selected:
                    selected[name] = schemas[name]
                    visit(schemas[name])
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    selected["ErrorResponse"] = schemas["ErrorResponse"]
    visit(selected["ErrorResponse"])
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = (
        '"""Generated from the Core OpenAPI error schemas; do not edit."""\n'
        "# ruff: noqa: E501 -- generated schema strings retain authoritative prose\n\n"
        'import json\n\nSCHEMAS = json.loads(\n    r"""'
        + json.dumps(build_contract(), indent=2, sort_keys=True)
        + '\n"""\n)\n'
    )
    if args.check:
        return int(not TARGET.exists() or TARGET.read_text() != rendered)
    TARGET.write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
