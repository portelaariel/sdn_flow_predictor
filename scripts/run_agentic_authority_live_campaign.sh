#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Uso:
  bash scripts/run_agentic_authority_live_campaign.sh \
    --allow-agentic-mitigation

Executa uma campanha authority-live isolada. Cada par roda primeiro um controle
benigno; o DDoS do par só é liberado se esse controle passar. O padrão usa:
  h1->h8 a 1M/50M
  h2->h7 a 2M/100M
  h3->h6 a 5M/150M

Pré-requisitos obrigatórios:
  - campanha authority-dry-run com promotion_ready=true;
  - canário authority-live com canary_ready=true;
  - mesmo artefato de modelo promovido;
  - consentimento explícito acima, pois os casos DDoS instalam DROP real.

Variáveis úteis:
  AGENTIC_LIVE_CAMPAIGN_REPETITIONS=3
  AGENTIC_LIVE_CAMPAIGN_MIN_RUNS_PER_SCENARIO=3
  AGENTIC_LIVE_CAMPAIGN_MIN_DISTINCT_FLOWS=3
  AGENTIC_LIVE_CAMPAIGN_SOURCE_HOSTS=h1,h2,h3
  AGENTIC_LIVE_CAMPAIGN_DESTINATION_HOSTS=h8,h7,h6
  AGENTIC_LIVE_CAMPAIGN_BASELINE_RATES=1M,2M,5M
  AGENTIC_LIVE_CAMPAIGN_ATTACK_RATES=50M,100M,150M
  AGENTIC_LIVE_CAMPAIGN_BUILD_IMAGE=true|false
  AGENTIC_LIVE_CAMPAIGN_MCDA_CONVERGENCE_WINDOW_MS=1000
  AGENTIC_LIVE_CAMPAIGN_MCDA_EPISODE_LOOKBACK_MS=2000
  AGENTIC_LIVE_CAMPAIGN_MCDA_MAX_PRECEDING_WINDOWS=1
  AGENTIC_LIVE_CAMPAIGN_RESULTS_ROOT=experiments/results
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ "${1:-}" != "--allow-agentic-mitigation" || $# -ne 1 ]]; then
  usage >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
RESULTS_ROOT="${AGENTIC_LIVE_CAMPAIGN_RESULTS_ROOT:-$PROJECT_ROOT/experiments/results}"
REPETITIONS="${AGENTIC_LIVE_CAMPAIGN_REPETITIONS:-3}"
MIN_RUNS="${AGENTIC_LIVE_CAMPAIGN_MIN_RUNS_PER_SCENARIO:-3}"
MIN_FLOWS="${AGENTIC_LIVE_CAMPAIGN_MIN_DISTINCT_FLOWS:-3}"
BUILD_IMAGE="${AGENTIC_LIVE_CAMPAIGN_BUILD_IMAGE:-true}"
BASELINE_DURATION_S="${AGENTIC_LIVE_CAMPAIGN_BASELINE_DURATION_S:-12}"
ATTACK_DURATION_S="${AGENTIC_LIVE_CAMPAIGN_ATTACK_DURATION_S:-20}"
MCDA_CONVERGENCE_WINDOW_MS="${AGENTIC_LIVE_CAMPAIGN_MCDA_CONVERGENCE_WINDOW_MS:-1000}"
MCDA_EPISODE_LOOKBACK_MS="${AGENTIC_LIVE_CAMPAIGN_MCDA_EPISODE_LOOKBACK_MS:-2000}"
MCDA_MAX_PRECEDING_WINDOWS="${AGENTIC_LIVE_CAMPAIGN_MCDA_MAX_PRECEDING_WINDOWS:-1}"
mkdir -p "$RESULTS_ROOT"

for value in "$REPETITIONS" "$MIN_RUNS" "$MIN_FLOWS"; do
  if ! [[ "$value" =~ ^[1-9][0-9]*$ ]] || (( value > 20 )); then
    echo "repetições e mínimos devem estar entre 1 e 20" >&2
    exit 2
  fi
done
for value in "$BASELINE_DURATION_S" "$ATTACK_DURATION_S"; do
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || {
    echo "durações devem ser inteiros positivos" >&2
    exit 2
  }
done
if ! [[ "$MCDA_CONVERGENCE_WINDOW_MS" =~ ^[1-9][0-9]*$ ]] \
    || (( MCDA_CONVERGENCE_WINDOW_MS > 10000 )); then
  echo "janela de convergência MCDA deve estar entre 1 e 10000 ms" >&2
  exit 2
fi
if ! [[ "$MCDA_EPISODE_LOOKBACK_MS" =~ ^[1-9][0-9]*$ ]] \
    || (( MCDA_EPISODE_LOOKBACK_MS > 10000 )); then
  echo "lookback do episódio MCDA deve estar entre 1 e 10000 ms" >&2
  exit 2
