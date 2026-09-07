"""Report a Postgres server whose major differs from the declared one.

The other parity checks read tracked files: they prove the repository agrees
with itself. This one asks the server the suite is actually running against,
which is the only place out-of-band drift shows up -- a workstation upgraded by
Homebrew, or a CI service container whose image tag did not resolve to what the
manifest declares.

The two cases are not equally severe, and the check treats them differently.
In CI the DSN points at a service container whose image tag is derived from
``versions.json``, so a mismatch means the derivation did not take effect: a
gate failure. On a workstation the server is the developer's own, and failing
the whole suite over its major would make the suite unusable for the duration
of an upgrade -- so it warns, every run, where the warnings summary shows it.
"""

from __future__ import annotations

import os
import warnings
from typing import Final

import pytest

from tests.helpers.versions import major_of, postgres_dev_major


# Emitted rather than a bare UserWarning so a developer can filter or escalate
# this one specifically.
class PostgresMajorDriftWarning(UserWarning):
    """The Postgres server under test is not the declared development major."""


_CI_ENV_VAR: Final[str] = "CI"

# A CI job whose Postgres is deliberately not the development major names the
# major it expects here. Only the deploy-floor job does; every other job runs
# the development major and leaves this unset.
_EXPECTED_MAJOR_ENV_VAR: Final[str] = "SAGE_TEST_EXPECTED_PG_MAJOR"


def expected_major() -> int:
    """The major the server under test should be running.

    Defaults to the declared development major, which is what the workstation
    and every CI service container but one run. A job standing up the deploy
    floor instead overrides it, so widening that job's path list to reach this
    module reports a real mismatch rather than a spurious one.
    """
    override = os.environ.get(_EXPECTED_MAJOR_ENV_VAR, "").strip()
    if override:
        return major_of(override)
    return major_of(postgres_dev_major())


def server_major(version_num: int) -> int:
    """Return the major from a ``server_version_num`` reading.

    ``SHOW server_version_num`` reports 170010 for 17.10, so the major is the
    reading over 10000. A helper rather than an inline expression so the
    conversion the check depends on is reachable from a test; asserting the
    arithmetic inline would assert a property of Python, not of this module.
    """
    return version_num // 10000


def report_major_drift(server_major: int, declared_major: int, *, in_ci: bool) -> None:
    """Fail under CI, warn otherwise, when the two majors differ.

    Split out from the test so the disposition can be exercised without a
    database -- see the controls at the end of this module.
    """
    if server_major == declared_major:
        return
    message = (
        f"the Postgres server under test is major {server_major}, but versions.json "
        f"declares development major {declared_major}. "
    )
    if in_ci:
        raise AssertionError(
            message + "In CI the service container's image tag is derived from that "
            "declaration, so this means the derivation did not take effect."
        )
    warnings.warn(
        message + "Statements valid only on one of these majors will behave differently here "
        "than in CI and at deploy.",
        PostgresMajorDriftWarning,
        stacklevel=2,
    )


def test_server_major_matches_the_declared_development_major() -> None:
    """The server the suite connects to runs the declared development major."""
    dsn = os.environ.get("SAGE_TEST_PG_DSN")
    if not dsn:
        pytest.skip("SAGE_TEST_PG_DSN unset; no server to ask")
    psycopg = pytest.importorskip("psycopg")

    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
        row = conn.execute("SHOW server_version_num").fetchone()
    assert row, "SHOW server_version_num returned no row"

    report_major_drift(
        server_major(int(row[0])),
        expected_major(),
        in_ci=bool(os.environ.get(_CI_ENV_VAR)),
    )


# --------------------------------------------------------------------------- #
# Anti-coincidental controls                                                   #
#                                                                             #
# The check above skips without a DSN and is silent when the majors agree,     #
# which is every ordinary run -- so on its own it never demonstrates that it   #
# can report anything. These drive the disposition directly.                   #
# --------------------------------------------------------------------------- #


def test_control_matching_majors_neither_fail_nor_warn() -> None:
    """Agreement is silent on both arms."""
    for in_ci in (True, False):
        with warnings.catch_warnings():
            warnings.simplefilter("error", PostgresMajorDriftWarning)
            report_major_drift(17, 17, in_ci=in_ci)


def test_control_drift_fails_under_ci() -> None:
    """A mismatch is a hard failure on the CI arm."""
    with pytest.raises(AssertionError, match="did not take effect"):
        report_major_drift(16, 17, in_ci=True)


def test_control_drift_warns_off_ci() -> None:
    """A mismatch warns, and does not raise, on the workstation arm."""
    with pytest.warns(PostgresMajorDriftWarning, match="major 16"):
        report_major_drift(16, 17, in_ci=False)


def test_control_expected_major_prefers_the_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A job naming its own major is believed; otherwise the declared one stands.

    Without the override branch, the deploy-floor job would be held to the
    development major and would red claiming the derivation failed -- the
    misleading message this selection exists to prevent. Both branches are
    exercised because a default that silently won would produce exactly that.
    """
    monkeypatch.delenv(_EXPECTED_MAJOR_ENV_VAR, raising=False)
    assert expected_major() == major_of(postgres_dev_major())

    monkeypatch.setenv(_EXPECTED_MAJOR_ENV_VAR, "16")
    assert expected_major() == 16

    # An empty value is the shape a workflow produces when an output it reads
    # does not exist, and must fall back rather than parse to nothing.
    monkeypatch.setenv(_EXPECTED_MAJOR_ENV_VAR, "")
    assert expected_major() == major_of(postgres_dev_major())


def test_control_server_major_extraction() -> None:
    """The conversion the check runs maps a real reading to its major.

    Exercises ``server_major`` itself rather than the arithmetic: asserting
    ``170010 // 10000 == 17`` inline states a fact about Python that stays true
    however this module changes, so no mutation of the code under test could
    make it red -- setup presented as a control but in no assertion's causal
    path. A wrong divisor here compares 170010 against 17 and reports drift on
    every run; this is what would catch it.
    """
    assert server_major(170010) == 17
    assert server_major(160006) == 16
    assert server_major(180000) == 18
