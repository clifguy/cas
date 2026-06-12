# syntax=docker/dockerfile:1
#
# SAGE infra-server container image.
#
# Builds the SAGE Core API process (`python -m sage`) as a minimized, non-root
# Linux image for the cloud profile. MLX (the only Apple-Silicon dependency) is
# excluded by its environment marker; the Nomic CPU embedder and the Postgres
# adapters are cross-platform and ship in the image. The Nomic weights are
# baked in so the runtime loads them offline. The build is repo-less, so the
# runtime version comes from the SAGE_BUILD_VERSION build arg (see build_info).
#
# Build:   docker build --build-arg SAGE_BUILD_VERSION=<MAJOR.MINOR.PATCH> -t cas-sage .
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

# Dependency layer (cached unless the lock changes): base runtime deps only --
# no test/mlx/dev/ocr extras. On Linux, torch resolves to the CPU wheel index
# pinned in pyproject's [tool.uv.sources].
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# Project source. app/backend rides along only because sage/app.py imports the
# in-process application-backend router today; that COPY drops out once the
# router is extracted to its own service. root_harness is not imported by the
# server and is omitted; the frontend SPA (app/src, app/dist) is never copied.
COPY sage/ ./sage/
COPY app/backend/ ./app/backend/
RUN uv sync --locked --no-dev

# Pre-bake the Nomic embedder weights so the runtime loads them with no
# HuggingFace egress at first vault init.
RUN .venv/bin/python -c "from sage.adapters.embedding_nomic import NomicEmbeddingProvider; NomicEmbeddingProvider()"

# --------------------------------------------------------------------------
# Runtime: non-root, minimized, offline model cache, health check.
# --------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

RUN groupadd --system sage \
    && useradd --system --gid sage --create-home --home-dir /home/sage sage

# Baked at build time so a .git-less image reports its real version
# (build_info resolves SAGE_BUILD_VERSION when no live git checkout is present).
ARG SAGE_BUILD_VERSION
ENV SAGE_BUILD_VERSION=${SAGE_BUILD_VERSION} \
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
