#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/run_agentic_runtime_faults.sh

Executa quatro episódios reais e seguros no testbed 2x2:
  1. flow-predictor-1 pausado durante o ataque;
  2. recuperação e novo consenso com evidências frescas;
  3. partição dos dois preditores em relação à rede ETCD;
  4. reconexão e novo consenso com evidências frescas.

O teste força shadow mode e DRY_RUN=true. Um trap sempre tenta desfazer pause e
partição de rede. Resultados ficam em experiments/results/runtime-fault-*/.

Variáveis úteis:
  RUNTIME_FAULT_BUILD_IMAGE=true|false
  RUNTIME_FAULT_BOOTSTRAP_ENV=true|false
  RUNTIME_FAULT_BASELINE_DURATION_S=8
  RUNTIME_FAULT_ATTACK_DURATION_S=16
  RUNTIME_FAULT_RECOVERY_WAIT_S=14
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ $# -ne 0 ]]; then
  usage >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="${SDN_RUNTIME_CONFIG:-$PROJECT_ROOT/config/runtime.env}"
# shellcheck disable=SC1090
source "$CONFIG_FILE"

CSETS=2
SPER=2
SOURCE_HOST="${RUNTIME_FAULT_SOURCE_HOST:-h1}"
DESTINATION_HOST="${RUNTIME_FAULT_DESTINATION_HOST:-h8}"
SOURCE_IP="10.0.0.${SOURCE_HOST#h}"
DESTINATION_IP="10.0.0.${DESTINATION_HOST#h}"
FLOW="${SOURCE_IP}->${DESTINATION_IP}"
BASELINE_RATE="${RUNTIME_FAULT_BASELINE_RATE:-1M}"
ATTACK_RATE="${RUNTIME_FAULT_ATTACK_RATE:-100M}"
BASELINE_DURATION_S="${RUNTIME_FAULT_BASELINE_DURATION_S:-8}"
ATTACK_DURATION_S="${RUNTIME_FAULT_ATTACK_DURATION_S:-16}"
SETTLE_S="${RUNTIME_FAULT_SETTLE_S:-2}"
PROPOSAL_TTL_CEIL="$(python3 -c 'import math,sys; print(math.ceil(float(sys.argv[1])))' \
  "$PREDICTOR_AGENT_PROPOSAL_TTL_S")"
MIN_ISOLATION_S=$((PROPOSAL_TTL_CEIL + 2))
RECOVERY_WAIT_S="${RUNTIME_FAULT_RECOVERY_WAIT_S:-$MIN_ISOLATION_S}"
POLL_S="${RUNTIME_FAULT_POLL_S:-0.5}"
BUILD_IMAGE="${RUNTIME_FAULT_BUILD_IMAGE:-true}"
BOOTSTRAP_ENV="${RUNTIME_FAULT_BOOTSTRAP_ENV:-true}"
RESULTS_ROOT="${RUNTIME_FAULT_RESULTS_ROOT:-$PROJECT_ROOT/experiments/results}"
MODEL_PATH="${PREDICTOR_OFFLINE_MODEL:-$PROJECT_ROOT/models/cic2019-drddos-udp-holt.json}"

for value in "$BASELINE_DURATION_S" "$ATTACK_DURATION_S" "$SETTLE_S" "$RECOVERY_WAIT_S"; do
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || {
    echo "durações devem ser inteiros positivos" >&2
    exit 2
  }
done
if (( RECOVERY_WAIT_S < MIN_ISOLATION_S || ATTACK_DURATION_S < MIN_ISOLATION_S )); then
  echo "ataque e espera de recuperação devem cobrir TTL+2 (${MIN_ISOLATION_S}s)" >&2
  exit 2
fi
for value in "$BUILD_IMAGE" "$BOOTSTRAP_ENV"; do
  [[ "$value" == "true" || "$value" == "false" ]] || {
    echo "flags de build/bootstrap devem ser true ou false" >&2
    exit 2
  }
done
for host in "$SOURCE_HOST" "$DESTINATION_HOST"; do
  [[ "$host" =~ ^h[1-8]$ ]] || { echo "host fora da topologia 2x2: $host" >&2; exit 2; }
done
SOURCE_INDEX="${SOURCE_HOST#h}"
DESTINATION_INDEX="${DESTINATION_HOST#h}"
SOURCE_DOMAIN=$((((SOURCE_INDEX + 1) / 2 - 1) / SPER))
DESTINATION_DOMAIN=$((((DESTINATION_INDEX + 1) / 2 - 1) / SPER))
if (( SOURCE_DOMAIN == DESTINATION_DOMAIN )); then
  echo "o gate requer hosts em domínios diferentes" >&2
  exit 2