fi
if ! [[ "$MCDA_MAX_PRECEDING_WINDOWS" =~ ^[0-9]+$ ]] \
    || (( MCDA_MAX_PRECEDING_WINDOWS > 4 )); then
  echo "janelas MCDA precedentes devem estar entre 0 e 4" >&2
  exit 2
fi
if (( MCDA_CONVERGENCE_WINDOW_MS != 1000 \
      || MCDA_EPISODE_LOOKBACK_MS != 2000 \
      || MCDA_MAX_PRECEDING_WINDOWS != 1 )); then
  echo "definição MCDA v2 congelada exige futuro=1000 ms, lookback=2000 ms e uma janela precedente" >&2
  exit 2
fi
if [[ "$BUILD_IMAGE" != "true" && "$BUILD_IMAGE" != "false" ]]; then
  echo "AGENTIC_LIVE_CAMPAIGN_BUILD_IMAGE deve ser true ou false" >&2
  exit 2
fi

PROMOTION_REPORT="${AGENTIC_LIVE_CAMPAIGN_PROMOTION_REPORT:-}"
if [[ -z "$PROMOTION_REPORT" ]]; then
  PROMOTION_REPORT="$(find "$RESULTS_ROOT" -mindepth 2 -maxdepth 2 -type f \
    -path '*/authority-campaign-*/campaign-summary.json' | sort | tail -n 1)"
fi
CANARY_REPORT="${AGENTIC_LIVE_CAMPAIGN_CANARY_REPORT:-}"
if [[ -z "$CANARY_REPORT" ]]; then
  CANARY_REPORT="$(find "$RESULTS_ROOT" -mindepth 2 -maxdepth 2 -type f \
    -path '*/agentic-live-canary-*/canary-summary.json' | sort | tail -n 1)"
fi
if [[ -z "$PROMOTION_REPORT" || ! -r "$PROMOTION_REPORT" ]] \
    || ! jq -e '.aggregate.promotion_ready == true' \
      "$PROMOTION_REPORT" >/dev/null; then
  echo "campanha authority-dry-run promovida não encontrada" >&2
  exit 1
fi
if [[ -z "$CANARY_REPORT" || ! -r "$CANARY_REPORT" ]] \
    || ! jq -e '.aggregate.canary_ready == true' \
      "$CANARY_REPORT" >/dev/null; then
  echo "canário authority-live aprovado não encontrado" >&2
  exit 1
fi

PROMOTION_COMMIT="$(jq -r '
  [.cases[].git_commit] | unique | if length == 1 then .[0] else "" end
' "$PROMOTION_REPORT")"
PROMOTION_MODEL="$(jq -r '
  [.cases[].model_sha256] | unique | if length == 1 then .[0] else "" end
' "$PROMOTION_REPORT")"
CANARY_PROMOTION_COMMIT="$(jq -r '.promotion_commit // ""' "$CANARY_REPORT")"
CANARY_MODEL="$(jq -r '.promotion_model_sha256 // ""' "$CANARY_REPORT")"
if [[ -z "$PROMOTION_COMMIT" || "$CANARY_PROMOTION_COMMIT" != "$PROMOTION_COMMIT" ]] \
    || ! git -C "$PROJECT_ROOT" merge-base --is-ancestor \
      "$PROMOTION_COMMIT" HEAD 2>/dev/null; then
  echo "canário/campanha não derivam do commit promovido atual" >&2
  exit 1
fi
MODEL_PATH="${PREDICTOR_OFFLINE_MODEL:-$PROJECT_ROOT/models/cic2019-drddos-udp-holt.json}"
if [[ ! -r "$MODEL_PATH" ]]; then
  echo "modelo offline não encontrado: $MODEL_PATH" >&2
  exit 1
fi
CURRENT_MODEL="$(sha256sum "$MODEL_PATH" | awk '{print $1}')"
if [[ -z "$PROMOTION_MODEL" || "$CANARY_MODEL" != "$PROMOTION_MODEL" \
      || "$CURRENT_MODEL" != "$PROMOTION_MODEL" ]]; then
  echo "modelo atual diverge do artefato promovido e validado no canário" >&2
  exit 1
fi

IFS=',' read -r -a SOURCE_HOSTS <<< \
  "${AGENTIC_LIVE_CAMPAIGN_SOURCE_HOSTS:-h1,h2,h3}"
IFS=',' read -r -a DESTINATION_HOSTS <<< \
  "${AGENTIC_LIVE_CAMPAIGN_DESTINATION_HOSTS:-h8,h7,h6}"
IFS=',' read -r -a BASELINE_RATES <<< \
  "${AGENTIC_LIVE_CAMPAIGN_BASELINE_RATES:-1M,2M,5M}"
