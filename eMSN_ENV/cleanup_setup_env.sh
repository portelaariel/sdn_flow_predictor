#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="${SDN_RUNTIME_CONFIG:-$PROJECT_ROOT/config/runtime.env}"

if [[ ! -r "$CONFIG_FILE" ]]; then
  echo "Runtime config not found: $CONFIG_FILE" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$CONFIG_FILE"

REMOVE_ALL=false
if [[ "${1:-}" == "--all" ]]; then
  REMOVE_ALL=true
elif [[ -n "${1:-}" ]]; then
  echo "Usage: $0 [--all]" >&2
  exit 2
fi

is_project_container() {
  [[ "$1" =~ ^(etcd[0-9]+|ryu-core-[0-9]+|simple-switch-[0-9]+|flow-blocker-[0-9]+|flow-predictor-[0-9]+)$ ]]
}

is_project_network() {
  case "$1" in
    "$ETCD_NET"|"$RYU_NETWORK_PREFIX")
      return 0
      ;;
    "$RYU_NETWORK_PREFIX"-*)
      local suffix="${1#"$RYU_NETWORK_PREFIX"-}"
      [[ "$suffix" =~ ^[0-9]+$ ]]
      return
      ;;
  esac
  return 1
}

if [[ "$REMOVE_ALL" == "true" ]]; then
  cleanup_scope="all"
else
  cleanup_scope="project"
fi

echo "Removing ${cleanup_scope} containers..."
while IFS= read -r name; do
  [[ -n "$name" ]] || continue
  if [[ "$REMOVE_ALL" == "true" ]] || is_project_container "$name"; then
    echo "  container: $name"
    sudo docker rm -f "$name" >/dev/null
  fi
done < <(sudo docker ps -a --format '{{.Names}}')

echo "Removing ${cleanup_scope} Docker networks..."
while IFS= read -r name; do
  [[ -n "$name" ]] || continue
  if [[ "$REMOVE_ALL" == "true" ]] || is_project_network "$name"; then
    echo "  network: $name"
    sudo docker network rm "$name" >/dev/null 2>&1 || true
  fi
done < <(sudo docker network ls --format '{{.Name}}')

if command -v mn >/dev/null 2>&1; then
  echo "Cleaning Mininet resources..."
  sudo mn -c || true
else
  echo "Mininet is not installed; skipping mn -c."
fi

echo "Cleanup complete."
