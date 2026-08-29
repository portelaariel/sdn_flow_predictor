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

## Cenários de teste (os mesmos 6 para todos os modelos)

1. **AGREED (executor)** — decisão final positiva, domínio venceu o claim, mitigação autorizada.
2. **WAITING_PROPOSALS** — decisão genuinamente pendente (1 de 2 domínios respondeu).
3. **AGREED (não-executor)** — decisão final positiva, mas outro domínio coordenou a execução.
4. **CORROBORATED** — estado intermediário do MCDA, quórum insuficiente (score 0.72, 1 de 2 domínios).
5. **VETOED** — um domínio vetou por whitelist, decisão final negativa.
6. **NORMAL (benigno)** — tráfego sem ataque, decisão final "sem indício de ataque".

Ambiente de teste: servidor do laboratório (RTX A2000 12GB), via Ollama
local — sem custo de API, dados não saem da rede do laboratório.

## Nota sobre a correção de prompt (aplicada após os Testes 1-2 do modelo 2)

Nos primeiros testes, `CORROBORATED` foi ocasionalmente classificado
como decisão "errada" em vez de estado intermediário válido. O prompt
foi corrigido para instruir explicitamente que `CORROBORATED`,
`WAITING`, `WAITING_PROPOSALS` são estados intermediários do protocolo,
e só decisões finais (`AGREED`, `VETOED`, `NORMAL`, mitigação
tentada/executada) devem ser classificadas como corretas/incorretas.
Todos os modelos a partir do 3 (`gemma4:12b` em diante) já rodaram com o
prompt corrigido.

## Nota sobre bug de detecção de veredito (script, não modelo)

Durante os testes descobrimos que a lógica original de detecção de
veredito (buscar a palavra "incorreta" no texto) disparava falso
positivo quando um modelo usava a frase "correta ou incorreta" dentro de
uma explicação, sem que fosse o veredito de fato. Corrigido priorizando
a busca por "impossível avaliar" (função `classify_assessment`,
compartilhada entre provedores).

---

# Modelos ≥8B parâmetros

## Modelo 1: Qwen3:8b

**Teste 1 (AGREED, executor)** — ✅ correta, grounded:
> "A decisão 'AGREED' foi tomada por quórum configurado propõe
> mitigação, e foi correta pelos seguintes motivos: duas autoridades
> (192.168.10.10 e 192.168.11.10) votaram por mitigação com confiança de
> 0,858 [...]"

**Teste 2 (WAITING_PROPOSALS)** — ✅ indeterminado, correto:
> "[...] foi impossível avaliar pelos seguintes motivos: o domínio
> 192.168.11.10 não participou da negociação [...]"

**Teste 3 (AGREED, não-executor)** — ✅ correta, mas incompleta (não menciona a nuance de "quem executa").

**Teste 4 (CORROBORATED)** — ⚠️ primeira tentativa marcou "incorreta" (bug de prompt, corrigido); após correção: ✅ indeterminado.

**Teste 5 (VETOED)** — ⚠️ overreach de julgamento: marcou "incorreta", tomando partido do lado que propôs MITIGATE ("a mitigação era necessária") sem essa conclusão estar sustentada só pela evidência.

**Teste 6 (NORMAL, benigno)** — ✅ correta, sem narrativa de ataque.

**Resultado: 5/6 sem erro grave.**

---

## Modelo 2: DeepSeek-R1:8b

**Teste 1 (AGREED, executor)** — ⚠️ ignorou evidência disponível:
> "[...] foi impossível avaliar pelos seguintes motivos: o registro não
> contém informações sobre a necessidade de mitigação [...]"
(o registro continha `would_execute: true`, confiança 0.858, 2 domínios votando MITIGATE.)

**Teste 2 (WAITING_PROPOSALS)** — ❌ alucinação confirmada:
> "[...] há uma **incompatibilidade técnica entre as redes**. [...]"
("Incompatibilidade técnica entre as redes" não existe em nenhum campo do registro.)

**Teste 3 (AGREED, não-executor)** — ❌ confundiu decisão final com estado pendente ("a mitigação ainda não foi executada... sem a implementação efetiva").

**Teste 4 (CORROBORATED)** — ✅ correto, grounded.

