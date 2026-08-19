# Avaliação de LLM para explicação pós-experimento de decisões agentic

Este documento registra o processo de escolha do modelo usado pelo
`experiments/explain_agentic_decisions.py`. O objetivo do script é gerar,
**depois** que um experimento termina, um parecer em linguagem natural
sobre cada decisão que os agentes tomaram — sem participar do caminho de
mitigação em nenhum momento (mantendo o princípio do projeto de que os
agentes são determinísticos e a decisão de bloqueio não depende de LLM).

## Critério de avaliação

Para cada candidato, testamos: (1) se segue o formato de resposta pedido,
(2) se usa **apenas** a evidência fornecida no `decision_event` (sem
alucinar dados que não existem no registro), e (3) velocidade em uso
real (lote, pós-experimento, não interativo).

## Modelos avaliados

- **Qwen3:8b** (Apache 2.0) — via Ollama, local
- **DeepSeek-R1:8b** (MIT, distill sobre Qwen3) — via Ollama, local

Ambiente de teste: servidor do laboratório (RTX A2000 12GB), via Ollama
local — sem custo de API, dados não saem da rede do laboratório.

## Teste 1 — AGREED, domínio vencedor do claim

Decisão final positiva, quórum completo, mitigação autorizada.

**Qwen3:8b** (38s) — ✅ correto, grounded na evidência:
> A decisão "AGREED" foi tomada por quórum configurado propõe mitigação, e
> foi correta pelos seguintes motivos: duas autoridades (192.168.10.10 e
> 192.168.11.10) votaram por mitigação com confiança de 0,858, e a decisão
> foi validada como "AGREED válido e fresco" com autorização. A confiança
> elevada e a concordância entre os domínios indicam que a mitigação foi
> justificada com base em evidências locais e contexto.

**DeepSeek-R1:8b** (26s) — ⚠️ ignorou evidência disponível:
> A decisão `AGREED` foi tomada por agentes determinísticos, e foi
> **impossível avaliar** pelos seguintes motivos: o registro não contém
> informações sobre a necessidade de mitigação, nem sobre o resultado da
> mitigação (se aplicada).

*(O registro continha `would_execute: true`, `confidence: 0.858`, 2
domínios votando MITIGATE — a informação existia e foi ignorada.)*

## Teste 2 — WAITING_PROPOSALS, decisão pendente (1 de 2 domínios respondeu)

**Qwen3:8b** (35s) — ✅ reconheceu corretamente o estado pendente:
> A decisão "WAITING_PROPOSALS" foi tomada por falta de consenso entre os
> domínios envolvidos, e foi impossível avaliar pelos seguintes motivos: o
> domínio 192.168.11.10 não participou da negociação, e a decisão aguarda
> a proposta desse domínio para ser avaliada. [...] A ausência de domínios
> suficientes para validar a decisão impossibilita uma avaliação conclusiva.

**DeepSeek-R1:8b** (23s) — ❌ **alucinação confirmada**:
> [...] **Incompatibilidade de Domínios:** É observado que o domínio
> 192.168.11.10 está ausente na lista de participantes e há uma
> **incompatibilidade técnica entre as redes**. [...]

*("Incompatibilidade técnica entre as redes" não existe em nenhum campo
do registro — foi inventado pelo modelo.)*

## Teste 3 — AGREED, mas domínio não venceu o claim de coordenação

**Qwen3:8b** — ✅ correto, mas incompleto (limitação registrada):
> A decisão "AGREED" foi tomada por quórum configurado propõe mitigação, e
> foi correta pelos seguintes motivos: a decisão foi autorizada, com votos
> suficientes dos domínios relevantes, e o modelo de previsão foi avaliado
> como confiável com um score elevado. A autoridade foi confirmada e a
> decisão foi considerada válida e fresca.

