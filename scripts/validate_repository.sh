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
  offline_model.py
  train_offline_model.py
  ryu_apps/emitter_cnsm.py
  rest_client/Simpleswitch_cnsm.py
  flow_blocker/flow_blocker_cnsm.py
  eMSN_ENV/setup_env.sh
  eMSN_ENV/setup_mininet.py
  deploy_flow_predictor.sh
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
expect_invalid_input bash eMSN_ENV/setup_env.sh 0 2
echo "input_validation: ok"

python3 -m unittest discover -s tests -v
bash tests/test_deploy_script.sh
