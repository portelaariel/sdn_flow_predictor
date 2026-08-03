#!/usr/bin/env bash
set -euo pipefail

############################
# Config (edit if needed)  #
############################
read -p "Enter the number of controller and switch sets (c): " c
read -p "Enter the number of switches per controller set (s): " s

# Image names (override with env if you use different tags)
RYU_IMG=ryu_core_cnsm
SSW_IMG=simpleswitch_cnsm
FB_IMG=flow_blocker_cnsm
ETCD_IMG=bitnami/etcd

# Base addressing/ports per controller set
SUBNET_BASE=10
CTRL_OF_PORT_BASE=6633
CTRL_API_PORT_BASE=8080
SSW_HTTP_PORT_BASE=9090
FB_HTTP_PORT_BASE=7070

# ETCD cluster
ETCD_SUBNET=253
ETCD_NODES=3
ETCD_NET="etcd-network"
ETCD_PREFIX="192.168.${ETCD_SUBNET}"
INITIAL_CLUSTER="etcd1=http://${ETCD_PREFIX}.11:2380,etcd2=http://${ETCD_PREFIX}.12:2380,etcd3=http://${ETCD_PREFIX}.13:2380"
ETCD_ENDPOINTS="${ETCD_PREFIX}.11:2379,${ETCD_PREFIX}.12:2379,${ETCD_PREFIX}.13:2379"

# Flow Predictor
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
MININET_SCRIPT="$SCRIPT_DIR/setup_mininet.py"

# Captures & test toggle
RUN_TEST="${RUN_TEST:-false}"               # set to "false" to skip quick test
RUN_PREDICTOR="${RUN_PREDICTOR:-true}"      # set to "false" to skip FlowPredictor
CAPTURE_SECONDS="${CAPTURE_SECONDS:-12}"   # basic capture duration
OUTDIR="${OUTDIR:-$PROJECT_ROOT/logs/run-$(date +%Y%m%d_%H%M%S)}"

########################################
# Helpers (idempotent + clean handling)#
########################################
log() { echo -e "[$(date +%H:%M:%S)] $*"; }

docker_rm_if() {
  local name="$1"
  if sudo docker ps -a --format '{{.Names}}' | grep -wq "$name"; then
    log "Removing container: $name"
    sudo docker rm -f "$name" >/dev/null 2>&1 || true
  fi
}

net_create_if_absent() {
  local net="$1" subnet="$2"
  if ! sudo docker network ls --format '{{.Name}}' | grep -wq "$net"; then
    log "Creating network $net ($subnet)"
    sudo docker network create --subnet="$subnet" "$net" >/dev/null
  else
    log "Network $net exists; reusing."
  fi
}

