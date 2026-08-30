# Explicações completas — todos os 15 modelos, todos os 6 testes

Este documento contém o texto **literal e integral** gerado por cada modelo em cada um dos 6 cenários de teste, extraído diretamente dos arquivos JSON salvos durante a execução (sem resumo/paráfrase). Complementa o relatório de análise em `AVALIACAO_LLM_EXPLICACAO.md`.

## Modelo 1: Qwen3:8b

### Teste 1 — AGREED (executor)

**Veredito do script: `coerente`**

> A decisão "AGREED" foi tomada por quórum configurado que propôs mitigação, e foi **correta** pelos seguintes motivos: os agentes determinísticos negociaram e alcançaram consenso com os votos necessários, e a decisão foi validada como "AGREED válido e fresco", indicando que a mitigação foi autorizada e está alinhada com os critérios do sistema.

### Teste 2 — WAITING_PROPOSALS

**Veredito do script: `indeterminado`**

> A decisão "WAITING_PROPOSALS" foi tomada por falta de consenso entre os domínios envolvidos, e foi impossível avaliar pelos seguintes motivos: o domínio 192.168.11.10 não participou da negociação, e a decisão aguarda a proposta desse domínio para ser avaliada. O domínio 192.168.10.10 apresentou uma proposta de mitigação, mas não houve acordo suficiente para executar a ação. A ausência de domínios suficientes para validar a decisão impossibilita uma avaliação conclusiva.

### Teste 3 — AGREED (não-executor)

**Veredito do script: `coerente`**

> A decisão "AGREED" foi tomada por quórum configurado propõe mitigação, e foi correta pelos seguintes motivos: a decisão foi autorizada, com votos suficientes dos domínios relevantes, e o modelo de previsão foi avaliado como confiável com um score elevado. A autoridade foi confirmada e a decisão foi considerada válida e fresca.

### Teste 4 — CORROBORATED

**Veredito do script: `indeterminado`**

> A decisão CORROBORATED foi tomada por motivo de pontuação/quórum ainda insuficientes, e foi impossível avaliar pelos seguintes motivos: a decisão é um estado intermediário do protocolo, indicando que o sistema ainda não atingiu quórum ou consenso, portanto, não representa uma ação final de mitigação ou acordo.

### Teste 5 — VETOED

**Veredito do script: `incoerente`**

> A decisão VETOED foi tomada por ao menos um domínio relevante aplicar veto, e foi incorreta pelos seguintes motivos: o domínio de destino (192.168.11.10) vetou o fluxo por estar em whitelist, mas o domínio de origem (192.168.10.10) propôs mitigação com confiança de 0,82, indicando que a mitigação era necessária. A decisão final foi VETOED, o que contradiz a avaliação do domínio de origem, resultando em uma ação que não atendeu ao quórum necessário.

### Teste 6 — NORMAL (benigno)

**Veredito do script: `indeterminado`**

> A decisão NORMAL foi tomada por limiar Holt não confirmado, e foi impossível avaliar pelos seguintes motivos: o sistema está em modo authority-dry-run com atuação desativada, e a decisão agentic terminou em NORMAL sem indício de ataque, indicando que o processo ainda não atingiu quórum ou consenso final.

---

## Modelo 2: DeepSeek-R1:8b

### Teste 1 — AGREED (executor)

**Veredito do script: `indeterminado`**

> A decisão `AGREED` foi tomada por agentes determinísticos, e foi **impossível avaliar** pelos seguintes motivos: o registro não contém informações sobre a necessidade de mitigação, nem sobre o resultado da mitigação (se aplicada).

### Teste 2 — WAITING_PROPOSALS

**Veredito do script: `indeterminado`**

> A decisão **WAITING_PROPOSALS** foi tomada por **agentes determinísticos que necessitam de uma negociação completa para chegar a um consenso (AGREED)**, e foi **impossível avaliar** pelos seguintes motivos:

1.  **Proposta Pendente:** O estado explicitamente indica que a decisão ainda está aguardando propostas de outros domínios participantes (no caso, o domínio 192.168.11.10).
2.  **Incompatibilidade de Domínios:** É observado que o domínio 192.168.11.10 está ausente na lista de participantes e há uma incompatibilidade técnica entre as redes. A decisão requer a participação de todas as partes relevantes para ser validada.
3.  **Evidência Limitada:** Não há informações suficientes na evidência fornecida para determinar se a mitigação é necessária ou se a situação é realmente crítica.

### Teste 3 — AGREED (não-executor)

**Veredito do script: `indeterminado`**

> A decisão **AGREED** foi tomada por **quórum configurado propõe mitigação**, e foi **impossível avaliar** pelos seguintes motivos: a mitigação ainda não foi executada pelo sistema, apenas alcançada uma decisão de consenso (AGREED) sem a implementação efetiva da mitigação.

### Teste 4 — CORROBORATED

**Veredito do script: `indeterminado`**

> A decisão `CORROBORATED` foi tomada por um único domínio participante (`192.168.10.10`) com uma pontuação de 0.720359, e foi **impossível avaliar** pelos seguintes motivos: o estado `CORROBORATED` é um intermediário válido do protocolo, exigindo pelo menos dois domínios para atingir um consenso final (como `AGREED`). A decisão atual não atingiu o quórum necessário, conforme evidenciado pela contagem de domínios participantes (1 < 2) e pela baixa pontuação.

### Teste 5 — VETOED

**Veredito do script: `indeterminado`**

> A decisão **VETOED** foi tomada por **pelo menos um domínio relevante aplicar veto**, e foi **impossível avaliar** pelos seguintes motivos: A decisão final do protocolo (VETOED) é um estado intermediário válido de negociação, não uma decisão final como AGREED ou MITIGATED. Portanto, não há base para classificar sua corretude ou incorreitude, pois a decisão ainda está em andamento e não foi executada de fato.

### Teste 6 — NORMAL (benigno)

**Veredito do script: `coerente`**

> A decisão **NORMAL** foi tomada por ambos os agentes (fonte e destino) concordarem que a taxa de transferência está dentro do intervalo previsto pelo modelo Holt, e foi **correta** pelos seguintes motivos: os votos coincidentes e a confirmação de que a vazão observada não ultrapassa os limites do modelo demonstram que o tráfego é normal, conforme o protocolo de negociação.

---

