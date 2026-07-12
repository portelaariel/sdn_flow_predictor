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
#   sudo ./deploy_flow_predictor.sh <num_dominios> [dry_run:true|false]
# Exemplo:
#   sudo ./deploy_flow_predictor.sh 2 true      # 2 domínios, modo DRY_RUN (não bloqueia)

C=${1:-2}
DRY_RUN=${2:-true}

SUBNET_BASE=10
API_PORT_BASE=8080
BLOCKER_PORT_BASE=7070
PREDICTOR_PORT_BASE=6060

ETCD_ENDPOINTS="192.168.253.11:2379,192.168.253.12:2379,192.168.253.13:2379"
PRED_IMG=flow_predictor_cnsm

log() { echo "[$(date +%H:%M:%S)] $*"; }

# Build da imagem se ausente
if ! sudo docker images --format '{{.Repository}}' | grep -qx "$PRED_IMG"; then
  log "Imagem $PRED_IMG não encontrada; construindo..."
  sudo docker build -t "$PRED_IMG" -f Dockerfile.flow_predictor .
fi

for ((i=0; i<C; i++)); do
  SUBNET=$((SUBNET_BASE + i))
  CTRL_IP="192.168.${SUBNET}.10"
  FB_IP="192.168.${SUBNET}.30"
  PRED_IP="192.168.${SUBNET}.40"

  CTRL_API_PORT=$((API_PORT_BASE + i))
  FB_HTTP_PORT=$((BLOCKER_PORT_BASE + i))
  PRED_HTTP_PORT=$((PREDICTOR_PORT_BASE + i))

  # Detecta o nome da rede do domínio (ryu-network compartilhada OU ryu-network-$i)
  if sudo docker network ls --format '{{.Name}}' | grep -qx "ryu-network-$i"; then
    NET="ryu-network-$i"
  else
    NET="ryu-network"
  fi

  # Remove instância antiga, se houver
  if sudo docker ps -a --format '{{.Names}}' | grep -qx "flow-predictor-$i"; then
    log "Removendo flow-predictor-$i antigo..."
    sudo docker rm -f "flow-predictor-$i" >/dev/null
  fi

  # Diretório do host para o dataset de predição (1 CSV por fluxo)
  HIST_DIR="$(pwd)/prediction_history_domain${i}"
  mkdir -p "$HIST_DIR"

  log "Iniciando flow-predictor-$i em $PRED_IP:$PRED_HTTP_PORT (rede: $NET, dry_run=$DRY_RUN)"
  log "  Dataset em: $HIST_DIR"
  sudo docker run -d --name "flow-predictor-$i" --network "$NET" --ip "$PRED_IP" \
    -v "$HIST_DIR:/app/prediction_history" \
    -e EXPORT_ENABLED="true" \
    -e EXPORT_DIR="/app/prediction_history" \
    -e EXPORT_PREFIXES="flow:" \
    -e EXPORT_FLUSH_EVERY="10" \
    -e RYU_BASE_URL="http://${CTRL_IP}:${CTRL_API_PORT}" \
    -e FLOWBLOCKER_URL="http://${FB_IP}:${FB_HTTP_PORT}" \
    -e CONTROLLER_ID="$CTRL_IP" \
    -e ETCD_ENDPOINTS="$ETCD_ENDPOINTS" \
    -e PORT="$PRED_HTTP_PORT" \
    -e POLL_INTERVAL_S="2.0" \
    -e Z_THRESHOLD="4.0" \
    -e MIN_RATE_BPS="50000" \
    -e AUTO_MITIGATE="true" \
    -e DRY_RUN="$DRY_RUN" \
    -e MITIGATION_COOLDOWN_S="60" \
    -e WHITELIST_IPS="" \
    -p "$PRED_HTTP_PORT:$PRED_HTTP_PORT" \
    "$PRED_IMG"

  # Conecta à rede ETCD (mesmo padrão do FlowBlocker)
  sudo docker network connect etcd-network "flow-predictor-$i" 2>/dev/null || true

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
log "📊 CSVs do dataset (1 por fluxo) em: ./prediction_history_domain<i>/"
log ""
log "⚠️  DRY_RUN=$DRY_RUN — para ativar mitigação real:"
log "    curl -X POST http://127.0.0.1:6060/predictor/config -H 'Content-Type: application/json' -d '{\"dry_run\": false}'"
