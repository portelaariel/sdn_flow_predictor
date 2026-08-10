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
| Decisão colaborativa | `collaborative_decision.py` | critérios MCDA puros e reproduzíveis |
| Agente de domínio | `domain_agent.py` | deliberação local e negociação distribuída |
| Protocolo dos agentes | `agent_protocol.py` | contrato JSON estrito e validação de propostas |
| Gate de autoridade | `agent_authority.py` | revalidação fail-closed e claim agentic exclusivo |
| Contrato do modelo | `offline_model.py` | valida o artefato JSON no treino e no runtime |
| Treinamento offline | `train_offline_model.py` | converte CSVs rotulados em um modelo versionável |
| Preparação CIC-DDoS2019 | `prepare_cicddos2019.py` | agrega CSVs grandes em janelas temporais compactas |
| Avaliação offline | `evaluate_offline_model.py` | mede o modelo em uma captura independente |
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
                                             │ ajusta o threshold do lado afetado
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
5s do SimpleSwitch) e Δt ≤ 0 (amostras fora de ordem). Durante o alinhamento
inicial, taxas abaixo de `MIN_RATE_BPS` são ignoradas para não transformar um
intervalo parcial em baseline. Depois do alinhamento elas podem atualizar o
nível, mas não geram alertas, filtrando o ruído de ARP/LLDP.

Para séries de fluxo, uma taxa zero isolada é tratada como uma lacuna entre
rajadas, sem pontuar nem alterar Holt. O nível só é reiniciado após
`FLOW_IDLE_RESET_SAMPLES` zeros consecutivos (2 por padrão). Isso evita que
coletores com fases diferentes usem o começo de um ataque como novo baseline.

Regras OpenFlow com `actions=[]` são regras DROP e não representam
tráfego entregue. Quando um DROP cobre `src->dst`, o coletor exclui todas
as entradas desse par no DPID. Isso impede que o contador da própria
mitigação realimente o detector ou produza uma falsa queda logo após o
bloqueio.

### 2.3 Treinamento offline e Holt online

O treinamento recebe séries temporais de vazão normais e, opcionalmente,
amostras rotuladas como ataque. Ele executa quatro passos:

1. converte a unidade para `rate_bps` e agrupa as linhas por série;
2. escolhe `alpha` e `beta` do Holt por busca em grade, usando somente
   trechos normais consecutivos;
3. calcula mediana e escala robusta (MAD) dos resíduos em `log1p(bps)`;
4. calibra separadamente os dois lados do detector: picos positivos
   (`spike_z_threshold`) maximizam a F1 dos ataques rotulados; quedas
   (`drop_z_threshold`) usam por padrão o quantil 99,9% dos resíduos benignos
   negativos. Sem ataques, picos usam o quantil 99,5% benigno.

O `log1p` é importante para transferir o modelo entre datasets e o
Mininet: a decisão passa a refletir uma mudança proporcional de vazão,
em vez de depender de um número absoluto de bits por segundo. O artefato
JSON registra o hash do dataset, colunas usadas, contagens, parâmetros e
métricas de calibração.

No runtime, o artefato declara `series_priming_samples` (atualmente 2). As duas
primeiras taxas válidas de cada fluxo acima do piso de ruído ajustam apenas o
nível específico daquela série; elas não recalibram escala nem thresholds.
Intervalos parciais abaixo do piso são ignorados. A terceira taxa já é
classificada com a distribuição aprendida offline. Portanto, não há o warmup
estatístico de 15 amostras do modo adaptativo, apenas um alinhamento de dois
intervalos. Um ataque detectado não atualiza Holt, evitando que um DDoS
prolongado seja absorvido como o novo comportamento normal.

Como o detector é baseado em resíduos por série, um ataque que já esteja ativo
nas duas primeiras observações válidas pode compor esse alinhamento e não ser
detectado imediatamente. Para experimentos reprodutíveis, inicie o tráfego
benigno antes do ataque e registre esse intervalo no relatório do benchmark.

O artefato atual usa `schema_version: 3`; além dos thresholds independentes da
versão 2, ele registra o contrato de alinhamento em `runtime`. `z_threshold`
permanece como alias compatível do limiar de pico. Artefatos da versão 1 continuam válidos: ao
carregá-los, o runtime aplica seu único threshold simetricamente aos dois
lados. Artefatos das versões 1 e 2 preservam uma única amostra de alinhamento.

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
| `THROUGHPUT_SPIKE` | resíduo > +k_spike·σ em série de fluxo/porta | DDoS volumétrico, exfiltração, *elephant flow* inesperado | ✅ (se série de fluxo) |
| `THROUGHPUT_DROP` | resíduo < −k_drop·σ | Falha de link, *blackhole*, regra DROP indevida | ❌ (alerta apenas) |
| `NEW_FLOW_SURGE` | nº de fluxos no DPID > 3× baseline | Port scan, SYN flood distribuído | ❌ (alerta apenas) |