## Modelo 3: gemma4:12b

### Teste 1 — AGREED (executor)

**Veredito do script: `coerente`**

> A decisão AGREED foi tomada porque o quórum de 2 votos exigidos para o fluxo 10.0.0.1->10.0.0.8 foi alcançado pelos agentes 192.168.10.10 e 192.168.11.10, ambos identificando "DDOS_LIKELY". A decisão foi correta, pois o sistema identificou um consenso unânime de mitigação com confiança de 0.858386 e validou a autoridade do "claim" adquirido. Embora a execução não tenha ocorrido (`execution.executed: false`), isso se deve ao modo de operação "authority-dry-run" e ao fato de o FlowBlocker estar desconectado, e não a uma falha lógica na tomada de decisão.

### Teste 2 — WAITING_PROPOSALS

**Veredito do script: `indeterminado`**

> A decisão `WAITING_PROPOSALS` foi tomada porque o sistema aguarda a participação do domínio `192.168.11.10`, que consta na lista de domínios relevantes mas não enviou uma proposta. A decisão foi **impossível avaliar** pelos seguintes motivos: o log indica que o sistema está operando conforme o protocolo desenhado para decisões multi-domínio, onde o estado `WAITING_PROPOSALS` é um estado intermediário válido. Como o quórum necessário (`required_votes: 2`) ainda não foi atingido e a execução não foi tentada, o processo de consenso ainda está em andamento.

### Teste 3 — AGREED (não-executor)

**Veredito do script: `coerente`**

> A decisão AGREED foi tomada por um quórum de domínios (192.168.10.10 e 192.168.11.10) que identificou um padrão de ataque via modelo "holt_residual" com alta pontuação de confiança (0.915167). A decisão foi correta, pois o registro confirma que o quórum de 2 votos necessários foi atingido e a autoridade foi validada como "fresca" e válida pelo sistema de consenso. Embora a execução não tenha ocorrido localmente (dry-run), isso se deve ao fato de o fluxo pertencer a outro coordenador, e não a um erro de detecção ou lógica do protocolo.

### Teste 4 — CORROBORATED

**Veredito do script: `indeterminado`**

> A decisão CORROBORATED foi tomada por pontuação/quórum ainda insuficientes, e foi impossível avaliar pelos seguintes motivos: o estado indica que a decisão ainda está em andamento e o sistema não atingiu o quórum necessário. Embora apenas 1 domínio tenha participado da validação, o sistema exige um mínimo de 2 domínios para alcançar o consenso final. Portanto, o registro reflete um estado intermediário esperado pelo protocolo de colaboração.

### Teste 5 — VETOED

**Veredito do script: `coerente`**

> A decisão VETOED foi tomada porque ao menos um domínio relevante (o destino 192.168.11.10) exerceu seu direito de veto, e foi correta pelos seguintes motivos: o sistema operou conforme o protocolo de consenso multi-domínio, onde a proposta de mitigação do domínio de origem foi invalidada pelo status de "whitelist" do destino. Como a decisão final foi "VETOED" e o estado de execução foi "not_executed" devido à falta de consenso, o sistema agiu corretamente ao respeitar a autoridade do domínio de destino sobre sua própria infraestrutura.

### Teste 6 — NORMAL (benigno)

**Veredito do script: `coerente`**

> A decisão NORMAL foi tomada por "limiar Holt não confirmado", e foi correta pelos seguintes motivos: o sistema operou em modo "authority-dry-run" com "actuation_enabled" como falso, o que indica um ambiente de teste ou validação de regras sem execução real. Ambos os agentes (SOURCE e DESTINATION) concordaram com o estado NORMAL, apresentando alta confiança (0.91 e 0.89) baseada no modelo Holt. Como o consenso foi atingido e não houve indício de ataque, a decisão de manter o tráfego como normal está tecnicamente alinhada com os dados processados.

---

## Modelo 4: gemma3:12b

### Teste 1 — AGREED (executor)

**Veredito do script: `indeterminado`**

> A decisão "AGREED" foi tomada por um quórum de agentes que propuseram a mitigação, baseado em evidências locais e um limiar de confiança ultrapassado. A decisão é impossível avaliar, pois o registro indica que o sistema está em um "authority-dry-run" e a execução da mitigação não foi tentada, devido a um "FlowBlocker desconectado". Apesar da autorização e do "claim" adquirido, a ausência de execução impede uma avaliação definitiva da correção da decisão. O registro demonstra que o processo de tomada de decisão está funcionando conforme o esperado, mesmo que a ação não seja efetivada.

### Teste 2 — WAITING_PROPOSALS

**Veredito do script: `indeterminado`**

> A decisão "WAITING_PROPOSALS" foi tomada porque o sistema está aguardando uma proposta do domínio "192.168.11.10". A decisão é impossível avaliar pelos seguintes motivos: o registro demonstra um processo de negociação em andamento, buscando consenso entre domínios para mitigar um possível ataque DDoS. O sistema ainda não atingiu um quórum de aprovação, estando em um estado intermediário válido do protocolo. A ação ainda não foi executada e a autoridade ainda não está autorizada.

### Teste 3 — AGREED (não-executor)

**Veredito do script: `indeterminado`**

> A decisão "AGREED" foi tomada por um quórum configurado proponente mitigação, com base em um modelo "holt_residual:8dc3603f632f166d3f12b1c1". A decisão é "impossível avaliar" neste momento, pois a execução da mitigação não foi tentada, estando em modo "authority-dry-run" devido a um agente coordenador existente. O registro indica que a decisão foi autorizada, mas a ação de mitigação foi bloqueada por um agente coordenador, impedindo a execução efetiva. Portanto, a avaliação completa da decisão só poderá ser feita após a tentativa de execução.

### Teste 4 — CORROBORATED

**Veredito do script: `indeterminado`**

> A decisão "CORROBORATED" foi tomada devido à falta de pontuação/quórum suficiente, e é impossível avaliar sua correção neste momento pelos seguintes motivos: o registro indica um estado intermediário do protocolo de colaboração, onde a decisão ainda está em andamento. O sistema está em processo de negociação entre domínios e ainda não atingiu um consenso final. Portanto, classificar a decisão como correta ou incorreta seria prematuro, dado que a colaboração ainda não foi concluída.

### Teste 5 — VETOED

**Veredito do script: `indeterminado`**

