#!/bin/bash

echo "Enter the number of controller and switch sets (d):"
read d

echo "Enter the number of switches per controller set (s):"
read s

# Define starting IPs and ports
base_ip=10
controller_port=6633
api_port=8080
switch_port=8090

# Create a Mininet script
echo "#!/usr/bin/python3" > setup_mininet.py
echo "from mininet.net import Mininet" >> setup_mininet.py
echo "from mininet.node import Controller, RemoteController, OVSSwitch" >> setup_mininet.py
echo "from mininet.cli import CLI" >> setup_mininet.py
echo "from mininet.link import TCLink" >> setup_mininet.py
echo "from mininet.log import setLogLevel, info" >> setup_mininet.py
echo "" >> setup_mininet.py
echo "def customTopology():" >> setup_mininet.py
echo "    net = Mininet(controller=RemoteController, link=TCLink, switch=OVSSwitch)" >> setup_mininet.py
echo "" >> setup_mininet.py

net_ip="192.168.10.0/24"
# Create Docker network
sudo docker network create --subnet=$net_ip ryu-network

controllers=()
# Add controllers
for ((i=0; i<d; i++))
do
    controller_ip="192.168.10.$((base_ip + i))"
    switch_ip="192.168.10.$((4*base_ip + i))"

    # Run Ryu controller
    sudo docker run -d --network ryu-network --ip $controller_ip -p $((controller_port+i)):6633 -p $((api_port+i)):8080 -e simpleswitch="http://$switch_ip:$((switch_port+i))/packetin" -e OFP_PORT=$((controller_port+i)) -e WSGI_PORT=$((api_port+i)) ryu_controller_vr

    # Run Simple Switch
    sudo docker run -d --network ryu-network --ip $switch_ip -p $((switch_port+i)):8090 -e RYU_BASE_URL="http://$controller_ip:$((api_port+i))" -e PORT=$((switch_port+i)) simple_switch_rest_vr

    # Add controller to Mininet script
    echo "    c$i = net.addController('c$i', controller=RemoteController, ip='$controller_ip', port=$((controller_port+i)))" >> setup_mininet.py
    controllers+=("c$i")
done

# Add switches
switch_count=1
for ((i=0; i<d; i++))
do
    for ((j=1; j<=s; j++))
    do
        echo "    s$switch_count = net.addSwitch('s$switch_count')" >> setup_mininet.py
        switch_count=$((switch_count + 1))
    done
done

# Add hosts and connect to switches
host_count=1
switch_count=1
for ((i=0; i<d; i++))
do
    for ((j=1; j<=s; j++))
    do
        echo "    h$host_count = net.addHost('h$host_count')" >> setup_mininet.py
        echo "    net.addLink(s$switch_count, h$host_count)" >> setup_mininet.py
        host_count=$((host_count + 1))
        echo "    h$host_count = net.addHost('h$host_count')" >> setup_mininet.py
        echo "    net.addLink(s$switch_count, h$host_count)" >> setup_mininet.py
        host_count=$((host_count + 1))
        switch_count=$((switch_count + 1))
    done
done

# Connect switches under each controller
switch_count=1
for ((i=0; i<d; i++))
do
    for ((j=1; j<s; j++))
    do
        next_switch=$((switch_count + 1))
        echo "    net.addLink(s$switch_count, s$next_switch)" >> setup_mininet.py
        switch_count=$((switch_count + 1))
    done
    # Move to the next set of switches for the next controller
    switch_count=$((switch_count + 1))
done

# Assign controllers to switches
switch_count=1
for ((i=0; i<d; i++))
do
    for ((j=1; j<=s; j++))
    do
        echo "    s$switch_count.start([${controllers[i]}])" >> setup_mininet.py
        switch_count=$((switch_count + 1))
    done
done

# Start the network
echo "    net.build()" >> setup_mininet.py
for ((i=0; i<d; i++))
do
    echo "    ${controllers[i]}.start()" >> setup_mininet.py
done

# Start the CLI
echo "    CLI(net)" >> setup_mininet.py

# Stop the network
echo "    net.stop()" >> setup_mininet.py

echo "" >> setup_mininet.py
echo "if __name__ == '__main__':" >> setup_mininet.py
echo "    setLogLevel('info')" >> setup_mininet.py
echo "    customTopology()" >> setup_mininet.py

chmod +x setup_mininet.py

echo "Mininet setup script 'setup_mininet.py' is ready to be executed. Run it with 'sudo python3.10 setup_mininet.py'."

