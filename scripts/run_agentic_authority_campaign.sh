#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/run_agentic_authority_campaign.sh

Executa uma campanha isolada de promoção do authority-dry-run. Para cada
configuração, roda um controle benigno e um DDoS, valida o episódio e agrega
precision/recall, latências, claims e invariantes do plano de dados.

Defaults (3 pares, 6 execuções):
  h1->h8 a 1M/50M
  h2->h7 a 2M/100M
  h3->h6 a 5M/150M

Variáveis úteis:
  AUTHORITY_CAMPAIGN_REPETITIONS=3
  AUTHORITY_CAMPAIGN_MIN_RUNS_PER_SCENARIO=3
  AUTHORITY_CAMPAIGN_MIN_DISTINCT_FLOWS=3
  AUTHORITY_CAMPAIGN_SOURCE_HOSTS=h1,h2,h3
  AUTHORITY_CAMPAIGN_DESTINATION_HOSTS=h8,h7,h6
  AUTHORITY_CAMPAIGN_BASELINE_RATES=1M,2M,5M
  AUTHORITY_CAMPAIGN_ATTACK_RATES=50M,100M,150M
  AUTHORITY_CAMPAIGN_BUILD_IMAGE=true|false
  AUTHORITY_CAMPAIGN_BASELINE_DURATION_S=12
  AUTHORITY_CAMPAIGN_ATTACK_DURATION_S=20
  AUTHORITY_CAMPAIGN_RESULTS_ROOT=experiments/results

O runner sempre recria o ambiente entre casos. O primeiro caso pode reconstruir
as imagens; os demais reutilizam essas imagens, mas não o estado dos containers.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ $# -ne 0 ]]; then
  usage >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
REPETITIONS="${AUTHORITY_CAMPAIGN_REPETITIONS:-3}"
MIN_RUNS_PER_SCENARIO="${AUTHORITY_CAMPAIGN_MIN_RUNS_PER_SCENARIO:-3}"
MIN_DISTINCT_FLOWS="${AUTHORITY_CAMPAIGN_MIN_DISTINCT_FLOWS:-3}"
BUILD_IMAGE="${AUTHORITY_CAMPAIGN_BUILD_IMAGE:-true}"
RESULTS_ROOT="${AUTHORITY_CAMPAIGN_RESULTS_ROOT:-$PROJECT_ROOT/experiments/results}"
BASELINE_DURATION_S="${AUTHORITY_CAMPAIGN_BASELINE_DURATION_S:-12}"
ATTACK_DURATION_S="${AUTHORITY_CAMPAIGN_ATTACK_DURATION_S:-20}"

if ! [[ "$REPETITIONS" =~ ^[1-9][0-9]*$ ]] || (( REPETITIONS > 20 )); then
  echo "AUTHORITY_CAMPAIGN_REPETITIONS deve estar entre 1 e 20" >&2
  exit 2
fi
for value in "$MIN_RUNS_PER_SCENARIO" "$MIN_DISTINCT_FLOWS"; do
  if ! [[ "$value" =~ ^[1-9][0-9]*$ ]] || (( value > 20 )); then
    echo "mínimos da campanha devem estar entre 1 e 20" >&2
    exit 2
  fi
done
for value in "$BASELINE_DURATION_S" "$ATTACK_DURATION_S"; do
  if ! [[ "$value" =~ ^[1-9][0-9]*$ ]]; then
    echo "durações da campanha devem ser inteiros positivos" >&2
    exit 2
  fi
done
if [[ "$BUILD_IMAGE" != "true" && "$BUILD_IMAGE" != "false" ]]; then
  echo "AUTHORITY_CAMPAIGN_BUILD_IMAGE deve ser true ou false" >&2
  exit 2
fi

IFS=',' read -r -a SOURCE_HOSTS <<< \
  "${AUTHORITY_CAMPAIGN_SOURCE_HOSTS:-h1,h2,h3}"
IFS=',' read -r -a DESTINATION_HOSTS <<< \
  "${AUTHORITY_CAMPAIGN_DESTINATION_HOSTS:-h8,h7,h6}"
IFS=',' read -r -a BASELINE_RATES <<< \
  "${AUTHORITY_CAMPAIGN_BASELINE_RATES:-1M,2M,5M}"
IFS=',' read -r -a ATTACK_RATES <<< \
  "${AUTHORITY_CAMPAIGN_ATTACK_RATES:-50M,100M,150M}"

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
for value in "${BASELINE_RATES[@]}" "${ATTACK_RATES[@]}"; do
  [[ "$value" =~ ^[1-9][0-9]*([KMG])?$ ]] || {
    echo "taxa inválida: $value" >&2
    exit 2
  }
