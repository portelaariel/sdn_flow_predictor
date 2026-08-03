# FLOWPREDICTOR - MÓDULO DE PREDIÇÃO DE VAZÃO E DETECÇÃO DE ANOMALIAS

[![Repository validation](https://github.com/portelaariel/sdn_flow_predictor/actions/workflows/validate.yml/badge.svg)](https://github.com/portelaariel/sdn_flow_predictor/actions/workflows/validate.yml)

## 1. VISÃO GERAL

O **FlowPredictor** é o quarto microserviço por domínio. Ele consome as
estatísticas do Ryu `ofctl_rest`, reutiliza o mecanismo de mitigação do
FlowBlocker (`/flowblocker/service`) e segue o mesmo padrão operacional
(Flask, variáveis de ambiente, logging `[METRICS]` e ETCD opcional).

Para expor `nw_src` e `nw_dst` nas estatísticas OpenFlow 1.0, o caminho
ativo usa o SimpleSwitch L3-aware incluído neste repositório. Essa é a
única adaptação necessária nos componentes de encaminhamento existentes.

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

### 1.1 Estrutura ativa do repositório

| Componente | Arquivo ativo | Construção/execução |
| --- | --- | --- |
| FlowPredictor | `flow_predictor_cnsm.py` | `Dockerfile.flow_predictor` |
| Contrato do modelo | `offline_model.py` | valida o artefato JSON no treino e no runtime |
| Treinamento offline | `train_offline_model.py` | converte CSVs rotulados em um modelo versionável |
| Ryu controller | `ryu_apps/emitter_cnsm.py` + `ryu_apps/ofctl_rest.py` | `ryu_apps/Dockerfile` |
| SimpleSwitch | `rest_client/Simpleswitch_cnsm.py` | `rest_client/Dockerfile` |
| FlowBlocker | `flow_blocker/flow_blocker_cnsm.py` | `flow_blocker/Dockerfile` |
| Bootstrap | `eMSN_ENV/setup_env.sh` | cria os serviços por domínio |
| Topologia | `eMSN_ENV/setup_mininet.py` | cria a rede Mininet |
| Deploy isolado | `deploy_flow_predictor.sh` | adiciona o preditor a um ambiente existente |
| Configuração | `config/runtime.env` | defaults compartilhados pelos scripts ativos |
| Limpeza | `eMSN_ENV/cleanup_setup_env.sh` | remove somente recursos do projeto |
| Validação | `scripts/validate_repository.sh` | sintaxe, estrutura e testes unitários |

Os scripts alternativos que duplicavam bootstrap, geração de topologia
e execução de testes foram removidos. Os resultados históricos em
`eMSN_ENV/experiment_01` e `eMSN_ENV/teste_manual` foram preservados
como evidência experimental, mas não participam do runtime.

------------------------------------------------------------------------

## 2. PIPELINE DE DADOS (Ingestão → Predição → Detecção → Mitigação → Feedback)

 CSV normal + ataque ──► train_offline_model.py ──► modelo JSON validado
                                                        │ parâmetros + calibração
                                                        ▼
       ┌─────────────┐   Δbytes/Δt    ┌──────────────┐  resíduo log  ┌──────────────┐
       │  COLETOR    │──────────────► │  PREDITOR    │─────────────► │  DETECTOR    │
       │ /stats/port │  (taxa bps)    │ Holt offline │               │ robust z     │
       │ /stats/flow │                │ (nível+trend)│               │ sem warmup   │
       └─────────────┘                └──────────────┘               └──────┬───────┘
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

### 2.3 Treinamento offline e Holt online

O treinamento recebe séries temporais de vazão normais e, opcionalmente,
amostras rotuladas como ataque. Ele executa quatro passos:

1. converte a unidade para `rate_bps` e agrupa as linhas por série;
2. escolhe `alpha` e `beta` do Holt por busca em grade, usando somente
   trechos normais consecutivos;
3. calcula mediana e escala robusta (MAD) dos resíduos em `log1p(bps)`;
4. quando há rótulos de ataque, calibra o threshold para a melhor F1;
   sem ataques, usa o quantil 99,5% dos resíduos normais.

O `log1p` é importante para transferir o modelo entre datasets e o
Mininet: a decisão passa a refletir uma mudança proporcional de vazão,
em vez de depender de um número absoluto de bits por segundo. O artefato
JSON registra o hash do dataset, colunas usadas, contagens, parâmetros e
métricas de calibração.

No runtime, a primeira taxa de cada fluxo inicializa apenas o nível
específico daquela série. A taxa seguinte já é classificada com a
distribuição aprendida offline; não há o warmup de 15 amostras. Um ataque
detectado não atualiza Holt, evitando que um DDoS prolongado seja
absorvido como o novo comportamento normal.

Holt continua adequado ao processamento online por ter custo O(1) e
capturar nível e tendência. A predição de um passo é sempre feita antes
de observar a nova taxa.

Se nenhum modelo for configurado, a ferramenta mantém o detector
adaptativo anterior como fallback compatível. Nesse modo, e somente
nele, `WARMUP_SAMPLES` continua sendo usado.

### 2.4 Detecção de anomalias - z-score robusto calibrado offline

Três classes de anomalia são emitidas:

| Tipo | Gatilho | Interpretação típica | Mitigável? |
| --- | --- | --- | --- |
| `THROUGHPUT_SPIKE` | resíduo > +k·σ em série de fluxo/porta | DDoS volumétrico, exfiltração, *elephant flow* inesperado | ✅ (se série de fluxo) |
| `THROUGHPUT_DROP` | resíduo < −k·σ | Falha de link, *blackhole*, regra DROP indevida | ❌ (alerta apenas) |
| `NEW_FLOW_SURGE` | nº de fluxos no DPID > 3× baseline | Port scan, SYN flood distribuído | ❌ (alerta apenas) |

O warmup de `NEW_FLOW_SURGE` é independente e configurado por
`FLOW_SURGE_WARMUP_SAMPLES`, pois essa heurística conta fluxos e não usa
o modelo Holt de vazão.

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
**da série específica** (×1.25 por FP e ×0.95 por TP). O modo offline
respeita a faixa validada do artefato \[1, 20\]; o fallback preserva os
limites anteriores \[2.5, 10\]. O efeito é que séries naturalmente "nervosas" (tráfego bursty
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
| GET | `/predictor/model` | Modo efetivo, parâmetros e proveniência do modelo offline |
| GET | `/predictor/export/status` | Estado e contadores da exportação CSV |
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
  "model_residual": 4.35518628,
  "detection_mode": "offline",
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
executar `sudo bash deploy_flow_predictor.sh 20`.

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
simplesmente param de ser atualizadas. As regras IPv4 do SimpleSwitch
usam `idle_timeout=30` e permanecem enquanto houver tráfego.

------------------------------------------------------------------------

## 5. TREINAMENTO OFFLINE

### 5.1 Dataset exportado pelo próprio FlowPredictor

O formato mais confiável é o histórico criado em
`prediction_history_domain*/`. Para treinar com um ou vários diretórios:

``` bash
python3 train_offline_model.py prediction_history_domain*/ \
  --value-column observed_bps \
  --series-columns flow_key \
  --timestamp-column timestamp \
  --sample-interval-s 2 \
  --output models/mininet-holt.json
```

Esse exemplo pressupõe que a coleta contém somente o baseline normal;
o threshold é calibrado pelo quantil dos resíduos benignos. A coluna
`is_anomaly` exportada pela ferramenta é a decisão do detector anterior,
não um ground truth, e não deve ser usada como rótulo de treino sem
revisão. Para experimentos com ataques, adicione uma coluna rotulada a
partir do roteiro do experimento e use-a em `--label-column`.

### 5.2 Dataset DDoS externo

Datasets como CIC-DDoS podem ser usados se houver uma coluna de vazão,
ordem temporal, rótulo e observações repetidas para a mesma série. Por
exemplo, quando o CSV contém `Flow Bytes/s`, `Source IP`,
`Destination IP`, `Timestamp` e `Label`:

``` bash
python3 train_offline_model.py datasets/ddos.csv \
  --value-column "Flow Bytes/s" \
  --value-scale 8 \
  --series-columns "Source IP,Destination IP" \
  --timestamp-column Timestamp \
  --sample-interval-s 2 \
  --label-column Label \
  --normal-label BENIGN \
  --output models/ddos-holt.json
```

`--value-scale 8` converte bytes/s para bits/s. Nomes de colunas devem
ser passados exatamente como aparecem no CSV.

> Um dataset tabular com uma linha independente por conexão e sem ordem
> temporal não treina Holt corretamente. Nesse caso, agregue primeiro as
> linhas em janelas temporais por par origem/destino. O modelo precisa de
> séries, não apenas de exemplos isolados para classificação. O intervalo
> deve ser compatível com `PREDICTOR_POLL_INTERVAL_S` (2 s por padrão).

O comando imprime os parâmetros escolhidos e, quando há ataques,
precision, recall e F1 de calibração. Essas métricas usam o próprio
dataset de treino; a avaliação científica final deve usar outro arquivo
ou uma divisão temporal não vista no treinamento.

### 5.3 Executar o modelo no testbed

O caminho informado é montado como somente leitura em todos os
containers FlowPredictor:

``` bash
PREDICTOR_OFFLINE_MODEL="$PWD/models/ddos-holt.json" \
PREDICTOR_OFFLINE_MODEL_REQUIRED=true \
PREDICTOR_DRY_RUN=true \
  bash deploy_flow_predictor.sh 2 true
```

Use `PREDICTOR_OFFLINE_MODEL_REQUIRED=true` em experimentos: um arquivo
ausente ou inválido fará o serviço falhar explicitamente, em vez de cair
silenciosamente no warmup adaptativo. Mantenha
`PREDICTOR_ONLINE_MODEL_ADAPTATION=false` para que o teste online use
exatamente a calibração offline.

Confirme o modo efetivo:

``` bash
curl http://127.0.0.1:6060/predictor/model | jq .
curl http://127.0.0.1:6060/predictor/status | jq '.model'
```

O resultado deve conter `"loaded": true` e `"mode": "offline"`.

------------------------------------------------------------------------

## 6. INTEGRAÇÃO COM O TESTBED - PASSO A PASSO

``` bash
# 1. Construir as imagens ativas (uma vez)
docker build -t ryu_core_cnsm ryu_apps
docker build -t simpleswitch_cnsm rest_client
docker build -t flow_blocker_cnsm flow_blocker
docker build -t flow_predictor_cnsm -f Dockerfile.flow_predictor .

# 2. Inicializar a infraestrutura a partir da raiz do repositório
bash eMSN_ENV/setup_env.sh

# Alternativa não interativa: 2 domínios, 2 switches por domínio
bash eMSN_ENV/setup_env.sh 2 2

# O setup_env.sh realiza automaticamente o bootstrap de:
# - ETCD
# - Ryu-Core
# - SimpleSwitch
# - FlowBlocker
# - FlowPredictor

# 3. Criar a topologia Mininet
sudo CSETS=2 SPER=2 python3 eMSN_ENV/setup_mininet.py

# 4. Confirmar que os microserviços estão operacionais
docker ps

# 5. Verificar coleta (no modo offline, a segunda taxa já pode ser classificada)
curl http://127.0.0.1:6060/predictor/status | jq .
curl http://127.0.0.1:6060/predictor/predictions | jq .

# 6. Provocar uma anomalia (no Mininet)
mininet> h4 iperf3 -s -D
mininet> h1 ping -c 6 10.0.0.4 -i 0.5         # inicializa a série
mininet> h1 iperf3 -c 10.0.0.4 -t 20           # SPIKE súbito

# 7. Observar detecção + dry-run da mitigação
curl http://127.0.0.1:6060/predictor/anomalies | jq '.anomalies[0]'
docker logs flow-predictor-0 | grep "\[METRICS\]\[MITIGATION_DRYRUN\]"

# 8. Armar mitigação real e repetir o passo 6
curl -X POST http://127.0.0.1:6060/predictor/config \
  -H "Content-Type: application/json" -d '{"dry_run": false}'

# 9. Confirmar o DROP instalado pelo FlowBlocker (cross-domain!)
# Executar no terminal do host (fora do CLI do Mininet)
sudo ovs-ofctl -O OpenFlow10 dump-flows s1 | grep nw_src=10.0.0.1

# Validar no Mininet
mininet> h1 ping -c 3 10.0.0.4                 # deve falhar

# 10. Se foi falso positivo, ensinar o módulo
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

### 6.1 Validação da infraestrutura

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

### 6.2 Bootstrap automatizado

Nesta versão, o FlowPredictor foi integrado ao processo de inicialização
do ambiente. Dessa forma, não é mais necessário executar manualmente
`deploy_flow_predictor.sh` durante o fluxo normal de utilização. Todo o
ambiente é preparado pelo `setup_env.sh`, simplificando a implantação e
reduzindo erros de configuração.

### 6.3 Configuração e validação

Os defaults de imagens, endereçamento, portas, ETCD e FlowPredictor ficam
centralizados em `config/runtime.env`. Qualquer valor pode ser sobrescrito
por variável de ambiente sem editar os scripts:

``` bash
PREDICTOR_DRY_RUN=false PREDICTOR_Z_THRESHOLD=5.0 \
  bash eMSN_ENV/setup_env.sh 2 2
```

Também é possível manter uma configuração separada e apontá-la com
`SDN_RUNTIME_CONFIG=/caminho/runtime.env`.

Antes de publicar mudanças, execute a validação independente de Docker:

``` bash
bash scripts/validate_repository.sh
```

Para desmontar o ambiente, o cleanup padrão remove apenas containers e
redes pertencentes a esta ferramenta:

``` bash
bash eMSN_ENV/cleanup_setup_env.sh
```

O modo `--all` mantém o comportamento legado de remover todos os
containers e redes customizadas do host e deve ser usado apenas em uma
máquina dedicada.

### 6.4 Integração contínua

O workflow `.github/workflows/validate.yml` executa o mesmo validador em
todo Pull Request, em pushes para `main` e sob demanda na aba Actions. O
job usa apenas permissão de leitura do conteúdo do repositório e não
requer secrets ou acesso ao servidor do testbed.

------------------------------------------------------------------------

**Versão**: 1.3 · **Data**: 2026-08-03 · **Status**: runtime consolidado,
configuração centralizada e validação automatizada