O warmup de `NEW_FLOW_SURGE` é independente e configurado por
`FLOW_SURGE_WARMUP_SAMPLES`, pois essa heurística conta fluxos e não usa
o modelo Holt de vazão.

Repetições do mesmo `(tipo, série)` dentro de
`ANOMALY_EVENT_COOLDOWN_S` são agregadas ao primeiro evento, sem repetir
log de detecção nem tentativa de mitigação. O registro conserva
`first_seen_ns`, atualiza `last_seen_ns`, pico observado e
`suppressed_count`. A classificação continua ocorrendo em cada amostra e
as amostras anômalas continuam fora do estado Holt; a deduplicação afeta
somente a emissão do evento. O total agregado fica em
`anomalies_suppressed` no endpoint de status. O log
`[METRICS][ANOMALY_SUPPRESS]` é emitido na primeira repetição e depois a
cada dez, reduzindo também escrita repetitiva no ETCD.

### 2.5 Decisão colaborativa multicritério (MCDA)

Com `PREDICTOR_COLLABORATION_ENABLED=true`, uma detecção local deixa de
ser uma ordem de bloqueio e passa a ser uma **evidência candidata**. Cada
FlowPredictor publica no ETCD somente o resumo do fluxo anômalo
`src->dst`; telemetria normal e séries completas não são replicadas. As
evidências expiram automaticamente e as visões do mesmo tráfego em
vários switches locais são agregadas pelo máximo, nunca somadas.

Todos os domínios calculam a mesma soma ponderada, com critérios
normalizados entre zero e um:

| Critério | Peso padrão | Pergunta respondida |
| --- | ---: | --- |
| severidade | 0,25 | Quanto o z-score excedeu o limiar offline? |
| corroboração | 0,25 | Quantos domínios independentes confirmaram? |
| razão de vazão | 0,13 | Quanto o observado excedeu a previsão? |
| persistência | 0,12 | O evento continuou por várias janelas? |
| confiabilidade do modelo | 0,08 | Qual foi a precisão registrada no artefato? |
| concordância | 0,07 | Os z-scores dos domínios são coerentes? |
| atualidade | 0,05 | As evidências ainda são recentes? |
| especificidade topológica | 0,05 | Existe um par de fluxo inequívoco? |

Os estados padrão são `NORMAL` (< 0,40), `SUSPECT` (0,40–0,60),
`CORROBORATED` (0,60–0,80) e `MITIGATE` (≥ 0,80). Mesmo acima de 0,80,
a ação só é liberada se `COLLAB_MIN_DOMAINS` tiver confirmado; caso
contrário o estado é `WAITING_QUORUM`. Artefatos com hashes diferentes
produzem `MODEL_MISMATCH` e não podem formar consenso.

As chaves efêmeras são
`flowpredictor/evidence/<hash>/<janela>/<cid>`. Depois do consenso, uma
transação atômica disputa
`flowpredictor/mitigation-claim/<hash>`: um único domínio vence e chama o
FlowBlocker; os demais registram qual foi o coordenador. Isso elimina as
duas chamadas independentes observadas anteriormente sem centralizar o
detector. Se a colaboração estiver desligada, o comportamento local
anterior é preservado. Se ela for solicitada mas o ETCD estiver
indisponível na inicialização, o status informa a degradação e a decisão
local permanece ativa para não interromper instalações existentes. Uma
queda do ETCD durante o consenso é tratada de forma conservadora: novas
ações colaborativas aguardam a recuperação, em vez de cada domínio
bloquear independentemente.

### 2.6 Agentes de domínio: shadow, authority-dry-run e authority-live

Com `PREDICTOR_AGENTIC_ENABLED=true`, cada FlowPredictor também instancia um
agente deliberativo associado ao seu domínio. O agente recebe a mesma evidência
Holt compacta usada pelo MCDA, consulta a tabela agregada do FlowBlocker para
identificar se representa a origem, o destino, ambos ou apenas um observador e
publica uma proposta efêmera em
`flowpredictor/agent-proposal/<hash>/<janela>/<cid>`.

