"""Retained and delivered filenames carry no control or shell-active characters.

A retained source's basename is chosen by whoever ingested it, and reaches
other callers as a download recipe's filename and a ``Content-Disposition``
header. An external file is retained under a restricted form of its name, a
recipe names only that restricted form, and every disposition header drops
control characters.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import sage.mcp_server as _mcp
from sage.adapters.stubs import StubAbstractionProvider, StubContentStore, StubEmbeddingProvider
from sage.api._disposition import attachment_disposition
from sage.config import VaultConfig
from sage.mcp_server import ingest_document
from sage.vault_source_binding import safe_retained_name
from tests.sage.conftest import initialize_services_for_test

_VAULT_ID = "test_vault"
_UNSAFE = set("`$;&|<>*?!'\"\\") | {chr(c) for c in range(32)} | {"\x7f"}


@pytest.fixture
async def vault(minimal_vault_config_dict, vault_source_backend):
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp._vaults[_VAULT_ID] = services
        try:
            yield services, config, vault_source_backend
        finally:
            _mcp._vaults.pop(_VAULT_ID, None)


async def _ingest_external(tmp_path: Path, name: str) -> dict:
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    src = inbox / name
    src.write_text("# Note\n\nBody.\n")
    result = await ingest_document(_VAULT_ID, str(src), "markdown")
    return result if isinstance(result, dict) else json.loads(result)


@pytest.mark.parametrize(
    ("name", "retained"),
    [
        ("-rf `id` $HOME;x.md", "imports/rf _id_ _HOME_x.md"),
        ("a\nb.md", "imports/a_b.md"),
        ("``$$.md", "imports/source.md"),
    ],
)
async def test_external_source_is_retained_under_a_restricted_name(vault, tmp_path, name, retained):
    """TEST-SAGE-BH-175: an external file's retained name has control and
    shell-active characters replaced and leading dashes and dots removed,
    falling back to ``source`` where no letter or digit is left, on both
    bindings.
    """
    result = await _ingest_external(tmp_path, name)

    assert "error" not in result, result
    assert result["source_path"] == retained


async def test_ordinary_names_are_retained_unchanged(vault, tmp_path):
    """TEST-SAGE-BH-176 (control): an ordinary name, spaces and parentheses
    included, is retained as it was.
    """
    result = await _ingest_external(tmp_path, "Quarterly notes (draft) v2.md")

    assert result["source_path"] == "imports/Quarterly notes (draft) v2.md"


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=40))
def test_restricted_name_is_safe_and_stable(name):
    """TEST-SAGE-BH-177: for any input the restricted name is non-empty, has
    no unsafe character and no leading dash or dot, and restricting it again
    changes nothing.
    """
    safe = safe_retained_name(name)

    assert safe and not (_UNSAFE & set(safe))
    assert not safe.startswith(("-", "."))
    assert safe_retained_name(safe) == safe


@pytest.mark.parametrize("filename", ["a\r\nSet-Cookie: x.md", "tab\there.md", "nul\x00.md"])
def test_disposition_header_carries_no_control_characters(filename):
    """TEST-SAGE-BH-178: a disposition header built from a name holding
    control characters holds none of them, so the response can be sent.
    """
    header = attachment_disposition(filename)

    assert not any(ord(c) < 32 or ord(c) == 127 for c in header)
    assert header.startswith('attachment; filename="')


def test_disposition_keeps_an_ordinary_name():
    """TEST-SAGE-BH-179 (control): an ordinary name is carried as before."""
    assert attachment_disposition("sample.md") == 'attachment; filename="sample.md"'


@pytest.mark.parametrize("module", ["sage.api.routers.documents", "sage.api.routers.transfer"])
def test_every_content_route_builds_its_disposition_through_the_helper(module):
    """TEST-SAGE-BH-180: no content route assembles a disposition header by
    hand; each calls the shared helper that strips control characters.
    """
    import importlib

    source = inspect.getsource(importlib.import_module(module))
    tree = ast.parse(source)
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "attachment_disposition"
    ]
    assert calls
    assert "attachment; filename=" not in source


def test_download_recipe_names_the_restricted_form():
    """TEST-SAGE-BH-181: a source download recipe's filename is the restricted
    form of the retained name.
    """
    from sage.services.transfer import recipe_filename

    assert recipe_filename("imports/-x `y`.md") == "x _y_.md"
    assert recipe_filename("imports/plain.md") == "plain.md"


async def test_a_colliding_name_is_restricted_too(vault, tmp_path):
    """TEST-SAGE-BH-193: a second external file under the same unsafe name,
    with different bytes, is retained under the restricted stem with its
    disambiguating suffix, on both bindings.
    """
    first = await _ingest_external(tmp_path, "-rf `id`.md")
    other = tmp_path / "inbox2"
    other.mkdir()
    src = other / "-rf `id`.md"
    src.write_text("# Other\n\nDifferent bytes.\n")
    second = await ingest_document(_VAULT_ID, str(src), "markdown")
    second = second if isinstance(second, dict) else json.loads(second)

    assert first["source_path"] == "imports/rf _id_.md"
    assert second["source_path"].startswith("imports/rf _id__")
    assert not (_UNSAFE & set(second["source_path"]))
