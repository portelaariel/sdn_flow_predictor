# Copyright (C) 2020 Daniel Barattini.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# ============================================================================
# MODIFICAÇÃO L3 (integração FlowPredictor) — 2026-07
# ----------------------------------------------------------------------------
# Antes: todas as regras eram L2 (in_port + dl_src + dl_dst), então
#        /stats/flow/<dpid> nunca expunha nw_src/nw_dst e o FlowPredictor
#        não conseguia criar séries flow:* nem acionar mitigação.
#
# Agora:
#   1. Pacotes IPv4  -> regra L3: {in_port, dl_type=2048, nw_src, nw_dst}
#                       prioridade FORWARD_PRIORITY (3000) < DROP FlowBlocker (32768)
#                       timeouts IPV4_IDLE_TIMEOUT/IPV4_HARD_TIMEOUT (30/0)
#                       => contadores estáveis para o preditor.
#   2. Pacotes ARP   -> regra L2 COM dl_type=2054 no match. Sem o dl_type, a
#                       regra L2 "engoliria" também o IPv4 do mesmo par de MACs
#                       e o PacketIn IPv4 nunca ocorreria (regra L3 jamais
#                       seria instalada).
#   3. Outros tipos  -> apenas packet-out (sem instalar regra).
#   4. LLDP          -> ignorado (comportamento original).
#
# ENV novos (com defaults seguros):
#   FORWARD_PRIORITY    3000   (deve ficar ABAIXO do FLOW_PRIORITY do FlowBlocker)
#   IPV4_IDLE_TIMEOUT   30
#   IPV4_HARD_TIMEOUT   0
#   ARP_IDLE_TIMEOUT    5
#   ARP_HARD_TIMEOUT    5
# ============================================================================

from flask import Flask, request, abort
from lib.packet import packet
from lib.packet import ethernet
from lib.packet import ether_types
from lib.packet import ipv4
import requests
import logging
import time
import base64
import datetime
import os

api = Flask(__name__)

# TODO import from ofp
OFPP_FLOOD = 0xfffb
OFP_DEFAULT_PRIORITY = 32768
OFPFF_SEND_FLOW_REM = 1 << 0
OFP_NO_BUFFER = 0xffffffff

ETH_TYPE_IP = 0x0800   # 2048
ETH_TYPE_ARP = 0x0806  # 2054

SEND_OUT = None

RYU_BASE_URL = os.getenv('RYU_BASE_URL', 'http://127.0.0.1:8080')

# --- Parâmetros L3 (novos) ---------------------------------------------------
# Prioridade de forwarding ABAIXO do DROP do FlowBlocker (32768): em OF 1.0,
# prioridades iguais com matches sobrepostos têm comportamento indefinido —
# o DROP poderia perder para a regra de forwarding.
FORWARD_PRIORITY = int(os.getenv('FORWARD_PRIORITY', '3000'))

# Timeouts das regras IPv4: idle=30 mantém a regra viva durante tráfego
# contínuo (contadores cumulativos estáveis para o FlowPredictor); os 5s/5s
# originais resetavam byte_count a cada reinstalação, distorcendo as séries.
IPV4_IDLE_TIMEOUT = int(os.getenv('IPV4_IDLE_TIMEOUT', '30'))
IPV4_HARD_TIMEOUT = int(os.getenv('IPV4_HARD_TIMEOUT', '0'))

# ARP mantém timeouts curtos (comportamento original).
ARP_IDLE_TIMEOUT = int(os.getenv('ARP_IDLE_TIMEOUT', '5'))
ARP_HARD_TIMEOUT = int(os.getenv('ARP_HARD_TIMEOUT', '5'))
# ------------------------------------------------------------------------------

# Logging (add structured metrics without changing existing prints)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SimpleSwitchREST")

mac_to_port = {}


def build_flow(dpid, match, out_port, buffer_id, cookie=0, idle_timeout=0,
               hard_timeout=0, priority=FORWARD_PRIORITY, flags=1,
               actions=SEND_OUT):
    "Build and return a flow entry based on https://ryu.readthedocs.io/en/latest/app/ofctl_rest.html#add-a-flow-entry"

    if actions == SEND_OUT:
        actions = [{"type": "OUTPUT", "port": out_port}]

    flow = {
        'dpid': dpid,
        'match': match,
        'cookie': cookie,
        'idle_timeout': idle_timeout,
        'hard_timeout': hard_timeout,
        'priority': priority,
        'flags': flags,
        'actions': actions,
        'buffer_id': buffer_id
    }

    return flow