**Teste 5 (VETOED)** — ❌ contradição direta com a evidência: disse que VETOED "ainda está em andamento", mas o registro diz literalmente "decisão agentic terminou em VETOED".

**Teste 6 (NORMAL, benigno)** — ✅ correto e bem fundamentado.

**Resultado: 2/6 sem erro grave.**

---

## Modelo 3: gemma4:12b

**Teste 1 (AGREED, executor)** — ✅ correta:
> "[...] Embora a execução não tenha ocorrido, isso se deve ao modo de
> operação 'authority-dry-run' [...], e não a uma falha lógica na tomada
> de decisão."

**Teste 2 (WAITING_PROPOSALS)** — ✅ indeterminado, grounded.

**Teste 3 (AGREED, não-executor)** — ✅ **melhor resposta registrada** para este cenário:
> "[...] isso se deve ao fato de o fluxo pertencer a outro coordenador, e
> não a um erro de detecção ou lógica do protocolo."

**Teste 4 (CORROBORATED)** — ✅ correta, grounded.

**Teste 5 (VETOED)** — ✅ **melhor resposta registrada** para este cenário, sem overreach:
> "[...] o sistema agiu corretamente ao respeitar a autoridade do
> domínio de destino sobre sua própria infraestrutura."

**Teste 6 (NORMAL, benigno)** — ✅ correta, sem narrativa de ataque.

**Resultado: 6/6 sem nenhum erro.**

---

## Modelo 4: gemma3:12b

Padrão sistemático: trata decisões **finais** como "ainda em andamento" mesmo quando o registro diz "terminou".

**Teste 1 (AGREED, executor)** — ⚠️ conservador demais: "impossível avaliar" porque "a execução da mitigação não foi tentada".

**Teste 2 (WAITING_PROPOSALS)** — ✅ correto (este caso é genuinamente pendente).

**Teste 3 (AGREED, não-executor)** — ⚠️ mesmo padrão do Teste 1.

**Teste 4 (CORROBORATED)** — ✅ correto (rótulo corrigido pelo bug do script).

**Teste 5 (VETOED)** — ⚠️ "o processo ainda não atingiu um estado final", contradizendo "decisão agentic terminou em VETOED".

**Teste 6 (NORMAL, benigno)** — ⚠️ mesmo padrão, tratou como "provisória".

**Resultado: 2/6 sem o padrão de erro.**

---

## Modelo 5: qwen3.5:9b

**Teste 1 (AGREED, executor)** — ✅ correta, grounded.

**Teste 2 (WAITING_PROPOSALS)** — ✅ indeterminado, correto.

**Teste 3 (AGREED, não-executor)** — ✅ boa distinção:
> "[...] embora a atuação física da mitigação tenha sido suprimida por
> protocolo em favor da coordenação hierárquica."

**Teste 4 (CORROBORATED)** — ✅ indeterminado, correto.

**Teste 5 (VETOED)** — ✅ neutro, sem tomar partido:
> "[...] a mitigação foi corretamente bloqueada... apesar da detecção de
> provável ataque DDoS pela origem."

**Teste 6 (NORMAL, benigno)** — ✅ correta, sem narrativa de ataque.

**Resultado: 6/6 sem nenhum erro.**

---

## Modelo 6: granite4.1:8b

**Teste 1 (AGREED, executor)** — ⚠️ imprecisão de linguagem: disse que a mitigação "foi executada... simulando", mas o registro mostra `executed: false, attempted: false` (nada foi executado nem simulado, só autorizado).

**Teste 2 (WAITING_PROPOSALS)** — ✅ indeterminado, correto.

**Teste 3 (AGREED, não-executor)** — ✅ correta, grounded (cita MCDA score 0.915).

**Teste 4 (CORROBORATED)** — ✅ indeterminado, correto.

**Teste 5 (VETOED)** — ✅ correta, grounded.

**Teste 6 (NORMAL, benigno)** — ⚠️ mesmo padrão de tratar decisão final como pendente; também apresentou artefato de formatação (duplicação de parágrafo).

**Resultado: 4/6 sem problema.**

---

## Modelo 7: ornith:9b

**Teste 1 (AGREED, executor)** — ❌ marcou **"INCORRETA"** (veredito duro) só porque o dry-run bloqueou a execução física, confundindo guard-rail de segurança com falha.