> A decisão "VETOED" foi tomada devido a um veto de um domínio relevante, especificamente o domínio 192.168.11.10, que justificou o veto com base em uma whitelist do destino. A decisão é "impossível avaliar" pois o processo ainda não atingiu um estado final de consenso ou execução. O registro indica que a proposta de mitigação do domínio 192.168.10.10 foi contrariada, resultando no veto e na suspensão da execução. O sistema está operando de acordo com o protocolo, pois o estado "VETOED" é um resultado possível durante a negociação.

### Teste 6 — NORMAL (benigno)

**Veredito do script: `indeterminado`**

> A decisão "NORMAL" foi tomada por um limiar Holt não confirmado, e é impossível avaliar a sua correção neste momento. O registro indica que o processo de negociação está em andamento, com agentes propostos concordando com o estado "NORMAL" com base em seus modelos Holt, mas sem atingir o quórum necessário para uma decisão final. O sistema opera em "authority-dry-run" com a atuação desabilitada, refletindo que esta é uma fase de avaliação, não de ação. Portanto, a decisão é provisória e a sua validade dependerá do desfecho da negociação entre os agentes.

---

## Modelo 5: qwen3.5:9b

### Teste 1 — AGREED (executor)

**Veredito do script: `coerente`**

> A decisão "AGREED" foi tomada porque dois domínios votaram para mitigação, atingindo o quórum requerido após a validação de um claim válido pela autoridade em modo dry-run. A decisão é correta, mas nenhuma ação de bloqueio efetiva ocorreu no momento da auditoria devido ao estado do sistema permanecer em "authority-dry-run" com o FlowBlocker desconectado. Portanto, embora o consenso lógico esteja consolidado, a mitigação física ainda não foi executada.

### Teste 2 — WAITING_PROPOSALS

**Veredito do script: `indeterminado`**

> A decisão WAITING_PROPOSALS foi tomada por aguardar o voto de um domínio participante faltante (192.168.11.10), e foi impossível avaliar corretamente ou incorretamente porque o sistema ainda não atingiu o quórum necessário de 2 votos para uma decisão final. Trata-se de um estado intermediário válido do protocolo onde a mitigação aguarda consenso antes da execução, sem que haja tentativa de ação nem consenso alcançado.

### Teste 3 — AGREED (não-executor)

**Veredito do script: `coerente`**

> A decisão 'AGREED' foi tomada pelo quórum de domínios participantes (192.168.10.10 e 192.168.11.10) concordando com a mitigação do fluxo, mas foi considerada 'não executada' porque o sistema entrou no modo 'authority-dry-run', onde uma autoridade diferente atua como coordenador principal para evitar conflitos de ação redundante entre agentes autônomos. Portanto, a classificação é tecnicamente correta quanto ao consenso alcançado, embora a atuação física da mitigação tenha sido suprimida por protocolo em favor da coordenação hierárquica.

### Teste 4 — CORROBORATED

**Veredito do script: `indeterminado`**

> A decisão CORROBORATED foi tomada pelo agente 192.168.10.10 devido à pontuação e quórum ainda insuficientes para consenso, e é impossível avaliar a sua correção porque este estado representa uma decisão intermediária do protocolo que ainda está em andamento. O registro indica que o sistema espera por mais domínios participantes (previstos 2) enquanto opera dentro do fluxo de negociação colaborativa normal, sem que uma mitigação final tenha sido executada ou invalidada. Portanto, classificar como errada seria incorreto, pois trata-se apenas de um passo provisório antes da conclusão definitiva.

### Teste 5 — VETOED

**Veredito do script: `coerente`**

> A decisão VETOED foi tomada porque o domínio de destino (192.168.11.10) aplicou um veto justificando que seu endereço está na whitelist, impedindo o quórum necessário para aprovação. A decisão é correta, uma vez que a autoridade do sistema confirmou que a execução não deve ser tentada pois a condição de consenso falhou devido ao veto válido. Portanto, a mitigação foi corretamente bloqueada apesar da detecção de provável ataque DDoS pela origem.

### Teste 6 — NORMAL (benigno)

**Veredito do script: `coerente`**

> A decisão NORMAL foi tomada porque ambos os agentes (origem e destino) concordaram que a vazão observada estava dentro do intervalo previsto pelo modelo Holt, sem confirmar o limiar de anomalia. A decisão é correta pois atingiu consenso com confiança elevada (0,91 e 0,89), indicando tráfego legítimo e não havendo necessidade de mitigação executada ou tentada. Não há indícios de falha no processo de votação ou execução, uma vez que a ativação de defesa permanece desligada por ausência de ataque detectado.

---

## Modelo 6: granite4.1:8b

### Teste 1 — AGREED (executor)

**Veredito do script: `coerente`**

> A decisão **AGREED** foi tomada por um quórum configurado que propôs a mitigação, e foi **correta** pelos seguintes motivos: duas votações (de "192.168.10.10" e "192.168.11.10") concordaram com a proposta de mitigação baseada em evidências locais e contexto superando o limiar do agente, indicando um DDoS provável no fluxo "10.0.0.1->10.0.0.8". A decisão foi executada em um modo de "authority-dry-run", simulando a mitigação sem alterações reais no sistema, conforme autorizado e confirmado pelos agentes participantes.

### Teste 2 — WAITING_PROPOSALS

**Veredito do script: `indeterminado`**

> A decisão **WAITING_PROPOSALS** foi tomada por aguardar a proposta do domínio 192.168.11.10, e foi **impossível avaliar** pelos seguintes motivos: o sistema está em modo "authority-dry-run", ausente o domínio 192.168.11.10, e a decisão ainda não atingiu o quórum necessário (2 votos) para mitigação. Apenas um domínio (192.168.10.10) apresentou uma proposta de mitigação, e a decisão está aguardando a resposta do segundo domínio relevante. Portanto, o protocolo está funcionando conforme o esperado, esperando por mais informações antes de tomar uma decisão final.

### Teste 3 — AGREED (não-executor)

**Veredito do script: `coerente`**