fi
for rate in "$BASELINE_RATE" "$ATTACK_RATE"; do
  [[ "$rate" =~ ^[1-9][0-9]*([KMG])?$ ]] || { echo "taxa inválida: $rate" >&2; exit 2; }
done
[[ -r "$MODEL_PATH" ]] || { echo "modelo offline não encontrado: $MODEL_PATH" >&2; exit 2; }
for command in python3 curl jq sudo docker mn ovs-vsctl; do
  command -v "$command" >/dev/null || { echo "comando obrigatório ausente: $command" >&2; exit 2; }
done

RUN_ID="runtime-fault-$(date -u +%Y%m%dT%H%M%SZ)"
OUTDIR="$RESULTS_ROOT/$RUN_ID"
mkdir -p "$OUTDIR"
OUTDIR="$(cd "$OUTDIR" && pwd)"
RUN_STARTED_ISO="$(date -Iseconds)"
ACTIVE_WORKLOAD_PID=""
ACTIVE_MONITOR_PID=""
ACTIVE_STOP_FILE=""
ACTIVE_GATE_DIR=""
PREDICTOR_PAUSED=false
ETCD_DISCONNECTED_0=false
ETCD_DISCONNECTED_1=false

restore_faults() {
  if [[ "$PREDICTOR_PAUSED" == "true" ]]; then
    sudo docker unpause flow-predictor-1 >/dev/null 2>&1 || true
    PREDICTOR_PAUSED=false
  fi
  if [[ "$ETCD_DISCONNECTED_0" == "true" ]]; then
    sudo docker network connect "$ETCD_NET" flow-predictor-0 >/dev/null 2>&1 || true
    ETCD_DISCONNECTED_0=false
  fi
  if [[ "$ETCD_DISCONNECTED_1" == "true" ]]; then
    sudo docker network connect "$ETCD_NET" flow-predictor-1 >/dev/null 2>&1 || true
    ETCD_DISCONNECTED_1=false
  fi
}

