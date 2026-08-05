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
      elif [[ "${2:-}" == "connect" ]]; then
        printf 'network-connect %s\n' "$*" >> "$COMMAND_LOG"
        if [[ "${MOCK_ETCD_CONNECT_FAIL:-false}" == "true" ]]; then
          return 1
        fi
      fi
      ;;
    create)
      printf 'create %s\n' "$*" >> "$COMMAND_LOG"
      ;;
    start)
      printf 'start %s\n' "$*" >> "$COMMAND_LOG"
      ;;
  esac
  return 0
}

curl() {
  local url="${*: -1}"
  case "$url" in
    */predictor/status)
      printf '%s\n' '{"cid":"domain-test"}'
      ;;
    */predictor/collaboration)
      printf '%s\n' '{"requested":true,"active":true}'
      ;;
    */predictor/agent)
      printf '%s\n' '{"requested":true,"active":true,"mode":"shadow","authoritative":false}'
      ;;
    *)
      printf '%s\n' '{}'
      ;;
  esac
  return 0
}

export -f sudo curl

PREDICTION_HISTORY_ROOT="$TEST_TMP/history" \
PREDICTOR_Z_THRESHOLD=5.5 \
  bash "$PROJECT_ROOT/deploy_flow_predictor.sh" 1 true >/dev/null

grep -q -- '--network ryu-network-0' "$COMMAND_LOG"
grep -q -- '--ip 192.168.10.40' "$COMMAND_LOG"
grep -q -- '-e Z_THRESHOLD=5.5' "$COMMAND_LOG"
grep -q -- '-e OFFLINE_MODEL_REQUIRED=false' "$COMMAND_LOG"
grep -q -- '-e WARMUP_SAMPLES=15' "$COMMAND_LOG"
grep -q -- '-e ANOMALY_EVENT_COOLDOWN_S=60' "$COMMAND_LOG"
grep -q -- '-e FLOW_IDLE_RESET_SAMPLES=2' "$COMMAND_LOG"
grep -q -- '-e COLLABORATION_ENABLED=false' "$COMMAND_LOG"
grep -q -- '-e COLLAB_EXPECTED_DOMAINS=1' "$COMMAND_LOG"
grep -q -- '-e COLLAB_MIN_DOMAINS=2' "$COMMAND_LOG"
grep -q -- '-e AGENTIC_ENABLED=false' "$COMMAND_LOG"
grep -q -- '-e AGENTIC_SHADOW=true' "$COMMAND_LOG"
grep -q -- '-e AGENT_REQUIRED_VOTES=2' "$COMMAND_LOG"
grep -q -- '-e DRY_RUN=true' "$COMMAND_LOG"
grep -q -- '-p 6060:6060' "$COMMAND_LOG"

CREATE_LINE="$(grep -n '^create ' "$COMMAND_LOG" | head -n 1 | cut -d: -f1)"
CONNECT_LINE="$(grep -n '^network-connect ' "$COMMAND_LOG" | head -n 1 | cut -d: -f1)"
START_LINE="$(grep -n '^start ' "$COMMAND_LOG" | head -n 1 | cut -d: -f1)"
(( CREATE_LINE < CONNECT_LINE && CONNECT_LINE < START_LINE ))

MODEL_PATH="$TEST_TMP/ddos-holt.json"
touch "$MODEL_PATH"
MODEL_PATH="$(cd "$(dirname "$MODEL_PATH")" && pwd)/$(basename "$MODEL_PATH")"
PREDICTION_HISTORY_ROOT="$TEST_TMP/history-offline" \
PREDICTOR_OFFLINE_MODEL="$MODEL_PATH" \
PREDICTOR_OFFLINE_MODEL_REQUIRED=true \
  bash "$PROJECT_ROOT/deploy_flow_predictor.sh" 1 true >/dev/null

grep -q -- "-v $MODEL_PATH:/app/models/offline_model.json:ro" "$COMMAND_LOG"
grep -q -- '-e OFFLINE_MODEL_PATH=/app/models/offline_model.json' "$COMMAND_LOG"
grep -q -- '-e OFFLINE_MODEL_REQUIRED=true' "$COMMAND_LOG"

PREDICTION_HISTORY_ROOT="$TEST_TMP/history-collaborative" \
PREDICTOR_COLLABORATION_ENABLED=true \
PREDICTOR_COLLAB_MIN_DOMAINS=2 \
  bash "$PROJECT_ROOT/deploy_flow_predictor.sh" 2 true >/dev/null

grep -q -- '-e COLLABORATION_ENABLED=true' "$COMMAND_LOG"
grep -q -- '-e COLLAB_EXPECTED_DOMAINS=2' "$COMMAND_LOG"
grep -q -- '-e COLLAB_MIN_DOMAINS=2' "$COMMAND_LOG"

PREDICTION_HISTORY_ROOT="$TEST_TMP/history-agentic" \
PREDICTOR_COLLABORATION_ENABLED=true \
PREDICTOR_AGENTIC_ENABLED=true \
PREDICTOR_AGENTIC_SHADOW=true \
PREDICTOR_AGENT_REQUIRED_VOTES=2 \
  bash "$PROJECT_ROOT/deploy_flow_predictor.sh" 2 true >/dev/null

grep -q -- '-e AGENTIC_ENABLED=true' "$COMMAND_LOG"
grep -q -- '-e AGENTIC_SHADOW=true' "$COMMAND_LOG"
grep -q -- '-e AGENT_REQUIRED_VOTES=2' "$COMMAND_LOG"
grep -q -- '-e AGENT_PROPOSAL_THRESHOLD=0.65' "$COMMAND_LOG"

# ETCD continua opcional para o detector local, mas é requisito estrito para
# colaboração/agentes.
MOCK_ETCD_CONNECT_FAIL=true \
PREDICTION_HISTORY_ROOT="$TEST_TMP/history-local-without-etcd" \
  bash "$PROJECT_ROOT/deploy_flow_predictor.sh" 1 true >/dev/null

if MOCK_ETCD_CONNECT_FAIL=true \
  PREDICTION_HISTORY_ROOT="$TEST_TMP/history-collab-without-etcd" \
  PREDICTOR_COLLABORATION_ENABLED=true \
  bash "$PROJECT_ROOT/deploy_flow_predictor.sh" 2 true >/dev/null 2>&1; then
  echo "collaborative deploy unexpectedly succeeded without ETCD network" >&2
  exit 1
fi

echo "deploy_smoke: ok"