As propostas usam um contrato JSON fechado e podem assumir `MITIGATE`, `WAIT`,
`NORMAL`, `ABSTAIN` ou `VETO`. O contrato carrega o par topológico ordenado
`source_cid`/`destination_cid`, valida se o papel alegado é coerente com o CID e
confere `cid`/janela contra a chave ETCD. A simultaneidade usa o instante da
observação Holt, não o horário de publicação — um retry não rejuvenesce uma
evidência antiga. Para um fluxo cross-domain, o quórum padrão de duas propostas
`MITIGATE` recentes, compatíveis e sem `NORMAL`/`VETO` gera `AGREED`. Ausência de
um domínio gera `WAITING_PROPOSALS`; topologias divergentes,
`TOPOLOGY_MISMATCH`; modelos diferentes, `MODEL_MISMATCH`; whitelist,
`VETOED`. Um fluxo inteiramente local exige apenas o agente do seu único domínio
responsável.

Timestamps e TTLs do protocolo são calculados integralmente em nanossegundos.
Isso evita a perda de precisão que ocorre ao somar uma duração `float` a um
epoch na ordem de `10^18`. O gate mantém a idade da observação estritamente
limitada e reserva apenas 1.024 ns de compatibilidade para o arredondamento do
TTL declarado por propostas produzidas antes dessa correção.

O modo padrão **shadow** anexa a decisão agentic à anomalia e a compara com a
decisão MCDA, sem disputar claim nem chamar o FlowBlocker. O estágio seguinte,
**authority-dry-run**, revalida cada novo `AGREED` em uma fronteira separada de
autorização e disputa um claim agentic exclusivo no ETCD. O vencedor registra
`would_execute=true`, mas tanto `attempted` quanto `executed` permanecem falsos:
o caminho agentic não possui chamada ao FlowBlocker. O MCDA continua operando em
dry-run como comparador independente. Os logs usam
`[METRICS][AGENT_PROPOSAL]`, `[METRICS][AGENT_CONSENSUS]` e
`[METRICS][AGENT_AUTHORITY_DRYRUN]`; o estado completo fica em
`GET /predictor/agent`.

A API também mantém os 200 eventos de transição mais recentes em
`decision_events`. Cada evento preserva o instante em que o estado foi atingido,
os horários da primeira e da última proposta relevante e a decomposição entre
coleta das propostas e deliberação. Esse histórico é limitado em memória e não
é usado para decidir; sua finalidade é impedir que o benchmark perca estados
curtos entre duas consultas HTTP. Durante o benchmark, cada `event_id` é escrito
uma única vez na linha do tempo, evitando replicar o histórico inteiro a cada
consulta e mantendo baixo o uso de armazenamento.

O endpoint MCDA preserva, de forma análoga, as 200 transições recentes. A
comparação agentic–MCDA procura uma decisão com `window_id` em comum, em vez de
usar cegamente apenas o estado MCDA mais recente. Assim, o avanço assíncrono de
um domínio para a janela seguinte não torna indisponível a decisão equivalente
do episódio anterior. Esse histórico é somente evidência de avaliação: não
participa do quórum, do gate de autoridade, do claim nem da mitigação.

Nos modos autoritativos, `authority.mcda_comparison` congela essa comparação no
instante em que o gate é avaliado. Isso impede que o DROP executado pelo agente
reduza a vazão, faça o MCDA avançar de `MITIGATE` para `CORROBORATED` e altere
retroativamente a taxa agente–MCDA. Para timelines produzidas antes desse
snapshot explícito, o sumarizador usa a primeira observação preservada do mesmo
`event_id`. O estado MCDA posterior continua registrado na timeline como efeito
da atuação, mas não é tratado como a decisão concorrente original.

No testbed, o cluster ETCD é parte do perímetro confiável: a checagem entre
chave e payload evita inconsistência acidental, mas não é autenticação
criptográfica de um domínio. Por isso `authority-live` é um canário de
laboratório, não uma configuração de produção multi-inquilino. Uma implantação
operacional deve exigir ACL por prefixo e identidade mTLS ou assinatura das
propostas antes de confiar em domínios administrados separadamente.

O score local do agente é explicável e combina severidade Holt (0,35), razão de
vazão (0,20), persistência (0,20), confiabilidade do modelo (0,15) e papel
topológico (0,10). Não há LLM ou aprendizado por reforço no caminho crítico.

#### Gate de segurança, promoção e canário com atuação

`agent_authority.py` adiciona uma segunda validação entre `AGREED` e qualquer
ação futura. O gate revalida identidade do evento, fluxo, quórum, papéis,
topologia, modelo, janela e TTL. Um claim separado em
`flowpredictor/agent-mitigation-claim/<hash>` garante um único vencedor e falha
fechada quando o ETCD está indisponível. Em `shadow`, gate e claim não são
executados. Em `authority-dry-run`, ambos fazem parte do runtime, mas permanecem
deliberadamente desconectados do FlowBlocker. A inicialização exige
`DRY_RUN=true` e a API rejeita a tentativa de desativá-lo durante a execução.

