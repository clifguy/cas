"""Runtime guardrails for the deployment-profile import boundary (CAS-ADR-042).

CAS-ADR-042 makes a SAGE deployment target a stack-scope profile and keeps the
layer above the ports profile-invariant: each cloud binding owns its
environment-specific imports lazily, so an on-box local-profile process never
loads them. The import-linter contracts in ``pyproject.toml`` fence the
statically pristine surfaces (the profile core and the BFF server entry) and
confine the Azure SDKs to their two owner modules. This module is the runtime
counterpart the contracts cannot express: it boots the local profile in a clean
interpreter and asserts that no cloud dependency was imported, with a
cloud-profile positive control so the negative cannot pass coincidentally.

Why a subprocess rather than an in-process ``sys.modules`` diff: the Azure and
psycopg modules are imported by sibling tests in the same suite -- the cloud
positive control here, the Postgres storage tests elsewhere -- so an in-process
absolute-absence check is order-polluted. A fresh interpreter is the faithful
model of "a fresh local boot," the exact surface the guardrail protects, and the
failure mode (a cloud import surfacing only on the next on-box restart) the
import-linter pass on a Linux/local-profile CI run cannot catch.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sage.profiles import (
    ABSTRACTION_SEAM,
    AUTH_SEAM,
    CLOUD_PROFILE,
    LOCAL_PROFILE,
    STORAGE_SEAM,
    VAULT_SOURCE_SEAM,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# The genuinely cloud-only surfaces a local-profile boot must never import. psycopg
# is deliberately excluded from the forbidden set: it is a shared dependency of both
# the local (password auth) and cloud (managed-identity token auth) Postgres paths,
# so the local profile may import it without crossing the boundary (CAS-ADR-042).
CLOUD_OWNER_MODULES = (
    "sage.secrets.key_vault",
    "sage.storage.postgres.managed_identity",
    "sage.vault_source_document_store",
)
CORE_SEAMS = (ABSTRACTION_SEAM, STORAGE_SEAM, VAULT_SOURCE_SEAM, AUTH_SEAM)

# Boot snippets executed in a clean interpreter. Each is paired with _SCAN_TAIL,
# which prints -- as the last stdout line -- a JSON list of the cloud-relevant
# modules (Azure SDKs, the two cloud-owner modules, and psycopg) that the boot left
# in ``sys.modules``. The tests below interpret that list.
_LOCAL_BOOT = """\
from sage.app import create_app
import app.backend.asgi  # noqa: F401  -- the BFF server entry point
from sage.config import SageCoreConfig
from sage.mcp_init import (
    resolve_stack_abstraction_provider,
    resolve_stack_auth_validator,
    resolve_stack_storage_provisioner,
    resolve_stack_vault_source_store,
)

cfg = SageCoreConfig(profile="local", storage_backend="{backend}")
create_app(stack_config=cfg)             # build the application factory
resolve_stack_abstraction_provider(cfg)  # resolve all four local-profile seams
resolve_stack_storage_provisioner(cfg)
resolve_stack_vault_source_store(cfg)
resolve_stack_auth_validator(cfg)
"""

_CLOUD_BOOT = """\
from sage.config import SageCoreConfig
from sage.mcp_init import (
    resolve_stack_abstraction_provider,
    resolve_stack_storage_provisioner,
    resolve_stack_vault_source_store,
)