def build_ipv4_flow(dpid, src_ip, dst_ip, in_port, out_port, buffer_id):
    """
    Regra L3 (OpenFlow 1.0 / ofctl_rest):
      match = {in_port, dl_type: 2048, nw_src, nw_dst}
    Expõe nw_src/nw_dst em /stats/flow/<dpid>, habilitando as séries
    flow:<dpid>:<src>-><dst> do FlowPredictor.
    """
    match = {
        'in_port': in_port,
        'dl_type': ETH_TYPE_IP,   # 2048 — obrigatório para nw_src/nw_dst em OF 1.0
        'nw_src': src_ip,
        'nw_dst': dst_ip
    }
    return build_flow(dpid, match, out_port, buffer_id,
                      idle_timeout=IPV4_IDLE_TIMEOUT,
                      hard_timeout=IPV4_HARD_TIMEOUT,
                      priority=FORWARD_PRIORITY)


def build_arp_flow(dpid, src, dst, in_port, out_port, buffer_id):
    """
    Regra L2 restrita a ARP. O dl_type=2054 é ESSENCIAL: sem ele, esta regra
    casaria também com o tráfego IPv4 do mesmo par de MACs direto no switch,
    suprimindo o PacketIn IPv4 e impedindo a instalação da regra L3.
    """
    match = {
        'in_port': in_port,
        'dl_type': ETH_TYPE_ARP,  # 2054 — restringe a regra a ARP
        'dl_src': src,
        'dl_dst': dst
    }
    return build_flow(dpid, match, out_port, buffer_id,
                      idle_timeout=ARP_IDLE_TIMEOUT,
                      hard_timeout=ARP_HARD_TIMEOUT,
                      priority=FORWARD_PRIORITY)


def add_flow(flow):
    "Add a flow entry through REST"
    rest_uri = RYU_BASE_URL + "/stats/flowentry/add"

    # TODO verbose mode
    print("sending {}".format(flow))

    # === METRICS: FLOW_MOD just before sending (nanosecond clock) ===
    try:
        ts_send_ns = time.time_ns()
        logger.info("[METRICS][FLOW_MOD] dpid=%s match=%s buffer_id=%s ts_send_ns=%s",
                    flow.get('dpid'), flow.get('match'), flow.get('buffer_id'), ts_send_ns)
    except Exception:
        # Do not alter behavior if metrics logging fails
        pass

    try:
        r = requests.post(rest_uri, json=flow)
        if r.status_code == 200:
            return True
        else:
            logger.error("add_flow rejected by Ryu: %s - %s", r.status_code, r.text)
            return False
    except requests.exceptions.RequestException as e:
        logger.error("add_flow REST error: %s", e)
        return False


def build_packet(dpid, in_port, out_port, data, buffer_id):
    "Build and return a packet"
    pkt = {
        'dpid': dpid,
        'buffer_id': buffer_id,
        'in_port': in_port,
        'actions': [{"type": "OUTPUT", "port": out_port}],
        'data': data
    }

    return pkt


def send_packet(pkt):
    "Send a packet to a switch through REST"
    rest_uri = RYU_BASE_URL + "/stats/sendpacket"

    start1 = datetime.datetime.now()
    print('send_packet start timestamp', start1)

    r = requests.post(rest_uri, json=pkt)

    if r.status_code == 200:
        return True
    else:
        return False


def extract_data(msg, event_name):
    data = msg[event_name]

    data['encoded_data'] = data['data']
    data['data'] = base64.b64decode(data['encoded_data'])
    data['dpid'] = msg['dpid']

    packet_data = packet.Packet(data['data'])
    eth = packet_data.get_protocol(ethernet.ethernet)

    if eth.ethertype == ether_types.ETH_TYPE_LLDP:
        data['is_lldp'] = True
    else:
        data['is_lldp'] = False

        data['dst'] = eth.dst
        data['src'] = eth.src
        data['ethertype'] = eth.ethertype

        # --- NOVO: extração IPv4 (base das séries flow:* do FlowPredictor) ---
        ip_pkt = packet_data.get_protocol(ipv4.ipv4)
        if ip_pkt:
            data['is_ipv4'] = True
            data['src_ip'] = ip_pkt.src
            data['dst_ip'] = ip_pkt.dst
        else:
            data['is_ipv4'] = False

    return data