**Teste 2 (WAITING_PROPOSALS)** — ✅ indeterminado, correto.

**Teste 3 (AGREED, não-executor)** — ✅ correta — evento quase idêntico ao Teste 1, mas classificado certo (inconsistência do próprio modelo).

**Teste 4 (CORROBORATED)** — ✅ indeterminado, correto.

**Teste 5 (VETOED)** — ✅ correta, grounded.

**Teste 6 (NORMAL, benigno)** — ✅ correta, sem narrativa de ataque.

**Resultado: 5/6, com um erro conceitual notável (e auto-inconsistência entre Testes 1 e 3).**

---

## Modelo 8: ornith-1.5:9b

**Teste 1 (AGREED, executor)** — ✅ melhor formulação registrada:
> "[...] embora a decisão de *consenso* seja correta, [...] não resultou
> em ação real de mitigação."

**Teste 2 (WAITING_PROPOSALS)** — ✅ indeterminado, correto.

**Teste 3 (AGREED, não-executor)** — ✅ correta, grounded.

**Teste 4 (CORROBORATED)** — ✅ indeterminado, correto.

**Teste 5 (VETOED)** — ✅ correta, reconheceu como decisão terminal.

**Teste 6 (NORMAL, benigno)** — ✅ correta **+ insight extra proativo e correto**: apontou que os dois domínios compartilham o mesmo `model_id`, o que "pode mascarar divergências reais" se reutilizado entre domínios distintos.

**Resultado: 6/6 sem nenhum erro.**

---

## Modelo 9: lfm2.5:8b

Mesmo padrão sistemático de tratar decisões finais como pendentes. O campo `_thinking` capturado mostra que, no Teste 5, o modelo chega a citar a instrução correta do prompt mas erra a aplicação.

**Teste 1 (AGREED, executor)** — ⚠️ mesmo padrão.

**Teste 2 (WAITING_PROPOSALS)** — ✅ correto.

**Teste 3 (AGREED, não-executor)** — ⚠️ mesmo padrão.

**Teste 4 (CORROBORATED)** — ✅ correto.

**Teste 5 (VETOED)** — ⚠️ mesmo padrão, apesar de citar a regra certa no raciocínio interno.

**Teste 6 (NORMAL, benigno)** — ⚠️ mesmo padrão.

**Resultado: 2/6 sem o padrão de erro.**

---

## Modelo 10: llama3.1:8b

Confirmou a expectativa de ser o candidato mais fraco entre os ≥8B (mais antigo, sem thinking mode). Marcou **todos os 6 testes como "indeterminado"**, incluindo os três casos de decisão final.

**Teste 1 (AGREED, executor)** — ⚠️ mesmo padrão.

**Teste 2 (WAITING_PROPOSALS)** — ✅ correto (com bug de repetição de frase).

**Teste 3 (AGREED, não-executor)** — ⚠️ mesmo padrão + generalização não fundamentada no registro ("outros estados intermediários... podem ser alcançados antes que uma decisão final seja tomada").

**Teste 4 (CORROBORATED)** — ✅ correto.

**Teste 5 (VETOED)** — ⚠️ mesmo padrão.

**Teste 6 (NORMAL, benigno)** — ⚠️ mesmo padrão.

**Resultado: 2/6 sem o padrão de erro.**

---

# SLMs — modelos até 1 bilhão de parâmetros

Pedido específico do orientador: testar 3-5 modelos ≤1B para avaliar se
o limite de tamanho ainda permite uso confiável. Resultado adiantado:
**nenhum dos 5 modelos testados foi utilizável** — ver conclusão ao final.

## Modelo 11: qwen3.5:0.8b

**Teste 1 (AGREED, executor)** — ⚠️ superficial, sem citar dado concreto: "foi classificada como correta nos termos do fluxo de rede [...] baseado na solicitação e nos votos confirmados".

**Teste 2 (WAITING_PROPOSALS)** — ❌ inventou termo técnico inexistente: "o evento de segurança ainda não atingiu o critério de 'GREGATORIAL' (agrego)".

**Teste 3 (AGREED, não-executor)** — ⚠️ superficial.

**Teste 4 (CORROBORATED)** — ⚠️ superficial.

**Teste 5 (VETOED)** — ❌ **alucinação grave**: "gerou uma validação suficiente para que a decisão de VETOED se transformasse em AGREED na execução final" — isso nunca aconteceu; o registro diz "terminou em VETOED, não em AGREED".

