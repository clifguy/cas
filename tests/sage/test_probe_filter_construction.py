"""Every ``RetrievalFilters`` call under ``scripts/`` names real fields.

Both tier3 latency probes shipped calling ``RetrievalFilters(tier3=...)``. The
field is ``tier3_metadata`` and the model is ``extra="forbid"``, so every run
raised at construction and neither probe could execute. Nothing noticed: a
probe is invoked by hand, no test imported one, and the defect was reachable
only by running the script.

The obvious test -- construct the filter the probe means to build and assert it
validates -- does not close this. It builds its own arguments, so it passes
whatever the script says, and reverting the repair leaves it green. What has to
be read is the **call site in the source**, which is what this gate walks:
every ``RetrievalFilters(...)`` under ``scripts/``, every keyword checked
against the model's actual fields.

Scoped to ``scripts/`` deliberately. Application code that constructs this
model is exercised by the tests around it; a script is the surface where a
wrong keyword ships un-run.

**What this gate does not see, stated so it is not mistaken for coverage.** It
reads keyword arguments written literally at the call. A ``RetrievalFilters(
**kwargs)`` or a filter assembled as a dict and splatted passes it unexamined,
because deciding those needs the value of an arbitrary expression rather than
the shape of the call. Closing that is constant-folding, a different and much
larger instrument than this one; no such call exists under ``scripts/`` today,
and this gate is the reason a new one would have to be written deliberately
rather than arrived at.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sage.models.schemas import RetrievalFilters

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts"

MODEL_NAME = "RetrievalFilters"
VALID_FIELDS = frozenset(RetrievalFilters.model_fields)


def _call_sites() -> list[tuple[Path, int, tuple[str, ...]]]:
    """Every ``RetrievalFilters(...)`` call in the script tree, with its keywords."""
    sites: list[tuple[Path, int, tuple[str, ...]]] = []
    for path in sorted(_SCRIPTS.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != MODEL_NAME:
                continue
            keywords = tuple(kw.arg for kw in node.keywords if kw.arg is not None)
            sites.append((path.relative_to(_REPO_ROOT), node.lineno, keywords))
    return sites


def test_the_scan_finds_the_call_sites_it_is_meant_to_guard():
    """A gate over an empty set is green and blind.

    If the walk stops finding calls -- the model renamed, the probes deleted,
    the parse silently failing -- the field check below passes vacuously. This
    is the control that says the gate is still looking at something.
    """
    sites = _call_sites()
    assert sites, "no RetrievalFilters call sites found under scripts/"
    files = {str(path) for path, _, _ in sites}
    assert "scripts/probe_catalog_index_latency.py" in files
    assert "scripts/probe_semantic_tier3_latency.py" in files


@pytest.mark.parametrize(
    ("path", "lineno", "keywords"),
    _call_sites(),
    ids=lambda value: str(value) if not isinstance(value, tuple) else "-".join(value),
)
def test_a_scripts_retrieval_filter_call_names_only_real_fields(path, lineno, keywords):
    """A keyword the model forbids makes the script raise before it does any work."""
    unknown = sorted(set(keywords) - VALID_FIELDS)
    assert not unknown, (
        f"{path}:{lineno} passes {unknown} to {MODEL_NAME}, which declares "
        f'extra="forbid"; the call raises at construction and the script cannot run'
    )


def test_the_field_name_the_probes_used_is_still_refused():
    """The boundary is genuinely closed, so the gate above is testing a real repair.

    Were the model to grow an alias for the old spelling, the original scripts
    would never have been broken and this whole module would attest nothing.
    """
    with pytest.raises(ValueError):
        RetrievalFilters(doc_type="ticket", tier3={"ticket_priority": "high"})