Em `authority-live`, o mesmo gate e o mesmo claim são reexecutados. O MCDA
continua calculando sua decisão para comparação científica, porém não disputa
claim e nunca chama o FlowBlocker. Somente o agente autorizado que vencer o
claim agentic atravessa `mitigate_agentic()`. A fronteira repete as verificações
de modo, opt-in, kill-switch, fluxo, autorização, claim não degradado e
coordenador local antes de delegar ao mitigador existente, que ainda aplica
whitelist, cooldown, tipo de anomalia e par IPv4 inequívoco.

O modo live não pode ser habilitado apenas mudando `DRY_RUN`: ele exige
simultaneamente `AGENTIC_MODE=authority-live`,
`AGENTIC_LIVE_ACTUATION=true`, modelo offline obrigatório, adaptação online
desabilitada, colaboração/ETCD ativos e `AUTO_MITIGATE=true`.

Depois do gate de falhas, execute o piloto autoritativo sem atuação:

``` bash
bash scripts/run_agentic_authority_dry_run.sh
```

O avaliador exige workload válido, dois agentes ativos e autorizados, propostas
posteriores ao início do ataque, exatamente um domínio coordenador,
`would_execute` apenas nesse domínio, concordância com o MCDA, zero tentativa de
atuação, zero request ao FlowBlocker e zero regra DROP. O resultado fica em
`experiments/results/authority-dry-run-*/authority-summary.json`; qualquer
invariante violada produz status de saída diferente de zero.

Uma execução isolada valida o encadeamento, mas não é evidência suficiente para
promover os agentes. A campanha de promoção executa controles positivos e
negativos em pares de hosts e taxas diferentes:

``` bash
bash scripts/run_agentic_authority_campaign.sh
```

O padrão executa seis casos isolados: benigno e DDoS para `h1->h8`, `h2->h7`
e `h3->h6`, usando respectivamente `1M/50M`, `2M/100M` e `5M/150M`. O ambiente
é recriado entre casos; a imagem só precisa ser construída no primeiro. O
agregador exige `TP` com dois agentes autorizados e um único claim nos casos
DDoS, além de `TN` sem `AGREED`, autorização, claim ou `would_execute` nos
controles benignos. Também exige um único commit, um único artefato de modelo,
zero atuação, zero request ao FlowBlocker e zero DROP em toda a campanha.

Os resultados ficam em
`experiments/results/authority-campaign-*/campaign-summary.json` e `.md`. O
campo `promotion_ready=true` significa apenas que o estágio dry-run atingiu os
critérios experimentais configurados; ele não habilita nem conecta
`authority-live` automaticamente.

Depois de obter `promotion_ready=true`, execute o canário live com consentimento
explícito:

``` bash
bash scripts/run_agentic_authority_live_canary.sh \
  --allow-agentic-mitigation
```

O runner recria o ambiente e executa primeiro um controle benigno, que exige
TN, zero acordo, zero request e zero DROP. Só então executa o DDoS, que exige
dois agentes autorizados, exatamente um claim vencedor, exatamente um executor
agentic, um pedido ao FlowBlocker, DROP observável e nenhuma atuação ou claim do
MCDA. Antes de começar, ele também exige uma campanha com
`promotion_ready=true`, confirma que seu commit é ancestral do código atual e
que o SHA-256 do modelo offline é exatamente o artefato promovido. O relatório
final fica em
`experiments/results/agentic-live-canary-*/canary-summary.json`; somente os dois
casos aprovados produzem `canary_ready=true`.

Depois do canário, a campanha live multi-fluxo mede repetibilidade com atuação
real:

``` bash
bash scripts/run_agentic_authority_live_campaign.sh \
  --allow-agentic-mitigation
```

O padrão executa `h1->h8`, `h2->h7` e `h3->h6`, com um controle benigno antes
do DDoS de cada par. Se o controle de um par falhar, seu ataque é bloqueado e
registrado como não executado. Cada caso recria ETCD, controladores, serviços e
Mininet; somente as imagens Docker podem ser reutilizadas. O runner exige tanto
`promotion_ready=true` quanto `canary_ready=true`, ancestralidade do commit
promovido e o mesmo SHA-256 do modelo offline.

