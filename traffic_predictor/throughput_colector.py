import requests
import time
import pandas as pd
import os

# --- Configurações da Topologia e Coleta ---
RYU_BASE_URL = "http://127.0.0.1:8080"
POLL_INTERVAL_SEC = 1.0
ARQUIVO_CSV = "dataset_cnsm2025_limpo.csv"

# --- A Regra de Ouro (Data Labeling Automático) ---
# Se a porta de um switch receber mais de 50 Mbps, marcamos a amostra como Ataque (1).
# Caso contrário, é tráfego Normal (0).
LIMIAR_ANOMALIA_BPS = 50000000  # 50 Mbps

def obter_switches():
    try:
        resp = requests.get(f"{RYU_BASE_URL}/stats/switches", timeout=2)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print("[-] Falha ao contactar o Controlador Ryu. Ele está a rodar?")
    return []

def obter_portas(dpid):
    try:
        resp = requests.get(f"{RYU_BASE_URL}/stats/port/{dpid}", timeout=2)
        if resp.status_code == 200:
            return resp.json().get(str(dpid), [])
    except: pass
    return []

def main():
    print("[*] Iniciando Coletor Inteligente SDN (Auto-Rotulagem)")
    print(f"[*] Limiar de Ataque: {LIMIAR_ANOMALIA_BPS / 1e6} Mbps")
    print(f"[*] Os dados serão anexados em: {ARQUIVO_CSV}")
    print("-" * 60)

    # Cria o cabeçalho do arquivo CSV mantendo a estrutura original exata do seu testbed
    colunas_originais = ['timestamp', 'dpid', 'port_no', 'rx_bytes', 'tx_bytes', 'rx_vazao_bps', 'tx_vazao_bps', 'anomalia']
    
    if not os.path.isfile(ARQUIVO_CSV):
        df_vazio = pd.DataFrame(columns=colunas_originais)
        df_vazio.to_csv(ARQUIVO_CSV, index=False)

    last_stats = {}
    last_time = {}

    try:
        while True:
            current_timestamp = time.time()
            dpids = obter_switches()
            
            novos_dados = []

            for dpid in dpids:
                portas = obter_portas(dpid)
                
                for port in portas:
                    port_no_raw = port['port_no']
                    if isinstance(port_no_raw, str) and not port_no_raw.isdigit():
                        continue
                    
                    port_no = int(port_no_raw)
                    if port_no > 65000: # Ignora portas lógicas de controle do OVS
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
                            
                            # --- ROTULAGEM AUTOMÁTICA ---
                            anomalia = 1 if rx_bps > LIMIAR_ANOMALIA_BPS else 0
                            
                            # Feedback visual no terminal (só imprime se houver tráfego para não poluir)
                            if rx_bps > 1000000: # Mostrar tráfego acima de 1 Mbps
                                estado_visual = "[!] ATAQUE" if anomalia == 1 else "[+] Normal"
                                print(f"{estado_visual} | Switch {dpid} Porta {port_no} | RX: {rx_bps/1e6:.2f} Mbps")

                            # Guarda a linha estruturada com todas as colunas
                            novos_dados.append({
                                'timestamp': current_timestamp,
                                'dpid': dpid,
                                'port_no': port_no,
                                'rx_bytes': rx_bytes,
                                'tx_bytes': tx_bytes,
                                'rx_vazao_bps': rx_bps,
                                'tx_vazao_bps': tx_bps,
                                'anomalia': anomalia
                            })

                    # Atualiza o estado passado para o cálculo do próximo segundo
                    last_stats[port_id] = {'rx': rx_bytes, 'tx': tx_bytes}
                    last_time[port_id] = current_timestamp
            
            # Escreve o lote de dados do segundo atual no final do arquivo CSV
            if novos_dados:
                df_batch = pd.DataFrame(novos_dados)
                # Garante que a ordem das colunas seja respeitada na hora de salvar
                df_batch = df_batch[colunas_originais]
                df_batch.to_csv(ARQUIVO_CSV, mode='a', header=False, index=False)
                    
            time.sleep(POLL_INTERVAL_SEC)
            
    except KeyboardInterrupt:
        print("\n[*] Coleta interrompida. Dataset atualizado e salvo em segurança!")

if __name__ == "__main__":
    main()