# SAGE container image

SAGE is a reusable, frontend-agnostic infra server (CAS-ADR-042). The
`Dockerfile` at the repository root packages the SAGE Core API process
(`python -m sage`) as a minimized, non-root Linux image for the cloud profile.
The image is application-agnostic: it carries no frontend SPA, and MLX (the only
Apple-Silicon dependency) is excluded — the cloud profile uses the hosted
abstraction provider. The Nomic CPU embedder and the Postgres adapters are
cross-platform and ship in the image, with the Nomic weights baked in so the
runtime loads them with no HuggingFace egress.

## Build

The build is repo-less (`.git` is excluded), so the runtime version comes from
a build arg — pass the real release version (`MAJOR.MINOR.PATCH`), which a
pipeline derives on the host with `git describe`:

```sh
docker build --build-arg SAGE_BUILD_VERSION="$(git describe --tags --abbrev=0 | sed 's/^v//')" -t cas-sage .
```

The image is architecture-agnostic at the Dockerfile level; the production
target is `linux/amd64`. On Apple Silicon, add `--platform linux/amd64` to build
the production architecture under emulation (slower), or omit it to build a
native `arm64` image for a quick local boot check.

## Run

With no extra configuration the container boots on the baked **smoke-minimal**
config (`deploy/sage.config.container.yaml`: embedded storage, stub abstraction)
— enough to start cleanly and answer the health check with no external
services:

```sh
docker run --rm -p 8000:8000 cas-sage
curl -s http://localhost:8000/health        # {"status":"ok","version":"<MAJOR.MINOR.PATCH>"}
```

The process binds `0.0.0.0:8000` (non-loopback) so a platform ingress can reach
it; map the platform's ingress target port to `8000`.

## Configure for a cloud deployment

Override the baked config by pointing `SAGE_CONFIG_PATH` at a stack config that
selects the cloud profile (`profile: cloud`) and binds the cloud providers. The
cloud profile resolves its secrets — the hosted abstraction key and the Postgres
credential — from Key Vault and the Entra-only Postgres endpoint via the
container's managed identity, so **no secret value is passed through the
environment**; the image only needs the vault URI and the managed-identity
client id at runtime (`AZURE_CLIENT_ID` selects the user-assigned identity).

```sh
docker run -p 8000:8000 \
  -e SAGE_CONFIG_PATH=/etc/sage/config.cloud.yaml \
  -e SAGE_KEY_VAULT_URI=https://<vault>.vault.azure.net/ \
  -e AZURE_CLIENT_ID=<sage-managed-identity-client-id> \
  -v /path/to/config.cloud.yaml:/etc/sage/config.cloud.yaml:ro \
  cas-sage
```

A `SAGE_CONFIG_PATH` that names a missing file fails loud at startup (it does
not silently fall back to defaults). The cloud `config.cloud.yaml` sets
`profile: cloud`, `storage_backend: postgres` (plus the `postgres` connection
block, host/user/sslmode) and `abstraction.provider: anthropic` (plus a Claude
model id). See the config schema at `docs/fs/sage/sage_core_config.schema.json`.
A missing or unreadable secret fails closed with a clear error rather than
booting a half-configured stack.

> A `profile: local` container run takes the env-secret path instead —
> `ANTHROPIC_API_KEY` and `SAGE_PG_PASSWORD` in the environment — the same as the
> on-box developer setup. The managed-identity path above is specific to the
> cloud profile.

## Runtime environment

| Variable | Purpose | Default in image |
|---|---|---|
| `SAGE_BUILD_VERSION` | Baked release version reported by a `.git`-less image | build arg |
| `SAGE_CONFIG_PATH` | Stack-config source (overrides the packaged default) | the baked smoke-minimal config |
| `SAGE_VAULT_ROOT` | Vault discovery root | `/var/lib/sage/vaults` |
| `SAGE_KEY_VAULT_URI` | Key Vault data-plane URI the cloud profile reads secrets from | unset |
| `AZURE_CLIENT_ID` | Client id of the managed identity the cloud profile authenticates with | unset |
| `ANTHROPIC_API_KEY` | Hosted abstraction provider key — `local`-profile env path only | unset |
| `SAGE_PG_PASSWORD` | Postgres password for a TCP endpoint — `local`-profile env path only | unset |
| `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` | Force offline model loading (weights are baked) | `1` |

## Health check

The image defines a `HEALTHCHECK` that probes `GET /health` on the loopback
interface inside the container. `/health` is a constant, store-free liveness
probe returning `{"status": "ok", "version": "<release>"}`; it answers before
vaults finish loading and while a backing store is unreachable.

## Smoke test

`deploy/smoke.sh` builds the image and runs the boot/health, offline-embedder,
non-root, and no-MLX checks. It requires Docker:

```sh
bash deploy/smoke.sh 1.0.41        # version arg is baked as SAGE_BUILD_VERSION
```

The same checks run as an opt-in, Docker-gated pytest module:

```sh
SAGE_TEST_DOCKER=1 .venv/bin/python -m pytest tests/deploy/test_container_smoke.py -q
```

A CI container-build job (multi-arch, registry push) is out of scope here and is
tracked as a separate CI/build ticket.
