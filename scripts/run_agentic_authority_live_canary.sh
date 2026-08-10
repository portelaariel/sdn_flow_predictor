#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

if [[ "${1:-}" != "--allow-agentic-mitigation" ]]; then
  echo "uso: bash scripts/run_agentic_authority_live_canary.sh --allow-agentic-mitigation" >&2
  echo "o canário instala DROP real somente no caso DDoS" >&2
  exit 2
fi

RESULTS_ROOT="${AGENTIC_LIVE_RESULTS_ROOT:-$PROJECT_ROOT/experiments/results}"
CANARY_ROOT="$RESULTS_ROOT/agentic-live-canary-$(date -u +%Y%m%dT%H%M%SZ)"
PROMOTION_REPORT="${AGENTIC_LIVE_PROMOTION_REPORT:-}"
if [[ -z "$PROMOTION_REPORT" ]]; then
  PROMOTION_REPORT="$(find "$RESULTS_ROOT" -mindepth 2 -maxdepth 2 -type f \
    -path '*/authority-campaign-*/campaign-summary.json' | sort | tail -n 1)"
fi
if [[ -z "$PROMOTION_REPORT" || ! -r "$PROMOTION_REPORT" ]]; then
  echo "campaign-summary.json de promoção não encontrado" >&2
  exit 1
fi
if ! jq -e '.aggregate.promotion_ready == true' \
  "$PROMOTION_REPORT" >/dev/null; then
  echo "a campanha authority-dry-run ainda não está pronta para promoção" >&2
  exit 1
fi

PROMOTION_COMMIT="$(jq -r '[.cases[].git_commit] | unique | if length == 1 then .[0] else "" end' \
  "$PROMOTION_REPORT")"
if [[ -z "$PROMOTION_COMMIT" ]] \
  || ! git -C "$PROJECT_ROOT" merge-base --is-ancestor \
    "$PROMOTION_COMMIT" HEAD 2>/dev/null; then
  echo "o commit promovido não é ancestral do código do canário" >&2
  exit 1
fi

MODEL_PATH="${PREDICTOR_OFFLINE_MODEL:-$PROJECT_ROOT/models/cic2019-drddos-udp-holt.json}"
if [[ ! -r "$MODEL_PATH" ]]; then
  echo "modelo offline do canário não encontrado: $MODEL_PATH" >&2
  exit 1
fi
PROMOTION_MODEL="$(jq -r '[.cases[].model_sha256] | unique | if length == 1 then .[0] else "" end' \
  "$PROMOTION_REPORT")"
CURRENT_MODEL="$(sha256sum "$MODEL_PATH" | awk '{print $1}')"
if [[ -z "$PROMOTION_MODEL" || "$PROMOTION_MODEL" != "$CURRENT_MODEL" ]]; then
  echo "o modelo offline diverge do artefato aprovado na campanha" >&2
  exit 1
fi

mkdir -p "$CANARY_ROOT"

run_case() {
  local scenario="$1"
  echo "[agentic-live] cenário=$scenario"
  BENCHMARK_RESULTS_ROOT="$CANARY_ROOT" \
  BENCHMARK_AGENTIC_ENABLED=true \
  BENCHMARK_AGENTIC_MODE=authority-live \
  BENCHMARK_BOOTSTRAP_ENV=true \
  BENCHMARK_EXPORT_HISTORY=false \
  PREDICTOR_OFFLINE_MODEL="$MODEL_PATH" \
    bash "$PROJECT_ROOT/scripts/run_collaborative_benchmark.sh" \
      agentic-live "$scenario" --allow-agentic-mitigation

  local run_dir
  run_dir="$(find "$CANARY_ROOT" -mindepth 1 -maxdepth 1 -type d \
    -name "*-agentic-live-$scenario" | sort | tail -n 1)"
  if [[ -z "$run_dir" ]]; then
    echo "resultado agentic-live não encontrado para $scenario" >&2
    exit 1
  fi
  python3 "$PROJECT_ROOT/experiments/evaluate_agentic_live_run.py" "$run_dir"
}

# O controle negativo vem primeiro: nenhum acordo ou DROP pode aparecer para
# tráfego benigno mesmo com a fronteira live habilitada.
run_case benign
run_case ddos

python3 - "$CANARY_ROOT" "$PROMOTION_REPORT" \
  "$PROMOTION_COMMIT" "$PROMOTION_MODEL" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
reports = [
    json.loads(path.read_text(encoding="utf-8"))
    for path in sorted(root.glob("*/agentic-live-summary.json"))
]
payload = {
    "mode": "authority-live-canary",
    "promotion_report": sys.argv[2],
    "promotion_commit": sys.argv[3],
    "promotion_model_sha256": sys.argv[4],
    "cases": reports,
    "aggregate": {
        "passed": sum(report["aggregate"]["safe"] for report in reports),
        "total": len(reports),
        "canary_ready": (
            len(reports) == 2
            and {report["scenario"] for report in reports} == {"benign", "ddos"}
            and all(report["aggregate"]["safe"] for report in reports)
        ),
    },
}
(root / "canary-summary.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    encoding="utf-8",
)
print(json.dumps(payload["aggregate"], indent=2, sort_keys=True))
if not payload["aggregate"]["canary_ready"]:
    raise SystemExit(1)
PY

echo "[agentic-live] canário concluído: $CANARY_ROOT"
