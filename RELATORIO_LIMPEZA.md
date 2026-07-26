# Relatório da limpeza do repositório

Este documento registra exatamente o que foi alterado na organização do
`sdn_flow_predictor`, e por quê. Nada foi removido "no escuro": cada arquivo
apagado foi comparado byte a byte com sua versão ativa e cruzado com os
`Dockerfile`s para confirmar que não era usado em produção.

Se algo aqui foi removido por engano, todo o histórico continua disponível
no `git log` do repositório original — nada foi perdido, só reorganizado.

## Decisão de escopo

A limpeza focou em **remover duplicatas e código morto**, mantendo a
estrutura de pastas atual (`ryu_apps/`, `rest_client/`, `flow_blocker/`,
`eMSN_ENV/`, `traffic_predictor/`). Não reorganizei a árvore de diretórios
em algo como `docker/<serviço>/`, porque os scripts (`setup_env.sh`) e os
`Dockerfile`s têm caminhos relativos amarrados a essa estrutura, e o
ambiente já está validado funcionando — mudar caminhos gratuitamente só
trocaria risco por estética. Se quiser essa reorganização mais profunda
depois, é um passo separado.

## Arquivos removidos (duplicatas / código morto)

| Arquivo removido | Motivo |
|---|---|
| `ryu_apps/ofp_emitter.py` | Versão antiga (~70 linhas) do emitter Ryu. Superada por `emitter_cnsm.py` (259 linhas, é o que o `Dockerfile` builda) |
| `ryu_apps/ofp_emitter_vr.py` | Variação intermediária do mesmo emitter, também não usada pelo `Dockerfile` |
| `ryu_apps/eofp_emitter.py` | Outra versão intermediária, mesma função de `emitter_cnsm.py` |
| `ryu_apps/entrypoint.sh` | Chamava `ofp_emitter_vr.py` (já removido); o `Dockerfile` atual nem usa esse entrypoint, roda `ryu-manager` direto |
| `ryu_apps/Vagrantfile` | Provisionamento de VM pré-Docker; referenciava `ofp_emitter.py`, já morto |
| `rest_client/Simpleswitch_cnsm_original.py` | Versão anterior de `Simpleswitch_cnsm.py` (a atual, usada pelo `Dockerfile`) |
| `rest_client/simple_switch_rest.py` | Mesma função, nome diferente, versão mais antiga |
| `rest_client/simple_switch_rest2.py` | Idem |
| `rest_client/simple_switch_rest_VR.py` | Idem |
| `rest_client/Vagrantfile` | Provisionamento pré-Docker; referenciava `simple_switch_rest.py`, já morto |
| `flow_blocker/flow_blocker.py` | Versão anterior de `flow_blocker_cnsm.py` (a atual, usada pelo `Dockerfile` — tem suporte a bloqueio cross-domain via ETCD, que a antiga não tinha) |
| `flow_predictor_cnsm_original.py` | Versão anterior de `flow_predictor_cnsm.py` |
| `deploy_flow_predictor_original.sh` | Versão anterior de `deploy_flow_predictor.sh` |
| `killallscreens.sh` | Utilitário avulso, sem nenhuma referência no resto do projeto |
| `eMSN_ENV/setup_env_without_test.sh` | Variação antiga do bootstrap principal |
| `eMSN_ENV/automated_setup_f.sh` | Idem |
| `eMSN_ENV/setup_testbed.sh` | Idem |
| `eMSN_ENV/setup_tests.sh` | Idem |
| `eMSN_ENV/setup_mininet.py` | Esse arquivo é **gerado automaticamente** pelo próprio `setup_env.sh` a cada execução (ele mesmo escreve o arquivo via heredoc). A cópia que estava versionada era só a sobra de uma execução antiga. Adicionei `eMSN_ENV/setup_mininet.py` ao `.gitignore` para isso não voltar a acontecer. |
| `scripts/` (pasta inteira: `cleanup_setup_environment.sh`, `customtopology.py`, `mininet_10_5.sh`, `setup_environment.sh`, `setup_mininet.py`) | Ferramenta original, anterior ao fork/adaptação para `eMSN_ENV/`. Não é referenciada em nenhum lugar do `README.md` atual nem chamada por nenhum outro script. `eMSN_ENV/` é o fluxo oficial e ativo. |

## Arquivos movidos (não removidos — só reorganizados)

| De | Para | Motivo |
|---|---|---|
| `eMSN_ENV/README_OLD.md` | `docs/legacy/README_OLD.md` | README da ferramenta original dos autores. Tem valor histórico, mas não devia ficar misturado com o ambiente ativo. |
| `eMSN_ENV/experiment_01/` | `examples/experiment_01/` | São **saídas** de uma execução de teste (csv, json, txt de pcap/ping/iperf), não código. Não deveriam estar em `eMSN_ENV/` misturadas com os scripts ativos. |
| `eMSN_ENV/teste_manual/` | `examples/teste_manual/` | Mesma razão acima. |

## Bugs corrigidos de brinde

Durante a validação com você, encontramos e corrigimos dois bugs reais do
`eMSN_ENV/setup_env.sh`, que já apliquei nesta versão limpa também:

1. **Imagem do ETCD**: `bitnami/etcd` foi descontinuada gratuitamente no
   Docker Hub. Trocado para `bitnamilegacy/etcd:3.5`.
2. **Build do FlowPredictor**: o comando de build usava caminho relativo
   (`-f Dockerfile.flow_predictor .`) que só funciona se o script for
   chamado a partir da raiz do projeto — mas ele é feito pra ser chamado
   de dentro de `eMSN_ENV/`. Corrigido para usar a variável `$PROJECT_ROOT`
   que o próprio script já calculava (e não usava nessa linha).

O `README.md` também ganhou uma seção nova explicando que as imagens
`ryu_core_cnsm`, `simpleswitch_cnsm` e `flow_blocker_cnsm` precisam ser
buildadas manualmente antes do primeiro `./setup_env.sh` (o script não faz
isso sozinho, só builda a `flow_predictor_cnsm`).

## Resultado

- **Antes**: ~130 arquivos, com pelo menos 4 pipelines de bootstrap
  concorrentes e 3-4 versões de cada app Ryu/SimpleSwitch/FlowBlocker.
- **Depois**: só os arquivos que o `Dockerfile` de cada serviço e o
  `README.md` realmente referenciam, mais os utilitários confirmados como
  ativos (`validate_l3_pipeline.sh`, `cnsm_measurment.sh`,
  `run_mininet_tests.py`).

Nada do que já estava funcionando no seu ambiente foi afetado — os
caminhos usados pelos `docker build`/`docker run` continuam os mesmos.
