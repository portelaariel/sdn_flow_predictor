import logging
import requests
import os
import json
import etcd3
from datetime import datetime
from flask import Flask, request, jsonify
from threading import Thread
import time

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FlowBlocker")

# Environment variables and configurations
RYU_BASE_URL = os.getenv('RYU_BASE_URL', 'http://127.0.0.1:8080')
CONTROLLER_ID = os.getenv('CONTROLLER_ID', RYU_BASE_URL.split('//')[1].split(':')[0])
PORT = int(os.getenv('PORT', 7070))
ETCD_ENDPOINTS = os.getenv('ETCD_ENDPOINTS', '127.0.0.1:2379').split(',')

# ETCD Configuration
ETCD_KEY_PREFIX = 'flowblocker/domain_table/'
ETCD_KEY = f"{ETCD_KEY_PREFIX}{CONTROLLER_ID}"

# Initialize ETCD client
etcd_available = False

def establish_etcd_connection():
    global etcd_available
    while not etcd_available:
        try:
            etcd_hosts = [(ep.split(':')[0], int(ep.split(':')[1])) for ep in ETCD_ENDPOINTS]
            global etcd
            etcd = etcd3.client(host=etcd_hosts[0][0], port=etcd_hosts[0][1])
            etcd.status()  # Test connection
            etcd_available = True
            logger.info(f"{datetime.now()} - Connected to ETCD at {ETCD_ENDPOINTS[0]}")
        except Exception as e:
            logger.error(f"{datetime.now()} - Failed to connect to ETCD: {e}. Retrying in 10 seconds.")
            time.sleep(10)

# Start ETCD connection establishment in a separate thread
etcd_thread = Thread(target=establish_etcd_connection)
etcd_thread.start()

# In-memory domain table as a fallback
domain_table = {
    "hosts": {},
    "switches": {}
}

def convert_keys_to_strings(data):
    """Recursively converts all dictionary keys to strings."""
    if isinstance(data, dict):
        return {str(key): convert_keys_to_strings(value) for key, value in data.items()}
    elif isinstance(data, list):
        return [convert_keys_to_strings(element) for element in data]
    else:
        return data

@app.route('/')
def index():
    message = f"FlowBlocker Service is running on Controller {CONTROLLER_ID}"
    logger.info(f"{datetime.now()} - {message}")
    return message

@app.route('/flowblocker/domain_table', methods=['POST'])
def update_domain_table():
    """
    Updates the domain table with topology information received from LLDP handler.
    """
    try:
        topology = request.get_json()
        if not topology:
            logger.error(f"{datetime.now()} - No topology data received in request.")
            return jsonify({'error': 'Invalid topology data'}), 400

        logger.info(f"{datetime.now()} - Received topology update.")

        # Update switches
        switches = topology.get('switches', {})
        for switch_id, switch_info in switches.items():
            domain_table['switches'][switch_id] = {
                "cid": switch_info.get("cid"),
                "fbid": switch_info.get("fbid"),
                "fb_port": switch_info.get("fb_port"),
                "status": switch_info.get("status", "active")
            }

        # Update hosts with IP as the key
        hosts = topology.get('hosts', {})
        for host_ip, host_info in hosts.items():
            domain_table['hosts'][host_ip] = {
                "cid": host_info.get("cid"),
                "fbid": host_info.get("fbid"),
                "fb_port": host_info.get("fb_port"),
                "mac": host_info.get("mac"),
                "dpid": host_info.get("dpid"),
                "port": host_info.get("port")
            }

        # Store updated domain table in ETCD
        if etcd_available:
            store_domain_table_in_etcd(domain_table)

        logger.info(f"{datetime.now()} - Domain table successfully updated.")
        return jsonify({'message': 'Domain table updated successfully'}), 200

    except Exception as e:
        logger.error(f"{datetime.now()} - Error updating domain table: {e}")
        return jsonify({'error': 'Failed to update domain table'}), 500

@app.route('/flowblocker/domain_table', methods=['GET'])
def get_domain_table():
    """
    Retrieves the current domain table.
    """
    try:
        current_domain_table = get_network_topology()
        domain_table_str_keys = convert_keys_to_strings(current_domain_table)
        logger.info(f"{datetime.now()} - Domain table retrieved successfully.")
        return jsonify(domain_table_str_keys), 200
    except Exception as e:
        logger.error(f"{datetime.now()} - Error retrieving domain table: {e}")
        return jsonify({'error': 'Failed to retrieve domain table'}), 500

def store_domain_table_in_etcd(domain_table):
    """
    Stores the domain table in ETCD.
    """
    try:
        etcd.put(ETCD_KEY, json.dumps(domain_table))
        logger.info(f"{datetime.now()} - Domain table stored in ETCD under key {ETCD_KEY}.")
    except Exception as e:
        logger.error(f"{datetime.now()} - Error storing domain table in ETCD: {e}")

