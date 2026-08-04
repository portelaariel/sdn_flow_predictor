#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash scripts/run_collaborative_benchmark.sh MODE [SCENARIO] [--allow-mitigation]

MODE:
  local-dry-run          Detectores independentes; mitigação apenas simulada
  collaborative-dry-run Consenso MCDA; mitigação apenas simulada
  collaborative-live    Consenso MCDA; DROP real (exige --allow-mitigation)

SCENARIO:
  benign                 Vazão UDP estável, sem salto de ataque
  ddos                   Baseline UDP seguido por salto volumétrico (default)

O runner encerra qualquer topologia Mininet ativa com `mn -c`, recria a
topologia 2x2, reimplanta os FlowPredictors e grava resultados compactos em
experiments/results/. Configure taxas/durações pelas variáveis BENCHMARK_*.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -eq 0 ]]; then
  usage
  exit 0
fi

MODE="$1"
SCENARIO="${2:-ddos}"
CONFIRMATION="${3:-}"
case "$MODE" in
  local-dry-run)
    COLLABORATION=false
    DRY_RUN=true
    ;;
  collaborative-dry-run)
    COLLABORATION=true
    DRY_RUN=true
    ;;
  collaborative-live)
    COLLABORATION=true
    DRY_RUN=false
    if [[ "$CONFIRMATION" != "--allow-mitigation" ]]; then
      echo "collaborative-live exige --allow-mitigation" >&2
      exit 2
    fi
    ;;
  *)
    echo "modo inválido: $MODE" >&2
    usage >&2
    exit 2
    ;;
esac
if [[ "$SCENARIO" != "benign" && "$SCENARIO" != "ddos" ]]; then
  echo "cenário inválido: $SCENARIO" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="${SDN_RUNTIME_CONFIG:-$PROJECT_ROOT/config/runtime.env}"
# shellcheck disable=SC1090
source "$CONFIG_FILE"

CSETS="${BENCHMARK_CSETS:-2}"
SPER="${BENCHMARK_SPER:-2}"
SOURCE_HOST="${BENCHMARK_SOURCE_HOST:-h1}"
DESTINATION_HOST="${BENCHMARK_DESTINATION_HOST:-h8}"
BASELINE_RATE="${BENCHMARK_BASELINE_RATE:-1M}"
ATTACK_RATE="${BENCHMARK_ATTACK_RATE:-100M}"
BASELINE_DURATION_S="${BENCHMARK_BASELINE_DURATION_S:-12}"
ATTACK_DURATION_S="${BENCHMARK_ATTACK_DURATION_S:-20}"
SETTLE_S="${BENCHMARK_SETTLE_S:-4}"
POLL_S="${BENCHMARK_POLL_S:-0.5}"
BUILD_IMAGE="${BENCHMARK_BUILD_IMAGE:-true}"
EXPORT_HISTORY="${BENCHMARK_EXPORT_HISTORY:-false}"
RESULTS_ROOT="${BENCHMARK_RESULTS_ROOT:-$PROJECT_ROOT/experiments/results}"
MODEL_PATH="${PREDICTOR_OFFLINE_MODEL:-$PROJECT_ROOT/models/cic2019-drddos-udp-holt.json}"

for integer in "$CSETS" "$SPER" "$BASELINE_DURATION_S" "$ATTACK_DURATION_S" "$SETTLE_S"; do
  [[ "$integer" =~ ^[1-9][0-9]*$ ]] || {
    echo "quantidades/durações devem ser inteiros positivos" >&2
    exit 2
  }
done
for host in "$SOURCE_HOST" "$DESTINATION_HOST"; do
  [[ "$host" =~ ^h[1-9][0-9]*$ ]] || { echo "host inválido: $host" >&2; exit 2; }
done
for rate in "$BASELINE_RATE" "$ATTACK_RATE"; do
  [[ "$rate" =~ ^[1-9][0-9]*([KMG])?$ ]] || { echo "taxa iperf inválida: $rate" >&2; exit 2; }
done
[[ "$BUILD_IMAGE" == "true" || "$BUILD_IMAGE" == "false" ]] || exit 2
[[ "$EXPORT_HISTORY" == "true" || "$EXPORT_HISTORY" == "false" ]] || exit 2
[[ -r "$MODEL_PATH" ]] || { echo "modelo offline não encontrado: $MODEL_PATH" >&2; exit 2; }

TOTAL_HOSTS=$((CSETS * SPER * 2))
SRC_INDEX="${SOURCE_HOST#h}"
DST_INDEX="${DESTINATION_HOST#h}"
if (( SRC_INDEX > TOTAL_HOSTS || DST_INDEX > TOTAL_HOSTS )); then
  echo "topologia ${CSETS}x${SPER} possui h1..h${TOTAL_HOSTS}" >&2
  exit 2
