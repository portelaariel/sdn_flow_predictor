# SDN Test Bed Setup with Ryu-Core, Flow Blocker, Simple Switch, and ETCD Cluster

This guide will walk you through the process of setting up a Software-Defined Networking (SDN) test bed using Docker containers and Mininet. The test bed will deploy multiple controller sets, each including Ryu-Core, Flow Blocker, Simple Switch, and a Mininet topology with Open vSwitch (OVS).

## Overview

The test bed consists of the following components:
- **Ryu-Core**: SDN controller responsible for managing the OpenFlow protocol and controlling OVS switches.
- **Flow Blocker**: Enforces network security policies by managing flow rules.
- **Simple Switch**: Provides basic packet forwarding capabilities.
- **ETCD Cluster**: A 3-node ETCD cluster for configuration management across the SDN components.

## Prerequisites

Make sure you have the following installed:
- **Docker**: For containerization.
- **Docker Compose** (optional): For managing multiple containers.
- **Mininet**: Network emulator for simulating network topologies.
- **Open vSwitch (OVS)**: For virtual switches.
- **Python 3.6+**: Required for running the Mininet setup script.
- **Git**: To clone this repository.

## Steps to Set Up the Environment

### 1. Clone the Repository

First, clone the repository to your local machine:

```bash
git clone https://gitlab.com/saedi.yasin/evolved-microservice-based-sdn-network.git
cd <repository-name>
```
### 2. Build Docker Images
Build the Docker images for the Ryu-Core, Flow Blocker, Simple Switch, and ETCD components.

```bash
# Build Ryu-Core Image
docker build -t ryu-core ./ryu_core
```
# Build Flow Blocker Image
```bash
docker build -t flow-blocker ./flow_blocker
```
# Build Simple Switch Image
```bash
docker build -t simple-switch-rest-vr ./simple_switch
```
# (Optional) Build ETCD Image if using a custom ETCD
```bash
docker build -t etcd-image ./etcd
```

### 3. Set Up the ETCD Cluster
Create a 3-node ETCD cluster using Docker:

```bash
# Create ETCD network
docker network create --subnet=192.168.253.0/24 etcd-network
# Run ETCD containers
for i in {1..3}; do
    docker run -d --name etcd$i --network etcd-network --ip 192.168.253.$((10+i)) \
    -p $((2378 + i)):2379 -p $((2380 + i)):2380 \
    -e ALLOW_NONE_AUTHENTICATION=yes \
    bitnami/etcd:latest
done
```
### 4. Set Up the Ryu-Core, Flow Blocker, and Simple Switch Containers
Run the Docker containers for Ryu-Core, Flow Blocker, and Simple Switch for each domain. The example below sets up two domains (Domain 1 and Domain 2):

```bash
# Domain 1 (Controller 1, Flow Blocker 1, Simple Switch 1)
docker run -d --name ryu-core-1 --network ryu-network-1 --ip 192.168.10.10 \
-e SIMPLESWITCH_URL="http://192.168.10.20:9090/packetin" \
-e FLOWBLOCKER_URL="http://192.168.10.30:7070/flowblocker/domain_table" \
-e CONTROLLER_ID="192.168.10.10" \
-e OFP_TCP_PORT=6633 \
-e WSGI_PORT=8080 \
-p 6633:6633 -p 8080:8080 ryu-core

docker run -d --name simple-switch-1 --network ryu-network-1 --ip 192.168.10.20 \
-e RYU_BASE_URL="http://192.168.10.10:8080" \
-p 9090:9090 simple-switch-rest-vr

docker run -d --name flow-blocker-1 --network ryu-network-1 --ip 192.168.10.30 \
-e RYU_BASE_URL="http://192.168.10.10:8080" \
-p 7070:7070 flow-blocker

# Domain 2 (Controller 2, Flow Blocker 2, Simple Switch 2)
docker run -d --name ryu-core-2 --network ryu-network-2 --ip 192.168.11.10 \
-e SIMPLESWITCH_URL="http://192.168.11.20:9092/packetin" \
-e FLOWBLOCKER_URL="http://192.168.11.30:7071/flowblocker/domain_table" \
-e CONTROLLER_ID="192.168.11.10" \
-e OFP_TCP_PORT=6634 \
-e WSGI_PORT=8081 \
-p 6634:6634 -p 8081:8081 ryu-core

docker run -d --name simple-switch-2 --network ryu-network-2 --ip 192.168.11.20 \
-e RYU_BASE_URL="http://192.168.11.10:8081" \
-p 9092:9090 simple-switch-rest-vr

docker run -d --name flow-blocker-2 --network ryu-network-2 --ip 192.168.11.30 \
-e RYU_BASE_URL="http://192.168.11.10:8081" \
-p 7071:7070 flow-blocker
5. Connect Flow Blockers to ETCD Cluster
Ensure that the Flow Blockers are connected to the ETCD network for configuration management:
```
```bash
docker network connect etcd-network flow-blocker-1
docker network connect etcd-network flow-blocker-2
```
### 6. Generate Mininet Topology
Use the provided Bash script to generate the Mininet topology based on your configuration (e.g., c=2 and s=2):

```bash
# Run the environment setup script
./setup_env.sh
```
This will generate a Python Mininet topology script (setup_mininet.py) based on the input parameters.

### 7. Run Mininet Topology
After generating the topology, run the Mininet script to simulate the network:

```bash
sudo python3 setup_mininet.py
```

Mininet will set up the OVS switches and connect them to the Ryu-Core controllers, allowing traffic to flow between the hosts.

### 8. Verify and Monitor the Environment
Monitor the containers and network to ensure everything is running smoothly:

```bash
#Ryu-Core Logs:
docker logs ryu-core-1
docker logs ryu-core-2

#Flow Blocker Logs:
docker logs flow-blocker-1
docker logs flow-blocker-2

#Simple Switch Logs:
docker logs simple-switch-1
docker logs simple-switch-2
```

Mininet CLI: Use the Mininet CLI to test connectivity between hosts:

```bash
sudo mn --test pingall
```

9. Tear Down the Environment
Once you're done with the test bed, you can tear down the environment by stopping and removing the Docker containers:

```bash
docker stop $(docker ps -aq)
docker rm $(docker ps -aq)
```
You can also remove the custom Docker networks:

```bash
docker network rm etcd-network ryu-network-1 ryu-network-2
```

This SDN test bed setup allows for easy deployment of Ryu-Core, Flow Blocker, Simple Switch, and an ETCD cluster across multiple domains. By containerizing these components and using Mininet for network simulation, the test bed provides a flexible environment to test and validate SDN architectures, including performance, scalability, and security policies.

vbnet
Copy code

### Explanation:
- The README file provides a step-by-step guide to set up the test bed, starting from building Docker images, configuring ETCD, and running Ryu-Core, Flow Blocker, and Simple Switch instances.
- It includes commands for connecting Flow Blocker to the ETCD cluster, generating Mininet topologies, and monitoring the environment.
- Finally, it provides instructions for tearing down the environment when finished. 