cleanup() {
  local force_mininet_cleanup=false
  if [[ -n "$ACTIVE_GATE_DIR" ]]; then
    touch "$ACTIVE_GATE_DIR/abort" 2>/dev/null || true
  fi
  restore_faults
  if [[ -n "$ACTIVE_STOP_FILE" ]]; then
    touch "$ACTIVE_STOP_FILE" 2>/dev/null || true
  fi
  for pid in "$ACTIVE_WORKLOAD_PID" "$ACTIVE_MONITOR_PID"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      if [[ "$pid" == "$ACTIVE_WORKLOAD_PID" ]]; then
        force_mininet_cleanup=true
      fi
    fi
  done
  if [[ "$force_mininet_cleanup" == "true" ]]; then
    # Em algumas versões, mn -c usa killall no namespace de PIDs do host e
    # também encerra ryu-manager dentro dos containers. Só é necessário se o
    # workload foi morto antes de executar net.stop(); depois restauramos Ryu.
    sudo mn -c >/dev/null 2>&1 || true
    for ((i=0; i<CSETS; i++)); do
      sudo docker start "ryu-core-${i}" >/dev/null 2>&1 || true
    done
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

wait_http() {
  local url="$1" tries="${2:-30}"
  for ((attempt=1; attempt<=tries; attempt++)); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_tcp() {
  local host="$1" port="$2" tries="${3:-30}"
  for ((attempt=1; attempt<=tries; attempt++)); do
    if (echo > /dev/tcp/"$host"/"$port") >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_gate() {
  local ready="$1" pid="$2" timeout_s="$3"
  local deadline=$((SECONDS + timeout_s))
  while (( SECONDS < deadline )); do
    [[ -f "$ready" ]] && return 0
    if ! kill -0 "$pid" 2>/dev/null; then
      return 1
    fi
    sleep 0.2
  done
  return 1
}

echo "[runtime-fault] fluxo=$FLOW saída=$OUTDIR"
echo "[runtime-fault] segurança: agentic=shadow dry_run=true"
if [[ "$BOOTSTRAP_ENV" == "true" ]]; then
  # Seguro antes do bootstrap: quaisquer Ryu afetados serão recriados abaixo.
  sudo mn -c >/dev/null 2>&1 || true
elif sudo ovs-vsctl list-br 2>/dev/null | grep -Eq '^s[1-9][0-9]*$'; then
  echo "há uma topologia Mininet ativa; finalize-a antes de reutilizar o ambiente" >&2
  exit 1
fi

if [[ "$BUILD_IMAGE" == "true" ]]; then
  echo "[runtime-fault] construindo imagens do commit atual"
  sudo docker build -t "$PRED_IMG" -f "$PROJECT_ROOT/Dockerfile.flow_predictor" "$PROJECT_ROOT"
  if [[ "$BOOTSTRAP_ENV" == "true" ]]; then
    sudo docker build -t "$RYU_IMG" "$PROJECT_ROOT/ryu_apps"
    sudo docker build -t "$SSW_IMG" "$PROJECT_ROOT/rest_client"
    sudo docker build -t "$FB_IMG" "$PROJECT_ROOT/flow_blocker"
  fi
fi

if [[ "$BOOTSTRAP_ENV" == "true" ]]; then
  echo "[runtime-fault] reiniciando ambiente SDN"
  RUN_TEST=false RUN_PREDICTOR=false \
    bash "$PROJECT_ROOT/eMSN_ENV/setup_env.sh" "$CSETS" "$SPER" \
    2>&1 | tee "$OUTDIR/bootstrap.log"
else
  echo "[runtime-fault] reutilizando ambiente SDN existente"
fi

# O setup considera o HTTP suficiente e seus checks são best-effort. Para o
# Mininet, porém, o listener OpenFlow é obrigatório. Usamos as portas publicadas
# no host para não depender de uma rota direta host -> bridge Docker, que pode
# ser bloqueada por firewall mesmo quando os containers estão saudáveis.
preflight_openflow() {
  local label="$1"
  echo "[runtime-fault] validando OpenFlow antes de $label"
  for ((i=0; i<CSETS; i++)); do
    local controller_port=$((CTRL_OF_PORT_BASE + i))
    local controller_api_port=$((CTRL_API_PORT_BASE + i))
    if ! wait_tcp 127.0.0.1 "$controller_port" 30; then
      sudo docker logs --tail 120 "ryu-core-${i}" \
        > "$OUTDIR/ryu-core-${i}-openflow-preflight.log" 2>&1 || true
      echo "Ryu do domínio $i não escuta OpenFlow em 127.0.0.1:${controller_port}" >&2
      echo "consulte $OUTDIR/ryu-core-${i}-openflow-preflight.log" >&2
      return 1
    fi
    wait_http "http://127.0.0.1:${controller_api_port}/stats/switches" 30 || {
      echo "API Ryu indisponível na porta ${controller_api_port}" >&2
      return 1
    }
  done
}

preflight_openflow deploy

PREDICTOR_OFFLINE_MODEL="$MODEL_PATH" \
PREDICTOR_OFFLINE_MODEL_REQUIRED=true \
PREDICTOR_ONLINE_MODEL_ADAPTATION=false \
PREDICTOR_COLLABORATION_ENABLED=true \
PREDICTOR_COLLAB_EXPECTED_DOMAINS=2 \
PREDICTOR_COLLAB_MIN_DOMAINS=2 \
PREDICTOR_AGENTIC_ENABLED=true \
PREDICTOR_AGENTIC_SHADOW=true \
PREDICTOR_AGENT_REQUIRED_VOTES=2 \
PREDICTOR_EXPORT_ENABLED=false \
  bash "$PROJECT_ROOT/deploy_flow_predictor.sh" 2 true

for port in "$PREDICTOR_PORT_BASE" "$((PREDICTOR_PORT_BASE + 1))"; do
  wait_http "http://127.0.0.1:${port}/predictor/agent" 30 || {
    echo "agente indisponível na porta $port" >&2
    exit 1
  }
  curl -fsS "http://127.0.0.1:${port}/predictor/agent" |
    jq -e '.active == true and .mode == "shadow" and .authoritative == false' >/dev/null || {
      echo "invariante shadow inválida na porta $port" >&2
      exit 1
    }
done

cat > "$OUTDIR/metadata.json" <<EOF
{
  "flow": "$FLOW",
  "source_host": "$SOURCE_HOST",
  "destination_host": "$DESTINATION_HOST",
  "agentic_mode": "shadow",
  "dry_run": true,
  "proposal_ttl_s": $PREDICTOR_AGENT_PROPOSAL_TTL_S,
  "recovery_wait_s": $RECOVERY_WAIT_S
}
EOF

run_episode() {
  local name="$1" fault="$2"
  local episode="$OUTDIR/$name"
  local gate="$episode/gate"
  local stop_file="$episode/monitor.stop"
  mkdir -p "$gate"
  # O workload anterior sempre chama net.stop(). Não usar mn -c aqui: no host
  # do testbed ele também mata ryu-manager dentro dos containers Docker.
  preflight_openflow "$name"
  echo "[runtime-fault] episódio=$name falha=$fault"

  python3 "$PROJECT_ROOT/experiments/monitor_predictors.py" \
    --endpoints "http://127.0.0.1:${PREDICTOR_PORT_BASE},http://127.0.0.1:$((PREDICTOR_PORT_BASE + 1))" \
    --flow "$FLOW" \
    --output "$episode" \
    --interval-s "$POLL_S" \
    --duration-s "$((BASELINE_DURATION_S + ATTACK_DURATION_S + 180))" \
    --stop-file "$stop_file" &
  ACTIVE_MONITOR_PID=$!
  ACTIVE_STOP_FILE="$stop_file"
  ACTIVE_GATE_DIR="$gate"

  set +e
  sudo env CSETS="$CSETS" SPER="$SPER" MININET_CONTROLLER_HOST=127.0.0.1 \
    python3 "$PROJECT_ROOT/experiments/run_mininet_workload.py" \
    --csets "$CSETS" \
    --sper "$SPER" \
    --source-host "$SOURCE_HOST" \
    --destination-host "$DESTINATION_HOST" \
    --destination-ip "$DESTINATION_IP" \
    --domain-table-url "http://127.0.0.1:${FB_HTTP_PORT_BASE}/flowblocker/domain_table" \
    --scenario ddos \
    --baseline-rate "$BASELINE_RATE" \
    --attack-rate "$ATTACK_RATE" \
    --baseline-duration-s "$BASELINE_DURATION_S" \
    --attack-duration-s "$ATTACK_DURATION_S" \
    --settle-s "$SETTLE_S" \
    --attack-gate-dir "$gate" \
    --output "$episode" \
    > "$episode/mininet.log" 2>&1 &
  ACTIVE_WORKLOAD_PID=$!
  set -e

  if ! wait_gate "$gate/attack.ready.json" "$ACTIVE_WORKLOAD_PID" 120; then
    echo "workload não alcançou o gate do ataque em $name" >&2
    touch "$gate/abort"
    wait "$ACTIVE_WORKLOAD_PID" || true
    return 1
  fi

  for port in "$PREDICTOR_PORT_BASE" "$((PREDICTOR_PORT_BASE + 1))"; do
    curl -fsS "http://127.0.0.1:${port}/predictor/agent" \
      > "$episode/agent-before-${port}.json"
  done
  date +%s%N > "$episode/fault_applied_ns.txt"
  case "$fault" in
    missing-agent)
      sudo docker pause flow-predictor-1 >/dev/null
      PREDICTOR_PAUSED=true
      ;;
    etcd-partition)
      sudo docker network disconnect "$ETCD_NET" flow-predictor-0
      ETCD_DISCONNECTED_0=true
      sudo docker network disconnect "$ETCD_NET" flow-predictor-1
      ETCD_DISCONNECTED_1=true
      ;;
    none)
      ;;
    *)
      echo "falha interna desconhecida: $fault" >&2
      return 1
      ;;
  esac
  touch "$gate/attack.release"

  set +e
  wait "$ACTIVE_WORKLOAD_PID"
  local workload_exit=$?
  set -e
  ACTIVE_WORKLOAD_PID=""
  touch "$stop_file"
  wait "$ACTIVE_MONITOR_PID" || true
  ACTIVE_MONITOR_PID=""
  ACTIVE_STOP_FILE=""
  ACTIVE_GATE_DIR=""
  date +%s%N > "$episode/fault_ended_ns.txt"
  restore_faults
  sudo chown -R "$(id -u):$(id -g)" "$episode" 2>/dev/null || true

  if (( workload_exit != 0 )); then
    echo "workload inválido no episódio $name; veja $episode/mininet.log" >&2
    return 1
  fi
  for port in "$PREDICTOR_PORT_BASE" "$((PREDICTOR_PORT_BASE + 1))"; do
    wait_http "http://127.0.0.1:${port}/predictor/agent" 30 || {
      echo "agente não recuperou HTTP na porta $port" >&2
      return 1
    }
  done
}

run_episode missing-agent missing-agent
echo "[runtime-fault] aguardando expiração das propostas do agente ausente"
sleep "$RECOVERY_WAIT_S"
run_episode missing-agent-recovery none
echo "[runtime-fault] aguardando isolamento de episódio antes da partição ETCD"
sleep "$RECOVERY_WAIT_S"
run_episode etcd-partition etcd-partition
echo "[runtime-fault] aguardando expiração das propostas anteriores à partição"
sleep "$RECOVERY_WAIT_S"
run_episode etcd-recovery none

: > "$OUTDIR/flowblocker-requests.log"
for ((i=0; i<CSETS; i++)); do
  sudo docker logs --since "$RUN_STARTED_ISO" "flow-blocker-${i}" 2>&1 |
    grep 'Service request to block traffic' >> "$OUTDIR/flowblocker-requests.log" || true
done
sudo chown -R "$(id -u):$(id -g)" "$OUTDIR" 2>/dev/null || true

python3 "$PROJECT_ROOT/experiments/evaluate_agentic_runtime_faults.py" \
  "$OUTDIR" --flow "$FLOW" --expected-domains 2

echo "[runtime-fault] concluído: $OUTDIR"
