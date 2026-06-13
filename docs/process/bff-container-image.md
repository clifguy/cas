# CAS BFF container image

The CAS backend-for-frontend (BFF) is the application-facing tier (CAS-ADR-042).
`Dockerfile.bff` at the repository root packages the standalone BFF process
(`python -m app.backend`) as a minimized, non-root Linux image for the cloud
profile. The image is a multi-stage build: a Node stage builds the SPA bundle
with Vite, and a Python runtime stage serves that bundle same-origin alongside
the BFF API. The BFF holds no embedder and loads no vaults, so — unlike the SAGE
infra image — it bakes no model weights; it reaches SAGE over HTTP, carrying the
signed-in user's delegated identity, while SAGE runs as a separate origin.

## Build

The build is repo-less (`.git` is excluded), so the runtime version comes from a
build arg — pass the real release version (`MAJOR.MINOR.PATCH`), which a pipeline
derives on the host with `git describe`:

```sh
docker build -f Dockerfile.bff \
  --build-arg SAGE_BUILD_VERSION="$(git describe --tags --abbrev=0 | sed 's/^v//')" \
  -t cas-bff .
```

`Dockerfile.bff` has a companion `Dockerfile.bff.dockerignore` (a per-Dockerfile
ignore, honored by BuildKit) that — unlike the SAGE image's `.dockerignore` —
keeps the SPA sources (`app/src`, `app/public`, the TS/Vite config) in the build
context, because the Node stage needs them.

The image is architecture-agnostic at the Dockerfile level; the production target
is `linux/amd64`. On Apple Silicon, add `--platform linux/amd64` to build the
production architecture under emulation (slower), or omit it for a native
`arm64` image for a quick local boot check.

## Run

With no extra configuration the container boots on the baked **smoke-minimal**
config (`deploy/sage.config.container.yaml`: the `local` profile, embedded
storage) — enough to start cleanly, serve the SPA, and answer the health check
with no external services. With no identity-provider coordinates set, the
interactive sign-in and the SAGE reverse proxy answer `auth_not_configured`; the
SPA and `/health` are served regardless.

```sh
docker run --rm -p 8001:8001 cas-bff
curl -s http://localhost:8001/health        # {"status":"ok","version":"<MAJOR.MINOR.PATCH>"}
```

The process binds `0.0.0.0:8001` (non-loopback) so a platform ingress can reach
it; map the platform's ingress target port to `8001`.

## Configure for a cloud deployment

Override the baked config by pointing `SAGE_CONFIG_PATH` at a stack config that
selects the cloud profile (`profile: cloud`); the cloud profile binds the BFF's
SAGE transport to the on-behalf-of HTTP client. Supply the BFF's Entra client
coordinates and the SAGE endpoint through the environment:

```sh
docker run -p 8001:8001 \
  -e SAGE_CONFIG_PATH=/etc/cas/config.cloud.yaml \
  -e CAS_BFF_TENANT_ID=<entra-tenant-id> \
  -e CAS_BFF_CLIENT_ID=<bff-app-client-id> \
  -e CAS_BFF_CLIENT_SECRET=<bff-client-secret> \
  -e CAS_BFF_SAGE_APP_ID_URI=<sage-resource-app-id-uri> \
  -e CAS_BFF_SAGE_BASE_URL=https://<sage-host>/ \
  -v /path/to/config.cloud.yaml:/etc/cas/config.cloud.yaml:ro \
  cas-bff
```

The image bakes **no secret value**. In the cloud profile the confidential
credentials (the BFF client secret and the Postgres credential backing the
externalized session store) are sourced from Key Vault via the container app's
managed identity, the same no-stored-credential model the SAGE image uses — see
[container-image.md](container-image.md). A `SAGE_CONFIG_PATH` that names a
missing file fails loud at startup rather than silently falling back to defaults.

## Runtime environment

| Variable | Purpose | Default in image |
|---|---|---|
| `SAGE_BUILD_VERSION` | Baked release version reported by a `.git`-less image | build arg |
| `SAGE_CONFIG_PATH` | Stack-config source (selects the profile; overrides the packaged default) | the baked smoke-minimal config |
| `CAS_BFF_TENANT_ID` | Entra tenant of the BFF app registration | unset |
| `CAS_BFF_CLIENT_ID` | BFF confidential-client application id | unset |
| `CAS_BFF_CLIENT_SECRET` | BFF client secret (cloud: from Key Vault via managed identity) | unset |
| `CAS_BFF_SAGE_APP_ID_URI` | SAGE resource-server app id URI scoped in the on-behalf-of exchange | unset |
| `CAS_BFF_SAGE_BASE_URL` | SAGE origin the BFF reaches server-side | unset |
| `CAS_BFF_AUTHORITY_HOST` | Entra authority host | `https://login.microsoftonline.com` |
| `CAS_BFF_POST_LOGIN_REDIRECT` | Path the SPA lands on after sign-in | `/` |

Interactive auth, the on-behalf-of exchange, and the externalized session store
activate only when the four required `CAS_BFF_*` coordinates are all present;
otherwise the BFF runs without auth and serves the SPA and health probe.

## Health check

The image defines a `HEALTHCHECK` that probes `GET /health` on the loopback
interface inside the container. `/health` is a constant, store-free liveness
probe returning `{"status": "ok", "version": "<release>"}`; it answers
independent of whether SAGE is reachable.

## Smoke test

`deploy/bff-smoke.sh` builds the image and runs the boot/health, SPA-serving,
non-root, and no-MLX checks. It requires Docker:

```sh
bash deploy/bff-smoke.sh 1.0.46        # version arg is baked as SAGE_BUILD_VERSION
```

The same checks run as an opt-in, Docker-gated pytest module:

```sh
SAGE_TEST_DOCKER=1 .venv/bin/python -m pytest tests/deploy/test_bff_container_smoke.py -q
```

The structural conventions (multi-stage Node + Python build, non-root, version
arg, health check, the frontend-keeping ignore) are additionally asserted by
`tests/deploy/test_bff_container_image.py`, which runs in the default suite with
no Docker daemon. A CI container-build job (multi-arch, registry push) is out of
scope here and is tracked as a separate CI/build ticket.
