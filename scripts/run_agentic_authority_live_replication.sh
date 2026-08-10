#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Uso:
  bash scripts/run_agentic_authority_live_replication.sh \
    --allow-agentic-mitigation

Executa uma replicação estatística congelada da autoridade agentic live.
O protocolo padrão contém 18 casos: três controles benignos e três ataques
para cada fluxo h1->h8, h2->h7 e h3->h6. Cada controle precede seu ataque.

Pré-requisitos obrigatórios:
  - campanha authority-live piloto com campaign_ready=true,
    operational_ready=true e comparative_ready=true;
  - campanha authority-dry-run promovida e canário live aprovados;
  - árvore Git rastreada limpa e commit descendente do piloto;
  - mesmo SHA-256 do modelo offline usado na promoção e no piloto;
  - espaço livre mínimo antes do início;
  - consentimento explícito acima, pois nove casos instalam DROP real.

Variáveis úteis (definidas antes do início e gravadas no manifesto):
  AGENTIC_REPLICATION_REPETITIONS=9        # múltiplo de 3, 3 por fluxo
  AGENTIC_REPLICATION_MIN_FREE_MB=512
  AGENTIC_REPLICATION_BUILD_IMAGE=true|false
  AGENTIC_REPLICATION_BASELINE_DURATION_S=12
  AGENTIC_REPLICATION_ATTACK_DURATION_S=20
  AGENTIC_REPLICATION_BOOTSTRAP_RESAMPLES=5000
  AGENTIC_REPLICATION_MCDA_CONVERGENCE_WINDOW_MS=1000
  AGENTIC_REPLICATION_RESULTS_ROOT=experiments/results
  AGENTIC_REPLICATION_PILOT_REPORT=/caminho/campaign-summary.json
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
RESULTS_ROOT="${AGENTIC_REPLICATION_RESULTS_ROOT:-$PROJECT_ROOT/experiments/results}"
REPETITIONS="${AGENTIC_REPLICATION_REPETITIONS:-9}"
MIN_FREE_MB="${AGENTIC_REPLICATION_MIN_FREE_MB:-512}"
BUILD_IMAGE="${AGENTIC_REPLICATION_BUILD_IMAGE:-true}"
BASELINE_DURATION_S="${AGENTIC_REPLICATION_BASELINE_DURATION_S:-12}"
ATTACK_DURATION_S="${AGENTIC_REPLICATION_ATTACK_DURATION_S:-20}"
BOOTSTRAP_RESAMPLES="${AGENTIC_REPLICATION_BOOTSTRAP_RESAMPLES:-5000}"
MCDA_CONVERGENCE_WINDOW_MS="${AGENTIC_REPLICATION_MCDA_CONVERGENCE_WINDOW_MS:-1000}"

if ! [[ "$REPETITIONS" =~ ^[1-9][0-9]*$ ]] \
    || (( REPETITIONS < 9 || REPETITIONS > 18 || REPETITIONS % 3 != 0 )); then
  echo "AGENTIC_REPLICATION_REPETITIONS deve ser 9, 12, 15 ou 18" >&2
  exit 2
fi
for value in "$MIN_FREE_MB" "$BASELINE_DURATION_S" "$ATTACK_DURATION_S" \
    "$BOOTSTRAP_RESAMPLES" "$MCDA_CONVERGENCE_WINDOW_MS"; do
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || {
    echo "mínimos, durações, bootstrap e janela devem ser inteiros positivos" >&2
    exit 2
  }
done
if (( BOOTSTRAP_RESAMPLES < 100 || BOOTSTRAP_RESAMPLES > 100000 )); then
  echo "AGENTIC_REPLICATION_BOOTSTRAP_RESAMPLES deve estar entre 100 e 100000" >&2
  exit 2
fi
if (( MCDA_CONVERGENCE_WINDOW_MS > 10000 )); then
  echo "AGENTIC_REPLICATION_MCDA_CONVERGENCE_WINDOW_MS deve ser <= 10000" >&2
  exit 2
fi
if [[ "$BUILD_IMAGE" != "true" && "$BUILD_IMAGE" != "false" ]]; then
  echo "AGENTIC_REPLICATION_BUILD_IMAGE deve ser true ou false" >&2
  exit 2
fi

mkdir -p "$RESULTS_ROOT"
RESULTS_ROOT="$(cd "$RESULTS_ROOT" && pwd)"

