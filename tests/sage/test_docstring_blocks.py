"""The ``Error modes:`` block reader the docstring gates share.

A tool's declared refusals are read from this block, so a block opened where no
header stands, or bounded past the header that ends it, reports codes a caller
never reads as declared.
"""

from __future__ import annotations

import pytest

from tests.helpers.docstring_blocks import error_modes_block


def _doc(text: str):
    def fn():  # noqa: D401 - fixture, not a documented callable
        pass

    fn.__doc__ = text
    return fn


def test_a_sentence_naming_error_modes_opens_no_block() -> None:
    """A narrative line is not a header, so ``Args:`` prose below it is not a block.

    The rival reads the phrase through to the next colon -- the ``Args:`` header on
    a later line -- and returns the argument prose as declared error modes.
    """
    fn = _doc(
        "Do the thing.\n\n"
        "Error modes are reported in the envelope, never raised.\n\n"
        "Args:\n"
        "    vault_id: Target vault. A bad one is ``vault_not_found`` (404).\n"
    )
    assert error_modes_block(fn) == ""


@pytest.mark.parametrize(
    "header",
    [
        "Error modes:",
        "Error modes (raised synchronously in this call's response;\nnot for background work):",
        "Per-item error modes (inside the response envelope):",
        "Batch-level error modes (the tool's error envelope):",
    ],
)
def test_a_header_opens_a_block_bounded_at_args(header: str) -> None:
    """A bare or parenthesised header opens the block, which ends at ``Args:``."""
    fn = _doc(
        "Do the thing.\n\n"
        f"{header}\n"
        "- ``not_found`` (404): no such record.\n\n"
        "Args:\n"
        "    record_id: ``invalid_record_id`` names this argument.\n"
    )
    block = error_modes_block(fn)
    assert "``not_found``" in block
    assert "``invalid_record_id``" not in block
