#!/bin/bash

echo "Enter the number of controller and switch sets (c):"
read c

echo "Enter the number of switches per controller set (s):"
read s

# Define base IP and ports
subnet_base=10

controller_port_base=6633
api_port_base=8080
switch_port_base=9090
blocker_port_base=7070

etcd_subnet=253
etcd_nodes=3

# Create Docker network for ETCD cluster
sudo docker network create --subnet=192.168.$etcd_subnet.0/24 etcd-network
sudo docker network create --subnet=192.168.0.0/18 ryu-network
# ETCD initial cluster string
initial_cluster="etcd1=http://192.168.253.11:2380,etcd2=http://192.168.253.12:2380,etcd3=http://192.168.253.13:2380"
etcd_endpoints="192.168.253.11:2379,192.168.253.12:2379,192.168.253.13:2379"


# Run ETCD cluster containers using the Bitnami image
for ((j=1; j<=etcd_nodes; j++))
do
    etcd_ip="192.168.$etcd_subnet.$((j+10))"
    etcd_name="etcd$j"
    sudo docker run -d --name $etcd_name --network etcd-network --ip $etcd_ip \
    -p $((2378 + j)):2379 -p $((2378 + j + 100)):2380 \
    -e ETCD_NAME=$etcd_name \
    -e ETCD_DATA_DIR=/etcd-data \
    -e ETCD_INITIAL_ADVERTISE_PEER_URLS=http://$etcd_ip:2380 \
    -e ETCD_LISTEN_PEER_URLS=http://0.0.0.0:2380 \
    -e ETCD_LISTEN_CLIENT_URLS=http://0.0.0.0:2379 \
    -e ETCD_ADVERTISE_CLIENT_URLS=http://$etcd_ip:2379 \
    -e ETCD_INITIAL_CLUSTER=$initial_cluster \
    -e ETCD_INITIAL_CLUSTER_STATE=new \
    -e ETCD_INITIAL_CLUSTER_TOKEN=etcd-cluster-1 \
    -e ALLOW_NONE_AUTHENTICATION=yes \
    bitnami/etcd:latest
done

# Setup Ryu controllers, simple switches, and flow blockers
for ((i=0; i<c; i++))
do
    subnet=$((subnet_base + i))
    net_ip="192.168.$subnet.0/24"
    controller_ip="192.168.$subnet.10"
    switch_ip="192.168.$subnet.20"
    blocker_ip="192.168.$subnet.30"

    controller_port=$((controller_port_base + i))
    api_port=$((api_port_base + i))
    switch_port=$((switch_port_base + i))
    blocker_port=$((blocker_port_base + i))

    # Run Ryu Core
    sudo docker run -d --name ryu-core-$i --network ryu-network --ip $controller_ip \
    -e SIMPLESWITCH_URL="http://$switch_ip:$switch_port/packetin" \
    -e FLOWBLOCKER_URL="http://$blocker_ip:$blocker_port/flowblocker/domain_table" \
    -e CONTROLLER_ID="$controller_ip" \
    -e OFP_TCP_PORT=$controller_port \
    -e WSGI_PORT=$api_port \
    -p $controller_port:6633 \
    -p $api_port:8080 \
    ryu_core_cnsm

    # Run Simple Switch
    sudo docker run -d --name simple-switch-$i --network ryu-network --ip $switch_ip \
    -e RYU_BASE_URL="http://$controller_ip:$api_port" \
    -e PORT=$switch_port \
    -p $switch_port:9090 \
    simpleswitch_cnsm

    # Run Flow Blocker
    sudo docker run -d --name flow-blocker-$i --network ryu-network --ip $blocker_ip \
    -e RYU_BASE_URL="http://$controller_ip:$api_port" \
    -e PORT=$blocker_port \
    -e CONTROLLER_ID="$controller_ip" \
    -p $blocker_port:7070 \
    -e ETCD_ENDPOINTS="$etcd_endpoints" \
    flow_blocker_cnsm

    # Connect Flow Blocker to ETCD network
    sudo docker network connect etcd-network flow-blocker-$i
