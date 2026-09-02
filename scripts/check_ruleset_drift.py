#!/usr/bin/env python3
"""Compare the captured branch-protection ruleset against the live one.

``docs/process/branch_protection.md`` reproduces the ``main-protection``
ruleset as a JSON block and declares that block the source of truth, with the
forge's UI as its reflection. Nothing enforced that claim: a change made in the
UI carries no commit, so the offline gates that read the tracked document
cannot see it, and the document goes on asserting something untrue.

This check reads the live ruleset and reports every diverging *leaf*. A flag
flipped inside a rule's ``parameters`` names that flag rather than the ``rules``
array containing it -- reporting at the granularity of the top-level keys would
say only that something, somewhere, had changed.

The comparison covers state, not claims. The document's prose can misdescribe a
ruleset the JSON block captures accurately, and no captured-to-live comparison
would catch it; that remains a review concern.

The read needs a token with repository-administration read access -- the
built-in Actions token cannot read the rulesets endpoint -- supplied via
``GH_RULESET_TOKEN``. With no token configured the check logs and exits 0, so
the surrounding automation stays dormant rather than failing until the token is
provisioned.

Usage::

    python -m scripts.check_ruleset_drift                       # compare against live
    python -m scripts.check_ruleset_drift --repo owner/name
    python -m scripts.check_ruleset_drift --ruleset-file r.json # compare offline

Exit status is 0 when the two agree (or when the check is dormant) and 1 when
they diverge.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Final, NamedTuple

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_DOCUMENT_PATH: Final[Path] = REPO_ROOT / "docs" / "process" / "branch_protection.md"

RULESET_NAME: Final[str] = "main-protection"

# Keys the forge adds to every ruleset response and the captured block
# deliberately omits: identifiers that vary per repository, and timestamps and
# links that change without the ruleset changing. Reading their absence from
# the capture as divergence would make the check fail on every run.
VOLATILE_TOP_LEVEL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "node_id",
        "source",
        "source_type",
        "created_at",
        "updated_at",
        "current_user_can_bypass",
        "_links",
    }
)

# The bypass actor is identified by its role; the numeric id beside it is
# repository-internal and is omitted from the capture for the same reason.
VOLATILE_BYPASS_ACTOR_KEYS: Final[frozenset[str]] = frozenset({"actor_id"})

# Lists whose members are identified by a field rather than by position, keyed
# by the name the list appears under. Diffing these positionally would report a
# reordering as drift and would name a changed member by an index nobody can
# read; keying them by identity names the rule, the check, or the role.
_LIST_IDENTITY_KEYS: Final[dict[str, str]] = {
    "rules": "type",
    "required_status_checks": "context",
    "bypass_actors": "actor_type",
}

_JSON_BLOCK_RE: Final[re.Pattern[str]] = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


class _Missing:
    """Sentinel for a field one side does not carry at all.

    Distinct from any value the field could hold, so an absent flag is never
    reported as agreeing with a present one that happens to be false.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<absent>"


MISSING: Final[_Missing] = _Missing()


class Divergence(NamedTuple):
    """One leaf on which the captured block and the live ruleset disagree."""

    path: str
    captured: Any
    live: Any


# --- pure logic -------------------------------------------------------------


def extract_captured_ruleset(text: str, name: str = RULESET_NAME) -> dict[str, Any]:
    """The captured ruleset definition reproduced in a Markdown document.

    Reads every fenced JSON block and returns the first that names the wanted
    ruleset, so an unrelated JSON block elsewhere in the document is skipped
    rather than mistaken for the capture. An absent or unparseable block yields
    an empty mapping, which callers treat as a failure rather than as
    agreement.
    """
    for match in _JSON_BLOCK_RE.finditer(text):
        try:
            candidate = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("name") == name:
            return candidate
    return {}


def normalize_ruleset(ruleset: dict[str, Any]) -> dict[str, Any]:
    """A live ruleset reduced to the shape the captured block records.

    Strips exactly the forge-internal and volatile keys the capture omits, so
    what remains is comparable field for field. Applying this to an already
    captured block is a no-op, which is what makes the comparison symmetric.
    """
    normalized = {
        key: value for key, value in ruleset.items() if key not in VOLATILE_TOP_LEVEL_KEYS
    }
    actors = normalized.get("bypass_actors")
    if isinstance(actors, list):
        normalized["bypass_actors"] = [
            {key: value for key, value in actor.items() if key not in VOLATILE_BYPASS_ACTOR_KEYS}
            if isinstance(actor, dict)
            else actor
            for actor in actors
        ]
    return normalized


def diff_rulesets(captured: dict[str, Any], live: dict[str, Any]) -> list[Divergence]:
    """Every leaf on which the two rulesets disagree, ordered by path.

    Both sides are expected to be normalized. Divergences are reported at the
    leaf: a flipped flag names the flag, an added rule names the rule's type,
    and a renamed status check names both contexts.
    """
    found: list[Divergence] = []
    _diff_value("", captured, live, found)
    return sorted(found, key=lambda divergence: divergence.path)


