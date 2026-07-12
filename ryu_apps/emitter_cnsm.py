import requests
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER, CONFIG_DISPATCHER, set_ev_cls
from ryu.lib.packet import packet, ethernet, arp, lldp, ipv4, tcp, udp
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_0
import os
import time  # for ns timestamps if ever needed

simpleswitch = os.getenv("SIMPLESWITCH_URL", "http://127.0.0.1:8090/packetin")

class OfpEmitterLldpHandler(app_manager.RyuApp):
    """Combined OfpEmitter and LldpHandler to propagate events and discover hosts."""

    OFP_VERSIONS = [ofproto_v1_0.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(OfpEmitterLldpHandler, self).__init__(*args, **kwargs)
        self.datapaths = {}
        self.topology = {
            "switches": {},  # Maps switch DPID to its status (connected/disconnected)
            "hosts": {},     # Maps IP to a dict with DPID, IP, port, CID, FBID, and FB_PORT
            "ports": {}      # Tracks which ports are inter-switch ports
        }
        self.flowblocker_url = os.getenv("FLOWBLOCKER_URL", "http://127.0.0.1:7070/flowblocker/domain_table")
        
        # Obtain CID from the environment variable
        self.cid = os.getenv("CONTROLLER_ID", "default_cid")
        #self.cid = os.getenv("CONTROLLER_ID", "default_cid")
        
        # Keep port from FLOWBLOCKER_URL
        _, self.fb_port = self._extract_ip_and_port_from_url(self.flowblocker_url)

        # Address advertised to other FlowBlockers
        self.fbid = os.getenv("FB_PEER_ID", "flow-blocker")
        
        # Extract FBID and FB_PORT from the flowblocker_url
        #self.fbid, self.fb_port = self._extract_ip_and_port_from_url(self.flowblocker_url)

        # Start LLDP loop in a separate thread
        self.lldp_thread = hub.spawn(self._lldp_loop)

    def _extract_ip_and_port_from_url(self, url):
        """Helper function to extract IP address and port from a URL."""
        ip = url.split("//")[1].split(":")[0]
        port = url.split("//")[1].split(":")[1].split("/")[0]  # Extract the port number
        return ip, port

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        self.datapaths[datapath.id] = datapath
        if datapath.id not in self.topology["switches"]:
            self.topology["switches"][datapath.id] = {
                "cid": self.cid,
                "fbid": self.fbid,
                "fb_port": self.fb_port,
                "status": "connected"
            }

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[datapath.id] = datapath
            if datapath.id not in self.topology["switches"]:
                self.topology["switches"][datapath.id] = {
                    "cid": self.cid,
                    "fbid": self.fbid,
                    "fb_port": self.fb_port,
                    "status": "connected"
                }
        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                del self.datapaths[datapath.id]
                if datapath.id in self.topology["switches"]:
                    self.topology["switches"][datapath.id]["status"] = "disconnected"

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        in_port = msg.in_port

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        # === METRICS: PACKET_IN breadcrumb for non-LLDP traffic ===
        # (Keeps behavior untouched; only logs to help offline correlation with FLOW_MOD)
        try:
            if eth:
                ip4 = pkt.get_protocol(ipv4.ipv4)
                l4 = pkt.get_protocol(tcp.tcp) or pkt.get_protocol(udp.udp)
                five_tuple = None
                if ip4:
                    five_tuple = {
                        "src": f"{ip4.src}:{getattr(l4, 'src_port', '')}",
                        "dst": f"{ip4.dst}:{getattr(l4, 'dst_port', '')}",
                        "proto": (l4 and l4.__class__.__name__.upper()) or 'IP'
                    }
                # Only emit for non-LLDP; the LLDP path below handles links
                # The original LLDP check line remains unchanged for compatibility
                self.logger.info(
                    "[METRICS][PACKET_IN] dpid=%s in_port=%s buf=%s eth_src=%s eth_dst=%s five_tuple=%s",
                    dpid, in_port, getattr(msg, 'buffer_id', None),
                    getattr(eth, 'src', None), getattr(eth, 'dst', None), five_tuple
                )
        except Exception as e:
            self.logger.warning("metrics packet_in logging failed: %s", e)

        # Original branching (leave intact)
        if eth and eth.ethertype == ethernet.ether.ETH_TYPE_LLDP:
            self._handle_lldp_packet(dpid, pkt, in_port)
        else:
            if self._is_host_port(dpid, in_port):
                self._record_host(dpid, eth, pkt, in_port)
                self._send_domain_table_to_flowblocker()

        self._forward_packet_to_simpleswitch(msg, dpid)

    def _handle_lldp_packet(self, dpid, pkt, in_port):
        """Handle LLDP packet for discovering links between switches."""
        self._record_switch_link(dpid, pkt, in_port)

    def _is_host_port(self, dpid, port_no):
        """Check if a port is a host port (not an inter-switch port)."""
        return port_no not in self.topology['ports'].get(dpid, set())

    def _record_switch_link(self, dpid, pkt, in_port):
        """Record the link between two switches."""
        lldp_pkt = pkt.get_protocol(lldp.lldp)
        if lldp_pkt:
            src_dpid = dpid
            src_port_no = in_port
            dst_dpid = None
            dst_port_no = None

            for tlv in lldp_pkt.tlvs:
                if isinstance(tlv, lldp.ChassisID):
                    dst_dpid = int(tlv.chassis_id.decode())
                if isinstance(tlv, lldp.PortID):
                    dst_port_no = int(tlv.port_id.decode())

            if dst_dpid and dst_port_no:
                if src_dpid not in self.topology['ports']:
                    self.topology['ports'][src_dpid] = set()
                if dst_dpid not in self.topology['ports']:
                    self.topology['ports'][dst_dpid] = set()

                self.topology['ports'][src_dpid].add(src_port_no)
                self.topology['ports'][dst_dpid].add(dst_port_no)

    def _record_host(self, dpid, eth, pkt, port_no):
        """Record the host connected to a specific port on the switch."""
        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt and arp_pkt.opcode == arp.ARP_REQUEST:
            mac = eth.src
            ip = arp_pkt.src_ip

            if ip not in self.topology["hosts"]:
                self.topology["hosts"][ip] = {
                    'dpid': dpid,
                    'port': port_no,
                    'cid': self.cid,
                    'fbid': self.fbid,
                    'fb_port': self.fb_port,
                    'mac': mac
                }
                # Print whenever a host is discovered
                print(f"Host {mac} with IP {ip} discovered on switch {dpid}, port {port_no}")

    def _forward_packet_to_simpleswitch(self, msg, dpid):
        try:
            packet_json = msg.to_jsondict()
            packet_json['dpid'] = dpid

            # Send packet to the simpleswitch microservice
            requests.post(simpleswitch, json=packet_json)
        except Exception as e:
            print(f"Error forwarding packet to simpleswitch from DPID {dpid}: {e}")

    def _send_domain_table_to_flowblocker(self):
        try:
            # Convert the switches dictionary to a simpler format for sending
            switches_dict = {
                str(switch): {
                    "cid": self.cid,
                    "fbid": self.fbid,
                    "fb_port": self.fb_port
                    # status intentionally omitted; FlowBlocker defaults to 'active' if missing
                } for switch in self.topology["switches"].keys()
            }
            topology_for_sending = {
                "switches": switches_dict,
                "hosts": self.topology["hosts"]
            }

            response = requests.post(self.flowblocker_url, json=topology_for_sending)
            if response.status_code != 200:
                print(f"Failed to send domain table to FlowBlocker: {response.status_code}, {response.text}")
        except Exception as e:
            print(f"Error sending domain table to FlowBlocker: {e}")

    def _lldp_loop(self):
        while True:
            for dpid, dp in self.datapaths.items():
                self._send_lldp_packets(dp)
            hub.sleep(10)  # Send LLDP packets every 10 seconds

    def _send_lldp_packets(self, datapath):
        for port_no in datapath.ports:
            if (port_no == datapath.ofproto.OFPP_LOCAL) or (port_no not in datapath.ports):
                continue  # Skip local port or non-existent port
            pkt = self._create_lldp_packet(datapath, port_no)
            if pkt:
                self._send_packet(datapath, port_no, pkt)

    def _create_lldp_packet(self, datapath, port_no):
        try:
            port_mac = datapath.ports[port_no].hw_addr

            pkt = packet.Packet()
            eth = ethernet.ethernet(
                dst=lldp.LLDP_MAC_NEAREST_BRIDGE,
                src=port_mac,
                ethertype=ethernet.ether.ETH_TYPE_LLDP)
            pkt.add_protocol(eth)

            chassis_id = lldp.ChassisID(
                subtype=lldp.ChassisID.SUB_LOCALLY_ASSIGNED,
                chassis_id=str(datapath.id).encode())
            port_id = lldp.PortID(
                subtype=lldp.PortID.SUB_PORT_COMPONENT,
                port_id=str(port_no).encode())
            ttl = lldp.TTL(ttl=120)
            end = lldp.End()

            pkt.add_protocol(lldp.lldp([chassis_id, port_id, ttl, end]))

            pkt.serialize()
            return pkt
        except KeyError as e:
            print(f"Error creating LLDP packet: Port {port_no} not found on datapath {datapath.id}: {e}")
            return None
        except Exception as e:
            print(f"Error creating LLDP packet: {e}")
            return None

    def _send_packet(self, datapath, port_no, pkt):
        actions = [datapath.ofproto_parser.OFPActionOutput(port_no)]
        out = datapath.ofproto_parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=datapath.ofproto.OFP_NO_BUFFER,
            in_port=datapath.ofproto.OFPP_CONTROLLER,
            actions=actions,
            data=pkt.data)
        datapath.send_msg(out)
