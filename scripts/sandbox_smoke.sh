#!/usr/bin/env bash
# =============================================================================
# sandbox_smoke.sh — prove the untrusted-code sandbox image works AND that its
# network isolation is real.
#
# The image runs untrusted repository code, so it is started exactly the way the
# orchestrator starts it: no network, memory/CPU/PID capped, read-only root
# filesystem with a writable /tmp. Two assertions:
#
#   1. a trivial command runs successfully inside the sandbox;
#   2. network egress is DENIED. If egress succeeds the script exits non-zero —
#      a sandbox that can reach the network is a security regression.
#
# Usage:
#   scripts/sandbox_smoke.sh                 # build the image if needed, then run
#   scripts/sandbox_smoke.sh --no-build      # use the image as-is
#   scripts/sandbox_smoke.sh --build-only
#
# Environment:
#   SCOUT_SANDBOX_IMAGE (default: scout-sandbox-python:3.12)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

IMAGE="${SCOUT_SANDBOX_IMAGE:-scout-sandbox-python:3.12}"
DOCKERFILE="${REPO_ROOT}/infra/docker/sandbox-python.Dockerfile"
MEMORY="512m"
CPUS="1"
PIDS_LIMIT="256"
PROBE_HOST="${SANDBOX_PROBE_HOST:-1.1.1.1}"
PROBE_PORT="${SANDBOX_PROBE_PORT:-443}"
COMMAND_TIMEOUT="${SANDBOX_COMMAND_TIMEOUT_SECONDS:-120}"
NETWORK_PROBE_TIMEOUT="${SANDBOX_NETWORK_PROBE_TIMEOUT_SECONDS:-15}"

BUILD=true
BUILD_ONLY=false
for arg in "$@"; do
    case "${arg}" in
        --no-build) BUILD=false ;;
        --build-only) BUILD_ONLY=true ;;
        -h|--help)
            sed -n '2,20p' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: ${arg}" >&2
            exit 2
            ;;
    esac
done

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed or not on PATH." >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "ERROR: cannot talk to the Docker daemon. Is Docker running?" >&2
    exit 1
fi

if [[ ! -f "${DOCKERFILE}" ]]; then
    echo "ERROR: sandbox Dockerfile not found: ${DOCKERFILE}" >&2
    exit 1
fi

echo "==> Sandbox image: ${IMAGE}"

if [[ "${BUILD}" == "true" ]]; then
    echo "==> Building ${IMAGE} from ${DOCKERFILE}"
    docker build \
        --file "${DOCKERFILE}" \
        --tag "${IMAGE}" \
        "${REPO_ROOT}"
fi

if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    echo "ERROR: image '${IMAGE}' is not present. Re-run without --no-build." >&2
    exit 1
fi

if [[ "${BUILD_ONLY}" == "true" ]]; then
    echo "Build-only requested; skipping smoke assertions."
    exit 0
fi

# Shared hardening flags — the default untrusted-code posture.
run_in_sandbox() {
    docker run --rm \
        --network none \
        --memory "${MEMORY}" \
        --cpus "${CPUS}" \
        --pids-limit "${PIDS_LIMIT}" \
        --read-only \
        --tmpfs /tmp:rw,noexec,nosuid,size=64m \
        --workdir /workspace \
        --env "PROBE_HOST=${PROBE_HOST}" \
        --env "PROBE_PORT=${PROBE_PORT}" \
        --env "SANDBOX_NETWORK_PROBE_TIMEOUT_SECONDS=${NETWORK_PROBE_TIMEOUT}" \
        "${IMAGE}" \
        "$@"
}

echo "==> Assertion 1/2: a trivial command runs inside the sandbox"
if ! run_in_sandbox /bin/sh -c 'echo "sandbox-ok: $(python --version 2>&1) node $(node --version 2>&1) git $(git --version | awk "{print \$3}")"'; then
    echo "ERROR: the sandbox could not run a trivial command." >&2
    exit 1
fi

# /tmp must be writable even though the root filesystem is read-only.
if ! run_in_sandbox /bin/sh -c 'printf hello > /tmp/probe.txt && cat /tmp/probe.txt | grep -q hello'; then
    echo "ERROR: /tmp is not writable inside the sandbox." >&2
    exit 1
fi
echo "    trivial command OK; /tmp writable"

echo "==> Assertion 2/2: network egress is denied (--network none)"

# Probe with the Python standard library: no extra packages, and python is
# guaranteed to be present in this image (it is a python:3.12-slim derivative).
# Exit status 0 therefore means "egress succeeded", which is a FAILURE here.
NETWORK_PROBE='python - <<'"'"'PY'"'"'
import os, socket, sys

host = os.environ["PROBE_HOST"]
port = int(os.environ["PROBE_PORT"])
timeout = float(os.environ.get("SANDBOX_NETWORK_PROBE_TIMEOUT_SECONDS", "15"))

try:
    socket.create_connection((host, port), timeout=timeout).close()
except OSError as exc:
    print(f"egress-denied: {type(exc).__name__}: {exc}")
    sys.exit(1)

print("EGRESS SUCCEEDED - SANDBOX IS NOT ISOLATED")
sys.exit(0)
PY'

echo "    probe: python socket -> ${PROBE_HOST}:${PROBE_PORT}"

set +e
run_in_sandbox /bin/sh -c "${NETWORK_PROBE}"
probe_status=$?
set -e

if [[ ${probe_status} -eq 0 ]]; then
    echo "ERROR: network egress SUCCEEDED inside '--network none'." >&2
    echo "       The sandbox is not isolated; refuse to run repository code." >&2
    exit 1
fi

echo "    egress denied as expected (probe exit ${probe_status})"
echo "Sandbox smoke test passed: untrusted code runs, network is isolated."