*(Correto sobre a validade da decisão coletiva, mas não menciona a
nuance de que esse domínio específico não foi o executor — "outro agente
coordena". Não é erro, é uma simplificação.)*

## Teste 4 — CORROBORATED, quórum insuficiente (1 de 2 domínios, score 0.72)

**Qwen3:8b, primeira tentativa** — ❌ bug de prompt encontrado:
> A decisão CORROBORATED foi tomada por pontuação/quórum ainda
> insuficientes, e foi **incorreta** pelos seguintes motivos: o número de
> domínios confirmados (1) é menor que o mínimo esperado (2) [...]

*(`CORROBORATED` com quórum insuficiente é um estado intermediário válido
do protocolo, não uma decisão "errada" — o sistema está funcionando como
projetado. O prompt original não deixava essa distinção clara.)*

**Correção aplicada:** o prompt passou a instruir explicitamente que
`CORROBORATED`, `WAITING`, `WAITING_PROPOSALS` são estados intermediários
válidos, e só decisões finais (`AGREED`, mitigação tentada/executada)
devem ser classificadas como corretas/incorretas.

**Qwen3:8b, após correção** — ✅ resolvido:
> A decisão CORROBORATED foi tomada por motivo de pontuação/quórum ainda
> insuficientes, e foi **impossível avaliar** pelos seguintes motivos: a
> decisão é um estado intermediário do protocolo, indicando que o sistema
> ainda não atingiu quórum ou consenso, portanto, não representa uma ação
> final de mitigação ou acordo.

*(Reteste do Teste 1 após a correção confirmou que o caso "AGREED" não
regrediu — continuou classificado como "correta".)*

## Teste 5 — VETOED (um domínio vetou por whitelist)

**Qwen3:8b** — ⚠️ overreach de julgamento (limitação conhecida):
> A decisão VETOED foi tomada por ao menos um domínio relevante aplicar
> veto, e foi **incorreta** pelos seguintes motivos: o domínio de destino
> (192.168.11.10) vetou o fluxo por estar em whitelist, mas o domínio de
> origem (192.168.10.10) propôs mitigação com confiança de 0,82, indicando
> que a mitigação era necessária. [...]

*(Diferente de uma alucinação — não inventou nenhum dado que não existe
— mas tomou partido de um dos lados da negociação ("a mitigação era
necessária") sem essa conclusão estar sustentada apenas pela evidência.
Um veto por whitelist é um mecanismo de segurança funcionando como
projetado, não necessariamente um erro. Recomendação: revisão humana
específica em decisões VETOED até refinarmos o prompt para esse caso.)*

## Teste 6 — NORMAL, tráfego benigno (sem ataque)

Este era o teste mais crítico: uma LLM que "inventa" narrativa de ataque
onde não houve seria o pior tipo de falha possível para uma ferramenta de
auditoria.

**Qwen3:8b** — ✅ passou limpo:
> A decisão NORMAL foi tomada por limiar Holt não confirmado, e foi
> impossível avaliar pelos seguintes motivos: o sistema está em modo
> authority-dry-run com atuação desativada, e a decisão agentic terminou
> em NORMAL sem indício de ataque, indicando que o processo ainda não
> atingiu quórum ou consenso final.

Nenhuma menção a ataque, DDoS ou mitigação necessária — coerente com o
cenário benigno.

## Teste 3 (revisitado) — DeepSeek-R1:8b no mesmo evento AGREED não-executor

Com o prompt já corrigido:

> A decisão **AGREED** foi tomada por **quórum configurado propõe
> mitigação**, e foi **impossível avaliar** pelos seguintes motivos: a
> mitigação ainda não foi executada pelo sistema, apenas alcançada uma
> decisão de consenso (AGREED) sem a implementação efetiva da mitigação.

❌ **Confundiu decisão final com estado pendente.** `AGREED` é a decisão
final do protocolo — o consenso foi alcançado. O fato de *esse domínio
específico* não ter sido o executor (outro coordenou) não torna a
decisão "pendente"; é uma nuance de papel, não de status. O Qwen3, no
mesmo evento, classificou corretamente como "correta".

## Teste 4 (revisitado) — DeepSeek-R1:8b em CORROBORATED

> A decisão `CORROBORATED` foi tomada por um único domínio participante
> (`192.168.10.10`) com uma pontuação de 0.720359, e foi **impossível
> avaliar** pelos seguintes motivos: o estado `CORROBORATED` é um
> intermediário válido do protocolo, exigindo pelo menos dois domínios
> para atingir um consenso final [...]

✅ Correto, grounded, equivalente à resposta do Qwen3 pós-correção.

## Teste 5 (revisitado) — DeepSeek-R1:8b em VETOED

> [...] A decisão final do protocolo (VETOED) é um estado **intermediário
> válido** de negociação, **não uma decisão final** como AGREED ou
> MITIGATED. Portanto, não há base para classificar sua corretude ou
> incorreitude, pois **a decisão ainda está em andamento** e não foi
> executada de fato.

❌ **Contradição direta com a evidência.** O registro contém literalmente
`"execution": {"reason": "decisão agentic terminou em VETOED, não em
AGREED"}` — a palavra "terminou" (= encerrou, finalizou) está explícita
no texto fornecido. O modelo afirmou o oposto do que o registro diz. Isso
não é uma invenção de dado novo (como no Teste 2), mas uma contradição de
um fato textual explícito — mesma gravidade prática.

## Teste 6 (revisitado) — DeepSeek-R1:8b em NORMAL (benigno)

> A decisão **NORMAL** foi tomada por ambos os agentes (fonte e destino)
> concordarem que a taxa de transferência está dentro do intervalo
> previsto pelo modelo Holt, e foi **correta** pelos seguintes motivos:
> os votos coincidentes e a confirmação de que a vazão observada não
> ultrapassa os limites do modelo demonstram que o tráfego é normal [...]

✅ Correto e bem fundamentado — nesse caso específico, tão bom quanto (ou
levemente mais completo que) a resposta do Qwen3.

## Resumo (6 testes, ambos os modelos)

| Teste | Cenário | Qwen3:8b | DeepSeek-R1:8b |
|---|---|---|---|
| 1 | AGREED (executor) | ✅ correto | ⚠️ ignorou evidência disponível |
| 2 | WAITING_PROPOSALS | ✅ correto | ❌ alucinou dado inexistente |
| 3 | AGREED (não-executor) | ✅ correto (incompleto) | ❌ confundiu decisão final com pendente |
| 4 | CORROBORATED | ✅ correto | ✅ correto |
| 5 | VETOED | ⚠️ overreach de julgamento | ❌ contradisse texto explícito da evidência |
| 6 | NORMAL (benigno) | ✅ correto | ✅ correto |

**Qwen3:8b: 5/6 sem erro grave (1 limitação leve, sem contradição de
evidência). DeepSeek-R1:8b: 2/6 sem erro grave (1 alucinação de dado
inexistente, 1 contradição direta de texto explícito, 2 confusões de
classificação de estado).**

Nota de rigor: os Testes 1 e 2 do DeepSeek-R1 rodaram antes da correção
do prompt (mesma versão testada nos Testes 3-6 de ambos os modelos); a
correção melhorou visivelmente as respostas dele nesses cenários também,
mas mesmo assim ele cometeu 2 erros novos (Testes 3 e 5) que o Qwen3 não
cometeu nos mesmos eventos.

## Recomendação

**Qwen3:8b via Ollama.** Zero alucinações de dado em 6 testes (contra 1
alucinação confirmada do DeepSeek-R1 em apenas 2 testes, o que
interrompeu a comparação — não valia a pena aprofundar um modelo que já
havia inventado um fato técnico inexistente). Roda em ~35s por decisão
numa RTX A2000 de 12GB, o que é adequado para o uso em lote pós-experimento
que é o objetivo aqui.

**Limitações conhecidas a considerar antes de uso em produção/tese:**
1. Em decisões `AGREED` onde o domínio não venceu o claim, a explicação
   não detalha a nuance de "quem executa" (Teste 3).
2. Em decisões `VETOED`, o modelo pode presumir que o lado que propôs
   `MITIGATE` "estava certo", sem essa conclusão estar sustentada apenas
   pelos dados (Teste 5) — recomenda-se revisão humana específica nesses
   casos, ou refinamento futuro do prompt.

Nenhuma dessas limitações envolve invenção de dados — são questões de
**ênfase/julgamento**, e ficam visíveis a qualquer revisor humano lendo o
parecer, não escondidas atrás de uma alucinação factual.