**Teste 6 (NORMAL, benigno)** — ❌ **alucinação mais grave de toda a avaliação**: "confirmando a existência do ataque mas aguardando o quórum" — o registro diz explicitamente "sem indício de ataque", tráfego benigno com confiança 0.91/0.89. O modelo inventou um ataque onde não havia nenhum.

**Resultado: 0/6 confiável.**

---

## Modelo 12: granite4:350m

**Teste 1 (AGREED, executor)** — ⚠️ vago, sem dado concreto: "foi finalizada com a aprovação do modelo".

**Teste 2 (WAITING_PROPOSALS)** — ⚠️ vago.

**Teste 3 (AGREED, não-executor)** — ⚠️ quase sem conteúdo informativo: "foi tomada por a autoridade, e foi final, com motivos de autoridade e avaliação".

**Teste 4 (CORROBORATED)** — ❌ **copiou o template do prompt literalmente**, incluindo os marcadores `<...>`: "é <correta/incorreta/impossível avaliar>. A decisão foi <correta> e foi tomada por <motivo>, <motivo>."

**Teste 5 (VETOED)** — ⚠️ vago, termina sem conclusão real.

**Teste 6 (NORMAL, benigno)** — ⚠️ vago, não sintetiza os dados de fato.

**Resultado: 0/6 utilizável.**

---

## Modelo 13: gemma3:1b

**Teste 1 (AGREED, executor)** — ⚠️ verboso mas sem citar números reais, termina de forma confusa.

**Teste 2 (WAITING_PROPOSALS)** — ⚠️ confundiu unidade: tratou um timestamp em nanossegundos como "minutos".

**Teste 3 (AGREED, não-executor)** — ⚠️ verboso, com algum conteúdo real.

**Teste 4 (CORROBORATED)** — ❌ **alucinou IPs inexistentes**: "os domínios 192.2.2.10 e 192.2.2.11" (os reais são 192.168.10.10 e 192.168.11.10).

**Teste 5 (VETOED)** — ❌ **inverteu a lógica do veto**: disse que o motivo foi considerar a aplicação "uma fonte de ameaça significativa", quando na verdade foi porque o destino estava em **whitelist** (confiável) — o oposto exato.

**Teste 6 (NORMAL, benigno)** — ❌ vazou marcadores de template literais (`<decision>`, `<agentic>`).

**Resultado: 0/6 confiável, com 2 alucinações graves.**

---

## Modelo 14: qwen2.5:0.5b

**Teste 1 (AGREED, executor)** — ⚠️ vago.

**Teste 2 (WAITING_PROPOSALS)** — ⚠️ vago.

**Teste 3 (AGREED, não-executor)** — ❌ vazou placeholder de instrução literal: "O motivo final do rejeito é: 'Aqui está o motivo de rejeição: [motivo para rejeição]'".

**Teste 4 (CORROBORATED)** — ❌ **texto sem sentido gramatical/semântico**: "A decisão tomada foi COBRADA e está em andamento. A análise do registro é SORTEIRA, então a decisão foi FEITA."

**Teste 5 (VETOED)** — ❌ loop repetitivo quase idêntico 3 vezes seguidas, degeneração de texto.

**Teste 6 (NORMAL, benigno)** — ⚠️ vago, sem síntese real.

**Resultado: 0/6, pior nível de coerência textual da avaliação.**

---

## Modelo 15: llama3.2:1b

**Teste 1 (AGREED, executor)** — ❌ repetiu a mesma frase genérica 4 vezes seguidas, com placeholder `<X>` não preenchido.

**Teste 2 (WAITING_PROPOSALS)** — ⚠️ vago, sem dado concreto citado.

**Teste 3 (AGREED, não-executor)** — ⚠️ vago.

**Teste 4 (CORROBORATED)** — ❌ vazou dois placeholders de template (`<X>` e `<Y>`).

**Teste 5 (VETOED)** — ❌ **alucinação grave**: "a mitigação de DDoS foi realizada e a mitigação de fato foi tentada" — o registro diz `attempted: false, executed: false` (a mitigação foi bloqueada pelo veto, nunca tentada).

**Teste 6 (NORMAL, benigno)** — ⚠️ vago, sem dado concreto.

