#!/bin/bash

# Step to stop and remove all related Docker containers
echo "Stopping and removing all Docker containers related to the setup..."
sudo docker ps -a | grep 'ryu_controller_vr\|simple_switch_rest_vr' | awk '{print $1}' | xargs -r sudo docker stop
sudo docker ps -a | grep 'ryu_controller_vr\|simple_switch_rest_vr' | awk '{print $1}' | xargs -r sudo docker rm

# Step to remove Docker networks
echo "Removing all Docker networks related to the setup..."
sudo docker network ls | grep 'ryu-network' | awk '{print $2}' | xargs -r sudo docker network rm

# Step to clean up Mininet
echo "Cleaning up Mininet..."
sudo mn -c

echo "Cleanup complete!"

