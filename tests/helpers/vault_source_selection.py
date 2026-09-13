"""Select the stack's vault-source binding for a test and wire its transport.

The one selector every suite uses to pin the vault-source binding (CAS-ADR-043),
whether it drives the store directly or serves a real app over it. Both legs run
the real dispatch in ``build_stack_vault_source_store`` through the environment
override; the document-store leg fakes only the Graph transport, so every line
of ``DocumentStoreVaultSourceStore`` still runs.

Kept in one place because a private copy fails silently: a construction path the
selector learns about is picked up here, and a caller carrying its own copy would
keep passing while its document-store leg quietly stopped going through the fake.

Importing this module needs only the standard library -- ``sage`` is imported
when a binding is selected -- so a suite whose other layers run without the app
installed can import it unconditionally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import pytest

    from tests.helpers.fake_graph_client import FakeGraphClient

# The backends this selector knows how to wire. Held to the stack config's
# ``vault_source_backend`` selector by the helper's own tests, so a backend added
# there fails until this selector is revisited. That check forces the revisit; it
# does not prove the new backend is wired correctly.
WIRED_BACKENDS: Final[tuple[str, ...]] = ("filesystem", "document_store")


def select_vault_source_binding(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> FakeGraphClient | None:
    """Pin ``backend`` as the stack's vault-source binding for the current test.

    ``filesystem`` sets the environment override and returns ``None``.
    ``document_store`` also replaces the Graph client factory with one returning
    a single shared ``FakeGraphClient``, and returns that fake. The factory is
    resolved at call time, so one instance carries state across the fresh store
    constructions the stack resolver performs per service call.

    Raises ``ValueError`` for any other value, before anything is set or patched:
    a selector that guessed would bind a leg other than the one its caller named.
    """
    if backend not in WIRED_BACKENDS:
        raise ValueError(
            f"Unknown vault-source backend {backend!r}; expected one of {WIRED_BACKENDS}."
        )

    from sage.vault_source_binding import VAULT_SOURCE_BACKEND_ENV_VAR

    monkeypatch.setenv(VAULT_SOURCE_BACKEND_ENV_VAR, backend)
    if backend == "filesystem":
        return None

    from tests.helpers.fake_graph_client import FakeGraphClient

    fake = FakeGraphClient()
    monkeypatch.setattr(
        "sage.vault_source_document_store.build_sharepoint_graph_client",
        lambda *args, **kwargs: fake,
    )
    return fake
