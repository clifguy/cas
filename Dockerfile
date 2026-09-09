# syntax=docker/dockerfile:1
#
# SAGE infra-server container image.
#
# Builds the SAGE Core API process (`python -m sage`) as a minimized, non-root
# Linux image for the cloud profile. MLX (the only Apple-Silicon dependency) is
# excluded by its environment marker; the Nomic CPU embedder and the Postgres
# adapters are cross-platform and ship in the image. The Nomic weights are
# baked in so the runtime loads them offline. The build is repo-less, so the
# runtime version and build identity come from the SAGE_BUILD_VERSION and
# SAGE_BUILD_IDENTITY build args (see build_info).
#
# Build:   docker build --build-arg SAGE_BUILD_VERSION=<MAJOR.MINOR.PATCH> \
#            --build-arg SAGE_BUILD_IDENTITY=<short-sha> -t cas-sage .
# Run:     docker run -p 8000:8000 cas-sage          # boots on the baked smoke config
# Override config at deploy time via SAGE_CONFIG_PATH. See
# docs/process/container-image.md.

# Base images are digest-pinned so two builds of one commit resolve the same
# bases. The deploy path leans on that: the image the deploy pushes is not
# smoke-tested itself, but is accepted on the strength of the CI run for that
# commit having built from the same bases and the same locked dependency sets.
#
# Note what this does and does not buy. It does not make the build byte-identical
# -- the runtime stage's apt layer resolves from moving indexes, and the embedder
# weights are fetched from a floating ref -- so those layers are identical across
# two builds only while the layer cache serves them, and rebuilt when it does
# not. A moving tag would break even the weaker guarantee, and break it silently.
# Keep the readable tag ahead of the digest when bumping.
#
# Every pin below sits on a FROM line, and has to stay on one. Dependabot's
# docker ecosystem is what keeps these pins from ageing into missed upstream
# security fixes, and it reads FROM lines and nothing else: it does not resolve
# an `ARG *_IMAGE` default, substitute a build arg, or look at `COPY --from=`. A
# pin moved to any of those keeps its digest and keeps passing the pinning gate
# while silently receiving no further updates. The `python-base` alias below is how
# several stages share one definition point without hiding it from the updater.
# `tests/deploy/test_dependabot_base_image_coverage.py` enforces this.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS python-base

# uv drives the install from the committed lockfile (byte-identical to CI). It is
# a stage rather than a bare `COPY --from=<image>` for the visibility reason above.
FROM ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 AS uv

# --------------------------------------------------------------------------
# Builder: resolve the locked runtime dependencies and pre-bake model weights.
# --------------------------------------------------------------------------
FROM python-base AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    HF_HOME=/opt/hf

WORKDIR /opt/sage

# Dependency layer (cached unless the lock changes): base runtime deps plus the
# [ocr] extra (ocrmypdf) so the scanned-PDF pre-pass runs on the cloud profile
# with the same code path as local. Test/mlx/dev extras stay out. On Linux,
# torch resolves to the CPU wheel index pinned in pyproject's [tool.uv.sources].
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --extra ocr

# Project source. app/backend rides along only because sage/app.py imports the
# in-process application-backend router today; that COPY drops out once the
# router is extracted to its own service. root_harness is not imported by the
# server and is omitted; the frontend SPA (app/src, app/dist) is never copied.
COPY sage/ ./sage/
COPY app/backend/ ./app/backend/
RUN uv sync --locked --no-dev --extra ocr

# Pre-bake the Nomic embedder weights so the runtime loads them with no
# HuggingFace egress at first vault init.
RUN .venv/bin/python -c "from sage.adapters.embedding_nomic import NomicEmbeddingProvider; NomicEmbeddingProvider()"

# --------------------------------------------------------------------------
# Runtime: non-root, minimized, offline model cache, health check.
# --------------------------------------------------------------------------
FROM python-base AS runtime

RUN groupadd --system sage \
    && useradd --system --gid sage --create-home --home-dir /home/sage sage

# System binaries for the runtime image:
#   * pg_dump for the maintenance job's snapshot-before-destroy step. The
#     client covers the greater of deploy/dev so it can dump either server.
#     Client 16 seeds the isolated PG16 rehearsal clone; client 17 runs the
#     measured migration. Retire the seed client with the one-time migration.
#     Debian stock
#     ships an older client, so add the PostgreSQL Global Development Group
#     (PGDG) apt repo for the pinned client.
#   * tesseract (+ English data) and ghostscript for the scanned-PDF OCR
#     pre-pass, which ocrmypdf drives as child processes. These come from Debian
#     main, so they need no extra repo; installing them here (runtime stage,
#     before USER sage) is what gives the cloud image local↔cloud OCR parity.
# The distro codename is read from /etc/os-release so this survives a base-image
# Debian bump. Build-time egress only (like the uv install); apt lists are
# dropped so the runtime layer stays minimized.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
    install -d /usr/share/postgresql-common/pgdg; \
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc; \
    . /etc/os-release; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      postgresql-client-16 postgresql-client-17 tesseract-ocr tesseract-ocr-eng ghostscript; \
    apt-get purge -y --auto-remove curl gnupg; \
    rm -rf /var/lib/apt/lists/*

# Baked at build time so a .git-less image reports its real version and
# commit (build_info resolves SAGE_BUILD_VERSION and SAGE_BUILD_IDENTITY when
# no live git checkout is present).
ARG SAGE_BUILD_VERSION
ARG SAGE_BUILD_IDENTITY
ENV SAGE_BUILD_VERSION=${SAGE_BUILD_VERSION} \
    SAGE_BUILD_IDENTITY=${SAGE_BUILD_IDENTITY} \
    PATH=/opt/sage/.venv/bin:$PATH \
    HF_HOME=/opt/hf \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    SAGE_CONFIG_PATH=/opt/sage/deploy/sage.config.container.yaml \
    SAGE_VAULT_ROOT=/var/lib/sage/vaults

WORKDIR /opt/sage
COPY --from=builder /opt/sage/.venv /opt/sage/.venv
COPY --from=builder --chown=sage:sage /opt/hf /opt/hf
COPY --from=builder /opt/sage/sage /opt/sage/sage
COPY --from=builder /opt/sage/app /opt/sage/app
COPY deploy/sage.config.container.yaml /opt/sage/deploy/sage.config.container.yaml
# The docs/fs files the runtime reads, expected under <repo-root>/docs/fs
# (parents[1] of the reading module, i.e. /opt/sage here).
#
# The stack-config JSON Schema that sage/config.py validates the loaded config
# against at startup:
COPY docs/fs/sage/sage_core_config.schema.json /opt/sage/docs/fs/sage/sage_core_config.schema.json
# The API specifications sage/app.py reads at startup for the authored prose it
# publishes in the served schema document. That document is fetched without a
# token and is all an outside developer has, so an image built without these
# would publish every path with every explanation missing; the app refuses to
# start instead.
COPY docs/fs/sage/sage_core_api.openapi.yaml /opt/sage/docs/fs/sage/sage_core_api.openapi.yaml
COPY docs/fs/cas_app_api.openapi.yaml /opt/sage/docs/fs/cas_app_api.openapi.yaml

RUN mkdir -p /var/lib/sage/vaults && chown -R sage:sage /var/lib/sage
USER sage
EXPOSE 8000

# Liveness probe on the loopback interface inside the container. External
# reachability requires the non-loopback bind in CMD below.
HEALTHCHECK --interval=30s --timeout=3s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]

# Bind a non-loopback interface so the platform ingress can reach the process.
ENTRYPOINT ["python", "-m", "sage"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
