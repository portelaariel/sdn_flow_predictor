# FLOWPREDICTOR - MÓDULO DE PREDIÇÃO DE VAZÃO E DETECÇÃO DE ANOMALIAS

## 1. VISÃO GERAL

O **FlowPredictor** é o quarto microserviço por domínio, projetado para
se encaixar na arquitetura existente sem modificar nenhum componente
atual. Ele consome as **mesmas fontes de dados já disponíveis** (Ryu
ofctl_rest), reutiliza o **mesmo mecanismo de mitigação** (FlowBlocker
`/flowblocker/service`) e segue o **mesmo padrão operacional** (Flask,
ENV vars, logging `[METRICS]`, ETCD opcional).

    ┌──────────────────────── DOMÍNIO i ─────────────────────────────┐
    │                                                                │
    │   [Ryu-Core-i]  ◄──── polling GET /stats/* ────┐               │
    │   192.168.(10+i).10:808(0+i)                   │               │
    │        │                                       │               │
    │   [SimpleSwitch-i]                    [FlowPredictor-i] NOVO   │
    │   192.168.(10+i).20                   192.168.(10+i).40        │
    │                                       :606(0+i)                │
    │   [FlowBlocker-i] ◄── POST /service ──┘   │                    │
    │   192.168.(10+i).30                       │                    │
    │        │                                  │                    │
    │        └────────── [ETCD Cluster] ────────┘                    │
    │                    (estado compartilhado multi-domínio)        │
    └────────────────────────────────────────────────────────────────┘

**Decisão de design fundamental**: o módulo é *read-only* sobre o plano
de controle. Ele nunca instala flows diretamente, toda ação corretiva
passa pelo FlowBlocker, que já implementa a lógica cross-domain, os
guard-rails e a instalação OF 1.0. Isso preserva a separação de
responsabilidades da arquitetura e evita duplicação da lógica de
coordenação entre domínios.

------------------------------------------------------------------------

## 2. PIPELINE DE DADOS (Ingestão → Predição → Detecção → Mitigação → Feedback)

       ┌─────────────┐   Δbytes/Δt    ┌──────────────┐  resíduo   ┌──────────────┐
       │  COLETOR    │──────────────► │  PREDITOR    │──────────► │  DETECTOR    │
       │ /stats/port │  (taxa bps)    │  Holt        │ obs - pred │  z-score MAD │
       │ /stats/flow │                │ (nível+trend)│            │  adaptativo  │
       └─────────────┘                └──────────────┘            └──────┬───────┘
            ▲ poll 2s                                                    │ anomalia
            │                                                            ▼
       ┌────┴────────┐                ┌──────────────┐  guard-rails ┌────────────┐
       │  Ryu REST   │                │  FEEDBACK    │◄─────────────│ MITIGADOR  │
       │  (ofctl)    │                │  API humana/ │  registro    │ FlowBlocker│
       └─────────────┘                │  externa     │              │ POST       │
                                      └──────┬───────┘              └────────────┘
                                             │ ajusta z_threshold da série
                                             ▼
                                      (ciclo se refina continuamente)

### 2.1 Ingestão (independente de topologia)

São mantidas duas granularidades de séries temporais, criadas sob demanda:

| Série | Chave | Fonte | Papel |
| --- | --- | --- | --- |
| **Porta** | `port:{dpid}:{port_no}` | `/stats/port/{dpid}` (rx+tx bytes) | Visão de enlace; detecta quedas de link e saturação agregada. |
| **Fluxo** | `flow:{dpid}:{src}->{dst}` | `/stats/flow/{dpid}` (match `nw_src`/`nw_dst`) | Visão fina; base da **mitigação** (há um par src/dst inequívoco). |

### 2.2 Pré-processamento

Os contadores do OpenFlow são **cumulativos**, então o módulo calcula
taxa por delta: `rate_bps = (Δbytes × 8) / Δt`. Dois casos degenerados
são tratados explicitamente: delta negativo (contador resetou porque o
flow foi reinstalado ou o switch reiniciou, comum com os timeouts de
5s do SimpleSwitch) e Δt ≤ 0 (amostras fora de ordem). Séries abaixo de
`MIN_RATE_BPS` alimentam o modelo mas não geram alertas, filtrando o
ruído de ARP/LLDP.

### 2.3 Predição - Holt (suavização exponencial dupla)

**Por que Holt e não LSTM/ARIMA?** Três razões alinhadas aos seus
requisitos de escalabilidade:

1.  **Custo O(1) por amostra e \~100 bytes de estado por série.** Uma
    topologia com 50 switches × 40 fluxos ativos = 2.000 séries custam
    menos de 1 MB de RAM e microssegundos de CPU por ciclo. Modelos de
    deep learning exigiriam GPU/batching e quebrariam a promessa de
    "qualquer topologia sem perda de desempenho".
2.  **Captura nível E tendência** - melhor que EWMA puro para rampas
    de tráfego (ex.: início de um iperf), reduzindo falsos positivos
    durante crescimento legítimo.
3.  **Interface pluggável**: a classe `HoltPredictor` expõe apenas
    `update(value)` e `predict(horizon)`. Trocar por ARIMA, Prophet ou
    um modelo treinado offline exige alterar uma única classe, sem tocar
    no coletor, detector ou mitigador.

A predição de 1 passo é feita **antes** do `update()`,  garantindo que
o resíduo compare a observação contra uma predição genuína
(out-of-sample), não contra um modelo que já viu o valor.

### 2.4 Detecção de anomalias - z-score robusto sobre resíduos

Três classes de anomalia são emitidas:

| Tipo | Gatilho | Interpretação típica | Mitigável? |
| --- | --- | --- | --- |
| `THROUGHPUT_SPIKE` | resíduo > +k·σ em série de fluxo/porta | DDoS volumétrico, exfiltração, *elephant flow* inesperado | ✅ (se série de fluxo) |
| `THROUGHPUT_DROP` | resíduo < −k·σ | Falha de link, *blackhole*, regra DROP indevida | ❌ (alerta apenas) |
| `NEW_FLOW_SURGE` | nº de fluxos no DPID > 3× baseline | Port scan, SYN flood distribuído | ❌ (alerta apenas) |

### 2.5 Mitigação autônoma - guard-rails antes de agir

A resposta automatizada só é segura se for **conservadora por
construção**. O mitigador aplica cinco portões em sequência antes de
qualquer POST ao FlowBlocker:

1.  `AUTO_MITIGATE` habilitado (kill-switch global, ajustável em
    runtime);
2.  Apenas `THROUGHPUT_SPIKE` **em série de fluxo** - nunca bloqueia
    uma porta inteira ou age sobre quedas (bloquear em resposta a uma
    queda agravaria a falha);
3.  Whitelist de IPs de infraestrutura (gateways, DNS, controladores);
4.  Cooldown por par `(src,dst)` - evita tempestade de políticas
    idênticas;
5.  `DRY_RUN` - modo de sombra que loga `[METRICS][MITIGATION_DRYRUN]`
    sem executar, permitindo validar o comportamento em produção antes
    de armar o gatilho.

Quando executada, a mitigação reutiliza integralmente o fluxo
cross-domain já validado do FlowBlocker: se o par src/dst atravessa
domínios, o FlowBlocker local instala o DROP no seu DPID e propaga ao
peer via `/receive_flow`, o FlowPredictor não precisa conhecer a
topologia inter-domínio.

### 2.6 Ciclo de feedback

`POST /predictor/feedback` com
`{"anomaly_id": "...", "verdict": "false_positive"}` ajusta o threshold
**da série específica** (×1.25 por FP, ×0.95 por TP, com limites \[2.5,
10.0\]). O efeito é que séries naturalmente "nervosas" (tráfego bursty
legítimo) ficam progressivamente menos sensíveis, enquanto séries
estáveis ganham sensibilidade, a precisão se refina por série, não
por um único knob global. Os vereditos ficam registrados na anomalia e
nas estatísticas expostas em `/predictor/status`, permitindo medir
precision/recall ao longo do experimento.

------------------------------------------------------------------------

## 3. API REST

| Método | Endpoint | Função |
| --- | --- | --- |
| GET | `/` | Health check |
| GET | `/predictor/status` | Uptime, nº de séries, configuração efetiva e estatísticas de feedback |
| GET | `/predictor/predictions?top=N` | Top-N séries por vazão com *forecast* h=1 e h=5 |
| GET | `/predictor/predictions/<key>` | Detalhe de uma série: *forecast* multi-horizonte + histórico completo |
| GET | `/predictor/anomalies?limit=N` | Anomalias recentes com resultado da mitigação |
| POST | `/predictor/feedback` | `{"anomaly_id", "verdict"}` — refina *thresholds* |
| POST | `/predictor/config` | Ajuste em tempo de execução: `auto_mitigate`, `dry_run`, `min_rate_bps`, `cooldown_s` |

**Exemplo de anomalia retornada:**

``` json
{
  "anomaly_id": "a3f8c92e1b04",
  "kind": "THROUGHPUT_SPIKE",
  "key": "flow:1:10.0.0.1->10.0.0.4",
  "meta": {"type": "flow", "dpid": 1, "nw_src": "10.0.0.1", "nw_dst": "10.0.0.4"},
  "observed_bps": 94500000.0,
  "predicted_bps": 1200000.0,
  "z_score": 18.7,
  "threshold": 4.0,
  "ts_detect_ns": 1752230000123456789,
  "cid": "192.168.10.10",
  "mitigation": {
    "attempted": true,
    "executed": true,
    "reason": "FlowBlocker HTTP 200",
    "flowblocker_response": {
      "message": "Cross-controller flow rules installed successfully",
      "policy_id": "auto-a3f8c92e1b04"
    }
  }
}
```

------------------------------------------------------------------------

## 4. ESCALABILIDADE E FLEXIBILIDADE

**Horizontal (multi-domínio)**: um FlowPredictor por domínio, sem estado
compartilhado obrigatório, o padrão exato do FlowBlocker. Cada
instância monitora apenas os DPIDs do seu controlador; a visibilidade
global é opcional via chave ETCD `flowpredictor/state/<cid>` (mesmo
prefixo-pattern das domain tables). Escalar de 2 para 20 domínios é
executar `deploy_flow_predictor.sh 20`.

**Vertical (dentro do domínio)**: o custo por ciclo é dominado pelos
GETs HTTP ao Ryu (um por dpid por tipo de stat), não pelo processamento.
Referências de dimensionamento:

| Escala | Séries estimadas | RAM do módulo | CPU/ciclo | Ajuste sugerido |
| --- | ---: | ---: | ---: | --- |
| 4 switches / 8 hosts (testbed atual) | ~40 | < 5 MB | < 5 ms | padrão |
| 20 switches / 100 fluxos ativos | ~500 | ~20 MB | ~50 ms | `POLL_INTERVAL_S=3` |
| 100 switches / 2000 fluxos | ~5.000 | ~150 MB | ~400 ms | `POLL_INTERVAL_S=5` + *sharding* de DPIDs em 2 instâncias |

**Flexibilidade de topologia**: nenhum pressuposto sobre número de
switches, forma da topologia ou esquema de IPs. Novas séries nascem
quando o primeiro contador aparece; séries de fluxos expirados
simplesmente param de ser atualizadas (os flows do SimpleSwitch têm
timeout de 5s, então fluxos ociosos somem naturalmente do
`/stats/flow`).

------------------------------------------------------------------------

## 5. INTEGRAÇÃO COM O TESTBED - PASSO A PASSO

``` bash
# 1. Inicializar toda a infraestrutura
./setup_env.sh

# O setup_env.sh realiza automaticamente o bootstrap de:
# - ETCD
# - Ryu-Core
# - SimpleSwitch
# - FlowBlocker
# - FlowPredictor

# 2. Criar a topologia Mininet
sudo CSETS=2 SPER=2 ./setup_mininet.py

# 3. Confirmar que os microserviços estão operacionais
docker ps

# 4. Verificar coleta (aguarde ~30 s de warm-up = 15 amostras × 2 s)
curl http://127.0.0.1:6060/predictor/status | jq .
curl http://127.0.0.1:6060/predictor/predictions | jq .

# 4. Provocar uma anomalia (no Mininet, após tráfego baseline estável)
mininet> h4 iperf3 -s -D
mininet> h1 ping -c 30 10.0.0.4 -i 0.5        # baseline ~modesto por ~15s
mininet> h1 iperf3 -c 10.0.0.4 -t 20           # SPIKE súbito

# 5. Observar detecção + dry-run da mitigação
curl http://127.0.0.1:6060/predictor/anomalies | jq '.anomalies[0]'
docker logs flow-predictor-0 | grep "\[METRICS\]\[MITIGATION_DRYRUN\]"

# 6. Armar mitigação real e repetir o passo 4
curl -X POST http://127.0.0.1:6060/predictor/config \
  -H "Content-Type: application/json" -d '{"dry_run": false}'

# 7. Confirmar o DROP instalado pelo FlowBlocker (cross-domain!)
# Executar no terminal do host (fora do CLI do Mininet)
sudo ovs-ofctl -O OpenFlow10 dump-flows s1 | grep nw_src=10.0.0.1

# Validar no Mininet
mininet> h1 ping -c 3 10.0.0.4                 # deve falhar

# 8. Se foi falso positivo, ensinar o módulo
curl -X POST http://127.0.0.1:6060/predictor/feedback \
  -H "Content-Type: application/json" \
  -d '{"anomaly_id": "a3f8c92e1b04", "verdict": "false_positive"}'
```

**Correlação de métricas fim-a-fim**: os logs
estruturados permitem medir a latência total de resposta autônoma
cruzando timestamps em nanosegundos, no mesmo estilo dos logs
existentes:

    [METRICS][ANOMALY_DETECT]   id=... ts_ns=T1        (FlowPredictor)
    [METRICS][MITIGATION_APPLY] ts_send_ns=T2          (FlowPredictor → FlowBlocker)
    [METRICS][POLICY_APPLY]     ts_decide_ns=T3        (FlowBlocker, já existente)
    [METRICS][FLOW_MOD]         ts_send_ns=T4          (via ofctl_rest)

    Latência de resposta autônoma = T4 − T1

### 5.1 Validação da infraestrutura

Antes de iniciar os experimentos, recomenda-se verificar o estado dos
microserviços:

``` bash
docker ps
```

Estado esperado:

-   `ryu-core-*` → healthy
-   `simple-switch-*` → healthy
-   `flow-blocker-*` → healthy
-   `flow-predictor-*` → Up

Os Docker Healthchecks utilizam os endpoints e portas corretos de cada
instância, permitindo validar automaticamente ambientes com múltiplos
domínios.

### 5.2 Bootstrap automatizado

Nesta versão, o FlowPredictor foi integrado ao processo de inicialização
do ambiente. Dessa forma, não é mais necessário executar manualmente
`deploy_flow_predictor.sh` durante o fluxo normal de utilização. Todo o
ambiente é preparado pelo `setup_env.sh`, simplificando a implantação e
reduzindo erros de configuração.

------------------------------------------------------------------------

## 6. Limitações

O detector é univariado por série, não correlaciona anomalias entre
séries (um DDoS distribuído aparece como N spikes independentes, não
como um evento único); uma camada de agregação por dst_ip é a extensão
natural. O `NEW_FLOW_SURGE` com os timeouts de 5s do SimpleSwitch pode
oscilar em tráfego bursty legítimo, por isso é apenas alerta. E,
coerente com o restante do testbed, os endpoints não têm autenticação,
 o `/predictor/config` em especial deveria receber um token antes de
qualquer uso fora de laboratório (o padrão está no seu doc
DEPLOYMENT_TECNICO_AVANCADO § Security Hardening).

Caminho de evolução sem quebra de interface: (1) preditor sazonal
Holt-Winters para tráfego com padrão diário, (2) exportador Prometheus
nos snapshots já existentes, (3) modelo global treinado offline injetado
via a interface pluggável do `HoltPredictor`.

------------------------------------------------------------------------

**Versão**: 1.1 · **Data**: 2026-07-12 · **Status**: ✅ Integrado ao
setup_env.sh e validado em ambiente multi-domínio