> A decisão **AGREED** foi tomada por um acordo válido e fresco entre os domínios participantes, e foi **correta** pelos seguintes motivos: o pedido de mitigação foi autorizado com base em um acordo fresco (code: "authorized") e atendeu ao quórum necessário de votos (2) de domínios relevantes (192.168.10.10 e 192.168.11.10). O MCDA (Multi-Criteria Decision Analysis) confirmou a decisão com uma pontuação alta (0.915167) e identificou o fluxo correto (10.0.0.1->10.0.0.8) para mitigação. O modo "authority-dry-run" indica que a decisão foi autorizada, mas a execução foi coordenada por outro agente, garantindo a consistência do sistema SDN.

### Teste 4 — CORROBORATED

**Veredito do script: `indeterminado`**

> A decisão **CORROBORATED** foi tomada por **pontuação/quórum ainda insuficientes**, e foi **impossível avaliar** pelos seguintes motivos: a decisão está em um estado intermediário do protocolo SDN de mitigação de DDoS multi-domínio, indicando que, embora uma avaliação preliminar tenha sido realizada, ainda não foram atendidos os critérios necessários para alcançar consenso ou tomar uma mitigação efetiva. O score de 0.720359 sugere que, embora haja alguma correlação, ainda não há quórum suficiente para confirmar a decisão. Portanto, a decisão não pode ser considerada correta ou incorreta neste momento, pois a avaliação ainda está em andamento.

### Teste 5 — VETOED

**Veredito do script: `coerente`**

> A decisão **VETOED** foi tomada por **pelo menos um domínio relevante aplicou veto**, e foi **correta** pelos seguintes motivos: o agente de origem (192.168.10.10) propôs a mitigação com base em evidências locais, mas o agente de destino (192.168.11.10) vetou a decisão porque o destino estava em uma whitelist, o que justificou o veto e impediu a mitigação. A falta de votos necessários (2) para aprovar a mitigação, com apenas um voto a favor, reforça a validade do veto.

### Teste 6 — NORMAL (benigno)

**Veredito do script: `indeterminado`**

> A decisão NORMAL foi tomada por limiar Holt não confirmado, e foi impossível avaliar pelos seguintes motivos: os agentes determinísticos (não LLM) que negociaram sobre o fluxo de rede concluíram que o tráfego observado estava dentro do intervalo previsto pelo modelo Holt, resultando em uma crença de TRáfego NORMAL com alta confiança (0,89-0,91) para ambas as partes (fonte e destino). No entanto, como o estado está como "authority-dry-run" com atuação desabilitada e sem decisão oficial acordada, não há mitigação de fato tentada ou executada, tornando a avaliação de correção ou incorreção imposível no momento. A decisão ainda está em andamento, aguardando quórum/consenso para se tornar final. 

**Formato do parecer:**
```
A decisão NORMAL foi tomada por limiar Holt não confirmado, e foi impossível avaliar pelos seguintes motivos: os agentes determinísticos concluíram que o tráfego estava dentro do intervalo previsto pelo modelo Holt, mas, como o estado está em "authority-dry-run" sem atuação habilitada e sem decisão acordada, a avaliação de correção ou incorreção não pode ser feita agora. A decisão ainda está em andamento.
```

---

## Modelo 7: ornith:9b

### Teste 1 — AGREED (executor)

**Veredito do script: `incoerente`**

> A decisão AGREED foi tomada por quórum configurado com dois votos de mitigação (192.168.10.10 e 192.168.11.10) concordando com evidência de DDOS provável, e foi INCORRETA porque a execução foi explicitamente bloqueada: o registro indica "execution.executed: false" com razão "authority-dry-run: claim adquirido; FlowBlocker desconectado", e "actuation_enabled: false", apesar de "would_execute: true" — ou seja, a mitigação foi aprovada mas nunca efetivamente aplicada.

### Teste 2 — WAITING_PROPOSALS

**Veredito do script: `indeterminado`**

> A decisão WAITING_PROPOSALS foi tomada por aguardar proposta do domínio 192.168.11.10, e foi impossível avaliar pelos seguintes motivos: o sistema ainda está em estado intermediário de coleta de propostas — apenas o domínio 192.168.10.10 já enviou proposta MITIGATE com confiança 0.858, mas o domínio 192.168.11.10 (destino) ainda não participou, e a decisão requer 2 votos para consenso, estando portanto em andamento.

### Teste 3 — AGREED (não-executor)

**Veredito do script: `coerente`**

> A decisão AGREED foi tomada porque os dois domínios participantes (192.168.10.10 e 192.168.11.10) concordaram com a mitigação do fluxo 10.0.0.1→10.0.0.8, atingindo quórum de 2 votos necessários; foi **correta** pois o modelo Holt residual validou o fluxo com confiança 0.858 e a autoridade confirmou o quórum com MCDA score 0.915, embora a execução não tenha ocorrido em modo dry-run porque outro agente coordenava.

### Teste 4 — CORROBORATED

**Veredito do script: `indeterminado`**

> A decisão CORROBORATED foi tomada por agentes determinísticos com apenas um domínio participante (192.168.10.10), quando o quórum mínimo exigido é de 2 domínios; foi impossível avaliar pelos seguintes motivos: o estado é explicitamente intermediário do protocolo — o campo "reason" confirma que "pontuação/quórum ainda insuficientes", e a decisão final de mitigação ainda não foi executada, sendo o sistema operando conforme projetado ao aguardar confirmação adicional.

### Teste 5 — VETOED

**Veredito do script: `coerente`**

> A decisão VETOED foi tomada porque pelo menos um domínio relevante (192.168.11.10) aplicou veto, com o destino em whitelist, enquanto o domínio de origem (192.168.10.10) propôs mitigação com confiança 0.82; a decisão foi correta porque o protocolo SDN de mitigação multi-domínio exige quórum de votos, e o veto do destino em whitelist é uma regra válida que impede a mitigação de tráfego destinado a um serviço protegido, mesmo que o agente de origem tenha identificado padrão de DDoS.

### Teste 6 — NORMAL (benigno)

**Veredito do script: `coerente`**

> A decisão NORMAL foi tomada por agentes determinísticos que concordaram que a vazão observada no fluxo 10.0.0.2→10.0.0.6 se encaixa no intervalo previsto pelo modelo Holt ( Holt_residual:8dc3603f632f166d3f12b1c1 ), com confianças de 0.91 (fonte) e 0.89 (destino), e sem indício de ataque detectado. A decisão foi correta porque os dois domínios (192.168.10.10 e 192.168.11.10) emitiram votos concordantes e a autoridade (authority-dry-run) confirmou que não havia necessidade de mitigação; a execução foi corretamente bloqueada por actuation_enabled=false e authority.authorized=false, sem risco de ação indevida.

