#!/usr/bin/env bash
set -euo pipefail

# ========= CONFIG =========
C=2          # controllers/domains
S=2          # switches per controller (total switches = C*S = 4)
SUBNET_BASE=10

CONTROLLER_PORT_BASE=6633     # domain A:6633, domain B:6634
API_PORT_BASE=8080            # domain A:8080, domain B:8081
SWITCH_PORT_BASE=9090
BLOCKER_PORT_BASE=7070        # domain A:7070, domain B:7071

ETCD_SUBNET=253
ETCD_NODES=3
ETCD_NET="etcd-network"
RYU_NET="ryu-network"

# Final copy to your Windows path (as requested)
#FINAL_SCP_CMD='scp -P 22056 -r ubuntu@137.204.56.38:~/cnsm2025/ "D:/Unibo research/CNSM/"'

# ========= OUTPUT DIRS =========
TS=$(date +%Y%m%d_%H%M%S)
BASE_DIR=~/cnsm2025
RES_DIR="$BASE_DIR/results_$TS"
PCAP_DIR="$RES_DIR/pcaps"
mkdir -p "$RES_DIR" "$PCAP_DIR"

log() { echo "[$(date +%H:%M:%S)] $*"; }
save_cmd() { echo -e "\n# $*\n" >> "$RES_DIR/commands_run.log"; "$@" | tee -a "$RES_DIR/commands_run.log"; }

# Ensure tools
if ! command -v jq >/dev/null 2>&1; then
  sudo apt-get update -y && sudo apt-get install -y jq
fi
if ! command -v tcpdump >/dev/null 2>&1; then
  sudo apt-get update -y && sudo apt-get install -y tcpdump
fi

# Always collect some logs on exit
cleanup() {
  log "Collecting docker logs..."
  for n in 0 1; do
    for svc in flow-blocker ryu-core simple-switch; do
      if sudo docker ps --format '{{.Names}}' | grep -qx "${svc}-${n}"; then
        sudo docker logs "${svc}-${n}" > "$RES_DIR/${svc}-${n}.log" 2>&1 || true
      fi
    done
  done
  log "Stopping tcpdump captures (if any)..."
  sudo pkill -f "tcpdump -i s" || true
  sudo pkill -f "tcpdump -i h" || true
  log "Done. Results in: $RES_DIR"
}
trap cleanup EXIT

# ========= NETWORKS =========
log "Creating/ensuring Docker networks..."
if ! sudo docker network inspect "$ETCD_NET" >/dev/null 2>&1; then
  save_cmd sudo docker network create --subnet=192.168.${ETCD_SUBNET}.0/24 "$ETCD_NET"
fi
if ! sudo docker network inspect "$RYU_NET" >/dev/null 2>&1; then
  save_cmd sudo docker network create --subnet=192.168.0.0/18 "$RYU_NET"
fi

# ========= ETCD CLUSTER =========
log "Starting ETCD cluster (if not already)..."
INITIAL_CLUSTER="etcd1=http://192.168.${ETCD_SUBNET}.11:2380,etcd2=http://192.168.${ETCD_SUBNET}.12:2380,etcd3=http://192.168.${ETCD_SUBNET}.13:2380"
ETCD_ENDPOINTS="http://192.168.${ETCD_SUBNET}.11:2379,http://192.168.${ETCD_SUBNET}.12:2379,http://192.168.${ETCD_SUBNET}.13:2379"

for j in $(seq 1 $ETCD_NODES); do
  etcd_ip="192.168.${ETCD_SUBNET}.$((10+j))"
  etcd_name="etcd${j}"
  if ! sudo docker ps -a --format '{{.Names}}' | grep -qx "$etcd_name"; then
    save_cmd sudo docker run -d --name "$etcd_name" --network "$ETCD_NET" --ip "$etcd_ip" \
      -p $((2378 + j)):2379 -p $((2478 + j)):2380 \
      -e ETCD_NAME="$etcd_name" \
      -e ETCD_DATA_DIR=/etcd-data \
      -e ETCD_INITIAL_ADVERTISE_PEER_URLS="http://$etcd_ip:2380" \
      -e ETCD_LISTEN_PEER_URLS="http://0.0.0.0:2380" \
      -e ETCD_LISTEN_CLIENT_URLS="http://0.0.0.0:2379" \
      -e ETCD_ADVERTISE_CLIENT_URLS="http://$etcd_ip:2379" \
      -e ETCD_INITIAL_CLUSTER="$INITIAL_CLUSTER" \
      -e ETCD_INITIAL_CLUSTER_STATE=new \
      -e ETCD_INITIAL_CLUSTER_TOKEN=etcd-cluster-1 \
      -e ALLOW_NONE_AUTHENTICATION=yes \
      bitnami/etcd:latest
  fi