def format_report(divergences: list[Divergence]) -> str:
    """A human-readable rendering of the diverging fields."""
    if not divergences:
        return f"{RULESET_NAME}: the captured block agrees with the live ruleset."
    field_word = "field" if len(divergences) == 1 else "fields"
    lines = [
        f"{RULESET_NAME}: the live ruleset diverges from the captured block "
        f"in {len(divergences)} {field_word}.",
        "",
    ]
    for divergence in divergences:
        lines += [
            f"  {divergence.path}",
            f"      captured: {_render(divergence.captured)}",
            f"      live:     {_render(divergence.live)}",
        ]
    lines += [
        "",
        f"Reconcile per the procedure in {DEFAULT_DOCUMENT_PATH.relative_to(REPO_ROOT)}: apply "
        "the intended change on whichever side is behind, re-capture, and commit.",
    ]
    return "\n".join(lines)


def _render(value: Any) -> str:
    if value is MISSING:
        return "(absent)"
    return json.dumps(value, sort_keys=True)


def _diff_value(path: str, captured: Any, live: Any, found: list[Divergence]) -> None:
    """Walk both sides in step, appending a Divergence at each disagreeing leaf."""
    if captured is MISSING or live is MISSING:
        found.append(Divergence(path, captured, live))
        return

    if isinstance(captured, dict) and isinstance(live, dict):
        for key in sorted(set(captured) | set(live)):
            child = f"{path}.{key}" if path else key
            _diff_value(child, captured.get(key, MISSING), live.get(key, MISSING), found)
        return

    if isinstance(captured, list) and isinstance(live, list):
        identity_key = _LIST_IDENTITY_KEYS.get(path.rsplit(".", 1)[-1])
        if identity_key and _keyed_by(captured, identity_key) and _keyed_by(live, identity_key):
            captured_by_id = {item[identity_key]: item for item in captured}
            live_by_id = {item[identity_key]: item for item in live}
            for identity in sorted(set(captured_by_id) | set(live_by_id), key=str):
                _diff_value(
                    f"{path}[{identity}]",
                    captured_by_id.get(identity, MISSING),
                    live_by_id.get(identity, MISSING),
                    found,
                )
            return
        # A list of scalars is a leaf, and the forge promises no order for one.
        if sorted(captured, key=repr) != sorted(live, key=repr):
            found.append(Divergence(path, captured, live))
        return

    if captured != live or type(captured) is not type(live):
        found.append(Divergence(path, captured, live))


def _keyed_by(items: list[Any], identity_key: str) -> bool:
    """True when every member carries the identity field, so keying is total."""
    return all(isinstance(item, dict) and identity_key in item for item in items)


# --- I/O edge ---------------------------------------------------------------


def _run(cmd: list[str], *, token: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}")
    return proc


def _rulesets_endpoint(repo: str | None, suffix: str = "") -> str:
    """The rulesets endpoint for ``repo``, or for the current repository.

    With no repository named, the placeholder form lets the forge CLI resolve
    it from the working directory.
    """
    base = f"repos/{repo}/rulesets" if repo else "repos/{owner}/{repo}/rulesets"
    return base + suffix


def fetch_live_ruleset(
    repo: str | None, name: str = RULESET_NAME, token: str | None = None
) -> dict[str, Any]:
    """The live ruleset named ``name``, read in two calls.

    The listing endpoint carries the ruleset's identifier but not its rules, so
    the identifier is resolved from the ruleset's name at run time and the
    definition fetched with it. Resolving by name is also what keeps that
    identifier -- which is repository-internal -- out of the tree.
    """
    listing = json.loads(_run(["gh", "api", _rulesets_endpoint(repo)], token=token).stdout)
    for entry in listing:
        if isinstance(entry, dict) and entry.get("name") == name:
            ruleset_id = entry["id"]
            break
    else:
        available = ", ".join(sorted(str(entry.get("name")) for entry in listing)) or "none"
        raise RuntimeError(f"no ruleset named {name!r} on the repository (found: {available})")
    endpoint = _rulesets_endpoint(repo, f"/{ruleset_id}")
    return json.loads(_run(["gh", "api", endpoint], token=token).stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare the captured branch-protection ruleset against the live one."
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GH_REPO", ""),
        help="owner/name of the repository (defaults to $GH_REPO, then the working directory).",
    )
    parser.add_argument(
        "--document",
        default=str(DEFAULT_DOCUMENT_PATH),
        help="path to the document carrying the captured ruleset block.",
    )
    parser.add_argument(
        "--ruleset-file",
        help="read the live ruleset from a file instead of the API (offline use).",
    )
    args = parser.parse_args(argv)

    captured = extract_captured_ruleset(Path(args.document).read_text(encoding="utf-8"))
    if not captured:
        print(f"no {RULESET_NAME!r} JSON block found in {args.document}", file=sys.stderr)
        return 1

    if args.ruleset_file:
        live = json.loads(Path(args.ruleset_file).read_text(encoding="utf-8"))
    else:
        token = os.environ.get("GH_RULESET_TOKEN", "").strip()
        if not token:
            print(
                "no ruleset read token configured (GH_RULESET_TOKEN); skipping",
                file=sys.stderr,
            )
            return 0
        live = fetch_live_ruleset(args.repo or None, RULESET_NAME, token)

    divergences = diff_rulesets(captured, normalize_ruleset(live))
    print(format_report(divergences))
    return 1 if divergences else 0


if __name__ == "__main__":
    raise SystemExit(main())
