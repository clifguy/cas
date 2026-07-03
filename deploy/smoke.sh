#!/usr/bin/env bash
#
# Container boot smoke for the SAGE infra-server image.
#
# Builds the image and runs four checks:
#   SMK-001  boots, binds a non-loopback interface, /health green, version baked
#   SMK-002  Nomic embedder weights load offline (baked into the image)
#   SMK-003  runs as a non-root user
#   SMK-004  MLX is absent from the Linux image
#
# Requires Docker. Exits non-zero on any failure.
#
# Usage: deploy/smoke.sh [VERSION]
#   VERSION  MAJOR.MINOR.PATCH baked as SAGE_BUILD_VERSION (default: sage.build_info.RELEASE_VERSION).
# Env:
#   SMOKE_IMAGE     image tag (default: cas-sage:smoke)
#   SMOKE_PLATFORM  docker --platform (default: linux/amd64; set empty for native)
#   SMOKE_PORT      host port to publish (default: 18000)
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Single-source the baked version from build_info -- the same MAJOR.MINOR.PATCH
# the dev box, CI, and the OpenAPI contract resolve (PATCH = commit distance
# since the last vMAJOR.MINOR.0 tag), not a bare `git describe --abbrev=0` which
# would report only the tag floor.
version="${1:-$("$repo_root/.venv/bin/python" -c 'from sage import build_info as b; print(b.RELEASE_VERSION)' 2>/dev/null)}"
# build_info yields "unknown" only without a v* tag or git checkout; fall back so
# the smoke still bakes a usable stamp.
case "$version" in ""|unknown) version="0.0.0" ;; esac
# Mirror build_info._compute_build_identity's own convention: short SHA, with
# -dirty appended for uncommitted *tracked* changes (untracked files ignored).
identity="$(git -C "$repo_root" rev-parse --short=7 HEAD 2>/dev/null || true)"
if [ -n "$identity" ] && [ -n "$(git -C "$repo_root" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  identity="${identity}-dirty"
fi
[ -n "$identity" ] || identity="unknown"
image="${SMOKE_IMAGE:-cas-sage:smoke}"
platform="${SMOKE_PLATFORM-linux/amd64}"
port="${SMOKE_PORT:-18000}"
name="sage-smoke-$$"

plat_arg=()
[ -n "$platform" ] && plat_arg=(--platform "$platform")

cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "==> Building $image (version=$version, identity=$identity, platform=${platform:-native})"
docker build ${plat_arg[@]+"${plat_arg[@]}"} --build-arg SAGE_BUILD_VERSION="$version" --build-arg SAGE_BUILD_IDENTITY="$identity" -t "$image" "$repo_root"

echo "==> SMK-003: runs as non-root"
uid="$(docker run --rm ${plat_arg[@]+"${plat_arg[@]}"} --entrypoint id "$image" -u)"
[ "$uid" != "0" ] || { echo "FAIL: container runs as root (uid 0)"; exit 1; }
echo "    uid=$uid (non-root) OK"

echo "==> SMK-004: MLX absent from the image"
docker run --rm ${plat_arg[@]+"${plat_arg[@]}"} --entrypoint python "$image" \
  -c "import importlib.util,sys; sys.exit(1 if importlib.util.find_spec('mlx') else 0)" \
  || { echo "FAIL: mlx is importable in the Linux image"; exit 1; }
echo "    mlx not importable OK"

echo "==> SMK-002: Nomic weights load offline"
docker run --rm ${plat_arg[@]+"${plat_arg[@]}"} -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
  --entrypoint python "$image" \
  -c "from sage.adapters.embedding_nomic import NomicEmbeddingProvider; NomicEmbeddingProvider()" \
  || { echo "FAIL: embedder could not load baked weights offline"; exit 1; }
echo "    offline embedder load OK"

echo "==> SMK-001: boots, binds non-loopback, health green, version baked"
docker run -d --name "$name" ${plat_arg[@]+"${plat_arg[@]}"} -p "$port:8000" "$image" >/dev/null
body=""
for _ in $(seq 1 60); do
  body="$(curl -fsS "http://127.0.0.1:$port/health" 2>/dev/null || true)"
  [ -n "$body" ] && break
  sleep 2
done
[ -n "$body" ] || { echo "FAIL: /health never became reachable"; docker logs "$name" | tail -40; exit 1; }
echo "    health body: $body"
echo "$body" | grep -Eq '"status": *"ok"' || { echo "FAIL: health status not ok"; exit 1; }
echo "$body" | grep -Eq "\"version\": *\"$version\"" || { echo "FAIL: baked version $version not reported"; exit 1; }
echo "    boot + non-loopback bind + health + version OK"

echo "==> SMK-001b: build identity baked, banner reports it"
logs="$(docker logs "$name" 2>&1)"
if [ "$identity" = "unknown" ]; then
  echo "$logs" | grep -q "build identity unavailable" \
    || { echo "FAIL: expected degraded-identity banner hint not found"; exit 1; }
else
  echo "$logs" | grep -Fq "build $identity" \
    || { echo "FAIL: banner does not report baked identity $identity"; exit 1; }
  echo "$logs" | grep -q "build identity unavailable" \
    && { echo "FAIL: banner reports identity unavailable despite baked stamp"; exit 1; }
fi
echo "    startup banner build identity OK"

docker image inspect "$image" --format '==> Image size: {{.Size}} bytes ({{len .RootFS.Layers}} layers)' || true
echo "ALL SMOKE CHECKS PASSED"