O agregado exige TN sem autorização/claim/DROP nos controles; TP, dois agentes
autorizados, um único claim, um único executor, exatamente um request ao
FlowBlocker e DROP observável nos ataques. A igualdade agente–MCDA no instante
da autoridade é mantida como métrica, não como invariante de segurança: os dois
métodos têm ciclos assíncronos e o MCDA é apenas observacional em
`authority-live`. O gate comparativo exige que cada MCDA alcance `MITIGATE` na
mesma `window_id` em até 1.000 ms. Assim, uma passagem transitória por
`CORROBORATED` não é escondida nem confundida com falha de mitigação.

O resumo separa `operational_ready` (segurança e atuação), `comparative_ready`
(convergência limitada do observador) e `campaign_ready` (ambos). Ele informa
concordância exata por execução e por domínio, taxa de convergência e latência
até a convergência. A campanha também exige um único commit e modelo em todos
os casos. Os relatórios ficam em
`experiments/results/agentic-live-campaign-*/campaign-summary.json` e `.md`;
`campaign_ready=true` indica que todos os casos e invariantes configurados
passaram. Como há mitigação real, o consentimento da linha de comando não é
opcional.

O limite pode ser alterado explicitamente com
`AGENTIC_LIVE_CAMPAIGN_MCDA_CONVERGENCE_WINDOW_MS`; o valor usado é gravado no
manifesto e deve ser idêntico em todos os casos.

A matriz determinística de fault injection pode ser executada sem Mininet,
containers ou privilégios de administrador:

``` bash
python3 experiments/run_agentic_fault_suite.py \
  --output experiments/results/agentic-fault-gate
```

Ela cobre ausência de agente, TTL, incompatibilidade de modelo/topologia, veto,
divergência, duplicação, episódio antigo, payload `AGREED` adulterado, falha do
ETCD e disputa simultânea do claim. O relatório registra explicitamente
`dataplane_touched=false`; qualquer autorização insegura produz status de saída
diferente de zero e também faz `scripts/validate_repository.sh` falhar.

Depois que essa matriz offline passar, o gate equivalente pode ser executado no
testbed real:

``` bash
bash scripts/run_agentic_runtime_faults.sh
```

O runner força `shadow=true` e `DRY_RUN=true`, reinicia o ambiente por padrão e
executa quatro episódios isolados: pausa de um FlowPredictor, recuperação desse
agente, partição dos dois preditores em relação à rede ETCD e recuperação do
ETCD. O ataque só começa depois que o runner confirma a falha, por meio de um
gate de sincronização no workload Mininet. A pausa e a partição são sempre
desfeitas por um `trap`, inclusive em interrupções.

Antes de criar a topologia, o runner valida separadamente REST e OpenFlow em
cada Ryu. O Mininet usa as portas OpenFlow publicadas em `127.0.0.1`, evitando
que uma política de firewall do host contra a bridge Docker seja confundida com
falha dos agentes. Se um listener realmente não subir, o teste para antes do
baseline e salva `ryu-core-*-openflow-preflight.log`.

Cada workload encerra sua própria topologia com `net.stop()`. O runner não usa
`mn -c` entre episódios porque algumas instalações executam `killall` no
namespace de PIDs do host e terminam também o `ryu-manager` dos containers. Uma
limpeza forçada só ocorre se um workload precisar ser abortado; nesse caso os
containers Ryu são iniciados novamente pelo cleanup de emergência.

O relatório `experiments/results/runtime-fault-*/summary.json` exige ausência
de `AGREED` e de claim novo durante a falha, `WAITING_PROPOSALS` quando um
agente desaparece, erro observável durante a partição, e um novo `AGREED` dos
dois agentes após cada restauração. As propostas da recuperação precisam ter
timestamps posteriores ao novo ataque; consenso antigo não conta. O gate ainda
confirma zero requests ao FlowBlocker, zero regras DROP e registra as latências
de falha/recuperação. Ele não substitui o benchmark de TP/TN: mede segurança e
disponibilidade do protocolo distribuído.

### 2.7 Mitigação autônoma - guard-rails antes de agir

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

### 2.8 Ciclo de feedback

`POST /predictor/feedback` com
`{"anomaly_id": "...", "verdict": "false_positive"}` ajusta somente o
threshold correspondente ao tipo do evento **na série específica**
(×1.25 por FP e ×0.95 por TP). Assim, feedback de uma queda não reduz nem
aumenta a sensibilidade a DDoS. O modo offline
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
| GET | `/predictor/collaboration` | Configuração MCDA, evidências, claims e decisões explicadas |
| GET | `/predictor/agent` | Propostas, negociação, autorização/claim dry-run e comparação com o MCDA |
| GET | `/predictor/export/status` | Estado e contadores da exportação CSV |
| POST | `/predictor/feedback` | `{"anomaly_id", "verdict"}` — refina *thresholds* |
| POST | `/predictor/config` | Ajuste em tempo de execução: `auto_mitigate`, `dry_run`, `min_rate_bps`, `cooldown_s`, `event_cooldown_s` |

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
  "threshold": 3.75,
  "spike_z_threshold": 3.75,
  "drop_z_threshold": 20.0,
  "model_residual": 4.35518628,
  "detection_mode": "offline",
  "ts_detect_ns": 1752230000123456789,
  "first_seen_ns": 1752230000123456789,
  "last_seen_ns": 1752230018123456789,
  "suppressed_count": 8,
  "peak_observed_bps": 101300000.0,
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

