#!/usr/bin/env bash
set -uo pipefail
#
# validate_l3_pipeline.sh
# Valida a cadeia completa após a modificação L3 do SimpleSwitch:
#
#   PacketIn IPv4 → regra L3 (nw_src/nw_dst) → /stats/flow expõe L3
#   → FlowPredictor cria séries flow:* → SPIKE → mitigação attempted=true
#   → FlowBlocker instala DROP (prio 32768 > forwarding 3000) → ping falha
#
# Pré-condição: ambiente rodando (setup_env.sh) + Mininet ativo com tráfego.
# Uso:  ./validate_l3_pipeline.sh [dominio]     (default: 0)

I=${1:-0}
API_PORT=$((8080 + I))
PRED_PORT=$((6060 + I))
FB_PORT=$((7070 + I))

PASS=0; FAIL=0
ok()   { echo "  ✅ $*"; PASS=$((PASS+1)); }
fail() { echo "  ❌ $*"; FAIL=$((FAIL+1)); }
step() { echo ""; echo "── PASSO $* ──────────────────────────────────────"; }

step "0: Rebuild + restart (executar manualmente se ainda não fez)"
cat <<'CMDS'
  docker build -t simpleswitch_cnsm .          # com o novo Simpleswitch_cnsm.py
  docker rm -f simple-switch-0 simple-switch-1
  # re-executar os docker run do setup_env.sh para simple-switch-*  (ou o setup inteiro)
  # gerar tráfego no Mininet:  mininet> h1 ping -c 10 10.0.0.4
CMDS

step "1: /stats/flow expõe nw_src/nw_dst?"
FLOWS=$(curl -fsS "http://127.0.0.1:${API_PORT}/stats/flow/1" 2>/dev/null)
if echo "$FLOWS" | grep -q '"nw_src"'; then
  ok "/stats/flow/1 contém nw_src (regras L3 sendo instaladas)"
  echo "$FLOWS" | jq -r '.["1"][] | select(.match.nw_src) | "     \(.match.nw_src) -> \(.match.nw_dst)  prio=\(.priority)  bytes=\(.byte_count)"' 2>/dev/null | head -5
else
  fail "/stats/flow/1 SEM nw_src — gere tráfego IPv4 (ping) e verifique docker logs simple-switch-${I}"
fi

step "2: Prioridade do forwarding < 32768 (DROP deve vencer)?"
BAD_PRIO=$(echo "$FLOWS" | jq '[.["1"][] | select(.match.nw_src and .actions != [] and .priority >= 32768)] | length' 2>/dev/null || echo 0)
if [ "${BAD_PRIO:-0}" = "0" ]; then
  ok "nenhuma regra de forwarding com prioridade >= 32768"
else
  fail "$BAD_PRIO regra(s) de forwarding empatando com o DROP — verifique FORWARD_PRIORITY"
fi

step "3: FlowPredictor criou séries flow:* ?"
PREDS=$(curl -fsS "http://127.0.0.1:${PRED_PORT}/predictor/predictions?top=200" 2>/dev/null)
N_FLOW=$(echo "$PREDS" | jq '[.predictions[] | select(.key | startswith("flow:"))] | length' 2>/dev/null || echo 0)
if [ "${N_FLOW:-0}" -gt 0 ]; then
  ok "$N_FLOW série(s) flow:* ativas"
  echo "$PREDS" | jq -r '.predictions[] | select(.key | startswith("flow:")) | "     \(.key)  obs=\(.observed_bps)bps  pred=\(.predicted_next_bps)bps"' | head -5
else
  fail "nenhuma série flow:* — aguarde ~1 ciclo de coleta (POLL_INTERVAL_S) após o passo 1 passar"
fi

step "4: Provocar SPIKE (execute no Mininet, depois re-rode este script)"
cat <<'CMDS'
  mininet> h4 iperf3 -s -D
  mininet> h1 ping -c 30 10.0.0.4 -i 0.5          # baseline (>= WARMUP_SAMPLES)
  mininet> h1 iperf3 -c 10.0.0.4 -t 20            # SPIKE
CMDS

step "5: Anomalia de fluxo com mitigação attempted=true?"
ANOMS=$(curl -fsS "http://127.0.0.1:${PRED_PORT}/predictor/anomalies?limit=100" 2>/dev/null)
ATTEMPTED=$(echo "$ANOMS" | jq '[.anomalies[] | select(.meta.type=="flow" and .mitigation.attempted==true)] | length' 2>/dev/null || echo 0)
EXECUTED=$(echo "$ANOMS" | jq '[.anomalies[] | select(.mitigation.executed==true)] | length' 2>/dev/null || echo 0)
if [ "${ATTEMPTED:-0}" -gt 0 ]; then
  ok "mitigação tentada em $ATTEMPTED anomalia(s); executada em ${EXECUTED:-0}"
  echo "$ANOMS" | jq -r '.anomalies[] | select(.mitigation.attempted==true) | "     id=\(.anomaly_id) \(.key) z=\(.z_score) executed=\(.mitigation.executed) (\(.mitigation.reason))"' | head -3
  if [ "${EXECUTED:-0}" = "0" ]; then
    echo "     ⚠️  attempted mas não executed — se reason=DRY_RUN, desarme com:"
    echo "        curl -X POST http://127.0.0.1:${PRED_PORT}/predictor/config -H 'Content-Type: application/json' -d '{\"dry_run\": false}'"
  fi
else
  fail "nenhuma mitigação tentada ainda — provoque o SPIKE (passo 4)"
fi

step "6: DROP instalado no OVS?"
DROPS=$(sudo ovs-ofctl -O OpenFlow10 dump-flows s1 2>/dev/null | grep -c "actions=drop")
if [ "${DROPS:-0}" -gt 0 ]; then
  ok "$DROPS regra(s) DROP em s1"
  sudo ovs-ofctl -O OpenFlow10 dump-flows s1 | grep "actions=drop" | sed 's/^/     /'
else
  fail "nenhum DROP em s1 (esperado após mitigação executed=true)"
fi

step "7: Tráfego efetivamente bloqueado?"
echo "     mininet> h1 ping -c 3 10.0.0.4      # deve falhar (100% loss)"
echo "     mininet> h1 ping -c 3 10.0.0.2      # controle: deve continuar OK"

echo ""
echo "══════════════════════════════════════════════════"
echo "  RESULTADO: $PASS passos OK, $FAIL pendentes/falhos"
echo "══════════════════════════════════════════════════"
[ "$FAIL" -eq 0 ]
