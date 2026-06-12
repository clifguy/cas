"""Backend-for-frontend authentication: interactive OIDC sign-in, delegated
downstream-token acquisition, and an externalized session store.

The whole surface is gated on configuration presence (see
:func:`app.backend.auth.config.load_bff_auth_settings`): when the identity-
provider coordinates are absent, the application backend runs exactly as it did
before, with no auth and no session store. When they are present, the backend
becomes a confidential client that signs users in, holds their tokens server-
side, and reaches SAGE with the user's delegated identity on every call.
"""