**Horizontal (multi-domínio)**: um FlowPredictor por domínio. Cada
instância monitora apenas os DPIDs do seu controlador. No modo local, o
estado compartilhado continua opcional em
`flowpredictor/state/<cid>`. No modo colaborativo, o ETCD combina
evidências efêmeras e arbitra a ação, sem receber a telemetria completa.
`deploy_flow_predictor.sh N` preenche automaticamente
`COLLAB_EXPECTED_DOMAINS=N`; em uma implantação parcial, esse valor pode
ser sobrescrito explicitamente.

O emitter de cada `ryu-core-i` anuncia `flow-blocker-i` como endpoint do
seu domínio. Como todos os FlowBlockers também participam da rede Docker
compartilhada do ETCD, esse nome é resolvível entre domínios durante a
propagação de regras DROP.

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
quando o primeiro contador aparece; taxa zero de um fluxo é tratada como
término apenas após a tolerância configurada e nunca como anomalia de queda.
As regras IPv4 do SimpleSwitch usam `idle_timeout=30` e permanecem enquanto
houver tráfego.

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

### 5.2 CIC-DDoS2019 sem copiar os arquivos grandes para o servidor

Não passe os CSVs originais diretamente ao treinador: cada linha do
CICFlowMeter representa um fluxo concluído, enquanto o runtime observa
`rate_bps` em janelas de polling. Faça a preparação no computador que
armazena o dataset. O processo lê uma linha por vez e mantém somente as
janelas agregadas em memória.

Os comandos abaixo usam `DrDoS_UDP.csv` para treino e `UDP.csv` para
validação independente:

``` bash
python3 prepare_cicddos2019.py ~/Downloads/01-12/DrDoS_UDP.csv \
  --attack-label DrDoS_UDP \
  --attack-inbound-only \
  --series-key cic2019:drdos_udp \
  --output datasets/cic2019_drddos_udp_train.csv

python3 prepare_cicddos2019.py ~/Downloads/03-11/UDP.csv \
  --attack-label UDP \
  --attack-inbound-only \
  --series-key cic2019:udp \
  --output datasets/cic2019_udp_validation.csv
```

O filtro de rótulo é deliberado: o arquivo `UDP.csv` também contém
registros `MSSQL`. `--attack-inbound-only` mantém a direção atacante →
vítima; a direção de resposta não é tratada como um segundo ataque de
vazão.

Por padrão, o conversor:

1. soma os bytes forward/backward e os converte para bits;
2. distribui cada fluxo sobre sua duração (`Flow Duration` em µs);
3. agrega por par `Source IP` → `Destination IP` em janelas de 2 s;
4. mantém séries benignas observadas e antepõe dez janelas de baseline
   às séries que no CIC contêm somente ataque.

O último passo reproduz explicitamente o cenário do testbed: tráfego
baixo entre um par de hosts seguido pelo `iperf3`. O baseline sintético
usa a mediana das janelas benignas da própria captura e fica identificado
por `phase_source=synthetic_baseline`; não deve ser apresentado como uma
sequência de pacotes originalmente capturada.

Os CSVs compactos e seus metadados ficam em `datasets/`, ignorado pelo
Git. Em seguida, treine e avalie:

``` bash
python3 train_offline_model.py datasets/cic2019_drddos_udp_train.csv \
  --label-column label \
  --normal-label BENIGN \
  --series-priming-samples 2 \
  --output models/cic2019-drddos-udp-holt.json

python3 evaluate_offline_model.py \
  models/cic2019-drddos-udp-holt.json \
  datasets/cic2019_udp_validation.csv \
  --normal-label BENIGN \
  --min-rate-bps 50000 \
  --output models/cic2019-drddos-udp-validation.json
```

O treinador registra no modelo o hash do CSV compacto, as opções da
preparação e as métricas de calibração. A avaliação simula o estado do
runtime: a primeira amostra inicializa cada série e somente observações
classificadas como normais atualizam Holt. O relatório separa qualquer
anomalia de vazão da métrica `ddos_throughput_spike`, pois quedas são
alertadas pelo runtime, mas não representam DDoS e não são mitigadas.
Os dois thresholds também são gravados no relatório para tornar cada
resultado reproduzível. Para experimentos específicos, eles podem ser
fixados com `--spike-z-threshold` e `--drop-z-threshold`.