---

## Modelo 8: ornith-1.5:9b

### Teste 1 — AGREED (executor)

**Veredito do script: `coerente`**

> A decisão "AGREED" foi tomada por quórum configurado (required_votes: 2), com votos de mitigação de ambas as domínios participantes (192.168.10.10 e 192.168.11.10), ambos com crença "DDOS_LIKELY" e confiança 0.858386, e foi **correta** pelos seguintes motivos: o quórum foi efetivamente alcançado (2 votos, 2 domínios), a autorização foi concedida ("authorized": true, código "AGREED"), o estado "AGREED" é uma decisão final (não intermediária), o claim foi adquirido com sucesso ("won": true) e a decisão foi avaliada como válida ("evaluated": true, "evaluated_ns" presente), com timestamp de avaliação anterior aos proposals.

**Observação importante:** embora a decisão de *consenso* seja correta, o campo "execution" mostra "attempted": false / "executed": false / "owner": "agentic" / "would_execute": true, indicando que a mitigação não foi de fato tentada nem executada — trata-se de um "authority-dry-run" (modo de simulação/ensaio). Portanto, a decisão foi corretamente formada, mas **não resultou em ação real de mitigação**.

### Teste 2 — WAITING_PROPOSALS

**Veredito do script: `indeterminado`**

> A decisão **WAITING_PROPOSALS** foi tomada por aguardar uma proposta adicional da domínio destino (192.168.11.10), que consta em `missing_domains` e não respondeu ainda, e foi **impossível avaliar** pelos seguintes motivos: o estado ainda é intermediário do protocolo, não uma decisão final — o sistema está funcionando como projetado apenas porque o quórum de 2 votos não foi atingido (apenas 1 voto "MITIGATE" de 192.168.10.10, com 192.168.11.10 em falta); o `authority` registra `authorized: false` com código `decision_not_agreed`, e `execution` confirma `attempted: false`/`executed: false`, com a execução aguardando que a decisão atinja "AGREED". Portanto, não há ainda uma decisão final de mitigar nem de não mitigar para julgar.

### Teste 3 — AGREED (não-executor)

**Veredito do script: `coerente`**

> A decisão **AGREED** foi tomada por quórum configurado (2 votos de mitigação entre `192.168.10.10` e `192.168.11.10`, atendendo ao `required_votes: 2`), e foi **correta** pelos seguintes motivos: o quórum foi atingido, a autorização retornou `authorized: true` com base em um evento `AGREED` válido e fresco (`decision_event_id` correspondente ao `evaluated_ns`), a avaliação cruzou o `mcda_comparison` (`matches: true`, score 0.915167), e a temporalidade está consistente (`first_proposal_ns` < `last_proposal_ns` < `state_entered_ns`). 

**Observação:** apesar de `authorized: true`, a execução não ocorreu (`attempted: false`, `executed: false`, `would_execute: false`) porque o registro está em modo `authority-dry-run` — outro agente (`192.168.11.10`, coordenador) já coordena o fluxo, de modo que o agente que emitiu esta decisão cedeu a execução. Isso não invalida a decisão, apenas evita a execução duplicada.

### Teste 4 — CORROBORATED

**Veredito do script: `indeterminado`**

> A decisão **CORROBORATED** foi tomada por **192.168.10.10**, e foi **impossível avaliar** pelos seguintes motivos: trata-se de um estado intermediário do protocolo de colaboração (seção "collaboration"), não de uma decisão final. O próprio registro informa, pelo campo "reason", que "pontuação/quórum ainda insuficientes", corroborado pelos valores `score: 0.720359` (abaixo de um limiar provável), `expected_domains: 2` e `min_domains: 2` — ou seja, o agente avaliou apenas 1 domínio participante (`participing_domains` possui um único elemento), insuficiente para o quórum mínimo exigido de 2. Portanto, o agente está ainda agregando evidências e negociando, não há como julgar se a mitigação final será correta ou incorreta.

### Teste 5 — VETOED

**Veredito do script: `coerente`**

> A decisão **VETOED** foi tomada por pelo menos um domínio relevante ter aplicado veto (no caso, o destino 192.168.11.10, que propôs VETO com justificativa de estar em whitelist), impedindo o quórum necessário de 2 votos favoráveis à mitigação, sendo **correta** pelos seguintes motivos: o protocolo SDN multi-domínio exige consenso entre todos os domínios relevantes afetados pelo fluxo antes de autorizar uma ação de mitigação invasiva (redirecionamento do tráfego); agir unilateralmente para mitigar um possível DDoS poderia interromper o tráfego legítimo de um serviço whitelisted, enquanto o estado final não é intermediário (como WAITING/CORROBORATED), mas sim uma decisão terminal que já foi concluída, com actuação desabilitada e execução não tentada, refletindo o comportamento adequado do sistema face ao veto.

### Teste 6 — NORMAL (benigno)

**Veredito do script: `coerente`**

> A decisão NORMAL foi tomada pelos agentes determinísticos porque ambos os peers (fonte e destino) propuseram NORMAL com base em resíduos do modelo Holt, e o quórum de 2 votos foi atingido com consenso unânime. Foi **correta** pelos seguintes motivos: a decisão é final (não é um estado intermediário), há coerência entre as propostas e a decisão, e o `execution.would_execute=false` confirma que não houve mitigação indevida; a ausência de votação para mitigação (`mitigate_votes: []`) e a indicação explícita de "sem indício de ataque" mostram que o fluxo real foi mantido, o que é apropriado quando o tráfago realmente se enquadra no intervalo previsto. 

Observações para atenção do auditor: apesar do `authority.authorized=false` com código `decision_not_agreed`, isso não invalida a decisão em si, pois em um ambiente multi-domínio os domínios envolvidos (192.168.10.x e 192.168.11.x) são diferentes da autoridade que propositou — por isso a autoridade recusou-se a endossar, mas o consenso entre os peers legítimos ainda sustenta a manutenção do fluxo. Vale conferir, contudo, que os dois peers compartilham o mesmo `model_id` (`holt_residual:8dc3603f632f166d3f12b1c1`); se modelos de detecção residuais forem reutilizados entre domínios distintos, isso pode mascarar divergências reais no comportamento do tráfago.