# Resolving the cloud bindings imports their environment-specific modules, then
# fails offline -- the storage path on the managed-identity credential (no
# reachable managed identity), the abstraction path on the absent Key Vault URI.
# Those offline failures are expected and tolerated; the imports we are proving
# have already happened by the time each raises, and the scan below reports what
# was loaded. An ImportError is deliberately NOT tolerated: a cloud binding whose
# transitive closure is incomplete (e.g. the async-transport peer the
# managed-identity credential builds at construction) is a packaging defect, not
# an offline condition, so it propagates and fails the probe here -- at the
# boundary a cloud boot hits it -- rather than only on the next on-box cloud
# restart (CAS-ADR-042).
#
# storage_backend="postgres" is load-bearing: the embedded override short-circuits
# the storage builder before the managed-identity branch, which would make this
# positive control vacuous. vault_source_backend="document_store" plays the same
# role for the vault-source seam: the filesystem default would resolve a binding
# that imports no cloud SDK, so the document-store backend must be selected for
# this control to exercise the SharePoint/Graph adapter (CAS-ADR-043).
try:
    resolve_stack_storage_provisioner(
        SageCoreConfig(profile="cloud", storage_backend="postgres")
    )
except ImportError:
    raise
except Exception:
    pass
try:
    resolve_stack_abstraction_provider(
        SageCoreConfig(
            profile="cloud",
            abstraction={"provider": "anthropic", "model": "claude-haiku-4-5"},
        )
    )
except ImportError:
    raise
except Exception:
    pass
try:
    resolve_stack_vault_source_store(
        SageCoreConfig(profile="cloud", vault_source_backend="document_store")
    )
except ImportError:
    raise
except Exception:
    pass
