"""Cross-cutting provider-agnostic gate for the cloud infrastructure-as-code.

The CAS cloud deployment profile (CAS-ADR-042) keeps DNS provider-agnostic: the
deploy computes the records an operator must publish but never scripts — or even
presumes — a particular DNS provider. The operator publishes the emitted records
in whatever provider their tenant uses. ``test_dns_script_is_provider_agnostic``
(``tests/deploy/test_bootstrap_scripts.py``) locks that for the DNS emitter
script; this gate locks it for the *reusable* IaC surfaces under ``infra/`` — the
Bicep modules and orchestrator, the per-tenant parameter template (and its
committed ``.example``), and the module reference docs — where a provider name is
description or comment prose rather than a scripted API call.

The one place a concrete provider *is* named is the operator runbook
(``docs/process/custom-domains-dns.md``), which documents this operator's manual
publication step. That lives under ``docs/`` — outside this gate's reach — and is
gated separately by ``test_custom_domains.py``.

A provider name wrapped across two comment lines (``... AWS Route\\n// 53 ...``)
is the form the drift actually took, so the detector normalizes whitespace and
comment markers before matching. A check that caught only the single-line
spelling would carry the same blind spot the script-only gate had for Bicep,
which is what let this class of drift reach the committed IaC surfaces.

These checks read tracked text only — no Azure or Bicep tooling — so they run in
the ordinary Python test job. The anti-coincidental controls below prove the
detector fires on the regressions it targets; a "must-not-contain" scan whose
matcher silently never fires would pass every surface coincidentally.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
INFRA_DIR: Final[Path] = REPO_ROOT / "infra"

# Reusable IaC surfaces under infra/: the Bicep modules and orchestrator, the
# per-tenant parameter template, and the module reference docs. The committed
# example parameter file carries the ``.example`` suffix, so it is matched by
# name rather than by ``Path.suffix``.
_IAC_SUFFIXES: Final[tuple[str, ...]] = (".bicep", ".bicepparam", ".md")
_IAC_NAME_SUFFIXES: Final[tuple[str, ...]] = (".bicepparam.example",)

# DNS-provider names a provider-agnostic IaC surface must never bake in. The
# deploy computes records; the operator publishes them in whatever provider the
# tenant uses (CAS-ADR-042). Matched against normalized (whitespace-collapsed,
# lowercased) text, so a single space is the only separator that can appear
# between tokens.
_PROVIDER_TOKENS: Final[tuple[str, ...]] = (
    r"route ?53",
    r"\baws\b",
    r"\bcloudflare\b",
    r"azure dns",
    r"gcloud dns",
)


# ---------------------------------------------------------------------------
# Detectors (pure text functions — exercised by the control tests below)
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Lowercase ``text`` and collapse every run of non-alphanumeric characters
    to a single space.

    Comment markers, line breaks, and punctuation all become separators, so a
    provider name split across two comment lines (``AWS Route\\n// 53``) reads as
    ``aws route 53``. Matching the normalized form catches the wrapped spelling
    the original drift used, not only the single-line one.
    """
    return re.sub(r"[^a-z0-9]+", " ", text.lower())


def _provider_hits(text: str) -> list[str]:
    """Return the provider tokens (as their pattern strings) found in ``text``."""
    normalized = _normalize(text)
    return [token for token in _PROVIDER_TOKENS if re.search(token, normalized)]


def _iter_iac_files() -> list[Path]:
    """Every reusable IaC source under ``infra/`` this gate scans."""
    files: list[Path] = []
    for path in sorted(INFRA_DIR.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix in _IAC_SUFFIXES or path.name.endswith(_IAC_NAME_SUFFIXES):
            files.append(path)
    return files


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_infra_iac_names_no_dns_provider() -> None:
    """No reusable IaC surface under ``infra/`` names a DNS provider — the
    operator publishes the emitted records in whatever provider the tenant uses.
    """
    offenders: dict[str, list[str]] = {}
    for path in _iter_iac_files():
        hits = _provider_hits(path.read_text(encoding="utf-8"))
        if hits:
            offenders[str(path.relative_to(REPO_ROOT))] = hits
    assert not offenders, (
        "infra IaC surfaces must name no DNS provider (publication is the "
        f"operator's step, in whatever provider the tenant uses): {offenders}"
    )


def test_gate_reaches_every_surface_kind() -> None:
    """The scan reaches all four reusable surface kinds — Bicep, the parameter
    template, its committed example, and the module docs — so a provider name in
    any of them is caught, not just in Bicep ``@description`` strings (the gap
    that let the original drift slip through).
    """
    scanned = {path.name for path in _iter_iac_files()}
    for expected in (
        "main.bicep",
        "container-apps.bicep",
        "main.bicepparam",
        "main.bicepparam.example",
        "README.md",
    ):
        assert expected in scanned, f"{expected} must be in the provider-agnostic scan"


# ---------------------------------------------------------------------------
# Anti-coincidental-pass controls
#
# These verify the detector actually fires on the regressions it targets, NOT
# that any specific surface is clean. Without them, a broken regex would let the
# "must-not-contain" gate above pass every surface coincidentally.
# ---------------------------------------------------------------------------


def test_detector_fires_on_single_line_provider_name() -> None:
    """The detector flags a single-line provider name (both spellings of 53)."""
    assert r"route ?53" in _provider_hits("DNS lives in AWS Route 53.")
    assert r"route ?53" in _provider_hits("publish the Route53 records")
    assert r"\bcloudflare\b" in _provider_hits("zone hosted on Cloudflare")


def test_detector_fires_on_comment_wrapped_provider_name() -> None:
    """The detector catches a provider name split across two comment lines — the
    exact form the original drift took in the parameter template and module docs.
    """
    wrapped = "// covers both hostnames. DNS lives in AWS Route\n// 53 and is published."
    hits = _provider_hits(wrapped)
    assert r"route ?53" in hits
    assert r"\baws\b" in hits


def test_detector_passes_provider_neutral_prose() -> None:
    """Provider-neutral wording (the remediated form) trips nothing."""
    clean = "the operator publishes its DNS records into their own provider at deploy time"
    assert _provider_hits(clean) == []


def test_detector_respects_word_boundaries() -> None:
    """``aws`` matches as a word, not as a substring of 'draws' / 'laws'."""
    assert _provider_hits("the module draws on the laws of the cloud") == []