wait_http() {
  local url="$1" tries="${2:-30}"
  for ((t=1;t<=tries;t++)); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_tcp() {
  local host="$1" port="$2" tries="${3:-30}"
  for ((t=1;t<=tries;t++)); do
    if (echo > /dev/tcp/"$host"/"$port") >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

################################
# 0) Prepare run output folder #
################################
mkdir -p "$OUTDIR"
log "Output directory: $OUTDIR"

####################################
# 1) ETCD cluster (idempotent boot) #
####################################
net_create_if_absent "$ETCD_NET" "${ETCD_PREFIX}.0/24"

log "Starting ETCD cluster (${ETCD_NODES} nodes)"
for j in $(seq 1 $ETCD_NODES); do
  etcd_ip="${ETCD_PREFIX}.$((j+10))"
  etcd_name="etcd${j}"
  docker_rm_if "$etcd_name"
  sudo docker run -d --name "$etcd_name" --network "$ETCD_NET" --ip "$etcd_ip" \
    -p $((2378 + j)):2379 -p $((2378 + j + 100)):2380 \
    -e ETCD_NAME="$etcd_name" \
    -e ETCD_DATA_DIR=/etcd-data \
    -e ETCD_INITIAL_ADVERTISE_PEER_URLS="http://${etcd_ip}:2380" \
    -e ETCD_LISTEN_PEER_URLS="http://0.0.0.0:2380" \
    -e ETCD_LISTEN_CLIENT_URLS="http://0.0.0.0:2379" \
    -e ETCD_ADVERTISE_CLIENT_URLS="http://${etcd_ip}:2379" \
    -e ETCD_INITIAL_CLUSTER="$INITIAL_CLUSTER" \
    -e ETCD_INITIAL_CLUSTER_STATE=new \
    -e ETCD_INITIAL_CLUSTER_TOKEN=etcd-cluster-1 \
    -e ALLOW_NONE_AUTHENTICATION=yes \
    "$ETCD_IMG" >/dev/null
done

# Wait for at least one endpoint to answer
log "Waiting for ETCD client port on ${ETCD_PREFIX}.11:2379"
wait_tcp "${ETCD_PREFIX}.11" 2379 60 || { log "ETCD not reachable"; exit 1; }
log "ETCD cluster should be up."

#####################################################
# 2) Per-domain: network + Ryu + SimpleSwitch + FB  #
#####################################################
for ((i=0; i<c; i++)); do
  SUBNET=$((SUBNET_BASE + i))
  NET="ryu-network-$i"
  NET_SUBNET="192.168.${SUBNET}.0/24"
  CTRL_IP="192.168.${SUBNET}.10"
  SSW_IP="192.168.${SUBNET}.20"
  FB_IP="192.168.${SUBNET}.30"

  CTRL_OF_PORT=$((CTRL_OF_PORT_BASE + i))
  CTRL_API_PORT=$((CTRL_API_PORT_BASE + i))
  SSW_HTTP_PORT=$((SSW_HTTP_PORT_BASE + i))
  FB_HTTP_PORT=$((FB_HTTP_PORT_BASE + i))

  # Create/ensure network
  net_create_if_absent "$NET" "$NET_SUBNET"

  # Remove old containers if any
  docker_rm_if "ryu-core-$i"
  docker_rm_if "simple-switch-$i"
  docker_rm_if "flow-blocker-$i"

  # Ryu Core (emitter + ofctl_rest)
  log "Starting ryu-core-$i (${CTRL_IP})"
  sudo docker run -d --name "ryu-core-$i" --network "$NET" --ip "$CTRL_IP" \
    -e SIMPLESWITCH_URL="http://${SSW_IP}:${SSW_HTTP_PORT}/packetin" \
    -e FLOWBLOCKER_URL="http://${FB_IP}:${FB_HTTP_PORT}/flowblocker/domain_table" \
    -e CONTROLLER_ID="${CTRL_IP}" \
    -e OFP_TCP_PORT="${CTRL_OF_PORT}" \
    -e WSGI_PORT="${CTRL_API_PORT}" \
    -p "${CTRL_OF_PORT}:${CTRL_OF_PORT}" \
    -p "${CTRL_API_PORT}:${CTRL_API_PORT}" \
    "$RYU_IMG" >/dev/null

  # SimpleSwitch REST
  log "Starting simple-switch-$i (${SSW_IP})"
  sudo docker run -d --name "simple-switch-$i" --network "$NET" --ip "$SSW_IP" \
    -e RYU_BASE_URL="http://${CTRL_IP}:${CTRL_API_PORT}" \
    -e PORT="${SSW_HTTP_PORT}" \
    -p "${SSW_HTTP_PORT}:${SSW_HTTP_PORT}" \
    "$SSW_IMG" >/dev/null

  # FlowBlocker
  log "Starting flow-blocker-$i (${FB_IP})"
  sudo docker run -d --name "flow-blocker-$i" --network "$NET" --ip "$FB_IP" \
    -e RYU_BASE_URL="http://${CTRL_IP}:${CTRL_API_PORT}" \
    -e PORT="${FB_HTTP_PORT}" \
    -e ETCD_ENDPOINTS="${ETCD_ENDPOINTS}" \
    -e CONTROLLER_ID="${CTRL_IP}" \
    -e FB_PEER_ID="flow-blocker-$i" \
    -p "${FB_HTTP_PORT}:${FB_HTTP_PORT}" \
    "$FB_IMG" >/dev/null

  # Connect FB to ETCD network for cluster access
  sudo docker network connect "$ETCD_NET" "flow-blocker-$i"

  # Basic readiness checks (best-effort)
  log "Waiting Ryu REST on ${CTRL_API_PORT} and SimpleSwitch ${SSW_HTTP_PORT}, FlowBlocker ${FB_HTTP_PORT}"
  wait_tcp "127.0.0.1" "${CTRL_API_PORT}" 60 || true
  wait_http "http://127.0.0.1:${SSW_HTTP_PORT}/" 60 || true
  wait_http "http://127.0.0.1:${FB_HTTP_PORT}/" 60 || true
done

#########################################
# 3) Validate Mininet topology launcher #
#########################################
if [[ ! -f "$MININET_SCRIPT" ]]; then
  log "Mininet setup script not found: $MININET_SCRIPT"
  exit 1
fi
log "Mininet setup script: $MININET_SCRIPT"

############################################
# 4) (Optional) quick policy + capture run #
############################################
if [[ "$RUN_TEST" == "true" ]]; then
  log "Starting quick test & basic captures (duration: ${CAPTURE_SECONDS}s)"
  mkdir -p "$OUTDIR"

  # Pick domain 0 endpoints for the basic test (h1->h2)
  SRC_IP="10.0.0.1"
  DST_IP="10.0.0.2"

  # Helpful URLs (domain 0)
  FB_URL="http://127.0.0.1:${FB_HTTP_PORT_BASE}/flowblocker"
  HOST_LOCUS="${FB_URL}/host_locus?ip=${SRC_IP}"
  POL_URL="${FB_URL}/service"

  # Best-effort: start captures on control TCP & save to json/pcap
  OF_JSON="$OUTDIR/openflow.json"
  PKT_PCAP="$OUTDIR/blocked_ingress.pcap"

  # Control channel capture (host-any; decode OpenFlow on 6633)
  tshark -i any -Y "tcp.port == ${CTRL_OF_PORT_BASE}" \
    -o tcp.desegment_tcp_streams:true \
    -d "tcp.port==${CTRL_OF_PORT_BASE},openflow" \
    -T json > "$OF_JSON" &
  TSHARK_PID=$!

  # Launch mininet non-interactively to establish links & IPs
  # We’ll open CLI for a moment to issue a ping and then background
  sudo CSETS="$c" SPER="$s" python3 "$MININET_SCRIPT" <<'MINICMDS'
py net.hosts[0].cmd("ifconfig")  # touch
MINICMDS

  # Resolve ingress iface via host_locus
  IFJSON=$(curl -fsS "$HOST_LOCUS" || true)
  DPID=$(python3 - <<PY
import sys, json
try:
  d=json.loads('''$IFJSON'''); print(d.get("dpid",""))
except: print("")
PY
)
  PORT=$(python3 - <<PY
import sys, json
try:
  d=json.loads('''$IFJSON'''); print(d.get("port",""))
except: print("")
PY
)
  IFACE=""
  if [[ -n "$DPID" && -n "$PORT" ]]; then
    IFACE="s${DPID}-eth${PORT}"
  fi

  if [[ -n "$IFACE" ]]; then
    log "Sniffing ingress iface: $IFACE"
    sudo timeout "${CAPTURE_SECONDS}s" tcpdump -i "$IFACE" -w "$PKT_PCAP" \
      "(ip src ${SRC_IP} and ip dst ${DST_IP}) or (arp and host ${SRC_IP})" >/dev/null 2>&1 &
  else
    log "Could not resolve IFACE from host_locus; skipping ingress tcpdump."
  fi

  # Trigger policy on domain 0 to block SRC->DST
  POL_JSON=$(curl -fsS -X POST "${POL_URL}" -H 'Content-Type: application/json' \
    -d "{\"src_ip\":\"${SRC_IP}\",\"dst_ip\":\"${DST_IP}\"}" || true)
  echo "$POL_JSON" > "$OUTDIR/policy_response.json"
  log "Policy response: $POL_JSON"

  # Generate some traffic to be blocked
  log "Skipping probe topology generation"
#   log "Generating probe traffic via mininet (ping from h1 to h2)"
#   sudo mn -c >/dev/null 2>&1 || true
#   sudo mn --topo single,2 --controller=remote,ip=127.0.0.1,port=${CTRL_OF_PORT_BASE} --link tc <<'MINI'
# h1 ping -c 5 10.0.0.2
# exit
# MINI

  # Close tshark capture
  sleep 1
  kill $TSHARK_PID >/dev/null 2>&1 || true

  log "Basic captures written to: $OUTDIR"
fi

log "All done. To launch topology interactively: sudo CSETS=$c SPER=$s python3 $MININET_SCRIPT"


#########################################
# 5) Flow Predictor
#########################################

if [[ "$RUN_PREDICTOR" == "true" ]]; then

    if ! sudo docker image inspect flow_predictor_cnsm >/dev/null 2>&1; then
        echo "[i] Building Flow Predictor image..."
        sudo docker build -t flow_predictor_cnsm \
          -f "$PROJECT_ROOT/Dockerfile.flow_predictor" "$PROJECT_ROOT"
    fi

    echo "[i] Deploying Flow Predictor..."
    sudo bash "$PROJECT_ROOT/deploy_flow_predictor.sh" "$c" true
fi