def update_mac_to_port(dpid, src, in_port):
    mac_to_port.setdefault(dpid, {})

    # learn a mac address to avoid FLOOD next time.
    mac_to_port[dpid][src] = in_port


def out_port_lookup(dpid, dst):
    if dst in mac_to_port[dpid]:
        return mac_to_port[dpid][dst]
    else:
        return OFPP_FLOOD


@api.route('/')
def index():
    return 'Simple Switch Rest Server (L3-aware)'


@api.route('/packetin', methods=['POST'])
def post_packetin():
    start1 = datetime.datetime.now()
    print('post_packetin start timestamp', start1)

    if not request.json:
        abort(400)

    start2 = datetime.datetime.now()
    data = extract_data(request.json, "OFPPacketIn")
    stop2 = datetime.datetime.now()
    time_diff = (stop2 - start2)
    ex_time = time_diff.total_seconds() * 1000
    print('extract_data: ', ex_time)

    if data['is_lldp']:
        # ignore lldp packet
        # TODO maybe server side
        return "ACK"

    dpid = data['dpid']
    src = data['src']
    dst = data['dst']
    in_port = data['in_port']
    buffer_id = data['buffer_id']
    encoded_data = data['encoded_data']

    # === METRICS: PACKET_IN as soon as we have the decoded fields (nanosecond clock) ===
    try:
        logger.info("[METRICS][PACKET_IN] dpid=%s in_port=%s src_mac=%s dst_mac=%s "
                    "ipv4=%s src_ip=%s dst_ip=%s buffer_id=%s ts_ns=%s",
                    dpid, in_port, src, dst,
                    data.get('is_ipv4'), data.get('src_ip'), data.get('dst_ip'),
                    buffer_id, time.time_ns())
    except Exception:
        pass

    update_mac_to_port(dpid, src, in_port)
    out_port = out_port_lookup(dpid, dst)

    start3 = datetime.datetime.now()
    if out_port != OFPP_FLOOD:
        # --- NOVO: seleção do tipo de regra por ethertype ---
        if data.get('is_ipv4'):
            # Regra L3: expõe nw_src/nw_dst para o FlowPredictor
            flow = build_ipv4_flow(dpid, data['src_ip'], data['dst_ip'],
                                   in_port, out_port, buffer_id)
            add_flow(flow)
        elif data.get('ethertype') == ETH_TYPE_ARP:
            # Regra ARP disjunta do IPv4 (dl_type=2054)
            flow = build_arp_flow(dpid, src, dst, in_port, out_port, buffer_id)
            add_flow(flow)
        else:
            # Ethertype desconhecido (ex.: IPv6): só packet-out, sem regra.
            print(f'ethertype {data.get("ethertype"):#06x}: forwarding without flow install')
    stop3 = datetime.datetime.now()
    time_diff = (stop3 - start3)
    ex_time = time_diff.total_seconds() * 1000
    print('build_flow: ', ex_time)

    msg = None
    if buffer_id == OFP_NO_BUFFER:
        msg = encoded_data

    start4 = datetime.datetime.now()
    pkt = build_packet(dpid, in_port, out_port, msg, buffer_id)
    stop4 = datetime.datetime.now()
    time_diff = (stop4 - start4)
    ex_time = time_diff.total_seconds() * 1000
    print('build_packet: ', ex_time)

    start5 = datetime.datetime.now()
    send_packet(pkt)
    stop5 = datetime.datetime.now()
    time_diff = (stop5 - start5)
    ex_time = time_diff.total_seconds() * 1000
    print('send_packet: ', ex_time)

    stop1 = datetime.datetime.now()
    time_diff = (stop1 - start1)
    ex_time = time_diff.total_seconds() * 1000
    print('post_packetin: ', ex_time)

    print('post_packetin stop timestamp', stop1)

    return "ACK"


if __name__ == "__main__":
    logger.info("SimpleSwitch L3-aware iniciando: FORWARD_PRIORITY=%s "
                "IPV4 timeouts=%s/%s ARP timeouts=%s/%s",
                FORWARD_PRIORITY, IPV4_IDLE_TIMEOUT, IPV4_HARD_TIMEOUT,
                ARP_IDLE_TIMEOUT, ARP_HARD_TIMEOUT)
    port = int(os.getenv('PORT', 8090))
    api.run(host='0.0.0.0', port=port)