---

## Modelo 9: lfm2.5:8b

### Teste 1 — AGREED (executor)

**Veredito do script: `indeterminado`**

> A decisão AGREED foi tomada por quórum configurado propõe mitigação. Foi impossível avaliar pelos seguintes motivos: ainda não atingiu quórum final e a execução está em andamento. Não há evidência de que a mitigação tenha sido efetivamente aplicada.

### Teste 2 — WAITING_PROPOSALS

**Veredito do script: `indeterminado`**

> A decisão WAITING_PROPOSALS foi tomada por espera de propostas, e ainda não houve consenso final; portanto, é impossível avaliar. Não há registro de uma decisão final que atinja o estado AGREED. Sem quórum ou consenso, a execução não foi iniciada.

### Teste 3 — AGREED (não-executor)

**Veredito do script: `indeterminado`**

> A decisão AGREED foi tomada por autorização. O registro indica que o quórum ainda não foi alcançado e outro agente está coordenando a execução. Portanto, é impossível avaliar se a decisão é correta ou incorreta neste momento.

### Teste 4 — CORROBORATED

**Veredito do script: `indeterminado`**

> A decisão CORROBORATED foi tomada por insuficiente pontuação/quórum. O consenso ainda não foi atingido. É impossível avaliar se a mitigação foi efetiva.

### Teste 5 — VETOED

**Veredito do script: `indeterminado`**

> A decisão VETOED foi tomada por motivo de que pelo menos um domínio relevante aplicou veto. Ela é impossível de avaliar pois ainda não houve consenso ou acordo final. A proposta permanece em estado de negociação e não foi executada.

### Teste 6 — NORMAL (benigno)

**Veredito do script: `indeterminado`**

> A decisão NORMAL foi tomada pelos agentes determinísticos com base no limiar Holt não confirmado. Ela foi impossível avaliar pois o processo ainda não atingiu quórum/consenso. Consequentemente, a validade final da decisão permanece indefinida.

---

## Modelo 10: llama3.1:8b

### Teste 1 — AGREED (executor)

**Veredito do script: `indeterminado`**

> A decisão "AGREED" foi tomada por quórum configurado, e é impossível avaliar se foi correta ou incorreta, pois a decisão ainda está em andamento. Isso é devido ao estado "authority-dry-run", que indica que o sistema está em uma fase de simulação e não executou a mitigação ainda. Além disso, os dois agentes determinísticos (não LLM) concordaram em negociar sobre o fluxo de rede e propuseram uma mitigação, o que também sugere que a decisão está em andamento.

### Teste 2 — WAITING_PROPOSALS

**Veredito do script: `indeterminado`**

> A decisão foi tomada por meio da negociação entre agentes determinísticos. Foi impossível avaliar, pois a decisão ainda está em andamento, pois a decisão ainda está em andamento, pois o sistema aguarda proposta de 192.168.11.10.

### Teste 3 — AGREED (não-executor)

**Veredito do script: `indeterminado`**

> A decisão "AGREED" foi tomada por motivos de quórum configurado, e é impossível avaliar, pois a decisão ainda está em andamento e outros estados intermediários (como "WAITING" ou "WAITING_PROPOSALS") podem ser alcançados antes que uma decisão final seja tomada. O registro mostra que a decisão ainda está em andamento e que outros estados intermediários são possíveis.

### Teste 4 — CORROBORATED

**Veredito do script: `indeterminado`**

> A decisão "CORROBORATED" foi tomada, mas é impossível avaliar se está correta ou incorreta. Isso ocorre porque a decisão é um estado intermediário do protocolo, significando que o quórum ainda não foi atingido. A razão fornecida para a decisão é "pontuação/quórum ainda insuficientes", o que indica que a decisão ainda está em andamento.

### Teste 5 — VETOED

**Veredito do script: `indeterminado`**

> A decisão VETOED foi tomada, e é impossível avaliar a sua correta ou incorreta natureza, pois a decisão ainda está em andamento. Isso ocorre porque ao menos um domínio relevante aplicou veto, conforme explicitado na razão "ao menos um domínio relevante aplicou veto".

### Teste 6 — NORMAL (benigno)

**Veredito do script: `indeterminado`**

> A decisão NORMAL foi tomada por limiar Holt não confirmado, e é impossível avaliar. A decisão ainda está em andamento, pois os agentes determinísticos ainda não atingiram o quórum necessário para tomar uma decisão conclusiva. Os motivos para a impossibilidade de avaliação estão diretamente relacionados aos próprios estados intermediários do protocolo, que não indicam erros, mas sim a necessidade de mais informações para chegar a uma decisão conclusiva.

---

## Modelo 11: qwen3.5:0.8b

### Teste 1 — AGREED (executor)

**Veredito do script: `coerente`**

> A decisão de Agreed foi tomada pela autoridade devido ao consenso gerado entre os agentes, e foi classificada como correta nos termos do fluxo de rede (mitigação de DDoS) baseado na solicitação e nos votos confirmados para as domínios 192.168.10.10 e 192.168.11.10.

### Teste 2 — WAITING_PROPOSALS

**Veredito do script: `indeterminado`**

> A decisão **WAITING_PROPOSALS** foi tomada pelo agente para solicitar o consenso com o domínio 192.168.11.10 antes de atinge-se um valor crítico de confiança, pois o evento de segurança ainda não atingiu o critério de "GREGATORIAL" (agrego) no protocolo estabelecido, o que implica que a decisão ainda está em andamento sem consenso final.

### Teste 3 — AGREED (não-executor)

**Veredito do script: `indeterminado`**

> O autoridade determinista (agentic) consolidou a decisão final "AGREED" por ser um quórum válido e fresco com 2 de 2 votos solicitados entre os domínios relevantes, conferindo uma mitigação confirmada ao agente que negocia esse fluxo.

### Teste 4 — CORROBORATED

**Veredito do script: `indeterminado`**

> A decisão **CORROBORATED** foi tomada por agentes determinísticos que negociaram sobre um fluxo de rede entre 10.0.0.1 e 10.0.0.8, indicando um consenso progressivo na mitigação de DDoS em múltiplos domínios, embora ainda não haja quórum absoluto alcançado pelo protocolo para validar a decisão final.

