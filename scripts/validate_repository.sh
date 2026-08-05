#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

python3 - <<'PY'
import ast
from pathlib import Path

files = sorted(Path(".").rglob("*.py"))
for path in files:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
print(f"python_ast: {len(files)} files ok")
PY

while IFS= read -r script; do
  bash -n "$script"
done < <(find . -type f -name '*.sh' -not -path './.git/*' | sort)
echo "shell_syntax: ok"

required_files=(
  config/runtime.env
  Dockerfile.flow_predictor
  flow_predictor_cnsm.py
  agent_protocol.py
  domain_agent.py
  collaborative_decision.py
  offline_model.py
  train_offline_model.py
  prepare_cicddos2019.py
  evaluate_offline_model.py
  ryu_apps/emitter_cnsm.py
  rest_client/Simpleswitch_cnsm.py
  flow_blocker/flow_blocker_cnsm.py
  eMSN_ENV/setup_env.sh
  eMSN_ENV/setup_mininet.py
  deploy_flow_predictor.sh
  experiments/monitor_predictors.py
  experiments/run_mininet_workload.py
  experiments/summarize_benchmark.py
  scripts/run_collaborative_benchmark.sh
)
for path in "${required_files[@]}"; do
  [[ -f "$path" ]] || { echo "missing required file: $path" >&2; exit 1; }
done
echo "active_files: ok"

# shellcheck disable=SC1091
source config/runtime.env
[[ "$CTRL_API_PORT_BASE" == "8080" ]]
[[ "$FB_HTTP_PORT_BASE" == "7070" ]]
[[ "$PREDICTOR_PORT_BASE" == "6060" ]]
[[ "$ETCD_ENDPOINTS" == *"192.168.253.11:2379"* ]]
[[ "$PREDICTOR_OFFLINE_MODEL_REQUIRED" == "false" ]]
[[ "$PREDICTOR_ONLINE_MODEL_ADAPTATION" == "false" ]]
[[ "$PREDICTOR_EVENT_COOLDOWN_S" == "60" ]]
[[ "$PREDICTOR_FLOW_IDLE_RESET_SAMPLES" == "2" ]]
[[ "$PREDICTOR_COLLABORATION_ENABLED" == "false" ]]
[[ -z "$PREDICTOR_COLLAB_EXPECTED_DOMAINS" ]]
[[ "$PREDICTOR_COLLAB_MIN_DOMAINS" == "2" ]]
[[ "$PREDICTOR_AGENTIC_ENABLED" == "false" ]]
[[ "$PREDICTOR_AGENTIC_SHADOW" == "true" ]]
[[ "$PREDICTOR_AGENT_REQUIRED_VOTES" == "2" ]]
[[ "$PREDICTOR_AGENT_PROPOSAL_THRESHOLD" == "0.65" ]]

override_config="$(ETCD_SUBNET=250 ETCD_NODES=2 bash -c '
  source config/runtime.env
  printf "%s|%s" "$INITIAL_CLUSTER" "$ETCD_ENDPOINTS"
')"
[[ "$override_config" == \
  "etcd1=http://192.168.250.11:2380,etcd2=http://192.168.250.12:2380|192.168.250.11:2379,192.168.250.12:2379" ]]
echo "runtime_config: ok"

expect_invalid_input() {
  set +e
  "$@" >/dev/null 2>&1
  local status=$?
  set -e
  [[ "$status" -eq 2 ]]
}

expect_invalid_input bash deploy_flow_predictor.sh 0 true
expect_invalid_input bash deploy_flow_predictor.sh 2 invalid
expect_invalid_input env PREDICTOR_COLLABORATION_ENABLED=true \
  PREDICTOR_COLLAB_MIN_DOMAINS=3 bash deploy_flow_predictor.sh 2 true
expect_invalid_input env PREDICTOR_FLOW_IDLE_RESET_SAMPLES=0 \
  bash deploy_flow_predictor.sh 2 true
expect_invalid_input env PREDICTOR_AGENTIC_ENABLED=true \
  PREDICTOR_COLLABORATION_ENABLED=false bash deploy_flow_predictor.sh 2 true
expect_invalid_input env PREDICTOR_AGENTIC_ENABLED=true \
  PREDICTOR_COLLABORATION_ENABLED=true PREDICTOR_AGENTIC_SHADOW=false \
  bash deploy_flow_predictor.sh 2 true
expect_invalid_input env PREDICTOR_AGENTIC_ENABLED=true \
  PREDICTOR_COLLABORATION_ENABLED=true PREDICTOR_AGENT_REQUIRED_VOTES=3 \
  bash deploy_flow_predictor.sh 2 true
expect_invalid_input bash eMSN_ENV/setup_env.sh 0 2
expect_invalid_input bash scripts/run_collaborative_benchmark.sh invalid ddos
expect_invalid_input bash scripts/run_collaborative_benchmark.sh collaborative-live ddos
expect_invalid_input env BENCHMARK_AGENTIC_ENABLED=true \
  bash scripts/run_collaborative_benchmark.sh local-dry-run ddos
echo "input_validation: ok"

python3 -m unittest discover -s tests -v
bash tests/test_deploy_script.sh
