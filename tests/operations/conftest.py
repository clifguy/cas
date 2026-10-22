"""Every recovery test uses synthetic target identities and temporary evidence."""

import pytest

from scripts.sage_recovery import config


@pytest.fixture
def recovery_settings(tmp_path):
    previous = config._current
    value = config.Settings(
        config_path=tmp_path / "config.json",
        base_url="https://sage.example.invalid",
        audience="fake-audience",
        subscription="fake-subscription",
        resource_group="fake-group",
        app="fake-app",
        revision="fake-app--revision",
        image="fake.invalid/sage:pinned",
        vault_id="test_vault",
        indexed_document="indexed_control",
        expected_ids=tuple(f"document_{n}" for n in range(16)),
        six_ids=tuple(f"document_{n}" for n in range(6)),
        root=tmp_path,
        evidence_out=tmp_path,
        token_file=tmp_path / "fake-token.json",
        manifest=tmp_path / "manifest.json",
        ledger=tmp_path / "ledger.json",
        manifest_sha256="a" * 64,
        ledger_sha256="b" * 64,
    )
    config.configure(value)
    yield value
    config._current = previous
