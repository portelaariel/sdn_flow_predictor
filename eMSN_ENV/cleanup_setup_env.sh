#!/bin/bash

# Function to stop and remove all Docker containers
cleanup_all_containers() {
    echo "Stopping all running containers..."
    sudo docker stop $(sudo docker ps -q) || true

    echo "Removing all containers..."
    sudo docker rm $(sudo docker ps -a -q) || true
}

# Function to remove all Docker networks
cleanup_all_networks() {
    echo "Removing all networks..."
    sudo docker network prune -f || true
}

# Function to clean up Mininet resources
cleanup_mininet() {
    echo "Cleaning up Mininet..."
    sudo mn -c || true
}

# Cleanup all containers
cleanup_all_containers

# Cleanup all networks
cleanup_all_networks

# Clean up Mininet
cleanup_mininet

echo "Cleanup complete."

