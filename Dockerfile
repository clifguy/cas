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

ARG PYTHON_IMAGE=python:3.14-slim

# --------------------------------------------------------------------------
# Builder: resolve the locked runtime dependencies and pre-bake model weights.
# --------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

# uv drives the install from the committed lockfile (byte-identical to CI).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

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
FROM ${PYTHON_IMAGE} AS runtime

RUN groupadd --system sage \
    && useradd --system --gid sage --create-home --home-dir /home/sage sage

# System binaries for the runtime image:
#   * pg_dump for the maintenance job's snapshot-before-destroy step. The
#     Flexible Server is major 16 (infra/modules/postgres.bicep); pin the client
#     major to it so pg_dump does not refuse a newer-server dump. Debian stock
#     ships an older client, so add the PostgreSQL Global Development Group
#     (PGDG) apt repo for the 16 client.
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
      postgresql-client-16 tesseract-ocr tesseract-ocr-eng ghostscript; \
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
COPY --from=builder /opt/hf /opt/hf
COPY --from=builder /opt/sage/sage /opt/sage/sage
COPY --from=builder /opt/sage/app /opt/sage/app
COPY deploy/sage.config.container.yaml /opt/sage/deploy/sage.config.container.yaml
# The stack-config JSON Schema that sage/config.py validates the loaded config
# against at startup -- the sole docs/fs file the runtime reads, expected at
# <repo-root>/docs/fs/sage/ (parents[1] of sage/config.py, i.e. /opt/sage here).
COPY docs/fs/sage/sage_core_config.schema.json /opt/sage/docs/fs/sage/sage_core_config.schema.json

RUN mkdir -p /var/lib/sage/vaults && chown -R sage:sage /var/lib/sage /opt/hf
USER sage
EXPOSE 8000

# Liveness probe on the loopback interface inside the container. External
# reachability requires the non-loopback bind in CMD below.
HEALTHCHECK --interval=30s --timeout=3s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]

# Bind a non-loopback interface so the platform ingress can reach the process.
ENTRYPOINT ["python", "-m", "sage"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
