#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FlowBlocker (OpenFlow 1.0 consistent)
- Installs DROP rules (src_ip -> dst_ip) on local switches only (never remote)
- Communicates with peer FlowBlockers for cross-domain installs
- Stores/reads domain tables in ETCD (optional)
- Works with Ryu ofctl_rest (OF 1.0): /stats/flowentry/add
"""

import os
import json
import time
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import requests

# ---------- Configuration via ENV ----------
RYU_BASE_URL      = os.environ.get("RYU_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
PORT              = int(os.environ.get("PORT", "7070"))
CONTROLLER_ID     = os.environ.get("CONTROLLER_ID", "unset_cid")
ETCD_ENDPOINTS    = os.environ.get("ETCD_ENDPOINTS", "").strip()  # "http://ip1:2379,http://ip2:2379"
FB_HOST           = os.environ.get("FB_HOST", None)  # optional for logs/diagnostics
REQUEST_TIMEOUT_S = float(os.environ.get("REQUEST_TIMEOUT_S", "5.0"))

# Priority/timeouts for DROP rule
FLOW_PRIORITY     = int(os.environ.get("FLOW_PRIORITY", "32768"))  # high, above typical L2 learning rules
IDLE_TIMEOUT      = int(os.environ.get("IDLE_TIMEOUT", "0"))
HARD_TIMEOUT      = int(os.environ.get("HARD_TIMEOUT", "0"))

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:FlowBlocker:%(asctime)s - %(message)s",
)
logger = logging.getLogger("FlowBlocker")

# ---------- Optional ETCD (etcd3) ----------
# We gracefully degrade to in-memory if etcd3 is unavailable or endpoints are not set.
_etcd = None
if ETCD_ENDPOINTS:
    try:
        import etcd3
        def _parse_etcd_host_port(url: str) -> Tuple[str, int]:
            # url is like http://a.b.c.d:2379 or a.b.c.d:2379
            u = url.strip()
            if "://" in u:
                u = u.split("://", 1)[1]
            if ":" in u:
                host, port = u.split(":", 1)
                return host, int(port)
            return u, 2379

        # pick the first endpoint for client ops (watch/aggregations can be done via any member)
        _first = ETCD_ENDPOINTS.split(",")[0].strip()
        _h, _p = _parse_etcd_host_port(_first)
        _etcd = etcd3.client(host=_h, port=_p, timeout=5)
        logger.info(f"ETCD client initialized against {_h}:{_p}")
    except Exception as e:
        logger.error(f"Failed to init etcd3 client: {e}. Falling back to in-memory store.")
        _etcd = None

# ---------- In-memory domain table (always maintained) ----------
# Canonical structure expected from Emitter:
# {
#   "switches": {
#       "<dpid>": {"cid": "<controller_id>", "fbid": "<fb_host>", "fb_port": <port>, ...}
#   },
#   "hosts": {
#       "10.0.0.1": {"cid": "<controller_id>", "dpid": <int>, "fbid": "<fb_host>", "fb_port": <port>, "mac": "...", "port": <int>}
#   }
# }
_local_domain_table: Dict[str, Any] = {"switches": {}, "hosts": {}}

# ---------- Flask App ----------
from flask import Flask, jsonify, request

app = Flask(__name__)

# ---------- Utilities ----------

def now_ns() -> int:
    return time.time_ns()

def _metric(tag: str, msg: str) -> None:
    logger.info(f"[METRICS][{tag}] {msg}")

def _find_host(ip: str, domain_table: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    hosts = domain_table.get("hosts", {})
    return hosts.get(ip)

def _aggregate_domain_table() -> Dict[str, Any]:
    """
    Aggregate domain tables across all CIDs from ETCD if available,
    else return local only.
    """
    if not _etcd:
        return _local_domain_table

    agg_switches: Dict[str, Any] = {}
    agg_hosts: Dict[str, Any] = {}

    try:
        # keys are: flowblocker/domain_table/<cid>
        prefix = "flowblocker/domain_table/"
        for value, meta in _etcd.get_prefix(prefix):
            try:
                dt = json.loads(value.decode("utf-8"))
                # merge switches
                for dpid, sinfo in dt.get("switches", {}).items():
                    agg_switches[str(dpid)] = sinfo
                # merge hosts
                for ip, hinfo in dt.get("hosts", {}).items():
                    agg_hosts[ip] = hinfo
            except Exception as e:
                logger.error(f"Skipping malformed domain table from ETCD: {e}")

        return {"switches": agg_switches, "hosts": agg_hosts}
    except Exception as e:
        logger.error(f"ETCD aggregation failed: {e}. Using local domain table.")
        return _local_domain_table

def _store_domain_table_in_etcd(domain_table: Dict[str, Any]) -> None:
    if not _etcd:
        return
    key = f"flowblocker/domain_table/{CONTROLLER_ID}"
    try:
        _etcd.put(key, json.dumps(domain_table))
        _metric("ETCD_WRITE",
                f"cid={CONTROLLER_ID} key={key} ts_write_ns={now_ns()}")
    except Exception as e:
        logger.error(f"Failed to write domain table to ETCD: {e}")

def build_block_flow(dpid: int, src_ip: str, dst_ip: str) -> Dict[str, Any]:
    """
    Build an **OpenFlow 1.0** drop flow for src_ip -> dst_ip on a specific DPID.
    Uses OF1.0 fields: dl_type=2048 (IPv4), nw_src, nw_dst.
    """
    flow = {
        "dpid": int(dpid),
        "cookie": 0,
        "idle_timeout": IDLE_TIMEOUT,
        "hard_timeout": HARD_TIMEOUT,
        "priority": FLOW_PRIORITY,
        "match": {
            "dl_type": 2048,        # 0x0800 IPv4
            "nw_src": src_ip,
            "nw_dst": dst_ip
        },
        "actions": []               # empty => DROP
    }
    return flow

def add_flow(flow: Dict[str, Any]) -> bool:
    """
    Add flow via Ryu REST ofctl_rest (OpenFlow 1.0).
    Endpoint: /stats/flowentry/add
    """
    url = f"{RYU_BASE_URL}/stats/flowentry/add"
    try:
        r = requests.post(url, json=flow, timeout=REQUEST_TIMEOUT_S)
        if r.status_code == 200:
            logger.info(f"Flow added successfully via Ryu REST API.")
            return True
        else:
            logger.error(f"Failed to add flow via Ryu REST API: {r.status_code} - {r.text}")
            return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Exception adding flow via Ryu REST API: {e}")
        return False

def send_flow_to_peer(url: str, flow: Dict[str, Any]) -> bool:
    """
    Send a built flow (for peer's local DPID) to a peer FlowBlocker instance.
    """
    try:
        r = requests.post(url, json=flow, timeout=REQUEST_TIMEOUT_S)
        if r.status_code == 200:
            logger.info(f"Flow sent successfully to peer at {url}.")
            return True
        else:
            logger.error(f"Failed to send flow to peer at {url}: {r.status_code} - {r.text}")
            return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Exception occurred while sending flow to peer at {url}: {e}")
        return False

def _peer_endpoint(hostinfo: Dict[str, Any]) -> Optional[str]:
    fbid = hostinfo.get("fbid")
    fb_port = hostinfo.get("fb_port")
    if not fbid or not fb_port:
        return None
    return f"http://{fbid}:{fb_port}/flowblocker/receive_flow"

def _install_local_drop_for(hostinfo: Dict[str, Any], src_ip: str, dst_ip: str) -> bool:
    dpid = hostinfo.get("dpid")
    if dpid is None:
        logger.error(f"Host DPID information missing for {hostinfo}.")
        return False
    flow = build_block_flow(int(dpid), src_ip, dst_ip)
    return add_flow(flow)

# ---------- Cross-controller coordination (fixed, OF1.0 consistent) ----------

def communicate_with_other_flowblocker(src_ip: str, dst_ip: str,
                                       src_host: Dict[str, Any],
                                       dst_host: Dict[str, Any]) -> bool:
    """
    Only install on *local* DPIDs. For the remote domain's DPID, send the flow to its FlowBlocker.
    """
    try:
        if src_host.get("cid") == CONTROLLER_ID and dst_host.get("cid") == CONTROLLER_ID:
            # Both hosts belong to this controller: install both locally.
            ok1 = _install_local_drop_for(src_host, src_ip, dst_ip)
            ok2 = _install_local_drop_for(dst_host, src_ip, dst_ip)
            return ok1 and ok2

        elif src_host.get("cid") == CONTROLLER_ID:
            # We manage the source: install locally for src->dst; ask dest domain to install on its DPID.
            ok_local = _install_local_drop_for(src_host, src_ip, dst_ip)

            dest_url = _peer_endpoint(dst_host)
            if not dest_url:
                logger.error(f"Destination FlowBlocker info missing for {dst_host}.")
                return False

            # Build flow for destination DPID (remote domain will install it).
            flow_dst = build_block_flow(int(dst_host["dpid"]), src_ip, dst_ip)
            ok_remote = send_flow_to_peer(dest_url, flow_dst)
            return ok_local and ok_remote

        elif dst_host.get("cid") == CONTROLLER_ID:
            # We manage the destination: install locally for src->dst on dest DPID; ask source domain to install on its DPID.
            ok_local = _install_local_drop_for(dst_host, src_ip, dst_ip)

            src_url = _peer_endpoint(src_host)
            if not src_url:
                logger.error(f"Source FlowBlocker info missing for {src_host}.")
                return False

            flow_src = build_block_flow(int(src_host["dpid"]), src_ip, dst_ip)
            ok_remote = send_flow_to_peer(src_url, flow_src)
            return ok_local and ok_remote

        else:
            # Neither host is under this controller; we shouldn't act.
            logger.error(f"Neither host is managed by this controller ({CONTROLLER_ID}).")
            return False

    except Exception as e:
        logger.error(f"Error in cross-controller communication: {e}")
        return False

# ---------- Flask endpoints ----------

@app.route("/")
def index():
    cid = CONTROLLER_ID
    host = FB_HOST or "unknown"
    return f"FlowBlocker Service is running on Controller {cid} ({host})"

@app.route("/flowblocker/domain_table", methods=["POST"])
def update_domain_table():
    """
    Emitter posts domain table here. We store locally and write to ETCD (if configured).
    """
    try:
        dt = request.get_json(force=True, silent=False)
        if not isinstance(dt, dict):
            return jsonify({"error": "domain_table must be a JSON object"}), 400

        # Normalize DPIDs as str keys
        switches = dt.get("switches", {})
        hosts = dt.get("hosts", {})
        if not isinstance(switches, dict) or not isinstance(hosts, dict):
            return jsonify({"error": "domain_table requires 'switches' and 'hosts' objects"}), 400

        _local_domain_table["switches"] = {str(k): v for k, v in switches.items()}
        _local_domain_table["hosts"]    = hosts

        _store_domain_table_in_etcd(_local_domain_table)

        _metric("ETCD_WRITE",
                f"cid={CONTROLLER_ID} key=flowblocker/domain_table/{CONTROLLER_ID} ts_write_ns={now_ns()}")
        logger.info("Domain table successfully updated.")
        return jsonify({"message": "Domain table updated"}), 200

    except Exception as e:
        logger.error(f"Failed to update domain table: {e}")
        return jsonify({"error": "Failed to update domain table"}), 500

@app.route("/flowblocker/domain_table", methods=["GET"])
def get_domain_table():
    """
    Return aggregated domain table (ETCD prefix) or local table if ETCD not available.
    """
    dt = _aggregate_domain_table()
    return jsonify(dt), 200

@app.route("/flowblocker/receive_flow", methods=["POST"])
def receive_flow():
    """
    Peer FlowBlocker sends a fully built OF1.0 flow that targets our *local* DPID. Just add it.
    """
    try:
        flow = request.get_json(force=True, silent=False)
        if not isinstance(flow, dict):
            return jsonify({"error": "flow must be a JSON object"}), 400

        # Safety check: ensure OF1.0 fields exist
        m = flow.get("match", {})
        if m.get("dl_type") != 2048 or "nw_src" not in m or "nw_dst" not in m:
            return jsonify({"error": "flow must be OF1.0 with dl_type=2048, nw_src, nw_dst"}), 400

        ok = add_flow(flow)
        if not ok:
            return jsonify({"error": "Failed to install flow rule"}), 500

        logger.info("Flow rule installed successfully.")
        return jsonify({"message": "Flow rule installed"}), 200

    except Exception as e:
        logger.error(f"Failed to install flow via receive_flow: {e}")
        return jsonify({"error": "Failed to install flow rule"}), 500

@app.route("/flowblocker/service", methods=["POST"])
def flowblocker_service():
    """
    Northbound API to block traffic from src_ip to dst_ip.
    This will:
      - find src/dst hosts from aggregated domain table (ETCD or local)
      - install DROP on *local* DPID(s)
      - ask peer(s) to install on their local DPID(s)
    """
    ts_decide = now_ns()
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    src_ip = (payload or {}).get("src_ip")
    dst_ip = (payload or {}).get("dst_ip")
    if not src_ip or not dst_ip:
        return jsonify({"error": "src_ip and dst_ip are required"}), 400

    logger.info(f"Service request to block traffic from {src_ip} to {dst_ip} received.")
    _metric("POLICY_APPLY", f"policy_id={_policy_id(src_ip, dst_ip)} src={src_ip} dst={dst_ip} ts_decide_ns={ts_decide}")

    try:
        dt = _aggregate_domain_table()
        src_host = _find_host(src_ip, dt)
        dst_host = _find_host(dst_ip, dt)

        if not src_host or not dst_host:
            return jsonify({"error": "Source or destination host not found in domain table"}), 404

        success = communicate_with_other_flowblocker(src_ip, dst_ip, src_host, dst_host)
        if not success:
            logger.error("Failed to install cross-controller flow rules.")
            return jsonify({"error": "Failed to install flow rule(s)"}), 500

        logger.info("Cross-controller flow rules installed successfully.")
        return jsonify({"message": "Cross-controller flow rules installed successfully",
                        "policy_id": _policy_id(src_ip, dst_ip)}), 200

    except Exception as e:
        logger.error(f"Error while handling service request: {e}")
        return jsonify({"error": "Internal server error"}), 500

def _policy_id(src_ip: str, dst_ip: str) -> str:
    """
    A stable-ish hexdigest-like ID (no external deps) to tag logs.
    """
    import hashlib
    return hashlib.sha1(f"{src_ip}->{dst_ip}".encode("utf-8")).hexdigest()

# ---------- Main ----------

if __name__ == "__main__":
    logger.info(f"FlowBlocker Service is running on Controller {CONTROLLER_ID}")
    app.run(host="0.0.0.0", port=PORT, debug=False)


