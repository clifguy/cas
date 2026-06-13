#!/usr/bin/env bash
#
# Container boot smoke for the CAS BFF image.
#
# Builds the image and runs four checks:
#   BSMK-001  boots, binds a non-loopback interface, /health green, version baked
#   BSMK-002  serves the built SPA shell at / and falls back to it on a deep link
#   BSMK-003  runs as a non-root user
#   BSMK-004  MLX is absent from the Linux image
#
# Requires Docker. Exits non-zero on any failure.
#
# Usage: deploy/bff-smoke.sh [VERSION]
#   VERSION  MAJOR.MINOR.PATCH baked as SAGE_BUILD_VERSION (default: sage.build_info.RELEASE_VERSION).
# Env:
#   SMOKE_IMAGE     image tag (default: cas-bff:smoke)
#   SMOKE_PLATFORM  docker --platform (default: linux/amd64; set empty for native)
#   SMOKE_PORT      host port to publish (default: 18001)
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
image="${SMOKE_IMAGE:-cas-bff:smoke}"
platform="${SMOKE_PLATFORM-linux/amd64}"
port="${SMOKE_PORT:-18001}"
name="cas-bff-smoke-$$"

plat_arg=()
[ -n "$platform" ] && plat_arg=(--platform "$platform")

cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "==> Building $image (version=$version, platform=${platform:-native})"
docker build ${plat_arg[@]+"${plat_arg[@]}"} -f "$repo_root/Dockerfile.bff" \
  --build-arg SAGE_BUILD_VERSION="$version" -t "$image" "$repo_root"

echo "==> BSMK-003: runs as non-root"
uid="$(docker run --rm ${plat_arg[@]+"${plat_arg[@]}"} --entrypoint id "$image" -u)"
[ "$uid" != "0" ] || { echo "FAIL: container runs as root (uid 0)"; exit 1; }
echo "    uid=$uid (non-root) OK"

echo "==> BSMK-004: MLX absent from the image"
docker run --rm ${plat_arg[@]+"${plat_arg[@]}"} --entrypoint python "$image" \
  -c "import importlib.util,sys; sys.exit(1 if importlib.util.find_spec('mlx') else 0)" \
  || { echo "FAIL: mlx is importable in the Linux image"; exit 1; }
echo "    mlx not importable OK"

echo "==> Booting container for the runtime checks"
docker run -d --name "$name" ${plat_arg[@]+"${plat_arg[@]}"} -p "$port:8001" "$image" >/dev/null

echo "==> BSMK-001: boots, binds non-loopback, health green, version baked"
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

echo "==> BSMK-002: serves the built SPA shell and falls back on a deep link"
index="$(curl -fsS "http://127.0.0.1:$port/" 2>/dev/null || true)"
echo "$index" | grep -q '<div id="root">' || { echo "FAIL: / is not the SPA shell"; exit 1; }
echo "$index" | grep -q '/assets/' || { echo "FAIL: / lacks a built /assets reference"; exit 1; }
deep="$(curl -fsS "http://127.0.0.1:$port/graph/deadbeef" 2>/dev/null || true)"
echo "$deep" | grep -q '<div id="root">' || { echo "FAIL: deep link did not fall back to the SPA shell"; exit 1; }
echo "    SPA index + deep-link fallback OK"

docker image inspect "$image" --format '==> Image size: {{.Size}} bytes ({{len .RootFS.Layers}} layers)' || true
echo "ALL SMOKE CHECKS PASSED"