### 5.3 Executar o modelo no testbed

O caminho informado é montado como somente leitura em todos os
containers FlowPredictor:

``` bash
PREDICTOR_OFFLINE_MODEL="$PWD/models/cic2019-drddos-udp-holt.json" \
PREDICTOR_OFFLINE_MODEL_REQUIRED=true \
PREDICTOR_COLLABORATION_ENABLED=true \
PREDICTOR_COLLAB_MIN_DOMAINS=2 \
PREDICTOR_AGENTIC_ENABLED=true \
PREDICTOR_AGENTIC_SHADOW=true \
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
curl http://127.0.0.1:6060/predictor/collaboration | jq .
curl http://127.0.0.1:6060/predictor/agent | jq .
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
mininet> h1 ping -c 6 10.0.0.4 -i 0.5         # inicializa o mesmo par src/dst
mininet> h1 iperf3 -c 10.0.0.4 -u -b 100M -t 20  # SPIKE UDP súbito

# 7. Observar detecção, consenso e dry-run da mitigação
curl http://127.0.0.1:6060/predictor/anomalies | jq '.anomalies[0]'
curl http://127.0.0.1:6060/predictor/collaboration | jq '.decisions[0]'
curl http://127.0.0.1:6060/predictor/agent | jq '.decisions[0]'
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

## 7. BENCHMARK REPRODUZÍVEL

`scripts/run_collaborative_benchmark.sh` automatiza deploy, topologia,
tráfego e coleta. Ele encerra qualquer Mininet ativo com `mn -c`, cria a
topologia configurada, acompanha as APIs a cada 500 ms e desmonta a
topologia ao final. Por padrão, também reinicia Ryu, ETCD, SimpleSwitch e
FlowBlocker antes de cada execução. As imagens ativas são reconstruídas antes
desse bootstrap, garantindo que os containers correspondam ao commit gravado
nos metadados. O runner elimina estado residual, verifica as conexões OpenFlow,
força descoberta ARP bidirecional e exige que origem e destino estejam na
tabela agregada dos domínios antes de gerar o baseline. Portanto, não o execute
junto a outra experiência ativa. Para reutilizar conscientemente um ambiente
já validado, defina `BENCHMARK_BOOTSTRAP_ENV=false`.

Há quatro modos:

| Modo | Colaboração | Mitigação |
| --- | --- | --- |
| `local-dry-run` | não | simulada em cada domínio |
| `collaborative-dry-run` | MCDA + quórum | simulada apenas pelo coordenador |
| `collaborative-live` | MCDA + quórum | DROP real; exige `--allow-mitigation` |
| `agentic-live` | agentes + gate + claim; MCDA observacional | DROP real; exige `--allow-agentic-mitigation` |

E dois cenários: `benign`, que mantém UDP estável, e `ddos`, que executa
um baseline de 1 Mbit/s seguido por um salto de 100 Mbit/s. Uma bateria
mínima é:

``` bash
# Controle negativo: não deve chegar a MITIGATE
bash scripts/run_collaborative_benchmark.sh collaborative-dry-run benign

# Mede duplicação/latência da decisão local
bash scripts/run_collaborative_benchmark.sh local-dry-run ddos

# Mede consenso e eleição sem alterar o plano de dados
bash scripts/run_collaborative_benchmark.sh collaborative-dry-run ddos

# Compara a negociação dos agentes shadow com o MCDA no mesmo ensaio
BENCHMARK_AGENTIC_ENABLED=true \
  bash scripts/run_collaborative_benchmark.sh collaborative-dry-run ddos

# Agentes revalidam AGREED e elegem quem agiria, sem chamar o FlowBlocker
bash scripts/run_agentic_authority_dry_run.sh

# Campanha positiva/negativa antes de considerar autoridade operacional
bash scripts/run_agentic_authority_campaign.sh

# Canário agentic: benigno sem atuação, depois DDoS com um único executor
bash scripts/run_agentic_authority_live_canary.sh \
  --allow-agentic-mitigation

# Após o claim anterior expirar, valida o DROP real
bash scripts/run_collaborative_benchmark.sh \
  collaborative-live ddos --allow-mitigation
