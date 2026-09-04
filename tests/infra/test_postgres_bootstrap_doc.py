"""Structural and identity-hygiene gate for the Postgres bootstrap operator doc.

Locks the shape of ``docs/process/postgres-entra-bootstrap.md`` — the thin operator
doc for the cloud Postgres database bootstrap (CAS-ADR-042, CAS-ADR-043). Unlike a
hand-run runbook, the executable substance here is code: the Bicep-declared
Container Apps Job and the committed ``sage.storage.postgres.cloud_bootstrap``
entrypoint. The doc records only what an operator must do around that job — the
Entra-admin prerequisite, how the job is triggered, and the success signal to
verify against (CAS Cloud Deployment Discipline, Principle 3).

These checks read the tracked doc only — no Azure tooling, no live server — so they
run in the ordinary Python test job alongside the other infra gates. Identity
coordinates may never be hardcoded: they are resolved at run time into shell
variables.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
DOC: Final[Path] = REPO_ROOT / "docs" / "process" / "postgres-entra-bootstrap.md"

_GUID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"
)


def _git_owner() -> str | None:
    """Derive the repository owner from the origin remote, or ``None``."""
    try:
        url = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    match = re.search(r"[:/]([^/]+)/[^/]+?(?:\.git)?$", url)
    return match.group(1) if match else None


def _doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_doc_exists() -> None:
    """The thin operator doc for the database bootstrap exists in the repo."""
    assert DOC.is_file(), "docs/process/postgres-entra-bootstrap.md missing"


def test_doc_documents_codified_job() -> None:
    """The executable mechanism is on the record as the codified Container Apps Job
    (provisioning-as-code), and how the operator triggers it — not a hand-run SQL
    procedure.
    """
    text = _doc_text()
    lowered = text.lower()
    assert "containerapp job" in lowered, (
        "doc must name the `az containerapp job` trigger for the codified bootstrap job"
    )
    assert "sage.storage.postgres.cloud_bootstrap" in text or "job-pg-bootstrap" in text, (
        "doc must reference the codified bootstrap entrypoint or job"
    )


def test_doc_documents_entra_admin_prereq() -> None:
    """The Entra-admin prerequisite is on the record: the bootstrap identity is the
    server administrator the job's token authenticates as.
    """
    text = _doc_text()
    lowered = text.lower()
    assert "administrator" in lowered or "ad-admin" in lowered, (
        "doc must document the Entra administrator prerequisite"
    )
    assert "id-pg-bootstrap" in text or "bootstrap identity" in lowered, (
        "doc must identify the bootstrap identity as the administrator"
    )


def test_doc_documents_success_signal() -> None:
    """The success signal is on the record: the deployed SAGE logs the seeded vault
    loading with no authentication error.
    """
    text = _doc_text()
    lowered = text.lower()
    assert "vaults loaded" in lowered, "doc must name the `vaults loaded` success signal"
    assert "cloud_validation" in lowered, (
        "doc must name the seeded `cloud_validation` vault in the verification"
    )
    assert "operationalerror" in lowered, "doc must name the OperationalError the bootstrap clears"


def test_doc_documents_least_privilege() -> None:
    """The grant is least-privilege: the app roles get CONNECT + CREATE on the
    database only, never the broad azure_pg_admin role.
    """
    text = _doc_text()
    lowered = text.lower()
    assert "least privilege" in lowered or "least-privilege" in lowered, (
        "doc must state the grant is least-privilege"
    )
    assert re.search(r"CREATE\s+ON\s+DATABASE", text, re.IGNORECASE), (
        "doc must name CREATE ON DATABASE as the scoped grant"
    )
    assert "azure_pg_admin" in lowered, (
        "doc must name the azure_pg_admin role it deliberately does not grant"
    )


def test_doc_documents_extension_prereq() -> None:
    """The extension prerequisite is on the record: the admin pre-creates the
    extensions, and the doc states why pgstattuple cannot be created by the app
    roles (it is untrusted).
    """
    text = _doc_text()
    lowered = text.lower()
    assert "vector" in lowered, "doc must name the vector extension"
    assert "pgstattuple" in lowered, "doc must name the pgstattuple extension"
    assert "untrusted" in lowered, "doc must explain the untrusted extension must be admin-created"


def test_doc_no_hardcoded_identity() -> None:
    """No subscription/tenant/client/object GUID or repository owner is baked into
    the doc — identity is resolved at run time into shell variables.
    """
    text = _doc_text()
    assert not _GUID_RE.search(text), (
        "doc hardcodes a GUID; resolve identity into a shell variable instead"
    )
    owner = _git_owner()
    if owner:
        assert owner.lower() not in text.lower(), (
            "doc hardcodes the repository owner; use a resolved variable"
        )


def test_guid_regex_control() -> None:
    """Control for the negative GUID assert: the detector matches a real GUID and
    rejects a non-GUID, so test_doc_no_hardcoded_identity cannot pass vacuously.
    """
    assert _GUID_RE.search("12345678-90ab-cdef-1234-567890abcdef")
    assert not _GUID_RE.search("id-pg-bootstrap-prod is not a guid")
