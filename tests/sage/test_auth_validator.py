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
