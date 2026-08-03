#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
TEST_TMP="$(mktemp -d "${TMPDIR:-/tmp}/sdn-deploy-test.XXXXXX")"
COMMAND_LOG="$TEST_TMP/docker-run.log"
export COMMAND_LOG
trap 'rm -rf "$TEST_TMP"' EXIT

sudo() {
  [[ "${1:-}" == "docker" ]] || return 0
  shift
  case "${1:-}" in
    images)
      echo "flow_predictor_cnsm"
      ;;
    network)
      if [[ "${2:-}" == "ls" ]]; then
        echo "ryu-network-0"
      fi
      ;;
    run)
      printf '%s\n' "$*" >> "$COMMAND_LOG"
      ;;
  esac
  return 0
}

curl() {
  return 0
}

export -f sudo curl

PREDICTION_HISTORY_ROOT="$TEST_TMP/history" \
PREDICTOR_Z_THRESHOLD=5.5 \
  bash "$PROJECT_ROOT/deploy_flow_predictor.sh" 1 true >/dev/null

grep -q -- '--network ryu-network-0' "$COMMAND_LOG"
grep -q -- '--ip 192.168.10.40' "$COMMAND_LOG"
grep -q -- '-e Z_THRESHOLD=5.5' "$COMMAND_LOG"
grep -q -- '-e DRY_RUN=true' "$COMMAND_LOG"
grep -q -- '-p 6060:6060' "$COMMAND_LOG"

echo "deploy_smoke: ok"