done

# Generate Mininet setup script
cat <<EOL > setup_mininet.py
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
    for i in range($c):
        controllers.append(net.addController('c'+str(i), controller=RemoteController, ip='192.168.' + str(10 + i) + '.10', port=6633+i))
    
    # Add switches and hosts
    switch_count = 1
    host_count = 1
    for i in range($c):
        for j in range($s):
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
    for i in range($c):
        for j in range($s):
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
EOL

chmod +x setup_mininet.py

echo "Mininet setup script 'setup_mininet.py' is ready to be executed. Run it with 'sudo ./setup_mininet.py'."

########################################
# ADD-ONLY: instrumentation + test run #
########################################

# --- tools (no config changes) ---
command -v jq >/dev/null 2>&1 || { echo "[i] installing jq"; sudo apt-get update && sudo apt-get install -y jq; }
command -v tcpdump >/dev/null 2>&1 || { echo "[i] installing tcpdump"; sudo apt-get update && sudo apt-get install -y tcpdump; }
command -v iperf3  >/dev/null 2>&1 || { echo "[i] installing iperf3"; sudo apt-get update && sudo apt-get install -y iperf3; }

# Run folder
RUN_ID=$(date +"%Y%m%d_%H%M%S")
RUN_DIR="run_${RUN_ID}"
mkdir -p "$RUN_DIR"/{logs,pcaps,stats,artifacts}

# Start container logs (background)
for i in $(seq 0 $((c-1))); do
  for name in ryu-core-$i simple-switch-$i flow-blocker-$i; do
    if sudo docker ps --format '{{.Names}}' | grep -qx "$name"; then
      sudo bash -c "docker logs -f --since=0 $name > $RUN_DIR/logs/${name}.log 2>&1 & echo \$! > $RUN_DIR/logs/${name}.pid"
    fi
  done
done
for name in etcd1 etcd2 etcd3; do
  if sudo docker ps --format '{{.Names}}' | grep -qx "$name"; then
    sudo bash -c "docker logs -f --since=0 $name > $RUN_DIR/logs/${name}.log 2>&1 & echo \$! > $RUN_DIR/logs/${name}.pid"
  fi
done

# Warm-up discovery
echo "[i] Warming up discovery (LLDP/ARP)…"
sleep 12

# BEFORE snapshots
echo "[i] Snapshotting BEFORE-state…"
total_switches=$((c * s))
for i in $(seq 0 $((c-1))); do
  subnet=$((subnet_base + i))
  controller_ip="192.168.$subnet.10"
  api_port=$((api_port_base + i))
  blocker_ip="192.168.$subnet.30"
  blocker_port=$((blocker_port_base + i))

  curl -s "http://$blocker_ip:$blocker_port/flowblocker/domain_table" \
    -o "$RUN_DIR/stats/domain_table_before_c${i}.json"

  first_dpid=$((i * s + 1))
  last_dpid=$(((i + 1) * s))
  for dpid in $(seq $first_dpid $last_dpid); do
    curl -s "http://$controller_ip:$api_port/stats/flow/$dpid" \
      -o "$RUN_DIR/stats/flows_before_dpid_${dpid}.json"
    curl -s "http://$controller_ip:$api_port/stats/port/$dpid" \
      -o "$RUN_DIR/stats/ports_before_dpid_${dpid}.json"
  done
done

# Control-plane tcpdump on host for OF ports
OF_FILTER=""
for i in $(seq 0 $((c-1))); do
  p=$((controller_port_base + i))
  if [ -z "$OF_FILTER" ]; then OF_FILTER="tcp port $p"; else OF_FILTER="$OF_FILTER or tcp port $p"; fi
done
echo "[i] Starting host OpenFlow tcpdump: $OF_FILTER"
sudo timeout 180s tcpdump -i any "($OF_FILTER)" -w "$RUN_DIR/pcaps/openflow_host.pcap" >/dev/null 2>&1 &
OF_TCPDUMP_PID=$!

# Auto Mininet tests (no config changes to containers)
cat > run_mininet_tests.py <<'PY'
#!/usr/bin/python3
import argparse, time, os, shutil, json, subprocess
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel

def run_cmd(cmd):
    return subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

def build_net(c, s):
    net = Mininet(controller=RemoteController, link=TCLink, switch=OVSSwitch, cleanup=True)
    controllers = []
    for i in range(c):
        ctrl_ip = f"192.168.{10+i}.10"
        controllers.append(net.addController(f'c{i}', controller=RemoteController, ip=ctrl_ip, port=6633+i))
    sw_idx, host_idx = 1, 1
    for _ in range(c):
        for __ in range(s):
            sw = net.addSwitch(f's{sw_idx}')
            h1 = net.addHost(f'h{host_idx}'); net.addLink(sw,h1); host_idx += 1
            h2 = net.addHost(f'h{host_idx}'); net.addLink(sw,h2); host_idx += 1
            sw_idx += 1
    for i in range(1, sw_idx-1):
        net.addLink(f's{i}', f's{i+1}')
    net.build()
    for ctrl in controllers: ctrl.start()
    sw_ptr = 1
    for i in range(c):
        cl=[controllers[i]]
        for __ in range(s):
            net.get(f's{sw_ptr}').start(cl); sw_ptr+=1
    for i in range(1, sw_idx):
        net.get(f's{i}').dpctl('add-flow', 'dl_type=0x88cc,actions=CONTROLLER')
    return net, sw_idx-1, host_idx-1

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--c', type=int, required=True)
    ap.add_argument('--s', type=int, required=True)
    ap.add_argument('--outdir', required=True)
    ap.add_argument('--src', type=int, default=1)
    ap.add_argument('--dst', type=int, required=True)
    ap.add_argument('--block_url', required=True)
    args = ap.parse_args()
    setLogLevel('info')
    outdir = args.outdir
    os.makedirs(outdir, exist_ok=True)
    net, nsw, nhosts = build_net(args.c, args.s)

    h1 = net.get(f'h{args.src}')
    hd = net.get(f'h{args.dst}')
    s1 = net.get('s1')

    # start pcaps
    h1.cmd(f'tcpdump -i h{args.src}-eth0 -s 96 -w /tmp/h{args.src}.pcap & echo $! > /tmp/h{args.src}.tcpdump.pid')
    hd.cmd(f'tcpdump -i h{args.dst}-eth0 -s 96 -w /tmp/h{args.dst}.pcap & echo $! > /tmp/h{args.dst}.tcpdump.pid')
    s1.cmd('tcpdump -i s1-eth1 -s 96 -w /tmp/s1.pcap & echo $! > /tmp/s1.tcpdump.pid')
    time.sleep(2)

    # BEFORE tests
    loss = net.pingAll()
    open(os.path.join(outdir, 'pingall_before.txt'),'w').write(f'loss_percent={loss}\n')
    h1.cmd(f'ping -c 5 10.0.0.{args.dst} | tee /tmp/ping_before.txt')
    shutil.copy('/tmp/ping_before.txt', os.path.join(outdir,'ping_before.txt'))
    hd.cmd('pkill -f iperf3; iperf3 -s -D'); time.sleep(1)
    h1.cmd(f'iperf3 -J -t 8 -c 10.0.0.{args.dst} > /tmp/iperf_before.json')
    shutil.copy('/tmp/iperf_before.json', os.path.join(outdir,'iperf_before.json'))

    # Trigger policy block
    t0 = time.time()
    run_cmd(f"curl -s -X POST '{args.block_url}' -H 'Content-Type: application/json' -d '{{\"src_ip\":\"10.0.0.{args.src}\",\"dst_ip\":\"10.0.0.{args.dst}\"}}' > {outdir}/block_response.json")
    open(os.path.join(outdir,'block_request_epoch_ms.txt'),'w').write(str(int(t0*1000))+'\n')
    time.sleep(3)

    # AFTER tests
    loss2 = net.pingAll()
    open(os.path.join(outdir, 'pingall_after.txt'),'w').write(f'loss_percent={loss2}\n')
    h1.cmd(f'ping -c 5 10.0.0.{args.dst} | tee /tmp/ping_after.txt')
    shutil.copy('/tmp/ping_after.txt', os.path.join(outdir,'ping_after.txt'))
    h1.cmd(f'iperf3 -J -t 8 -c 10.0.0.{args.dst} > /tmp/iperf_after.json')
    shutil.copy('/tmp/iperf_after.json', os.path.join(outdir,'iperf_after.json'))
    hd.cmd('pkill -f iperf3')

    # OVS flows on all switches
    for dpid in range(1, nsw+1):
        net.get(f's{dpid}').cmd(f'ovs-ofctl -O OpenFlow13 dump-flows s{dpid} > /tmp/ovs_flows_s{dpid}.txt')
        shutil.copy(f'/tmp/ovs_flows_s{dpid}.txt', os.path.join(outdir,f'ovs_flows_s{dpid}.txt'))

    # stop pcaps, collect
    try:
        h1.cmd(f'kill -9 $(cat /tmp/h{args.src}.tcpdump.pid)'); hd.cmd(f'kill -9 $(cat /tmp/h{args.dst}.tcpdump.pid)'); s1.cmd('kill -9 $(cat /tmp/s1.tcpdump.pid)')
    except: pass
    for p in [f'/tmp/h{args.src}.pcap', f'/tmp/h{args.dst}.pcap', '/tmp/s1.pcap']:
        if os.path.exists(p): shutil.copy(p, os.path.join(outdir, os.path.basename(p)))
    net.stop()