fi
SOURCE_IP="10.0.0.${SRC_INDEX}"
DESTINATION_IP="10.0.0.${DST_INDEX}"
FLOW="${SOURCE_IP}->${DESTINATION_IP}"

for command in python3 curl jq sudo git docker mn ovs-ofctl iperf3 sha256sum; do
  command -v "$command" >/dev/null || { echo "comando obrigatório ausente: $command" >&2; exit 2; }
done

# Um claim da execução anterior impediria uma nova eleição para o mesmo fluxo.
# Falhar cedo é mais reprodutível do que aguardar silenciosamente ou apagar
# coordenação que ainda pode pertencer a um experimento ativo.
if [[ "$COLLABORATION" == "true" ]]; then
  NOW_NS="$(date +%s%N)"
  MAX_CLAIM_EXPIRY=0
  for ((i=0; i<CSETS; i++)); do
    port=$((PREDICTOR_PORT_BASE + i))
    expiry="$(curl -fsS "http://127.0.0.1:${port}/predictor/collaboration" 2>/dev/null |
      jq -r --arg flow "$FLOW" \
        '[.decisions[]? | select(.flow == $flow) | .claim.expires_ns // 0] | max // 0' \
      2>/dev/null || echo 0)"
    if [[ "$expiry" =~ ^[0-9]+$ ]] && (( expiry > MAX_CLAIM_EXPIRY )); then
      MAX_CLAIM_EXPIRY="$expiry"
    fi
  done
  if (( MAX_CLAIM_EXPIRY > NOW_NS )); then
    WAIT_S=$(((MAX_CLAIM_EXPIRY - NOW_NS + 999999999) / 1000000000))
    echo "claim colaborativo ainda ativo para $FLOW; aguarde ${WAIT_S}s e repita" >&2
    exit 2
  fi
fi

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-${MODE}-${SCENARIO}"
OUTDIR="$RESULTS_ROOT/$RUN_ID"
mkdir -p "$OUTDIR"
OUTDIR="$(cd "$OUTDIR" && pwd)"
COMMANDS_FILE="$(mktemp "${TMPDIR:-/tmp}/flow-benchmark.XXXXXX")"
STOP_FILE="$OUTDIR/monitor.stop"
MONITOR_PID=""

cleanup() {
  if [[ -n "$MONITOR_PID" ]] && kill -0 "$MONITOR_PID" 2>/dev/null; then
    touch "$STOP_FILE"
    wait "$MONITOR_PID" || true
  fi
  rm -f "$COMMANDS_FILE"
}
trap cleanup EXIT

echo "[benchmark] modo=$MODE cenário=$SCENARIO fluxo=$FLOW saída=$OUTDIR"
echo "[benchmark] limpando topologia Mininet anterior"
sudo mn -c >/dev/null 2>&1 || true

if [[ "$BUILD_IMAGE" == "true" ]]; then
  echo "[benchmark] construindo $PRED_IMG a partir do commit atual"
  sudo docker build -t "$PRED_IMG" -f "$PROJECT_ROOT/Dockerfile.flow_predictor" "$PROJECT_ROOT"
fi

echo "[benchmark] implantando preditores (collaboration=$COLLABORATION dry_run=$DRY_RUN)"
PREDICTOR_OFFLINE_MODEL="$MODEL_PATH" \
PREDICTOR_OFFLINE_MODEL_REQUIRED=true \
PREDICTOR_ONLINE_MODEL_ADAPTATION=false \
PREDICTOR_COLLABORATION_ENABLED="$COLLABORATION" \
PREDICTOR_COLLAB_EXPECTED_DOMAINS="$CSETS" \
PREDICTOR_COLLAB_MIN_DOMAINS=2 \
PREDICTOR_EXPORT_ENABLED="$EXPORT_HISTORY" \
  bash "$PROJECT_ROOT/deploy_flow_predictor.sh" "$CSETS" "$DRY_RUN"

ENDPOINTS=""
for ((i=0; i<CSETS; i++)); do
  port=$((PREDICTOR_PORT_BASE + i))
  ENDPOINTS+="${ENDPOINTS:+,}http://127.0.0.1:${port}"
done

RUN_STARTED_ISO="$(date -Iseconds)"
RUN_STARTED_NS="$(date +%s%N)"
GIT_COMMIT="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"
MODEL_SHA256="$(sha256sum "$MODEL_PATH" | awk '{print $1}')"

