"""Check that the cloud profile's abstraction model is the newest Haiku.

The hosted abstraction model is pinned once, as the ``abstractionModel``
parameter default in ``infra/main.bicep``. An undated model id follows
updates only within its own generation, so a newer Haiku never reaches the
deployment by itself. This check reads the pin from the Bicep source and
compares it against the Anthropic Models API, so a newer generation surfaces
as a failing scheduled run and the move to it stays a deliberate one-line
change.

Two conditions fail the check:

- **A newer Haiku exists.** The newest ``claude-haiku-*`` model by creation
  date is not the model the pin resolves to. The comparison uses the resolved
  id, so an alias pin and its dated id compare equal.
- **The pin no longer resolves.** The Models API reports no deprecation or
  retirement dates, so a retired pin is detected as one the API no longer
  knows. A listing with no Haiku at all is reported too, since it can confirm
  nothing.

It never passes for want of an answer. A missing ``ANTHROPIC_API_KEY`` or an
API error other than the pin's own not-found raises, which fails the run.

Usage::

    .venv/bin/python -m scripts.check_abstraction_model [--bicep PATH]

Exit status: 0 when the pin is the newest resolvable Haiku, 1 with findings.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MAIN_BICEP = Path(__file__).resolve().parent.parent / "infra" / "main.bicep"

HAIKU_PREFIX = "claude-haiku-"

_PIN_DECLARATION = re.compile(r"^\s*param\s+abstractionModel\b(.*)$", re.MULTILINE)
_PIN_DEFAULT = re.compile(r"^\s*string\s*=\s*'([^']+)'\s*$")
_LINE_COMMENT = re.compile(r"//.*$", re.MULTILINE)


@dataclass(frozen=True)
class ModelEntry:
    """One model as the Models API reports it."""

    id: str
    created_at: datetime


def read_pin(bicep_text: str) -> str:
    """The ``abstractionModel`` default declared in Bicep source.

    Raises:
        ValueError: If the parameter is not declared exactly once with a
            string default. An unreadable pin is an error, never a guess.
    """
    declarations = _PIN_DECLARATION.findall(_LINE_COMMENT.sub("", bicep_text))
    if len(declarations) != 1:
        raise ValueError(f"expected one abstractionModel declaration, found {len(declarations)}")
    match = _PIN_DEFAULT.match(declarations[0])
    if match is None:
        raise ValueError("abstractionModel declares no string default")
    return match.group(1)


def assess(pin: str, resolved: ModelEntry | None, listed: list[ModelEntry]) -> list[str]:
    """Findings for a pin against the listed models; empty when current.

    Args:
        pin: The pinned model id as written in Bicep.
        resolved: The model the pin resolves to, or None when the API does
            not resolve it.
        listed: Every model the API lists.
    """
    if resolved is None:
        return [f"pinned abstraction model {pin!r} no longer resolves; it is retired or mistyped"]
    haikus = [model for model in listed if model.id.startswith(HAIKU_PREFIX)]
    if not haikus:
        return ["the Models API listed no Haiku models, so the pin cannot be confirmed current"]
    # Every model created at the latest instant is newest, so a tie never
    # turns on the order the API happens to list them in.
    latest = max(model.created_at for model in haikus)
    newest = sorted(model.id for model in haikus if model.created_at == latest)
    if resolved.id not in newest:
        return [
            f"pinned abstraction model {pin!r} (resolves to {resolved.id!r}) is not the "
            f"newest Haiku; {', '.join(repr(model_id) for model_id in newest)} is newer"
        ]
    return []


# --- I/O edge ---


def _client() -> object:
    """The Anthropic client, constructed only when the check runs."""
    from anthropic import Anthropic

    return Anthropic()


def fetch(pin: str, client: object | None = None) -> tuple[ModelEntry | None, list[ModelEntry]]:
    """Resolve the pin and list every model.

    Only the pin's own not-found is converted, to None; every other error
    propagates so the run fails rather than reporting a result it did not
    obtain.
    """
    from anthropic import NotFoundError

    client = client if client is not None else _client()
    try:
        info = client.models.retrieve(pin)
        resolved: ModelEntry | None = ModelEntry(info.id, info.created_at)
    except NotFoundError:
        resolved = None
    listed = [ModelEntry(model.id, model.created_at) for model in client.models.list()]
    return resolved, listed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--bicep", type=Path, default=MAIN_BICEP, help="Bicep file declaring the pin"
    )
    args = parser.parse_args(argv)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set; the check cannot query the Models API")

    pin = read_pin(args.bicep.read_text(encoding="utf-8"))
    resolved, listed = fetch(pin, _client())
    findings = assess(pin, resolved, listed)
    for finding in findings:
        print(finding, file=sys.stderr)
    if not findings:
        print(f"abstraction model pin {pin!r} is the newest Haiku")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