```

Ao reutilizar o ambiente, se ainda existir um claim do mesmo fluxo, o runner
para antes de limpar a topologia e informa quantos segundos aguardar. Isso
evita contaminar uma execução com o coordenador da anterior. No bootstrap
padrão, o ETCD é recriado para cada ensaio. O histórico CSV fica desabilitado
no benchmark por padrão para economizar armazenamento; use
`BENCHMARK_EXPORT_HISTORY=true` se as séries também forem necessárias.

Taxas e durações podem ser alteradas sem editar o script:

``` bash
BENCHMARK_BASELINE_RATE=5M \
BENCHMARK_ATTACK_RATE=200M \
BENCHMARK_ATTACK_DURATION_S=30 \
  bash scripts/run_collaborative_benchmark.sh collaborative-dry-run ddos
```

Cada execução cria um diretório pequeno em
`experiments/results/<timestamp>-<modo>-<cenário>/` contendo:

- metadados, hash do modelo e commit Git;
- JSON do iperf e ping antes/depois;
- linha do tempo NDJSON de predições, anomalias, decisões MCDA e agentic;
- snapshots das APIs, flows OVS e logs dos containers;
- `summary.json` e `summary.md` com score, domínios confirmadores,
  coordenador, quantidade de domínios que agiram, latências e classificação
  `TP/TN/FP/FN/CONTAMINATED/INVALID`.

Quando os agentes estão habilitados, o resumo separa explicitamente a
latência MCDA da latência agentic. Ele registra detecção→primeira proposta,
primeira→última proposta, última proposta→`AGREED`, detecção→`AGREED` e
ataque→`AGREED`. O agregado informa taxas de concordância agente–agente e
agente–MCDA, além de propostas expiradas e episódios que aguardaram quórum.
O campo `agentic_mcda_comparisons` identifica, por domínio, o `event_id`, o
instante e a base (`authority_evaluation` ou `first_event_observation`) usados
nessa taxa, permitindo auditoria sem confundir decisão com pós-mitigação.
Contadores são calculados em relação ao snapshot inicial, portanto a opção
`BENCHMARK_BOOTSTRAP_ENV=false` não incorpora execuções anteriores.

Para selecionar diretamente o segundo estágio no runner genérico, use
`BENCHMARK_AGENTIC_ENABLED=true` e
`BENCHMARK_AGENTIC_MODE=authority-dry-run`. Esse modo só aceita
`collaborative-dry-run`; combiná-lo com mitigação live é erro de configuração.
O terceiro estágio só é aceito pelo modo dedicado `agentic-live` e exige
`--allow-agentic-mitigation`; o runner injeta o opt-in no deploy e valida que
todos os endpoints anunciem `authoritative=true` e `actuation_enabled=true`.

Uma execução sem conexão com os controladores, sem ping mensurável, sem vazão
do baseline/ataque, sem os dois hosts na tabela de domínios ou com erro nas
APIs é `INVALID` e faz o runner terminar com status diferente de zero. Em
`collaborative-live` ou `agentic-live`, uma decisão `MITIGATE` sem confirmação
HTTP 200 do FlowBlocker também é inválida e conserva o motivo operacional no
relatório.
O DROP pode encerrar o canal de controle do próprio `iperf3` e fazê-lo retornar
status 1 antes de produzir o JSON final. Nesse modo, o workload registra
`attack_disrupted`, continua até o ping final e só aceita a interrupção como
efeito esperado quando a linha do tempo também confirma a execução HTTP 200 e
o ping observa perda. Assim, ausência de tráfego, interrupção espontânea ou
falha do DROP nunca é apresentada como sucesso.

Em cenários DDoS, spikes ou decisões de mitigação anteriores ao timestamp do
ataque classificam o ensaio como `CONTAMINATED`. Anomalias e ações são
separadas entre baseline e ataque, e a latência de detecção considera apenas
eventos posteriores ao início do ataque; por construção, ela nunca é negativa.
Execuções `CONTAMINATED` e `INVALID` são contabilizadas separadamente e não
entram nos denominadores de precision, recall ou F1.

Compare quaisquer execuções em uma única tabela:

``` bash
python3 experiments/summarize_benchmark.py \
  experiments/results/<execução-1> \
  experiments/results/<execução-2> \
  experiments/results/<execução-3> \
  --output experiments/results/comparativo
```

O comparativo também calcula precision, recall e F1 agregadas. Para que
essas métricas tenham significado, execute várias repetições de `benign`
e `ddos` sob as mesmas taxas, durações, topologia, modelo e commit.

------------------------------------------------------------------------

**Versão**: 1.9 · **Data**: 2026-08-10 · **Status**: modelo offline,
consenso MCDA multi-domínio, agentes em shadow/authority-dry-run, canário
authority-live com propriedade exclusiva, claim distribuído e benchmark
reproduzível