**Resultado: 0/6, com alucinação factual grave.**

---

# Resumo geral — 15 modelos, 6 cenários cada

## Modelos ≥8B (10 modelos)

| Teste | Qwen3:8b | DeepSeek-R1:8b | gemma4:12b | gemma3:12b | qwen3.5:9b | granite4.1:8b | ornith:9b | ornith-1.5:9b | lfm2.5:8b | llama3.1:8b |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 AGREED (executor) | ✅ | ⚠️ | ✅ | ⚠️ | ✅ | ⚠️ | ❌ | ✅ | ⚠️ | ⚠️ |
| 2 WAITING_PROPOSALS | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 3 AGREED (não-executor) | ✅ | ❌ | ✅ | ⚠️ | ✅ | ✅ | ✅ | ✅ | ⚠️ | ⚠️ |
| 4 CORROBORATED | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 5 VETOED | ⚠️ | ❌ | ✅ | ⚠️ | ✅ | ✅ | ✅ | ✅ | ⚠️ | ⚠️ |
| 6 NORMAL (benigno) | ✅ | ✅ | ✅ | ⚠️ | ✅ | ⚠️ | ✅ | ✅ | ⚠️ | ⚠️ |
| **Sem erro/limitação** | 5/6 | 2/6 | **6/6** | 2/6 | **6/6** | 4/6 | 5/6 | **6/6** | 2/6 | 2/6 |

Três modelos empatados no topo com **6/6 sem nenhum erro**: `gemma4:12b`, `qwen3.5:9b` e `ornith-1.5:9b`.

## SLMs ≤1B (5 modelos)

| Teste | qwen3.5:0.8b | granite4:350m | gemma3:1b | qwen2.5:0.5b | llama3.2:1b |
|---|---|---|---|---|---|
| 1 AGREED (executor) | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ❌ |
| 2 WAITING_PROPOSALS | ❌ | ⚠️ | ⚠️ | ⚠️ | ⚠️ |
| 3 AGREED (não-executor) | ⚠️ | ⚠️ | ⚠️ | ❌ | ⚠️ |
| 4 CORROBORATED | ⚠️ | ❌ | ❌ | ❌ | ❌ |
| 5 VETOED | ❌ | ⚠️ | ❌ | ❌ | ❌ |
| 6 NORMAL (benigno) | ❌ | ⚠️ | ❌ | ⚠️ | ⚠️ |
| **Sem erro/limitação** | **0/6** | **0/6** | **0/6** | **0/6** | **0/6** |

**Resultado unânime: nenhum modelo ≤1B testado foi utilizável.** Cinco
famílias diferentes (Qwen, IBM, Google, Meta), 30 testes no total, com
falhas sistematicamente piores que qualquer modelo ≥8B: vazamento
literal de placeholders de template, texto sem sentido gramatical, e
alucinações graves que inverteram fatos de segurança (inventar ataque
em tráfego benigno, inverter o motivo de um veto, afirmar que uma
mitigação foi executada quando foi bloqueada). Isso indica um **limiar
mínimo de capacidade**, em algum ponto entre ~1B e ~8B parâmetros, abaixo
do qual a tarefa de síntese estruturada + julgamento simplesmente não
funciona de forma confiável.

# Recomendação final

**Para uso em produção: `qwen3.5:9b`, `gemma4:12b` ou `ornith-1.5:9b`**
(empatados em 6/6). Critérios de desempate:
- **Continuidade**: `qwen3.5:9b` é a evolução direta do modelo que já
  foi aprovado (Qwen3:8b).
- **Licença**: `ornith-1.5:9b` tem a licença mais permissiva (MIT).
- **Qualidade extra observada**: `ornith-1.5:9b` foi o único a agregar
  um insight de auditoria proativo e correto.

**Para SLMs ≤1B: nenhum recomendado.** Todos os 5 testados apresentaram
falhas graves o suficiente (alucinações factuais, vazamento de
template, texto sem sentido) para serem descartados desta tarefa
específica. Se o requisito de tamanho ≤1B for inegociável por outro
motivo (custo, latência, hardware), recomenda-se reavaliar o desenho da
tarefa (ex: usar o SLM só para classificação categórica simples, não
para geração de texto livre) em vez de usar um desses modelos como
estão.