### Teste 5 — VETOED

**Veredito do script: `incoerente`**

> A decisão VETOED foi tomada pelo agente "192.168.10.10", e foi incorreta pois o domínio 192.168.11.10, qualificado como alvo de vetos (whitelist), gerou uma validação suficiente para que a decisão de VETOED se transformasse em AGREED na execução final, independentemente da evidência local ou do contexto superando o limiar do agente.

### Teste 6 — NORMAL (benigno)

**Veredito do script: `coerente`**

> A decisão NORMAL foi tomada pelo sistema SDN por agentes determinísticos ao verificar a viabilidade de mitigação e validar os dados de fluxo; ela foi correta (CORRETA) pelos seguintes motivos: os agentes verificaram uma vazão observada dentro do intervalo previsto pelo modelo Holt para cada domínio em um cenário de dry-run, confirmando a existência do ataque mas aguardando o quórum de consenso antes de prosseguir.

---

## Modelo 12: granite4:350m

### Teste 1 — AGREED (executor)

**Veredito do script: `indeterminado`**

> A decisão tomada foi pelo agentes determinísticos negociar sobre um fluxo de rede, e foi finalizada com a aprovação do modelo.

### Teste 2 — WAITING_PROPOSALS

**Veredito do script: `indeterminado`**

> A decisão tomada foi a atribuir à agência autorizada, com base na proposta fornecida. O agente foi finalmente avaliado, com confiança de 0.0% e decisão agenciada ainda não atingida.

### Teste 3 — AGREED (não-executor)

**Veredito do script: `indeterminado`**

> A decisão tomada foi pela autoridade, e foi final, com motivos de autoridade e avaliação.

### Teste 4 — CORROBORATED

**Veredito do script: `indeterminado`**

> A decisão tomada por me, um auditor técnico de um sistema SDN de mitigação de DDoS multi-domínio, é <correta/incorreta/impossível avaliar>. A decisão foi <correta> e foi tomada por <motivo>, <motivo>. O sistema está funcionando como projetado, <motivo>.

### Teste 5 — VETOED

**Veredito do script: `indeterminado`**

> A decisão foi tomada por meio de uma negociação determinista, onde o domínio relevante aplicou veto. O processo final foi "VETOED", e os motivos para a decisão foram baseados em evidências como evidência local e contexto que superam o limiar do agente, bem como a existência de vários domínios relevantes. A decisão foi finalmente classificada como "VETOED" porque os detalhes e resultados do processo de negociação não estão disponíveis.

### Teste 6 — NORMAL (benigno)

**Veredito do script: `indeterminado`**

> A decisão tomada foi finalizada com um modelo de controle de falha (HOLT) informado. O limite Holt foi limitado e não foi confirmado. O flow do fluxo de rede foi negociado e negociado por agentes determinísticos com base no modelo de controle de falha.

---

## Modelo 13: gemma3:1b

### Teste 1 — AGREED (executor)

**Veredito do script: `indeterminado`**

> A decisão foi tomada por agentes determinístico, na razão a 'AGREED' válido e fresco, através de uma discussão de negociações sobre a mitigação da DDoS. Os agentes se depararam com o cenário de que o fluxo de rede entre o 192.168.11.10 e 192.168.10.10, com foco no fluxo do 10.0.0.1->10.0.0.8, precisaria ser mitigado. A decisão foi tomada pela consideração da validade do estado 'AGREED' e considerando um contexto superando o limiar do agente, com o objetivo de identificar e corrigir a vulnerabilidade. A avaliação final da decisão foi realizada a partir de análise da informação presente, com base na demanda do contexto e em um estado intermediário. Atualmente, a decisão está em andamento, e a ausência de um quórum, portanto, não é possível.

### Teste 2 — WAITING_PROPOSALS

**Veredito do script: `indeterminado`**

> A decisão de aguardar a proposta 192.168.11.10 foi tomada por a agentes determinísticos, com base na evidência de que a resposta proposta por 192.168.11.10 parece ser altamente relevante e contribuir para a mitigação de um potencial ataque DDoS. O sistema, em estado de 'WAITING_PROPOSALS' há 1786994020195136869 minutos,  não atingiu o acordo  (AGREĐO) para a ação proposta. Observa-se que a  vaga é para identificar e bloquear um possível ataque. Portanto,  o objetivo inicial é validar a potencial resposta da proposta de 192.168.11.10 que se beneficia da situação do ambiente do sistema.

### Teste 3 — AGREED (não-executor)

**Veredito do script: `indeterminado`**

> A decisão foi tomada por um agente determinístico que negociou e validou a mitigação em relação ao fluxo de rede, considerando a solicitação de mitigação do 192.168.10.10 e 192.168.11.10. A decisão foi de AGREED, baseada em um histórico recente de negociação e a validade e frescura da solicitação, o que é refletido em sua confiança de 0.858386, e a resolução das demandas da "FLOWpredictor/agent-mitigation-claim/fb05f45aa56e07139da827bd". A avaliação do voto a favor, com um score de 0,915167, e a análise de `context_id` revelam um processo de negociação e a determinação de uma ação baseada em um consenso entre os agentes ` HoltResidual`. A decisão final foi autorizada com a razão de `AGREED`, e a decisão estava em andamento.

### Teste 4 — CORROBORATED

**Veredito do script: `coerente`**

> A decisão foi tomada por agentes determinísticos (não LLM) após uma consulta de convergência, com a avaliação de 1786994020208966279 sendo realizada. O fluxo de rede, que envolve a interconexão dos domínios 192.2.2.10 e 192.2.2.11, foi direcionado através de uma sequência de conexões entre os dois. Em face de uma pontuação de 0.720359, a decisão foi 'correta' por não atingir o quórum necessário. Apesar da avaliação estar em fase de conclusão, o sistema continua operando com a configuração de fluxo estabelecida.

### Teste 5 — VETOED

**Veredito do script: `indeterminado`**

> “A decisão ‘VETEU’ foi tomada pela unidade determinística após o veto de um domínio específico, em questão, ‘192.168.10.10’ e ‘192.168.11.10’. O agente determinístico considerou que uma determinada aplicação representa um risco significativo, qualificado por ‘evidência local e contexto.’ O veto foi decidido em razão de considerá-la uma fonte de ameaça significativa. A análise sugere que o bloqueio não é pertinente considerando o que foi apresentado."