PILOT_REPORT="${AGENTIC_REPLICATION_PILOT_REPORT:-}"
if [[ -z "$PILOT_REPORT" ]]; then
  PILOT_REPORT="$(find "$RESULTS_ROOT" -mindepth 2 -maxdepth 2 -type f \
    -path '*/agentic-live-campaign-*/campaign-summary.json' \
    | sort | tail -n 1)"
fi
if [[ -z "$PILOT_REPORT" || ! -r "$PILOT_REPORT" ]] \
    || ! jq -e '
      .aggregate.campaign_ready == true and
      .aggregate.operational_ready == true and
      .aggregate.comparative_ready == true
    ' "$PILOT_REPORT" >/dev/null; then
  echo "campanha authority-live piloto integralmente aprovada não encontrada" >&2
  exit 1
fi
PILOT_REPORT="$(cd "$(dirname "$PILOT_REPORT")" && pwd)/$(basename "$PILOT_REPORT")"

PILOT_COMMIT="$(jq -r '
  [.cases[].git_commit | select(. != null)] | unique |
  if length == 1 then .[0] else "" end
' "$PILOT_REPORT")"
PILOT_MODEL="$(jq -r '
  [.cases[].model_sha256 | select(. != null)] | unique |
  if length == 1 then .[0] else "" end
' "$PILOT_REPORT")"
PROMOTION_REPORT="$(jq -r '.prerequisites.promotion_report // ""' \
  "$PILOT_REPORT")"
CANARY_REPORT="$(jq -r '.prerequisites.canary_report // ""' \
  "$PILOT_REPORT")"
if [[ -z "$PROMOTION_REPORT" || ! -r "$PROMOTION_REPORT" ]]; then
  PROMOTION_REPORT="$(find "$RESULTS_ROOT" -mindepth 2 -maxdepth 2 -type f \
    -path '*/authority-campaign-*/campaign-summary.json' | sort | tail -n 1)"
fi
if [[ -z "$CANARY_REPORT" || ! -r "$CANARY_REPORT" ]]; then
  CANARY_REPORT="$(find "$RESULTS_ROOT" -mindepth 2 -maxdepth 2 -type f \
    -path '*/agentic-live-canary-*/canary-summary.json' | sort | tail -n 1)"
fi
if [[ -z "$PROMOTION_REPORT" || ! -r "$PROMOTION_REPORT" ]] \
    || ! jq -e '.aggregate.promotion_ready == true' \
      "$PROMOTION_REPORT" >/dev/null; then
  echo "relatório de promoção dry-run aprovado não encontrado" >&2
  exit 1
fi
if [[ -z "$CANARY_REPORT" || ! -r "$CANARY_REPORT" ]] \
    || ! jq -e '.aggregate.canary_ready == true' \
      "$CANARY_REPORT" >/dev/null; then
  echo "relatório de canário live aprovado não encontrado" >&2
  exit 1
fi

CURRENT_COMMIT="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"
if [[ -z "$PILOT_COMMIT" ]] \
    || ! git -C "$PROJECT_ROOT" merge-base --is-ancestor \
      "$PILOT_COMMIT" "$CURRENT_COMMIT" 2>/dev/null; then
  echo "o commit atual não deriva da campanha live piloto" >&2
  exit 1
fi
if ! git -C "$PROJECT_ROOT" diff --quiet --ignore-submodules -- \
    || ! git -C "$PROJECT_ROOT" diff --cached --quiet --ignore-submodules --; then
  echo "árvore Git rastreada possui alterações; congele-as em um commit antes da replicação" >&2
  exit 1
fi

MODEL_PATH="${PREDICTOR_OFFLINE_MODEL:-$PROJECT_ROOT/models/cic2019-drddos-udp-holt.json}"
if [[ ! -r "$MODEL_PATH" ]]; then
  echo "modelo offline não encontrado: $MODEL_PATH" >&2
  exit 1
fi
MODEL_PATH="$(cd "$(dirname "$MODEL_PATH")" && pwd)/$(basename "$MODEL_PATH")"
CURRENT_MODEL="$(sha256sum "$MODEL_PATH" | awk '{print $1}')"
PROMOTION_MODEL="$(jq -r '.prerequisites.model_sha256 // ""' \
  "$PILOT_REPORT")"