def get_network_topology():
    """
    Retrieves and aggregates the network topology from ETCD or in-memory storage.
    """
    aggregated_topology = {"hosts": {}, "switches": {}}

    if etcd_available:
        try:
            for value, metadata in etcd.get_prefix(ETCD_KEY_PREFIX):
                controller_domain_table = json.loads(value.decode('utf-8'))
                aggregated_topology['hosts'].update(controller_domain_table.get('hosts', {}))
                aggregated_topology['switches'].update(controller_domain_table.get('switches', {}))
            logger.info(f"{datetime.now()} - Aggregated domain table retrieved from ETCD.")
            return aggregated_topology
        except Exception as e:
            logger.error(f"{datetime.now()} - Error retrieving domain table from ETCD: {e}. Using in-memory domain table.")

    # Fallback to in-memory domain table
    logger.warning(f"{datetime.now()} - Using in-memory domain table.")
    return domain_table

@app.route('/flowblocker/service', methods=['POST'])
def flowblocker_service():
    """
    Receives a service request to block traffic between specific hosts.
    """
    try:
        data = request.get_json()
        if not data:
            logger.error(f"{datetime.now()} - No data received in service request.")
            return jsonify({'error': 'Invalid request data'}), 400

        src_ip = data.get('src_ip')
        dst_ip = data.get('dst_ip')

        if not src_ip or not dst_ip:
            logger.error(f"{datetime.now()} - Missing src_ip or dst_ip in request.")
            return jsonify({'error': 'Missing required parameters: src_ip and dst_ip'}), 400

        logger.info(f"{datetime.now()} - Service request to block traffic from {src_ip} to {dst_ip} received.")

        topology = get_network_topology()

        src_host = topology['hosts'].get(src_ip)
        dst_host = topology['hosts'].get(dst_ip)

        if not src_host or not dst_host:
            logger.error(f"{datetime.now()} - Source or destination host information not found in domain table.")
            return jsonify({'error': 'Source or destination host not found'}), 404

        src_controller = src_host.get('cid')
        dst_controller = dst_host.get('cid')

        if src_controller == CONTROLLER_ID and dst_controller == CONTROLLER_ID:
            # Both hosts are under the same controller
            src_dpid = src_host.get('dpid')
            dst_dpid = dst_host.get('dpid')

            if not src_dpid or not dst_dpid:
                logger.error(f"{datetime.now()} - DPID information missing for source or destination host.")
                return jsonify({'error': 'DPID information missing for hosts'}), 500

            # Install flow on source switch
            flow_src = build_block_flow(src_dpid, src_ip, dst_ip)
            success_src = add_flow(flow_src)

            # Install flow on destination switch
            flow_dst = build_block_flow(dst_dpid, src_ip, dst_ip)
            success_dst = add_flow(flow_dst)

            if success_src and success_dst:
                logger.info(f"{datetime.now()} - Flow rules installed successfully on both source and destination switches.")
                return jsonify({'message': 'Flow rules installed successfully on both switches'}), 200
            else:
                logger.error(f"{datetime.now()} - Failed to install flow rules on one or both switches.")
                return jsonify({'error': 'Failed to install flow rules on switches'}), 500

        elif src_controller == CONTROLLER_ID or dst_controller == CONTROLLER_ID:
            # Cross-controller communication
            success = communicate_with_other_flowblocker(src_ip, dst_ip, src_host, dst_host)
            if success:
                logger.info(f"{datetime.now()} - Cross-controller flow rules installed successfully.")
                return jsonify({'message': 'Cross-controller flow rules installed successfully'}), 200
            else:
                logger.error(f"{datetime.now()} - Failed to install cross-controller flow rules.")
                return jsonify({'error': 'Failed to install cross-controller flow rules'}), 500
        else:
            logger.error(f"{datetime.now()} - Neither source nor destination host is managed by this controller.")
            return jsonify({'error': 'Hosts are not managed by this controller'}), 403

    except Exception as e:
        logger.error(f"{datetime.now()} - Error processing service request: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/flowblocker/receive_flow', methods=['POST'])
def receive_flow():
    """
    Receives a flow from another FlowBlocker instance for cross-controller communication.
    """
    try:
        flow = request.get_json()
        if not flow:
            logger.error(f"{datetime.now()} - No flow data received in receive_flow request.")
            return jsonify({'error': 'Invalid flow data'}), 400

        logger.info(f"{datetime.now()} - Received flow for installation: {flow}")

        success = add_flow(flow)
        if success:
            logger.info(f"{datetime.now()} - Flow rule installed successfully.")
            return jsonify({'message': 'Flow rule installed successfully'}), 200
        else:
            logger.error(f"{datetime.now()} - Failed to install flow rule.")
            return jsonify({'error': 'Failed to install flow rule'}), 500

    except Exception as e:
        logger.error(f"{datetime.now()} - Error processing received flow: {e}")
        return jsonify({'error': 'Internal server error'}), 500

def communicate_with_other_flowblocker(src_ip, dst_ip, src_host, dst_host):
    """
    Handles communication with other FlowBlocker instances for cross-controller flow installation.
    """
    try:
        if src_host.get('cid') == CONTROLLER_ID:
            # Current controller manages the source host
            src_dpid = src_host.get('dpid')
            if not src_dpid:
                logger.error(f"{datetime.now()} - Source DPID information missing.")
                return False

            # Install flow on source switch
            flow_src = build_block_flow(src_dpid, src_ip, dst_ip)
            success_src = add_flow(flow_src)

            # Send flow to destination controller
            dst_dpid = dst_host.get('dpid')
            if not dst_dpid:
                logger.error(f"{datetime.now()} - Destination DPID information missing.")
                return False

            flow_dst = build_block_flow(dst_dpid, src_ip, dst_ip)
            success_dst_local = add_flow(flow_dst)

            src_fbid = src_host.get('fbid')
            src_fb_port = src_host.get('fb_port')
            if not src_fbid or not src_fb_port:
                logger.error(f"{datetime.now()} - Source FlowBlocker information missing.")
                return False

            source_url = f"http://{src_fbid}:{src_fb_port}/flowblocker/receive_flow"
            success_dst_remote = send_flow_to_peer(source_url, flow_dst)

            return success_src and success_dst_local and success_dst_remote

        elif dst_host.get('cid') == CONTROLLER_ID:
            # Current controller manages the destination host
            dst_dpid = dst_host.get('dpid')
            if not dst_dpid:
                logger.error(f"{datetime.now()} - Destination DPID information missing.")
                return False

            # Install flow on destination switch
            flow_dst = build_block_flow(dst_dpid, src_ip, dst_ip)
            success_dst = add_flow(flow_dst)

            # Send flow to source controller
            src_fbid = src_host.get('fbid')
            src_fb_port = src_host.get('fb_port')
            if not src_fbid or not src_fb_port:
                logger.error(f"{datetime.now()} - Source FlowBlocker information missing.")
                return False

            source_url = f"http://{src_fbid}:{src_fb_port}/flowblocker/receive_flow"
            success_src = send_flow_to_peer(source_url, flow_dst)

            return success_dst and success_src

        else:
            logger.error(f"{datetime.now()} - Neither host is managed by this controller.")
            return False

    except Exception as e:
        logger.error(f"{datetime.now()} - Error in cross-controller communication: {e}")
        return False

def send_flow_to_peer(url, flow):
    """
    Sends a flow to a peer FlowBlocker instance.
    """
    try:
        response = requests.post(url, json=flow, timeout=5)
        if response.status_code == 200:
            logger.info(f"{datetime.now()} - Flow sent successfully to peer at {url}.")
            return True
        else:
            logger.error(f"{datetime.now()} - Failed to send flow to peer at {url}: {response.status_code} - {response.text}")
            return False
    except requests.exceptions.RequestException as e:
        logger.error(f"{datetime.now()} - Exception occurred while sending flow to peer at {url}: {e}")
        return False

def build_block_flow(dpid, src_ip, dst_ip):
    """
    Builds a flow entry to block traffic between src_ip and dst_ip on the specified dpid.
    """
    flow = {
        "dpid": int(dpid),
        "cookie": 0,
        "idle_timeout": 0,
        "hard_timeout": 0,
        "priority": 32768,
        "match": {
            "eth_type": 0x0800,
            "ipv4_src": src_ip,
            "ipv4_dst": dst_ip
        },
        "actions": []
    }
    logger.debug(f"{datetime.now()} - Built flow: {flow}")
    return flow

def add_flow(flow):
    """
    Adds a flow entry to the switch via Ryu REST API.
    """
    try:
        url = f"{RYU_BASE_URL}/stats/flowentry/add"
        response = requests.post(url, json=flow, timeout=5)
        if response.status_code == 200:
            logger.info(f"{datetime.now()} - Flow added successfully via Ryu REST API.")
            return True
        else:
            logger.error(f"{datetime.now()} - Failed to add flow via Ryu REST API: {response.status_code} - {response.text}")
            return False
    except requests.exceptions.RequestException as e:
        logger.error(f"{datetime.now()} - Exception occurred while adding flow via Ryu REST API: {e}")
        return False

if __name__ == '__main__':
    logger.info(f"{datetime.now()} - Starting FlowBlocker service on port {PORT} for Controller {CONTROLLER_ID}")
    app.run(host='0.0.0.0', port=PORT)