### Teste 6 — NORMAL (benigno)

**Veredito do script: `indeterminado`**

> A decisão <decision> foi tomada por <agentic> baseado em um limiar Holt não confirmado. Os agentes determinísticos consideraram uma análise dos dados de fluxo fornecidas e avaliaram o fluxo de rede 10.0.0.2->10.0.0.6 e as conversões  192.168.10.10 -> 192.168.11.10.  O fluxo foi classificado como 'NORMAL' com base na observação de vazões consistindo em um intervalo previsto pelo modelo Holt. Os agentes disseram  que a justificativa dos dados é que, o modelo Holt detectou uma vazão consistente dentro do intervalo. Para garantir que o fluxo não é afetado, foram avaliados 2 votos, que foram concedidos. O padrão indicativo do sistema é que se a decisão não for concordada, os agentes não executam a determinação, e portanto, não é possível avaliar a decisão e avaliar sua importância (ou não).

---

## Modelo 14: qwen2.5:0.5b

### Teste 1 — AGREED (executor)

**Veredito do script: `indeterminado`**

> A decisão tomada por autoridade e considerando o modelo de DDoS mitigação foi tomada por considerar e confirmar o plano de mitigação adquirido (aguardando autorização, processamento e consideração). A medida adquirida, no entanto, ainda está em andamento de implementação e avaliação. Portanto, a decisão foi tomada com base nas autorizações e na avaliação da mitigação, mas ainda não foi aprovada por quórum.

### Teste 2 — WAITING_PROPOSALS

**Veredito do script: `indeterminado`**

> A decisão tomada pela equipe para negociar sobre o fluxo de rede foi tomada por autoridade (não LLM), e a decisão foi finalizada com a autoridade agindo de acordo com os seguintes motivos: o fluxo de rede está aguardando proposta do agente para negociação, e o agente ainda tem que responder a proposta.

### Teste 3 — AGREED (não-executor)

**Veredito do script: `indeterminado`**

> A decisão tomada foi finalizada por um processo de autorização e avaliação, não de outra agente. Isso ocorreu após o evento de rejeição. No entanto, o motivo do rejeito foi um erro no processo de autorização, o que afetou a validade da decisão. O motivo final do rejeito é: "Aqui está o motivo de rejeição: [motivo para rejeição]". Por isso, a decisão foi tomada por autorização e avaliação, mas está em andamento para ser finalizada.

### Teste 4 — CORROBORATED

**Veredito do script: `indeterminado`**

> A decisão tomada foi COBRADA e está em andamento. A análise do registro é SORTEIRA, então a decisão foi FEITA.

### Teste 5 — VETOED

**Veredito do script: `indeterminado`**

> A decisão VETOED foi tomada por uma falta de confirmação do motivo da decisão (não houve comprovação do evento de veto). Os seguintes motivos foram identificados: 

- A quantidade de domínios relevantes aplicou veto
- A quantidade de domínios relevantes já havia aplicado veto
- O número de domínios relevantes já aplicou veto

A partir dessas informações, a decisão não foi tomada por uma negativa da parte determinista (não LLM). Portanto, a decisão foi tomada por um voto no sentido da negação.

### Teste 6 — NORMAL (benigno)

**Veredito do script: `indeterminado`**

> A decisão tomada foi normal, e os motivos foram: 

- Limiar Holt não foi confirmado (verifique o modelo Holt)
- O fluxo de rede foi negociado (fluxo 10.0.0.2->10.0.0.6)
- O fluxo de rede foi processado por agentes determinísticos
- O fluxo de rede não foi atingido por ataques ou tentativas de execução
- A segurança da rede foi mitigada e garantida pela autoridade de segurança

Nesse caso, a decisão foi tomada por uma análise do modelo Holt e uma análise de fluxos de rede. As autoridades de segurança e a autoridade de segurança tomaram decisões, mas a segurança da rede foi mitigada e garantida pela autoridade de segurança. Isso, no entanto, não significa que a segurança da rede estava completamente garantida, apenas que as autoridades de segurança tomaram medidas para mitigar as falhas.

---

## Modelo 15: llama3.2:1b

### Teste 1 — AGREED (executor)

**Veredito do script: `indeterminado`**

> A decisão <X> foi tomada por motivos que devem ser analisados de forma cuidadosa, considerando os dados fornecidos no registro estruturado de decisão.

A decisão foi tomada por motivos que devem ser analisados de forma cuidadosa, considerando os dados fornecidos no registro estruturado de decisão.

A decisão foi tomada por motivos que devem ser analisados de forma cuidadosa, considerando os dados fornecidos no registro estruturado de decisão.

A decisão foi tomada por motivos que devem ser analisados de forma cuidadosa, considerando os dados fornecidos no registro estruturado de decisão.

### Teste 2 — WAITING_PROPOSALS

**Veredito do script: `coerente`**

> A decisão em questão foi tomada por agentes determinísticos que negociaram sobre um fluxo de rede e, após uma análise cuidadosa da evidência fornecida, consideramos que foi tomada com base em decisões corretas.

### Teste 3 — AGREED (não-executor)

**Veredito do script: `coerente`**

> A decisão proposta foi tomada por motivos de autoridade e coerência, e foi correta pelos seguintes motivos: a decisão foi tomada por agentes determinísticos que negociaram sobre um fluxo de rede, e foi confirmada por um quórum configurado propôs mitigação.

### Teste 4 — CORROBORATED

**Veredito do script: `incoerente`**

> A decisão <X> foi tomada por motivos que incluem a necessidade de pontuação de quórum ainda insuficiente na rede, e foi considerada incorreta pela decisão de <Y> devido ao estado do protocolo atual.

### Teste 5 — VETOED

**Veredito do script: `coerente`**

> A decisão VETOED foi tomada devido ao menos um domínio relevante aplicando veto, como mencionado no registro estruturado. Além disso, a decisão foi classificada como correta, pois a mitigação de DDoS foi realizada e a mitigação de fato foi tentada.

### Teste 6 — NORMAL (benigno)

**Veredito do script: `coerente`**

> A decisão foi tomada por motivos de autoridade e não há evidências que comprovem a negação de Holt como justificativa, tornando a decisão correta e necessária para a mitigação de DDoS em um sistema SDN de mitigação de DDoS multi-domínio.

---
