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