IFS=',' read -r -a ATTACK_RATES <<< \
  "${AGENTIC_LIVE_CAMPAIGN_ATTACK_RATES:-50M,100M,150M}"
if (( ${#SOURCE_HOSTS[@]} == 0 || ${#DESTINATION_HOSTS[@]} == 0 \
      || ${#BASELINE_RATES[@]} == 0 || ${#ATTACK_RATES[@]} == 0 )); then
  echo "listas de hosts e taxas não podem ser vazias" >&2
  exit 2
fi

host_domain() {
  local host="$1" index
  [[ "$host" =~ ^h[1-8]$ ]] || return 1
  index="${host#h}"
  echo $(( ((index + 1) / 2 - 1) / 2 ))
}

for host in "${SOURCE_HOSTS[@]}" "${DESTINATION_HOSTS[@]}"; do
  host_domain "$host" >/dev/null || {
    echo "host fora da topologia 2x2: $host" >&2
    exit 2
  }
done
for rate_value in "${BASELINE_RATES[@]}" "${ATTACK_RATES[@]}"; do
  [[ "$rate_value" =~ ^[1-9][0-9]*([KMG])?$ ]] || {
    echo "taxa inválida: $rate_value" >&2
    exit 2
  }
done

RUN_ID="agentic-live-campaign-$(date -u +%Y%m%dT%H%M%SZ)"
CAMPAIGN_ROOT="$RESULTS_ROOT/$RUN_ID"
mkdir -p "$CAMPAIGN_ROOT"
CAMPAIGN_ROOT="$(cd "$CAMPAIGN_ROOT" && pwd)"
PLAN_TSV="$CAMPAIGN_ROOT/campaign-plan.tsv"
: > "$PLAN_TSV"

for ((index=0; index<REPETITIONS; index++)); do
  source_host="${SOURCE_HOSTS[index % ${#SOURCE_HOSTS[@]}]}"
  destination_host="${DESTINATION_HOSTS[index % ${#DESTINATION_HOSTS[@]}]}"
  baseline_rate="${BASELINE_RATES[index % ${#BASELINE_RATES[@]}]}"
  attack_rate="${ATTACK_RATES[index % ${#ATTACK_RATES[@]}]}"
  if [[ "$(host_domain "$source_host")" == "$(host_domain "$destination_host")" ]]; then
    echo "a campanha requer fluxo cross-domain: $source_host->$destination_host" >&2
    exit 2
  fi
  pair_id="$(printf '%02d-%s-%s' "$((index + 1))" "$source_host" "$destination_host")"
  flow="10.0.0.${source_host#h}->10.0.0.${destination_host#h}"
  for scenario in benign ddos; do
    case_id="$(printf '%02d-%s-%s-%s' \
      "$((index + 1))" "$scenario" "$source_host" "$destination_host")"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$case_id" "$pair_id" "$scenario" "$source_host" "$destination_host" \
      "$flow" "$baseline_rate" "$attack_rate" >> "$PLAN_TSV"
  done
done

CAMPAIGN_ROOT="$CAMPAIGN_ROOT" CAMPAIGN_MIN_RUNS="$MIN_RUNS" \
CAMPAIGN_MIN_FLOWS="$MIN_FLOWS" PROMOTION_REPORT="$PROMOTION_REPORT" \
CANARY_REPORT="$CANARY_REPORT" PROMOTION_COMMIT="$PROMOTION_COMMIT" \
PROMOTION_MODEL="$PROMOTION_MODEL" \
MCDA_CONVERGENCE_WINDOW_MS="$MCDA_CONVERGENCE_WINDOW_MS" \
MCDA_EPISODE_LOOKBACK_MS="$MCDA_EPISODE_LOOKBACK_MS" \
MCDA_MAX_PRECEDING_WINDOWS="$MCDA_MAX_PRECEDING_WINDOWS" python3 - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

root = Path(os.environ["CAMPAIGN_ROOT"])
cases = []
for line in (root / "campaign-plan.tsv").read_text(encoding="utf-8").splitlines():
    case_id, pair_id, scenario, source, destination, flow, baseline, attack = line.split("\t")
    cases.append({
        "case_id": case_id, "pair_id": pair_id, "scenario": scenario,
        "source_host": source, "destination_host": destination, "flow": flow,
        "baseline_rate": baseline, "attack_rate": attack,
    })
payload = {
    "schema_version": 2,
    "created_at": datetime.now(timezone.utc).isoformat(),
    "mode": "authority-live-multiflow-campaign",
    "mcda_convergence_window_ms": int(
        os.environ["MCDA_CONVERGENCE_WINDOW_MS"]
    ),
    "mcda_episode_definition": {
        "name": "bounded-episode-window-v2",
        "lookback_ms": int(os.environ["MCDA_EPISODE_LOOKBACK_MS"]),
        "max_preceding_windows": int(
            os.environ["MCDA_MAX_PRECEDING_WINDOWS"]
        ),
        "future_convergence_ms": int(
            os.environ["MCDA_CONVERGENCE_WINDOW_MS"]
        ),
    },
    "minimums": {
        "ddos": int(os.environ["CAMPAIGN_MIN_RUNS"]),
        "benign": int(os.environ["CAMPAIGN_MIN_RUNS"]),
        "distinct_flows": int(os.environ["CAMPAIGN_MIN_FLOWS"]),
    },
    "prerequisites": {
        "promotion_ready": True, "canary_ready": True,
        "promotion_report": os.environ["PROMOTION_REPORT"],
        "canary_report": os.environ["CANARY_REPORT"],
        "promotion_commit": os.environ["PROMOTION_COMMIT"],
        "model_sha256": os.environ["PROMOTION_MODEL"],
    },
    "cases": cases,
}
(root / "campaign-manifest.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY

echo "[agentic-live-campaign] raiz: $CAMPAIGN_ROOT"
echo "[agentic-live-campaign] casos: $((REPETITIONS * 2))"
first_case=true
while IFS=$'\t' read -r case_id pair_id scenario source_host destination_host \
    flow baseline_rate attack_rate; do
  case_root="$CAMPAIGN_ROOT/$case_id"
  mkdir -p "$case_root"
  control_marker="$CAMPAIGN_ROOT/.${pair_id}-benign-passed"
  if [[ "$scenario" == "ddos" && ! -f "$control_marker" ]]; then
    echo "[agentic-live-campaign] $case_id bloqueado: controle benigno não aprovado" \
      | tee "$case_root/skipped-reason.txt"
    printf '125\n' > "$case_root/benchmark-exit-code.txt"
    printf '125\n' > "$case_root/evaluator-exit-code.txt"
    continue
  fi
  case_build=false
  if [[ "$first_case" == "true" && "$BUILD_IMAGE" == "true" ]]; then
    case_build=true
  fi
  first_case=false
  echo "[agentic-live-campaign] $case_id fluxo=$flow taxas=$baseline_rate/$attack_rate"
  set +e
  BENCHMARK_SOURCE_HOST="$source_host" \
  BENCHMARK_DESTINATION_HOST="$destination_host" \
  BENCHMARK_BASELINE_RATE="$baseline_rate" \
  BENCHMARK_ATTACK_RATE="$attack_rate" \
  BENCHMARK_BASELINE_DURATION_S="$BASELINE_DURATION_S" \
  BENCHMARK_ATTACK_DURATION_S="$ATTACK_DURATION_S" \
  BENCHMARK_BUILD_IMAGE="$case_build" \
  BENCHMARK_BOOTSTRAP_ENV=true \
  BENCHMARK_EXPORT_HISTORY=false \
  BENCHMARK_RESULTS_ROOT="$case_root" \
  PREDICTOR_OFFLINE_MODEL="$MODEL_PATH" \
    bash "$PROJECT_ROOT/scripts/run_collaborative_benchmark.sh" \
      agentic-live "$scenario" --allow-agentic-mitigation \
      2>&1 | tee "$case_root/benchmark.log"
  benchmark_status="${PIPESTATUS[0]}"
  set -e
  printf '%s\n' "$benchmark_status" > "$case_root/benchmark-exit-code.txt"

  run_dir="$(find "$case_root" -mindepth 1 -maxdepth 1 -type d | sort | head -n 1)"
  evaluator_status=125
  if [[ -n "$run_dir" ]]; then
    set +e
    python3 "$PROJECT_ROOT/experiments/evaluate_agentic_live_run.py" \
      "$run_dir" --output "$case_root/agentic-live-summary.json" \
      --mcda-convergence-window-ms "$MCDA_CONVERGENCE_WINDOW_MS" \
      --mcda-episode-lookback-ms "$MCDA_EPISODE_LOOKBACK_MS" \
      --mcda-max-preceding-windows "$MCDA_MAX_PRECEDING_WINDOWS" \
      2>&1 | tee "$case_root/live-evaluator.log"
    evaluator_status="${PIPESTATUS[0]}"
    set -e
  fi
  printf '%s\n' "$evaluator_status" > "$case_root/evaluator-exit-code.txt"
  if [[ "$scenario" == "benign" && "$benchmark_status" -eq 0 \
        && "$evaluator_status" -eq 0 ]]; then
    : > "$control_marker"
  fi
done < "$PLAN_TSV"

python3 "$PROJECT_ROOT/experiments/evaluate_agentic_live_campaign.py" \
  "$CAMPAIGN_ROOT" --output "$CAMPAIGN_ROOT/campaign-summary"

echo "[agentic-live-campaign] concluída: $CAMPAIGN_ROOT"
