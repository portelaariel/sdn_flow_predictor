#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
RESULTS_ROOT="${AUTHORITY_DRY_RUN_RESULTS_ROOT:-$PROJECT_ROOT/experiments/results}"
PILOT_ROOT="$RESULTS_ROOT/authority-dry-run-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$PILOT_ROOT"

echo "[authority-dry-run] agente decide e disputa claim; FlowBlocker permanece desconectado"
BENCHMARK_AGENTIC_ENABLED=true \
BENCHMARK_AGENTIC_MODE=authority-dry-run \
BENCHMARK_RESULTS_ROOT="$PILOT_ROOT" \
  bash "$PROJECT_ROOT/scripts/run_collaborative_benchmark.sh" \
    collaborative-dry-run ddos

RUN_DIR="$(find "$PILOT_ROOT" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
if [[ -z "$RUN_DIR" ]]; then
  echo "benchmark não produziu diretório de execução" >&2
  exit 1
fi

python3 "$PROJECT_ROOT/experiments/evaluate_agentic_authority_run.py" \
  "$RUN_DIR" --output "$PILOT_ROOT/authority-summary.json"

echo "[authority-dry-run] concluído: $PILOT_ROOT"
