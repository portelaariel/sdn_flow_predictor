import requests
import time
import pickle
import pandas as pd
from datetime import datetime

# Estrutura para evitar re-injetar bloqueios na mesma porta
politicas_aplicadas = set() 
cached_culprit = None

# --- Configurações ---
RYU_BASE_URL = "http://127.0.0.1:8080"
FLOWBLOCKER_URL = "http://127.0.0.1:7070/flowblocker/service"
POLL_INTERVAL_SEC = 1.0

# Carrega os modelos treinados
try:
    with open('modelo_anomalia.pkl', 'rb') as f:
        clf_anomalia = pickle.load(f)
    with open('modelo_vazao.pkl', 'rb') as f:
        reg_vazao = pickle.load(f)
    print("[+] Modelos de IA carregados com sucesso!")
except FileNotFoundError:
    print("[-] Erro: Modelos .pkl não encontrados. Execute o treinamento primeiro.")
    exit(1)

def obter_switches():
    try:
        resp = requests.get(f"{RYU_BASE_URL}/stats/switches", timeout=2)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print("[-] [ALERTA CRÍTICO] Controlador Ryu não responde (Saturado/Timeout)!")
    return []

def obter_portas(dpid):
    try:
        resp = requests.get(f"{RYU_BASE_URL}/stats/port/{dpid}", timeout=2)
        if resp.status_code == 200:
            return resp.json().get(str(dpid), [])
    except: pass
    return []

def descobrir_ip_culpado(dpid):
    global cached_culprit
    try:
        resp = requests.get(f"{RYU_BASE_URL}/stats/flow/{dpid}", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            fluxos = data.get(str(dpid), [])
            candidatos = [f for f in fluxos if f['match'].get('dl_type') == 2048 and f['byte_count'] > 0]
            if candidatos:
                candidatos.sort(key=lambda x: x['byte_count'], reverse=True)
                match = candidatos[0]['match']
                cached_culprit = (match['nw_src'], match['nw_dst'])
                return cached_culprit
    except: pass
    return cached_culprit

def mitigar_ataque(src_ip, dst_ip, dpid, port_no):
    """ Notifica o Flow Blocker e aplica um DROP definitivo na porta de entrada do OVS """
    chave_porta = f"{dpid}-{port_no}"
    if chave_porta in politicas_aplicadas:
        return 

    # 1. Notifica o Flow Blocker para manter a sintonização Cross-Domain da arquitetura
    payload = {"src_ip": src_ip, "dst_ip": dst_ip}
    try:
        requests.post(FLOWBLOCKER_URL, json=payload, timeout=2)
    except: pass

    # 2. Injeção direta por Porta Ingress (Infalível no OpenFlow 1.0)
    hard_drop_rule = {
        "dpid": dpid,
        "priority": 65535, # Prioridade máxima absoluta
        "match": {
            "in_port": port_no
        },
        "actions": [] # Lista vazia indica descarte (DROP)
    }
    
    try:
        print(f"[!] INJETANDO REGRA DROP NA PORTA {port_no} DO SWITCH {dpid}...")
        resp = requests.post(f"{RYU_BASE_URL}/stats/flowentry/add", json=hard_drop_rule, timeout=3)
        if resp.status_code == 200:
            print(f"[+] SUCESSO: Porta {port_no} mitigada diretamente no core da rede!")
            politicas_aplicadas.add(chave_porta)
        else:
            print(f"[-] O Ryu rejeitou a injeção na porta: {resp.text}")
    except Exception as e:
        print(f"[-] Erro de comunicação com o Ryu para mitigação: {e}")

def main():
    print(f"[*] Iniciando Preditor em Tempo Real (Antecipação: 10s)")
    print(f"[*] Conectado ao Ryu em {RYU_BASE_URL}")
    print(f"[*] Conectado ao Flow Blocker em {FLOWBLOCKER_URL}")
    print("-" * 50)

    last_stats = {}
    last_time = {}

    while True:
        current_timestamp = time.time()
        dpids = obter_switches()
        
        for dpid in dpids:
            portas = obter_portas(dpid)
            
            for port in portas:
                port_no_raw = port['port_no']
                if isinstance(port_no_raw, str) and not port_no_raw.isdigit():
                    continue
                
                port_no = int(port_no_raw)
                if port_no > 65000:
                    continue
                    
                rx_bytes = port['rx_bytes']
                tx_bytes = port['tx_bytes']
                port_id = f"{dpid}_{port_no}"
                
                if port_id in last_stats:
                    delta_time = current_timestamp - last_time[port_id]
                    delta_rx = rx_bytes - last_stats[port_id]['rx']
                    delta_tx = tx_bytes - last_stats[port_id]['tx']
                    
                    if delta_time > 0 and delta_rx >= 0 and delta_tx >= 0:
                        rx_bps = (delta_rx * 8) / delta_time
                        tx_bps = (delta_tx * 8) / delta_time
                        
                        print(f"DEBUG: Switch {dpid} Porta {port_no} | RX: {rx_bps/1e6:.2f} Mbps | TX: {tx_bps/1e6:.2f} Mbps")

                        # Prepara a entrada da predição
                        features = pd.DataFrame([{
                            'rx_bytes': rx_bytes, 
                            'tx_bytes': tx_bytes, 
                            'rx_vazao_bps': rx_bps, 
                            'tx_vazao_bps': tx_bps
                        }])
                        
                        is_anomalia = clf_anomalia.predict(features)[0]
                        
                        # Gatilho de segurança: ativado se a IA detectar ou se o RX passar de 500 Mbps
                        if is_anomalia == 1 or rx_bps > 500000000:
                            human_time = datetime.fromtimestamp(current_timestamp).strftime('%H:%M:%S')
                            print(f"\n[!!! ALERTA {human_time} !!!] Saturação detectada na entrada!")
                            
                            # Tenta coletar os IPs apenas para fins de registro e envio ao Blocker
                            resultado = descobrir_ip_culpado(dpid)
                            if resultado and isinstance(resultado, tuple) and resultado[0] is not None:
                                s_ip, d_ip = resultado
                            else:
                                s_ip, d_ip = "10.0.0.1", "10.0.0.2" # Fallback para o payload
                            
                            # Executa a mitigação cirúrgica por porta
                            mitigar_ataque(s_ip, d_ip, dpid, port_no)

                last_stats[port_id] = {'rx': rx_bytes, 'tx': tx_bytes}
                last_time[port_id] = current_timestamp
                
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()