if [[ -z "$PILOT_MODEL" || -z "$PROMOTION_MODEL" \
      || "$CURRENT_MODEL" != "$PILOT_MODEL" \
      || "$CURRENT_MODEL" != "$PROMOTION_MODEL" ]]; then
  echo "modelo atual diverge do modelo promovido e usado no piloto" >&2
  exit 1
fi

AVAILABLE_MB="$(df -Pm "$RESULTS_ROOT" | awk 'NR == 2 {print $4}')"
if ! [[ "$AVAILABLE_MB" =~ ^[0-9]+$ ]] || (( AVAILABLE_MB < MIN_FREE_MB )); then
  echo "espaço insuficiente: ${AVAILABLE_MB:-?} MiB livres; mínimo=$MIN_FREE_MB MiB" >&2
  exit 1
fi

RUN_ID="agentic-live-replication-$(date -u +%Y%m%dT%H%M%SZ)"
REPLICATION_ROOT="$RESULTS_ROOT/$RUN_ID"
mkdir -p "$REPLICATION_ROOT"
REPLICATION_ROOT="$(cd "$REPLICATION_ROOT" && pwd)"
REPETITIONS_PER_FLOW=$((REPETITIONS / 3))

REPLICATION_ROOT="$REPLICATION_ROOT" PILOT_REPORT="$PILOT_REPORT" \
PILOT_COMMIT="$PILOT_COMMIT" CURRENT_COMMIT="$CURRENT_COMMIT" \
MODEL_SHA256="$CURRENT_MODEL" MODEL_PATH="$MODEL_PATH" \
PROMOTION_REPORT="$PROMOTION_REPORT" CANARY_REPORT="$CANARY_REPORT" \
REPETITIONS="$REPETITIONS" REPETITIONS_PER_FLOW="$REPETITIONS_PER_FLOW" \
MIN_FREE_MB="$MIN_FREE_MB" AVAILABLE_MB="$AVAILABLE_MB" \
BASELINE_DURATION_S="$BASELINE_DURATION_S" ATTACK_DURATION_S="$ATTACK_DURATION_S" \
BOOTSTRAP_RESAMPLES="$BOOTSTRAP_RESAMPLES" \
MCDA_CONVERGENCE_WINDOW_MS="$MCDA_CONVERGENCE_WINDOW_MS" python3 - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