RUN_METADATA_PATH="$OUTDIR/metadata.json" \
RUN_MODE="$MODE" RUN_SCENARIO="$SCENARIO" RUN_FLOW="$FLOW" \
RUN_SOURCE_HOST="$SOURCE_HOST" RUN_DESTINATION_HOST="$DESTINATION_HOST" \
RUN_BASELINE_RATE="$BASELINE_RATE" RUN_ATTACK_RATE="$ATTACK_RATE" \
RUN_BASELINE_DURATION="$BASELINE_DURATION_S" RUN_ATTACK_DURATION="$ATTACK_DURATION_S" \
RUN_CSETS="$CSETS" RUN_SPER="$SPER" RUN_STARTED_NS="$RUN_STARTED_NS" \
RUN_GIT_COMMIT="$GIT_COMMIT" RUN_MODEL_SHA256="$MODEL_SHA256" \
python3 - <<'PY'
import json
import os
from pathlib import Path

payload = {
    "mode": os.environ["RUN_MODE"],
    "scenario": os.environ["RUN_SCENARIO"],
    "flow": os.environ["RUN_FLOW"],
    "source_host": os.environ["RUN_SOURCE_HOST"],
    "destination_host": os.environ["RUN_DESTINATION_HOST"],
    "baseline_rate": os.environ["RUN_BASELINE_RATE"],
    "attack_rate": os.environ["RUN_ATTACK_RATE"],
    "baseline_duration_s": int(os.environ["RUN_BASELINE_DURATION"]),
    "attack_duration_s": int(os.environ["RUN_ATTACK_DURATION"]),
    "controller_sets": int(os.environ["RUN_CSETS"]),
    "switches_per_set": int(os.environ["RUN_SPER"]),
    "started_ns": int(os.environ["RUN_STARTED_NS"]),
    "git_commit": os.environ["RUN_GIT_COMMIT"],
    "model_sha256": os.environ["RUN_MODEL_SHA256"],
}
Path(os.environ["RUN_METADATA_PATH"]).write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

MONITOR_DURATION_S=$((BASELINE_DURATION_S + ATTACK_DURATION_S + SETTLE_S + 30))
python3 "$PROJECT_ROOT/experiments/monitor_predictors.py" \
  --endpoints "$ENDPOINTS" \
  --flow "$FLOW" \
  --output "$OUTDIR" \
  --interval-s "$POLL_S" \
  --duration-s "$MONITOR_DURATION_S" \
  --stop-file "$STOP_FILE" &
MONITOR_PID=$!

{
  echo "$DESTINATION_HOST iperf3 -s -D"
  echo "sh sleep 1"
  echo "$SOURCE_HOST ping -c 4 $DESTINATION_IP > $OUTDIR/ping_before.txt"
  echo "sh date +%s%N > $OUTDIR/baseline_start_ns.txt"
  echo "$SOURCE_HOST iperf3 -c $DESTINATION_IP -u -b $BASELINE_RATE -t $BASELINE_DURATION_S -J > $OUTDIR/baseline.json 2> $OUTDIR/baseline.stderr"
  echo "sh date +%s%N > $OUTDIR/baseline_end_ns.txt"
  if [[ "$SCENARIO" == "ddos" ]]; then
    echo "sh sleep 2"
    echo "sh date +%s%N > $OUTDIR/attack_start_ns.txt"
    echo "$SOURCE_HOST iperf3 -c $DESTINATION_IP -u -b $ATTACK_RATE -t $ATTACK_DURATION_S -J > $OUTDIR/attack.json 2> $OUTDIR/attack.stderr"
    echo "sh date +%s%N > $OUTDIR/attack_end_ns.txt"
  fi
  echo "sh sleep $SETTLE_S"
  echo "$SOURCE_HOST ping -c 5 $DESTINATION_IP > $OUTDIR/ping_after.txt"
  for ((sw=1; sw<=CSETS * SPER; sw++)); do
    echo "sh ovs-ofctl -O OpenFlow10 dump-flows s${sw} > $OUTDIR/ovs-flows-s${sw}.txt"
  done
  echo "exit"
} > "$COMMANDS_FILE"

echo "[benchmark] executando topologia e tráfego"
sudo env CSETS="$CSETS" SPER="$SPER" \
  python3 "$PROJECT_ROOT/eMSN_ENV/setup_mininet.py" \
  < "$COMMANDS_FILE" > "$OUTDIR/mininet.log" 2>&1

touch "$STOP_FILE"
wait "$MONITOR_PID" || true
MONITOR_PID=""
rm -f "$STOP_FILE"

for ((i=0; i<CSETS; i++)); do
  for service in flow-predictor flow-blocker simple-switch ryu-core; do
    container="${service}-${i}"
    sudo docker logs --since "$RUN_STARTED_ISO" "$container" \
      > "$OUTDIR/${container}.log" 2>&1 || true
  done
done

sudo chown -R "$(id -u):$(id -g)" "$OUTDIR" 2>/dev/null || true
python3 "$PROJECT_ROOT/experiments/summarize_benchmark.py" \
  "$OUTDIR" --output "$OUTDIR/summary"

echo "[benchmark] concluído: $OUTDIR"
