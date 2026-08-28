"""Shared cloud-job environment plumbing (``sage.maintenance._cloud_env``).

A lifespan-less ``python -m`` job never populates the stack-config singleton, so
each entrypoint builds its cloud configuration straight from the environment the
job image carries. The bulk-reabstract entrypoint needs one coordinate the purge
entrypoints do not -- a live abstraction provider -- so these tests pin that the
abstraction block is populated when the environment declares it and that its
absence leaves the purge entrypoints' contract unchanged.
"""

import pytest

from sage.maintenance._cloud_env import config_from_env

_BASE = {
    "PG_FQDN": "pg.example.internal",
    "PG_DATABASE": "sage",
    "PG_USER": "id-sage",
    "SHAREPOINT_SITE_ID": "site",
    "SHAREPOINT_DRIVE_ID": "drive",
}


def test_abstraction_block_is_populated_from_env():
    config = config_from_env(
        {**_BASE, "ABSTRACTION_PROVIDER": "anthropic", "ABSTRACTION_MODEL": "claude-haiku-4-5"}
    )

    assert config.abstraction.provider == "anthropic"
    assert config.abstraction.model == "claude-haiku-4-5"


def test_absent_abstraction_env_leaves_the_purge_path_unaffected():
    """A job that never resolves the abstraction seam still builds its config."""
    config = config_from_env(dict(_BASE))

    assert config.abstraction.model is None
    assert config.profile == "cloud"
    assert config.postgres.host == "pg.example.internal"


def test_a_missing_required_coordinate_still_fails_loud():
    with pytest.raises(ValueError, match="PG_FQDN"):
        config_from_env({k: v for k, v in _BASE.items() if k != "PG_FQDN"})