root = Path(os.environ["REPLICATION_ROOT"])
payload = {
    "schema_version": 1,
    "created_at": datetime.now(timezone.utc).isoformat(),
    "mode": "agentic-live-statistical-replication",
    "design": {
        "runs_per_scenario": int(os.environ["REPETITIONS"]),
        "repetitions_per_flow": int(os.environ["REPETITIONS_PER_FLOW"]),
        "distinct_flows": 3,
        "scenarios": ["benign", "ddos"],
        "flows": [
            {"flow": "10.0.0.1->10.0.0.8", "hosts": "h1->h8",
             "baseline_rate": "1M", "attack_rate": "50M"},
            {"flow": "10.0.0.2->10.0.0.7", "hosts": "h2->h7",
             "baseline_rate": "2M", "attack_rate": "100M"},
            {"flow": "10.0.0.3->10.0.0.6", "hosts": "h3->h6",
             "baseline_rate": "5M", "attack_rate": "150M"},
        ],
        "baseline_duration_s": int(os.environ["BASELINE_DURATION_S"]),
        "attack_duration_s": int(os.environ["ATTACK_DURATION_S"]),
        "mcda_convergence_window_ms": int(
            os.environ["MCDA_CONVERGENCE_WINDOW_MS"]
        ),
        "bootstrap_resamples": int(os.environ["BOOTSTRAP_RESAMPLES"]),
        "bootstrap_seed": 20260810,
        "confidence_level": 0.95,
    },
    "baseline": {
        "pilot_report": os.environ["PILOT_REPORT"],
        "pilot_commit": os.environ["PILOT_COMMIT"],
        "promotion_report": os.environ["PROMOTION_REPORT"],
        "canary_report": os.environ["CANARY_REPORT"],
        "model_sha256": os.environ["MODEL_SHA256"],
    },
    "runtime": {
        "git_commit": os.environ["CURRENT_COMMIT"],
        "model_path": os.environ["MODEL_PATH"],
        "model_sha256": os.environ["MODEL_SHA256"],
    },
    "execution": {
        "tracked_tree_clean": True,
        "disk_preflight_passed": True,
        "minimum_free_mb": int(os.environ["MIN_FREE_MB"]),
        "available_free_mb_at_start": int(os.environ["AVAILABLE_MB"]),
        "campaign_summary": None,
        "campaign_exit_code": None,
    },
}
(root / "replication-manifest.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY

echo "[agentic-replication] raiz: $REPLICATION_ROOT"
echo "[agentic-replication] protocolo congelado: $REPETITIONS ataques + $REPETITIONS controles"
echo "[agentic-replication] repetições por fluxo/cenário: $REPETITIONS_PER_FLOW"
echo "[agentic-replication] commit=$CURRENT_COMMIT modelo=$CURRENT_MODEL"
echo "[agentic-replication] espaço inicial=${AVAILABLE_MB} MiB"

set +e
AGENTIC_LIVE_CAMPAIGN_RESULTS_ROOT="$REPLICATION_ROOT" \
AGENTIC_LIVE_CAMPAIGN_REPETITIONS="$REPETITIONS" \
AGENTIC_LIVE_CAMPAIGN_MIN_RUNS_PER_SCENARIO="$REPETITIONS" \
AGENTIC_LIVE_CAMPAIGN_MIN_DISTINCT_FLOWS=3 \
AGENTIC_LIVE_CAMPAIGN_SOURCE_HOSTS=h1,h2,h3 \
AGENTIC_LIVE_CAMPAIGN_DESTINATION_HOSTS=h8,h7,h6 \
AGENTIC_LIVE_CAMPAIGN_BASELINE_RATES=1M,2M,5M \
AGENTIC_LIVE_CAMPAIGN_ATTACK_RATES=50M,100M,150M \
AGENTIC_LIVE_CAMPAIGN_BUILD_IMAGE="$BUILD_IMAGE" \
AGENTIC_LIVE_CAMPAIGN_BASELINE_DURATION_S="$BASELINE_DURATION_S" \
AGENTIC_LIVE_CAMPAIGN_ATTACK_DURATION_S="$ATTACK_DURATION_S" \
AGENTIC_LIVE_CAMPAIGN_MCDA_CONVERGENCE_WINDOW_MS="$MCDA_CONVERGENCE_WINDOW_MS" \
AGENTIC_LIVE_CAMPAIGN_PROMOTION_REPORT="$PROMOTION_REPORT" \
AGENTIC_LIVE_CAMPAIGN_CANARY_REPORT="$CANARY_REPORT" \
PREDICTOR_OFFLINE_MODEL="$MODEL_PATH" \
  bash "$PROJECT_ROOT/scripts/run_agentic_authority_live_campaign.sh" \
    --allow-agentic-mitigation \
    2>&1 | tee "$REPLICATION_ROOT/campaign.log"
campaign_status="${PIPESTATUS[0]}"
set -e

CAMPAIGN_SUMMARY="$(find "$REPLICATION_ROOT" -mindepth 2 -maxdepth 2 -type f \
  -path '*/agentic-live-campaign-*/campaign-summary.json' | sort | tail -n 1)"
REPLICATION_ROOT="$REPLICATION_ROOT" CAMPAIGN_SUMMARY="$CAMPAIGN_SUMMARY" \
CAMPAIGN_STATUS="$campaign_status" python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["REPLICATION_ROOT"]) / "replication-manifest.json"
payload = json.loads(path.read_text(encoding="utf-8"))
summary = os.environ.get("CAMPAIGN_SUMMARY") or None
if summary:
    try:
        summary = str(Path(summary).relative_to(path.parent))
    except ValueError:
        pass
payload["execution"]["campaign_summary"] = summary
payload["execution"]["campaign_exit_code"] = int(os.environ["CAMPAIGN_STATUS"])
path.write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY

set +e
python3 "$PROJECT_ROOT/experiments/evaluate_agentic_live_replication.py" \
  "$REPLICATION_ROOT" --pilot-report "$PILOT_REPORT" \
  --bootstrap-resamples "$BOOTSTRAP_RESAMPLES" \
  --output "$REPLICATION_ROOT/replication-summary"
replication_status=$?
set -e

echo "[agentic-replication] campanha_status=$campaign_status"
echo "[agentic-replication] concluída: $REPLICATION_ROOT"
exit "$replication_status"