done

RUN_ID="authority-campaign-$(date -u +%Y%m%dT%H%M%SZ)"
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
  source_domain="$(host_domain "$source_host")"
  destination_domain="$(host_domain "$destination_host")"
  if [[ "$source_domain" == "$destination_domain" ]]; then
    echo "a campanha requer fluxo cross-domain: $source_host->$destination_host" >&2
    exit 2
  fi
  source_ip="10.0.0.${source_host#h}"
  destination_ip="10.0.0.${destination_host#h}"
  flow="$source_ip->$destination_ip"
  for scenario in benign ddos; do
    case_id="$(printf '%02d-%s-%s-%s' \
      "$((index + 1))" "$scenario" "$source_host" "$destination_host")"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$case_id" "$scenario" "$source_host" "$destination_host" \
      "$flow" "$baseline_rate" "$attack_rate" >> "$PLAN_TSV"
  done
done

CAMPAIGN_ROOT="$CAMPAIGN_ROOT" \
CAMPAIGN_MIN_RUNS="$MIN_RUNS_PER_SCENARIO" \
CAMPAIGN_MIN_DISTINCT_FLOWS="$MIN_DISTINCT_FLOWS" \
python3 - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

root = Path(os.environ["CAMPAIGN_ROOT"])
cases = []
for line in (root / "campaign-plan.tsv").read_text(encoding="utf-8").splitlines():
    case_id, scenario, source, destination, flow, baseline, attack = line.split("\t")
    cases.append({
        "case_id": case_id,
        "scenario": scenario,
        "source_host": source,
        "destination_host": destination,
        "flow": flow,
        "baseline_rate": baseline,
        "attack_rate": attack,
    })
payload = {
    "schema_version": 1,
    "created_at": datetime.now(timezone.utc).isoformat(),
    "mode": "authority-dry-run-promotion-campaign",
    "minimums": {
        "ddos": int(os.environ["CAMPAIGN_MIN_RUNS"]),
        "benign": int(os.environ["CAMPAIGN_MIN_RUNS"]),
        "distinct_flows": int(os.environ["CAMPAIGN_MIN_DISTINCT_FLOWS"]),
    },
    "cases": cases,
}
(root / "campaign-manifest.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

echo "[authority-campaign] raiz: $CAMPAIGN_ROOT"
echo "[authority-campaign] casos: $((REPETITIONS * 2)) (benign + ddos)"
first_case=true
while IFS=$'\t' read -r case_id scenario source_host destination_host \
    flow baseline_rate attack_rate; do
  case_root="$CAMPAIGN_ROOT/$case_id"
  mkdir -p "$case_root"
  case_build=false
  if [[ "$first_case" == "true" && "$BUILD_IMAGE" == "true" ]]; then
    case_build=true
  fi
  first_case=false
  echo "[authority-campaign] $case_id fluxo=$flow taxas=$baseline_rate/$attack_rate"
  set +e
  BENCHMARK_AGENTIC_ENABLED=true \
  BENCHMARK_AGENTIC_MODE=authority-dry-run \
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
    bash "$PROJECT_ROOT/scripts/run_collaborative_benchmark.sh" \
      collaborative-dry-run "$scenario" \
      2>&1 | tee "$case_root/benchmark.log"
  benchmark_status="${PIPESTATUS[0]}"
  set -e
  printf '%s\n' "$benchmark_status" > "$case_root/benchmark-exit-code.txt"

  run_dir="$(find "$case_root" -mindepth 1 -maxdepth 1 -type d | sort | head -n 1)"
  if [[ -z "$run_dir" ]]; then
    echo "[authority-campaign] $case_id não produziu diretório de execução" >&2
    continue
  fi
  set +e
  python3 "$PROJECT_ROOT/experiments/evaluate_agentic_authority_run.py" \
    "$run_dir" --output "$case_root/authority-summary.json" \
    2>&1 | tee "$case_root/authority-evaluator.log"
  evaluator_status="${PIPESTATUS[0]}"
  set -e
  printf '%s\n' "$evaluator_status" > "$case_root/evaluator-exit-code.txt"
done < "$PLAN_TSV"

python3 "$PROJECT_ROOT/experiments/evaluate_agentic_authority_campaign.py" \
  "$CAMPAIGN_ROOT" --output "$CAMPAIGN_ROOT/campaign-summary"

echo "[authority-campaign] concluída: $CAMPAIGN_ROOT"