"""

_SCAN_TAIL = """
import json as _json, sys as _sys
_matched = sorted(
    _m for _m in _sys.modules
    if _m == "azure" or _m.startswith("azure.")
    or _m in (
        "sage.secrets.key_vault",
        "sage.storage.postgres.managed_identity",
        "sage.vault_source_document_store",
    )
    or _m == "psycopg" or _m.startswith("psycopg.")
)
print(_json.dumps(_matched))
"""


def _is_azure(module: str) -> bool:
    return module == "azure" or module.startswith("azure.")


def _run_boot_probe(
    boot_code: str, *, storage_backend: str, stub_providers: str = "1"
) -> list[str]:
    """Run a boot snippet in a clean interpreter; return the cloud modules it left.

    PYTHONPATH is pinned to this checkout's root (prepended, so a worktree shadows
    the editable primary checkout) and a fresh interpreter is spawned via the active
    Python. The snippet's last stdout line is a JSON list of cloud-relevant modules.
    ``stub_providers`` defaults to the suite stub gate; the positive control disables
    it so the cloud abstraction binding reaches its Key Vault import instead of the
    stub short-circuit.
    """
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    pythonpath = str(REPO_ROOT) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    env = {
        **os.environ,
        "PYTHONPATH": pythonpath,
        "SAGE_TEST_STUB_PROVIDERS": stub_providers,
        "SAGE_TEST_STORAGE_BACKEND": storage_backend,
    }
    result = subprocess.run(
        [sys.executable, "-c", boot_code + _SCAN_TAIL],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        "deployment-profile boot probe subprocess failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("storage_backend", ["embedded", "postgres"])
def test_local_profile_boot_imports_no_cloud_dependencies(storage_backend: str) -> None:
    """A fresh local-profile boot imports no Azure SDK and neither cloud-owner module.

    Covers both durable-storage backends. The ``postgres`` variant is the
    load-bearing one for the boundary's shape: it drives the local profile down the
    Postgres adapters (which may import the shared psycopg driver) and proves that
    even there the boot pulls no Azure module and neither managed-identity nor Key
    Vault owner module. psycopg is intentionally not asserted on -- it is shared.
    """
    matched = _run_boot_probe(
        _LOCAL_BOOT.format(backend=storage_backend), storage_backend=storage_backend
    )
    leaked = [m for m in matched if _is_azure(m) or m in CLOUD_OWNER_MODULES]
    assert leaked == [], (
        f"the local profile ({storage_backend} backend) imported cloud-only "
        f"dependencies {leaked}; a cloud import reached the local boot path and "
        "would load on the next on-box restart (CAS-ADR-042)."
    )


def test_cloud_profile_bindings_import_the_cloud_dependency_stack() -> None:
    """Positive control: resolving the cloud bindings does pull every guarded surface.

    This is the anti-coincidence guard for the local-profile negative test above,
    proving the cloud profile genuinely requires the same modules that test asserts
    the local profile omits: an Azure SDK and both cloud-owner modules (the
    managed-identity Postgres path via the storage binding, the Key Vault reader via
    the abstraction binding). If a rename, a removed import, or an uninstalled SDK
    meant a cloud path no longer imported one of them, the negative test would pass
    vacuously -- but this control would fail, exposing the coincidence. It also
    proves the embedded-override guard: were the override to leak through, the cloud
    storage binding would return the embedded provisioner and import no Azure module,
    failing the Azure assertion here. Because the cloud boot snippet lets an
    ImportError propagate (rather than swallowing every exception), this control
    also fails if a cloud binding's transitive closure is incomplete: a missing
    async-transport peer for the managed-identity credential surfaces here, at the
    boundary a cloud boot hits it, instead of only on the next on-box cloud
    restart. psycopg is not asserted: it is a shared driver for both the local and
    cloud Postgres paths, so its presence is not a cloud-only marker.
    """
    matched = _run_boot_probe(_CLOUD_BOOT, storage_backend="postgres", stub_providers="0")
    assert any(_is_azure(m) for m in matched), (
        f"the cloud bindings imported no Azure module (got {matched}); the positive "
        "control is vacuous, so the local-profile negative test cannot be trusted."
    )
    for owner in CLOUD_OWNER_MODULES:
        assert owner in matched, (
            f"the cloud bindings did not import {owner} (got {matched}); the "
            "local-profile negative test's check for it cannot be trusted."
        )


def test_cloud_async_transport_resolves_in_a_clean_interpreter() -> None:
    """The async managed-identity credential constructs without an ImportError.

    The cloud Postgres path authenticates with ``azure.identity.aio.
    DefaultAzureCredential``, which builds an aiohttp-backed ``azure-core``
    transport at construction. aiohttp is a peer the async ``azure-core`` extra
    provides but that nothing else declares; an image whose locked closure omits
    it raises ``ImportError`` the moment the credential is built, and the BFF --
    which opens its Postgres session store at startup -- crash-loops before
    serving traffic (CAS-ADR-042).

    This asserts the closure the lockfile must guarantee is actually installed,
    constructing the credential in a fresh interpreter so a sibling test's import
    cannot mask an absence. It is the deterministic counterpart to the positive
    control above, which proves the same closure at the binding-resolution
    boundary; here we exercise the credential directly so the guard does not
    depend on the cloud binding's offline failure timing.
    """
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    pythonpath = str(REPO_ROOT) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from azure.identity.aio import DefaultAzureCredential\nDefaultAzureCredential()\n",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": pythonpath},
    )
    assert result.returncode == 0, (
        "constructing the async managed-identity credential raised in a clean "
        "interpreter; the cloud async-transport closure is incomplete -- the "
        "credential's azure-core transport peer is missing from the locked "
        f"dependency set (CAS-ADR-042):\n--- stderr ---\n{result.stderr}"
    )


def test_every_core_seam_registers_both_local_and_cloud_bindings() -> None:
    """Every core seam registers a binding for both the local and the cloud profile.

    Guards completeness of the seam roster: a future seam or profile added on only
    one side would resolve to a half-populated profile at startup. Membership is
    checked through the public resolver, which consults the factory registry without
    building any binding.
    """
    import sage.mcp_init  # noqa: F401  -- registers the core seams for both profiles
    from sage import profiles
    from sage.config import SageCoreConfig

    stack_config = SageCoreConfig()
    missing = [
        (profile, seam)
        for profile in (LOCAL_PROFILE, CLOUD_PROFILE)
        for seam in CORE_SEAMS
        if seam not in profiles.resolve_profile(profile, stack_config).bindings
    ]
    assert not missing, f"deployment profiles are missing core-seam bindings: {missing}"