if __name__ == '__main__':
    main()
PY
chmod +x run_mininet_tests.py

# Derive src/dst & blocker endpoint
SRC_HOST=1
DST_HOST=$((2 * c * s))                         # last host id (cross-domain)
last_subnet=$((subnet_base + c - 1))            # e.g., 10+(c-1)
last_blocker_ip="192.168.${last_subnet}.30"
last_blocker_port=$((blocker_port_base + c - 1))
BLOCK_URL="http://${last_blocker_ip}:${last_blocker_port}/flowblocker/service"

echo "[i] Launching automated Mininet tests… (src=h${SRC_HOST}, dst=h${DST_HOST})"
sudo python3 ./run_mininet_tests.py \
  --c "$c" \
  --s "$s" \
  --outdir "$RUN_DIR/artifacts" \
  --src "$SRC_HOST" \
  --dst "$DST_HOST" \
  --block_url "$BLOCK_URL"

# AFTER snapshots
echo "[i] Snapshotting AFTER-state…"
for i in $(seq 0 $((c-1))); do
  subnet=$((subnet_base + i))
  controller_ip="192.168.$subnet.10"
  api_port=$((api_port_base + i))
  blocker_ip="192.168.$subnet.30"
  blocker_port=$((blocker_port_base + i))

  curl -s "http://$blocker_ip:$blocker_port/flowblocker/domain_table" \
    -o "$RUN_DIR/stats/domain_table_after_c${i}.json"

  first_dpid=$((i * s + 1))
  last_dpid=$(((i + 1) * s))
  for dpid in $(seq $first_dpid $last_dpid); do
    curl -s "http://$controller_ip:$api_port/stats/flow/$dpid" \
      -o "$RUN_DIR/stats/flows_after_dpid_${dpid}.json"
    curl -s "http://$controller_ip:$api_port/stats/port/$dpid" \
      -o "$RUN_DIR/stats/ports_after_dpid_${dpid}.json"
  done
done

# One-shot resource snapshot
sudo docker stats --no-stream > "$RUN_DIR/stats/docker_stats.txt" 2>&1 || true

# Stop control-plane tcpdump & log tails
kill $OF_TCPDUMP_PID 2>/dev/null || true
for pidf in "$RUN_DIR"/logs/*.pid; do
  [ -f "$pidf" ] && kill -9 "$(cat "$pidf")" 2>/dev/null || true
done

# Package
tar -czf "${RUN_DIR}.tar.gz" "$RUN_DIR"
echo "[✓] Done. Bundle: ${RUN_DIR}.tar.gz"

