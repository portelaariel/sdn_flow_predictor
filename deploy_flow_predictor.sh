#!/usr/bin/env bash
set -euo pipefail
#
# deploy_flow_predictor.sh
# Adiciona o FlowPredictor a domínios já em execução (mesmas convenções do setup_env.sh).
#
# Convenção de endereçamento por domínio i:
#   Ryu-Core       192.168.(10+i).10   API 8080+i
#   SimpleSwitch   192.168.(10+i).20   HTTP 9090+i
#   FlowBlocker    192.168.(10+i).30   HTTP 7070+i
#   FlowPredictor  192.168.(10+i).40   HTTP 6060+i   <── NOVO
#
# Uso:
#   bash ./deploy_flow_predictor.sh <num_dominios> [dry_run:true|false]
# Exemplo:
#   bash ./deploy_flow_predictor.sh 2 true # 2 domínios, modo DRY_RUN (não bloqueia)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SDN_RUNTIME_CONFIG:-$SCRIPT_DIR/config/runtime.env}"

if [[ ! -r "$CONFIG_FILE" ]]; then
  echo "Runtime config not found: $CONFIG_FILE" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$CONFIG_FILE"

C="${1:-${CSETS:-2}}"
DRY_RUN="${2:-$PREDICTOR_DRY_RUN}"
if ! [[ "$C" =~ ^[1-9][0-9]*$ ]]; then
  echo "num_dominios must be a positive integer" >&2
  exit 2
fi
if [[ "$DRY_RUN" != "true" && "$DRY_RUN" != "false" ]]; then
  echo "dry_run must be true or false" >&2
  exit 2
fi

HISTORY_ROOT="${PREDICTION_HISTORY_ROOT:-$SCRIPT_DIR}"

log() { echo "[$(date +%H:%M:%S)] $*"; }

# Build da imagem se ausente
if ! sudo docker images --format '{{.Repository}}' | grep -qx "$PRED_IMG"; then
  log "Imagem $PRED_IMG não encontrada; construindo..."
  sudo docker build -t "$PRED_IMG" -f "$SCRIPT_DIR/Dockerfile.flow_predictor" "$SCRIPT_DIR"
fi

for ((i=0; i<C; i++)); do
  SUBNET=$((SUBNET_BASE + i))
  CTRL_IP="192.168.${SUBNET}.10"
  FB_IP="192.168.${SUBNET}.30"
  PRED_IP="192.168.${SUBNET}.40"

  CTRL_API_PORT=$((CTRL_API_PORT_BASE + i))
  FB_HTTP_PORT=$((FB_HTTP_PORT_BASE + i))
  PRED_HTTP_PORT=$((PREDICTOR_PORT_BASE + i))

  # Detecta o nome da rede do domínio (ryu-network compartilhada OU ryu-network-$i)
  if sudo docker network ls --format '{{.Name}}' | grep -qx "${RYU_NETWORK_PREFIX}-$i"; then
    NET="${RYU_NETWORK_PREFIX}-$i"
  else
    NET="$RYU_NETWORK_PREFIX"
  fi

  # Remove instância antiga, se houver
  if sudo docker ps -a --format '{{.Names}}' | grep -qx "flow-predictor-$i"; then
    log "Removendo flow-predictor-$i antigo..."
    sudo docker rm -f "flow-predictor-$i" >/dev/null
  fi

  # Diretório do host para o dataset de predição (1 CSV por fluxo)
  HIST_DIR="$HISTORY_ROOT/prediction_history_domain${i}"
  mkdir -p "$HIST_DIR"

  log "Iniciando flow-predictor-$i em $PRED_IP:$PRED_HTTP_PORT (rede: $NET, dry_run=$DRY_RUN)"
  log "  Dataset em: $HIST_DIR"
  sudo docker run -d --name "flow-predictor-$i" --network "$NET" --ip "$PRED_IP" \
    -v "$HIST_DIR:/app/prediction_history" \
    -e EXPORT_ENABLED="$PREDICTOR_EXPORT_ENABLED" \
    -e EXPORT_DIR="/app/prediction_history" \
    -e EXPORT_PREFIXES="$PREDICTOR_EXPORT_PREFIXES" \
    -e EXPORT_FLUSH_EVERY="$PREDICTOR_EXPORT_FLUSH_EVERY" \
    -e RYU_BASE_URL="http://${CTRL_IP}:${CTRL_API_PORT}" \
    -e FLOWBLOCKER_URL="http://${FB_IP}:${FB_HTTP_PORT}" \
    -e CONTROLLER_ID="$CTRL_IP" \
    -e ETCD_ENDPOINTS="$ETCD_ENDPOINTS" \
    -e PORT="$PRED_HTTP_PORT" \
    -e POLL_INTERVAL_S="$PREDICTOR_POLL_INTERVAL_S" \
    -e Z_THRESHOLD="$PREDICTOR_Z_THRESHOLD" \
    -e MIN_RATE_BPS="$PREDICTOR_MIN_RATE_BPS" \
    -e AUTO_MITIGATE="$PREDICTOR_AUTO_MITIGATE" \
    -e DRY_RUN="$DRY_RUN" \
    -e MITIGATION_COOLDOWN_S="$PREDICTOR_COOLDOWN_S" \
    -e WHITELIST_IPS="$PREDICTOR_WHITELIST_IPS" \
    -p "$PRED_HTTP_PORT:$PRED_HTTP_PORT" \
    "$PRED_IMG"

  # Conecta à rede ETCD (mesmo padrão do FlowBlocker)
  sudo docker network connect "$ETCD_NET" "flow-predictor-$i" 2>/dev/null || true

  # Readiness check
  for retry in {1..30}; do
    if curl -fsS "http://127.0.0.1:${PRED_HTTP_PORT}/predictor/status" >/dev/null 2>&1; then
      log "✅ flow-predictor-$i pronto"
      break
    fi
    sleep 1
  done
done

log ""
log "FlowPredictor implantado em $C domínio(s)."
log "Endpoints úteis:"
for ((i=0; i<C; i++)); do
  p=$((PREDICTOR_PORT_BASE + i))
  echo "  Domínio $i:"
  echo "    Status:      curl http://127.0.0.1:$p/predictor/status | jq ."
  echo "    Predições:   curl http://127.0.0.1:$p/predictor/predictions | jq ."
  echo "    Anomalias:   curl http://127.0.0.1:$p/predictor/anomalies | jq ."
  echo "    Dataset:     curl http://127.0.0.1:$p/predictor/export/status | jq ."
done
log ""
log "📊 CSVs do dataset (1 por fluxo) em: $HISTORY_ROOT/prediction_history_domain<i>/"
log ""
log "⚠️  DRY_RUN=$DRY_RUN — para ativar mitigação real:"
log "    curl -X POST http://127.0.0.1:6060/predictor/config -H 'Content-Type: application/json' -d '{\"dry_run\": false}'"
