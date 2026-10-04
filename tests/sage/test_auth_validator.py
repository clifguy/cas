"""Token-validator unit tests (CAS-ADR-042).

Exercise the resource-server token validators directly with an in-test RSA
keypair and an injected signing-key resolver, so the JWT signature/claim
logic is verified without any network access to a JWKS endpoint. Each negative
case is the guard that the validator actually checks the corresponding claim:
remove the audience check and B3 goes green by accident, and so on.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from sage.auth import AuthError, EntraTokenValidator, NoAuthValidator

ISSUER = "https://login.microsoftonline.com/tid/v2.0"
AUDIENCE = "api://sage"


@pytest.fixture(scope="module")
def keypair() -> tuple:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, priv.public_key()


def _token(priv, **overrides) -> str:
    now = int(time.time())
    claims = {
        "aud": AUDIENCE,
        "iss": ISSUER,
        "iat": now,
        "exp": now + 3600,
        "sub": "user-1",
        "scp": "Sage.Access",
    }
    claims.update(overrides)
    return jwt.encode(claims, priv, algorithm="RS256", headers={"kid": "k1"})


def _validator(public_key, **overrides) -> EntraTokenValidator:
    kwargs = {
        "audience": AUDIENCE,
        "issuer": ISSUER,
        "required_scopes": frozenset({"Sage.Access"}),
        "required_roles": frozenset({"Sage.Reader"}),
        "signing_key_resolver": lambda _token: public_key,
    }
    kwargs.update(overrides)
    return EntraTokenValidator(**kwargs)


async def test_b1_noauth_passes_through() -> None:
    principal = await NoAuthValidator().validate(None)
    assert principal.anonymous is True


async def test_b2_happy_path(keypair) -> None:
    priv, pub = keypair
    principal = await _validator(pub).validate(_token(priv))
    assert principal.anonymous is False
    assert principal.subject == "user-1"
    assert "Sage.Access" in principal.scopes


async def test_b3_wrong_audience_rejected(keypair) -> None:
    priv, pub = keypair
    with pytest.raises(AuthError) as ei:
        await _validator(pub).validate(_token(priv, aud="api://wrong"))
    assert ei.value.status_code == 401
    assert ei.value.error == "invalid_token"


async def test_b4_wrong_issuer_rejected(keypair) -> None:
    priv, pub = keypair
    with pytest.raises(AuthError) as ei:
        await _validator(pub).validate(_token(priv, iss="https://evil.example/"))
    assert ei.value.status_code == 401


async def test_b5_expired_rejected(keypair) -> None:
    priv, pub = keypair
    past = int(time.time()) - 3600
    with pytest.raises(AuthError) as ei:
        await _validator(pub).validate(_token(priv, exp=past, iat=past - 60))
    assert ei.value.status_code == 401


async def test_b6_bad_signature_rejected(keypair) -> None:
    priv, pub = keypair
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    # Signed by `other`, but the resolver returns `pub` -> signature mismatch.
    with pytest.raises(AuthError) as ei:
        await _validator(pub).validate(_token(other))
    assert ei.value.status_code == 401
    assert ei.value.error == "invalid_token"


async def test_b7_missing_token_challenges(keypair) -> None:
    _priv, pub = keypair
    v = _validator(pub)
    for token in (None, ""):
        with pytest.raises(AuthError) as ei:
            await v.validate(token)
        assert ei.value.status_code == 401
        assert ei.value.error == "invalid_request"
        assert ei.value.www_authenticate().startswith("Bearer")


async def test_b8_insufficient_scope_is_403(keypair) -> None:
    priv, pub = keypair
    # Valid token, but neither a required scope nor a required role present.
    with pytest.raises(AuthError) as ei:
        await _validator(pub).validate(_token(priv, scp="Other.Scope"))
    assert ei.value.status_code == 403
    assert ei.value.error == "insufficient_scope"


async def test_b9_app_role_accepted(keypair) -> None:
    priv, pub = keypair
    # No delegated scope, but the app role satisfies the role requirement.
    principal = await _validator(pub).validate(_token(priv, scp="", roles=["Sage.Reader"]))
    assert "Sage.Reader" in principal.roles


async def test_b10_accepts_either_audience_form(keypair) -> None:
    # A validator configured with both the App ID URI and its bare GUID accepts
    # a token whose aud is either -- a v2.0 access token carries the bare GUID.
    priv, pub = keypair
    v = _validator(pub, audience=["api://sage", "sage"])
    for aud in ("sage", "api://sage"):
        principal = await v.validate(_token(priv, aud=aud))
        assert principal.anonymous is False
        assert principal.subject == "user-1"


async def test_b11_foreign_audience_still_rejected_with_list(keypair) -> None:
    # Broadening to the two-element list does not weaken the audience check: a
    # token for any other resource is still rejected.
    priv, pub = keypair
    v = _validator(pub, audience=["api://sage", "sage"])
    for aud in ("api://wrong", "some-other-guid"):
        with pytest.raises(AuthError) as ei:
            await v.validate(_token(priv, aud=aud))
        assert ei.value.status_code == 401
        assert ei.value.error == "invalid_token"


# ---------------------------------------------------------------------------
# The challenge carries a fixed description; the library's text is logged.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "api://wrong"},
        {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 3660},
        {"iss": 'https://evil.example/"quoted"'},
    ],
    ids=["audience", "expired", "issuer"],
)
async def test_b12_validation_failure_description_is_fixed(keypair, caplog, overrides) -> None:
    import logging

    priv, pub = keypair
    with caplog.at_level(logging.INFO, logger="sage.auth"):
        with pytest.raises(AuthError) as ei:
            await _validator(pub).validate(_token(priv, **overrides))

    assert ei.value.description == "Token validation failed."
    assert ei.value.www_authenticate() == (
        'Bearer error="invalid_token", error_description="Token validation failed."'
    )
    logged = [rec for rec in caplog.records if rec.name == "sage.auth"]
    assert logged, "the library's reason must reach the server log"
    assert any(
        rec.exc_info is not None and isinstance(rec.exc_info[1], jwt.PyJWTError) for rec in logged
    ), [rec.getMessage() for rec in logged]


def test_b13_challenge_parameters_are_quoted_strings() -> None:
    """Every quoted challenge parameter escapes the quote and backslash it may carry."""
    exc = AuthError(
        401,
        'bad"error',
        'say "hi" \\ there',
        resource_metadata_url='https://x.example/"m"',
    )
    assert exc.www_authenticate() == (
        'Bearer error="bad\\"error", error_description="say \\"hi\\" \\\\ there", '
        'resource_metadata="https://x.example/\\"m\\""'
    )


async def test_b14_unresolved_signing_key_is_logged(keypair, caplog) -> None:
    import logging

    priv, pub = keypair

    def _resolver(_token):
        raise RuntimeError("jwks-fetch-sentinel")

    with caplog.at_level(logging.INFO, logger="sage.auth"):
        with pytest.raises(AuthError) as ei:
            await _validator(pub, signing_key_resolver=_resolver).validate(_token(priv))

    assert ei.value.description == "Token signing key could not be resolved."
    assert "jwks-fetch-sentinel" not in ei.value.www_authenticate()
    assert any(
        rec.exc_info is not None and "jwks-fetch-sentinel" in str(rec.exc_info[1])
        for rec in caplog.records
        if rec.name == "sage.auth"
    )


async def test_b15_insufficient_scope_is_logged(keypair, caplog) -> None:
    import logging

    priv, pub = keypair
    with caplog.at_level(logging.INFO, logger="sage.auth"):
        with pytest.raises(AuthError) as ei:
            await _validator(pub).validate(_token(priv, scp="Other.Scope"))

    assert ei.value.description == "Token lacks a required scope or role."
    assert "Other.Scope" not in ei.value.www_authenticate()
    messages = [rec.getMessage() for rec in caplog.records if rec.name == "sage.auth"]
    assert any("Other.Scope" in m and "Sage.Access" in m for m in messages), messages


async def test_b16_scope_refusal_with_non_string_roles_is_still_403(keypair) -> None:
    priv, pub = keypair
    with pytest.raises(AuthError) as ei:
        await _validator(pub).validate(_token(priv, scp="Other.Scope", roles=["Other", 7]))
    assert ei.value.status_code == 403