done

log "Waiting for ETCD health..."
for ep in ${ETCD_ENDPOINTS//,/ }; do
  until curl -fsS "$ep/health" | tee -a "$RES_DIR/etcd_health.json"; do
    echo "waiting on $ep"; sleep 2
  done
done

# ========= DOMAIN CONTAINERS =========
log "Starting domain containers (Ryu Core, SimpleSwitch, FlowBlocker)..."
for i in 0 1; do
  subnet=$((SUBNET_BASE + i))
  controller_ip="192.168.${subnet}.10"
  switch_ip="192.168.${subnet}.20"
  blocker_ip="192.168.${subnet}.30"

  controller_port=$((CONTROLLER_PORT_BASE + i))
  api_port=$((API_PORT_BASE + i))
  switch_port=$((SWITCH_PORT_BASE + i))
  blocker_port=$((BLOCKER_PORT_BASE + i))

  # Ryu Core (emitter + ofctl_rest.py must be inside the image)
  if ! sudo docker ps -a --format '{{.Names}}' | grep -qx "ryu-core-$i"; then
    save_cmd sudo docker run -d --name "ryu-core-$i" --network "$RYU_NET" --ip "$controller_ip" \
      -e SIMPLESWITCH_URL="http://$switch_ip:$switch_port/packetin" \
      -e FLOWBLOCKER_URL="http://$blocker_ip:$blocker_port/flowblocker/domain_table" \
      -e CONTROLLER_ID="$controller_ip" \
      -e OFP_TCP_PORT="$controller_port" \
      -e WSGI_PORT="$api_port" \
      -p "$controller_port:6633" \
      -p "$api_port:8080" \
      ryu_core_cnsm
  fi

  # Simple Switch
  if ! sudo docker ps -a --format '{{.Names}}' | grep -qx "simple-switch-$i"; then
    save_cmd sudo docker run -d --name "simple-switch-$i" --network "$RYU_NET" --ip "$switch_ip" \
      -e RYU_BASE_URL="http://$controller_ip:$api_port" \
      -e PORT="$switch_port" \
      -p "$switch_port:9090" \
      simpleswitch_cnsm
  fi

  # FlowBlocker
  if ! sudo docker ps -a --format '{{.Names}}' | grep -qx "flow-blocker-$i"; then
    save_cmd sudo docker run -d --name "flow-blocker-$i" --network "$RYU_NET" --ip "$blocker_ip" \
      -e RYU_BASE_URL="http://$controller_ip:$api_port" \
      -e PORT="$blocker_port" \
      -e CONTROLLER_ID="$controller_ip" \
      -e ETCD_ENDPOINTS="$ETCD_ENDPOINTS" \
      -p "$blocker_port:$blocker_port" \
      flow_blocker_cnsm
    save_cmd sudo docker network connect "$ETCD_NET" "flow-blocker-$i"
  fi
done

log "Waiting for FlowBlockers REST to be ready..."
for i in 0 1; do
  subnet=$((SUBNET_BASE + i))
  blocker_port=$((BLOCKER_PORT_BASE + i))
  url="http://192.168.${subnet}.30:${blocker_port}/"
  until curl -fsS "$url" >/dev/null; do
    echo "waiting on FlowBlocker $i at $url"; sleep 2
  done
done

log "Waiting for Ryu REST to be ready..."
for i in 0 1; do
  subnet=$((SUBNET_BASE + i))
  api_port=$((API_PORT_BASE + i))
  url="http://192.168.${subnet}.10:${api_port}/stats/switches"
  until curl -fsS "$url" >/dev/null; do
    echo "waiting on Ryu Core $i at $url"; sleep 2
  done
done

# ========= MININET SCRIPT (your version, only i=2 and j=2) =========
log "Generating Mininet script (static i=2, j=2; no other changes)..."
MN_PY="$RES_DIR/setup_mininet.py"
cat > "$MN_PY" <<'PY'
#!/usr/bin/python3
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch, Host
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel, info

def customTopology():
    net = Mininet(controller=RemoteController, link=TCLink, switch=OVSSwitch)
    
    # Add controllers
    controllers = []
    for i in range(2):
        controllers.append(net.addController('c'+str(i), controller=RemoteController, ip='192.168.' + str(10 + i) + '.10', port=6633+i))
    
    # Add switches and hosts
    switch_count = 1
    host_count = 1
    for i in range(2):
        for j in range(2):
            s = net.addSwitch('s'+str(switch_count))
            net.addLink(s, net.addHost('h'+str(host_count)))
            host_count += 1
            net.addLink(s, net.addHost('h'+str(host_count)))
            host_count += 1
            switch_count += 1
    
    # Connect switches in series
    for i in range(1, switch_count - 1):
        net.addLink('s'+str(i), 's'+str(i+1))
    
    # Assign controllers to switches
    switch_index = 1
    for i in range(2):
        for j in range(2):
            net.get('s'+str(switch_index)).start([controllers[i]])
            switch_index += 1

    net.build()
    
    # Start controllers
    for controller in controllers:
        controller.start()
    
    # Add flow rules for LLDP packets on all switches
    for i in range(1, switch_count):
        net.get('s'+str(i)).dpctl('add-flow', 'dl_type=0x88cc,actions=CONTROLLER')
    
    CLI(net)
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    customTopology()
PY
chmod +x "$MN_PY"

# ========= RUN MININET (WITH SUDO) & SCRIPTED CLI =========
log "Running Mininet CLI plan as root (baseline → OF10 → warm-up → block → verify)..."
MN_LOG="$RES_DIR/mininet_cli.log"

sudo -E python3 "$MN_PY" > "$MN_LOG" 2>&1 <<'CLI_CMDS'
# Give time for handshakes
sleep 3

# Force OpenFlow10 on all bridges (outside the .py; script remains unchanged)
sh ovs-vsctl set bridge s1 protocols=OpenFlow10
sh ovs-vsctl set bridge s2 protocols=OpenFlow10
sh ovs-vsctl set bridge s3 protocols=OpenFlow10
sh ovs-vsctl set bridge s4 protocols=OpenFlow10

# Start tcpdump captures (host + OVS ports)
sh mkdir -p /home/ubuntu/cnsm2025/pcaps
h1 tcpdump -i h1-eth0 -s 0 -w /home/ubuntu/cnsm2025/pcaps/h1-eth0.pcap >/dev/null 2>&1 &
sh tcpdump -i s1-eth1 -s 0 -w /home/ubuntu/cnsm2025/pcaps/s1-eth1.pcap >/dev/null 2>&1 &
sh tcpdump -i s1-eth2 -s 0 -w /home/ubuntu/cnsm2025/pcaps/s1-eth2.pcap >/dev/null 2>&1 &
sh tcpdump -i s3-eth1 -s 0 -w /home/ubuntu/cnsm2025/pcaps/s3-eth1.pcap >/dev/null 2>&1 &
sh tcpdump -i s3-eth2 -s 0 -w /home/ubuntu/cnsm2025/pcaps/s3-eth2.pcap >/dev/null 2>&1 &

# Baseline reachability
pingall
h1 ping -c 2 h6

# Warm-up local edges so the emitter records correct host locus
h1 ping -c 1 h2
h6 ping -c 1 h5
sleep 2

# Snapshot domain tables BEFORE policy
sh bash -lc 'curl -fsS http://192.168.10.30:7070/flowblocker/domain_table > /home/ubuntu/cnsm2025/domain_A_before.json'
sh bash -lc 'curl -fsS http://192.168.11.30:7071/flowblocker/domain_table > /home/ubuntu/cnsm2025/domain_B_before.json'

# Apply block h1 -> h6 (cross-domain)
sh bash -lc 'curl -fsS -X POST "http://192.168.10.30:7070/flowblocker/service" -H "Content-Type: application/json" -d "{\"src_ip\":\"10.0.0.1\",\"dst_ip\":\"10.0.0.6\",\"policy_id\":\"block-h1-h6\"}" > /home/ubuntu/cnsm2025/block_response.json'
sleep 2

# Verify after policy
h1 ping -c 3 h2
h1 ping -c 3 h6
pingall

# Dump flows (OF10) on s1..s4
sh bash -lc 'ovs-ofctl -O OpenFlow10 dump-flows s1 > /home/ubuntu/cnsm2025/s1_flows_after.txt'
sh bash -lc 'ovs-ofctl -O OpenFlow10 dump-flows s2 > /home/ubuntu/cnsm2025/s2_flows_after.txt'
sh bash -lc 'ovs-ofctl -O OpenFlow10 dump-flows s3 > /home/ubuntu/cnsm2025/s3_flows_after.txt'
sh bash -lc 'ovs-ofctl -O OpenFlow10 dump-flows s4 > /home/ubuntu/cnsm2025/s4_flows_after.txt'

# Stop captures cleanly
h1 pkill tcpdump || true
sh pkill -f "tcpdump -i s1-eth" || true
sh pkill -f "tcpdump -i s3-eth" || true

exit
CLI_CMDS

# ========= COLLECT ARTIFACTS =========
log "Collecting artifacts from home into result directory..."
for f in domain_A_before.json domain_B_before.json block_response.json \
         s1_flows_after.txt s2_flows_after.txt s3_flows_after.txt s4_flows_after.txt; do
  if [[ -f "$BASE_DIR/$f" ]]; then
    mv -f "$BASE_DIR/$f" "$RES_DIR/" || true
  fi
done

# Copy pcaps
if [[ -d "$BASE_DIR/pcaps" ]]; then
  mv -f "$BASE_DIR/pcaps"/* "$PCAP_DIR/" || true
fi

# ========= CONTROLLER OWNERSHIP + DOMAIN TABLES (AFTER) =========
log "Querying controller ownership AFTER Mininet brought switches up..."
for i in 0 1; do
  subnet=$((SUBNET_BASE + i))
  api_port=$((API_PORT_BASE + i))
  save_cmd curl -fsS "http://192.168.${subnet}.10:${api_port}/stats/switches" \
    | tee "$RES_DIR/stats_switches_domain${i}_after.json" >/dev/null
done

log "Querying domain tables AFTER policy..."
save_cmd curl -fsS http://192.168.10.30:7070/flowblocker/domain_table | tee "$RES_DIR/domain_A_after.json" >/dev/null
save_cmd curl -fsS http://192.168.11.30:7071/flowblocker/domain_table | tee "$RES_DIR/domain_B_after.json" >/dev/null

# ========= ETCD HEALTH (AFTER) =========
log "Capturing ETCD health (after)..."
for ep in ${ETCD_ENDPOINTS//,/ }; do
  curl -fsS "$ep/health" | tee -a "$RES_DIR/etcd_health_after.json" >/dev/null || true
done

# ========= SUMMARY =========
log "Summary:"
echo "  - Mininet CLI transcript: $RES_DIR/mininet_cli.log"
echo "  - Flow dumps: $RES_DIR/s[1-4]_flows_after.txt"
echo "  - Domain tables: $RES_DIR/domain_*_before.json and *_after.json"
echo "  - Controller ownership: $RES_DIR/stats_switches_domain*_after.json"
echo "  - FlowBlocker & Ryu logs: $RES_DIR/*log"
echo "  - pcaps: $PCAP_DIR/*.pcap"

# ========= FINAL COPY TO WINDOWS (as requested) =========
#log "Executing final SCP to your local Windows path..."
#eval "$FINAL_SCP_CMD"

log "All done."

