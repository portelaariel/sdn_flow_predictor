#!/usr/bin/env python3
"""Avalia a replicação estatística controlada da autoridade agentic live."""

import argparse
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


Z_95 = 1.959963984540054
DEFAULT_BOOTSTRAP_RESAMPLES = 5000
DEFAULT_BOOTSTRAP_SEED = 20260810
STANDARD_PROTOCOL = {
    "10.0.0.1->10.0.0.8": ("1M", "50M"),
    "10.0.0.2->10.0.0.7": ("2M", "100M"),
    "10.0.0.3->10.0.0.6": ("5M", "150M"),
}


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return default


def rate(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 6) if denominator else None


def wilson_interval(successes: int, total: int) -> Dict[str, Any]:
    """Retorna o intervalo binomial de Wilson de 95%."""
    if total <= 0 or successes < 0 or successes > total:
        return {
            "confidence": 0.95, "successes": successes, "n": total,
            "estimate": None, "low": None, "high": None,
        }
    estimate = successes / total
    denominator = 1.0 + Z_95 ** 2 / total
    center = (estimate + Z_95 ** 2 / (2.0 * total)) / denominator
    margin = (
        Z_95
        * math.sqrt(
            estimate * (1.0 - estimate) / total
            + Z_95 ** 2 / (4.0 * total ** 2)
        )
        / denominator
    )
    return {
        "confidence": 0.95,
        "successes": successes,
        "n": total,
        "estimate": round(estimate, 6),
        "low": round(max(0.0, center - margin), 6),
        "high": round(min(1.0, center + margin), 6),
    }


def percentile(values: List[float], probability: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    position = (len(values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    fraction = position - lower
    return values[lower] * (1.0 - fraction) + values[upper] * fraction


def bootstrap_mean_interval(
    values: Iterable[float],
    *,
    resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> Dict[str, Any]:
    """IC percentil de 95% para a média, com reamostragem determinística."""
    sample = [float(value) for value in values]
    if not sample:
        return {
            "confidence": 0.95, "method": "percentile-bootstrap",
            "resamples": resamples, "seed": seed,
            "low": None, "high": None,
        }
    if resamples < 100:
        raise ValueError("bootstrap resamples must be at least 100")
    rng = random.Random(seed)
    size = len(sample)
    means = sorted(
        sum(rng.choice(sample) for _ in range(size)) / size
        for _ in range(resamples)
    )
    return {
        "confidence": 0.95,
        "method": "percentile-bootstrap",
        "resamples": resamples,
        "seed": seed,
        "low": round(percentile(means, 0.025), 3),
        "high": round(percentile(means, 0.975), 3),
    }


def distribution(
    values: Iterable[float],
    *,
    resamples: int,
    seed: int,
) -> Dict[str, Any]:
    sample = [float(value) for value in values]
    if not sample:
        return {
            "n": 0, "mean": None, "median": None,
            "sample_stddev": None, "min": None, "max": None,
            "mean_ci95": bootstrap_mean_interval(
                [], resamples=resamples, seed=seed
            ),
        }
    return {
        "n": len(sample),
        "mean": round(statistics.mean(sample), 3),
        "median": round(statistics.median(sample), 3),
        "sample_stddev": (
            round(statistics.stdev(sample), 3) if len(sample) > 1 else None
        ),
        "min": round(min(sample), 3),
        "max": round(max(sample), 3),
        "mean_ci95": bootstrap_mean_interval(
            sample, resamples=resamples, seed=seed
        ),
    }


def find_campaign_summary(root: Path, manifest: Dict[str, Any]) -> Optional[Path]:
    configured = ((manifest.get("execution") or {}).get("campaign_summary"))
    if configured:
        candidate = Path(str(configured))
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_file():
            return candidate
    candidates = sorted(root.glob("agentic-live-campaign-*/campaign-summary.json"))
    return candidates[-1] if candidates else None


def _protocol_rows(campaign_manifest: Dict[str, Any]) -> Dict[str, set]:
    observed: Dict[str, set] = defaultdict(set)
    for case in campaign_manifest.get("cases") or []:
        if not isinstance(case, dict) or case.get("scenario") != "ddos":
            continue
        observed[str(case.get("flow"))].add((
            str(case.get("baseline_rate")), str(case.get("attack_rate"))
        ))
    return observed


def evaluate(
    root: Path,
    pilot_report: Optional[Path] = None,
    *,
    bootstrap_resamples: Optional[int] = None,
    bootstrap_seed: Optional[int] = None,
) -> Dict[str, Any]:
    manifest = read_json(root / "replication-manifest.json", {}) or {}
    design = manifest.get("design") or {}
    baseline = manifest.get("baseline") or {}
    runtime = manifest.get("runtime") or {}
    execution = manifest.get("execution") or {}
    episode_definition = design.get("mcda_episode_definition") or {}
    try:
        frozen_episode_definition = {
            "name": str(episode_definition.get("name") or ""),
            "lookback_ms": float(episode_definition.get("lookback_ms", 0)),
            "max_preceding_windows": int(
                episode_definition.get("max_preceding_windows", -1)
            ),
            "future_convergence_ms": float(
                episode_definition.get("future_convergence_ms", 0)
            ),
        }
    except (TypeError, ValueError):
        frozen_episode_definition = {
            "name": "", "lookback_ms": 0.0,
            "max_preceding_windows": -1, "future_convergence_ms": 0.0,
        }
    episode_definition_valid = (
        frozen_episode_definition["name"] == "bounded-episode-window-v2"
        and frozen_episode_definition["lookback_ms"] == 2000.0
        and frozen_episode_definition["max_preceding_windows"] == 1
        and frozen_episode_definition["future_convergence_ms"] == 1000.0
    )
    try:
        repetitions_per_flow = int(design.get("repetitions_per_flow", 3))
        expected_flows = int(design.get("distinct_flows", 3))
        expected_per_scenario = int(design.get("runs_per_scenario", 9))
    except (TypeError, ValueError):
        repetitions_per_flow = expected_flows = expected_per_scenario = -1
    if bootstrap_resamples is None:
        try:
            bootstrap_resamples = int(
                design.get("bootstrap_resamples", DEFAULT_BOOTSTRAP_RESAMPLES)
            )
        except (TypeError, ValueError):
            bootstrap_resamples = DEFAULT_BOOTSTRAP_RESAMPLES
    if bootstrap_seed is None:
        try:
            bootstrap_seed = int(
                design.get("bootstrap_seed", DEFAULT_BOOTSTRAP_SEED)
            )
        except (TypeError, ValueError):
            bootstrap_seed = DEFAULT_BOOTSTRAP_SEED

    if pilot_report is None and baseline.get("pilot_report"):
        pilot_report = Path(str(baseline["pilot_report"]))
        if not pilot_report.is_absolute():
            pilot_report = root / pilot_report
    pilot = read_json(pilot_report, {}) if pilot_report else {}
    campaign_summary_path = find_campaign_summary(root, manifest)
    campaign = read_json(campaign_summary_path, {}) if campaign_summary_path else {}
    campaign_root = campaign_summary_path.parent if campaign_summary_path else None
    campaign_manifest = (
        read_json(campaign_root / "campaign-manifest.json", {})
        if campaign_root else {}
    ) or {}
    rows = campaign.get("cases") or []
    if not isinstance(rows, list):
        rows = []
    ddos_rows = [row for row in rows if row.get("expected_scenario") == "ddos"]
    benign_rows = [
        row for row in rows if row.get("expected_scenario") == "benign"
    ]
    counts = Counter(
        (str(row.get("expected_flow")), str(row.get("expected_scenario")))
        for row in rows
    )
    flows = sorted({flow for flow, _scenario in counts if flow and flow != "None"})
    classifications = Counter(str(row.get("classification")) for row in rows)
    commits = {str(row.get("git_commit")) for row in rows if row.get("git_commit")}
    models = {
        str(row.get("model_sha256")) for row in rows if row.get("model_sha256")
    }
    expected_protocol = {
        flow: tuple(rates) for flow, rates in STANDARD_PROTOCOL.items()
    }
    observed_protocol = _protocol_rows(campaign_manifest)
    protocol_matches = (
        set(observed_protocol) == set(expected_protocol)
        and all(observed_protocol[flow] == {expected_protocol[flow]} for flow in flows)
    )
    balanced = bool(flows) and all(
        counts[(flow, scenario)] == repetitions_per_flow
        for flow in flows for scenario in ("benign", "ddos")
    )
    pilot_aggregate = pilot.get("aggregate") or {}
    campaign_aggregate = campaign.get("aggregate") or {}
    pilot_ready = all(
        pilot_aggregate.get(name) is True
        for name in ("operational_ready", "comparative_ready", "campaign_ready")
    )
    pilot_episode_definition = pilot.get("mcda_episode_definition") or {}
    campaign_episode_definition = (
        campaign.get("mcda_episode_definition") or {}
    )
    campaign_manifest_episode_definition = (
        campaign_manifest.get("mcda_episode_definition") or {}
    )
    baseline_model = str(baseline.get("model_sha256") or "")
    runtime_model = str(runtime.get("model_sha256") or "")
    runtime_commit = str(runtime.get("git_commit") or "")
    campaign_commit = next(iter(commits), "") if len(commits) == 1 else ""
    campaign_model = next(iter(models), "") if len(models) == 1 else ""

    checks = {
        "manifest_valid": (
            manifest.get("schema_version") == 2
            and manifest.get("mode") == "agentic-live-statistical-replication"
            and repetitions_per_flow >= 3
            and expected_flows == 3
            and expected_per_scenario == repetitions_per_flow * expected_flows
            and bootstrap_resamples >= 100
            and episode_definition_valid
        ),
        "pilot_report_present": bool(pilot),
        "pilot_campaign_ready": pilot_ready,
        "pilot_episode_definition_matches": (
            pilot.get("schema_version") == 2
            and pilot_episode_definition == frozen_episode_definition
        ),
        "campaign_report_present": bool(campaign),
        "campaign_process_succeeded": execution.get("campaign_exit_code") == 0,
        "campaign_ready": campaign_aggregate.get("campaign_ready") is True,
        "campaign_episode_definition_matches": (
            campaign.get("schema_version") == 2
            and campaign_manifest.get("schema_version") == 2
            and campaign_episode_definition == frozen_episode_definition
            and campaign_manifest_episode_definition
            == frozen_episode_definition
        ),
        "operational_ready": campaign_aggregate.get("operational_ready") is True,
        "comparative_ready": campaign_aggregate.get("comparative_ready") is True,
        "expected_case_count": len(rows) == expected_per_scenario * 2,
        "expected_scenario_counts": (
            len(ddos_rows) == expected_per_scenario
            and len(benign_rows) == expected_per_scenario
        ),
        "minimum_repetitions_per_flow": (
            len(flows) == expected_flows and balanced
        ),
        "fixed_multiflow_protocol": protocol_matches,
        "all_cases_passed": bool(rows) and all(row.get("passed") is True for row in rows),
        "all_ddos_detected": bool(ddos_rows) and all(
            row.get("classification") == "TP" for row in ddos_rows
        ),
        "all_benign_rejected": bool(benign_rows) and all(
            row.get("classification") == "TN" for row in benign_rows
        ),
        "single_git_commit": len(commits) == 1,
        "single_model_artifact": len(models) == 1,
        "runtime_commit_matches_campaign": (
            bool(runtime_commit) and campaign_commit == runtime_commit
        ),
        "promoted_model_unchanged": (
            bool(baseline_model)
            and baseline_model == runtime_model == campaign_model
        ),
        "tracked_tree_clean_at_start": execution.get("tracked_tree_clean") is True,
        "disk_preflight_passed": execution.get("disk_preflight_passed") is True,
    }
    replication_ready = all(checks.values())

    def numeric(rows_: Iterable[Dict[str, Any]], field: str) -> List[float]:
        return [
            float(row[field]) for row in rows_
            if isinstance(row.get(field), (int, float))
        ]

    latency_fields = (
        "detection_latency_ms",
        "mcda_consensus_latency_ms",
        "agentic_consensus_latency_ms",
        "mcda_max_convergence_latency_ms",
        "mcda_max_early_lead_ms",
    )
    latencies = {
        field: distribution(
            numeric(ddos_rows, field),
            resamples=bootstrap_resamples,
            seed=bootstrap_seed + index,
        )
        for index, field in enumerate(latency_fields)
    }
    per_flow = {}
    for index, flow in enumerate(flows):
        flow_ddos = [row for row in ddos_rows if row.get("expected_flow") == flow]
        flow_benign = [
            row for row in benign_rows if row.get("expected_flow") == flow
        ]
        per_flow[flow] = {
            "ddos_runs": len(flow_ddos),
            "benign_runs": len(flow_benign),
            "TP": sum(row.get("classification") == "TP" for row in flow_ddos),
            "TN": sum(row.get("classification") == "TN" for row in flow_benign),
            "detection_latency_ms": distribution(
                numeric(flow_ddos, "detection_latency_ms"),
                resamples=bootstrap_resamples,
                seed=bootstrap_seed + 100 + index,
            ),
            "agentic_consensus_latency_ms": distribution(
                numeric(flow_ddos, "agentic_consensus_latency_ms"),
                resamples=bootstrap_resamples,
                seed=bootstrap_seed + 200 + index,
            ),
        }

    tp, tn = classifications["TP"], classifications["TN"]
    fp, fn = classifications["FP"], classifications["FN"]
    return {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "agentic-live-statistical-replication",
        "root": str(root),
        "pilot_report": str(pilot_report) if pilot_report else None,
        "campaign_summary": (
            str(campaign_summary_path) if campaign_summary_path else None
        ),
        "design": design,
        "checks": checks,
        "metrics": {
            "TP": tp, "TN": tn, "FP": fp, "FN": fn,
            "precision": rate(tp, tp + fp),
            "recall": rate(tp, tp + fn),
            "specificity": rate(tn, tn + fp),
            "f1": (
                round(2 * tp / (2 * tp + fp + fn), 6)
                if 2 * tp + fp + fn else None
            ),
            "sensitivity_ci95": wilson_interval(tp, tp + fn),
            "specificity_ci95": wilson_interval(tn, tn + fp),
            "agent_to_mcda_exact_run_rate": (
                (campaign.get("metrics") or {}).get(
                    "agent_to_mcda_agreement_rate"
                )
            ),
            "agent_to_mcda_exact_domain_rate": (
                (campaign.get("metrics") or {}).get(
                    "agent_to_mcda_domain_agreement_rate"
                )
            ),
            "mcda_bounded_convergence_rate": (
                (campaign.get("metrics") or {}).get(
                    "mcda_bounded_convergence_rate"
                )
            ),
            "mcda_convergence_direction_distribution": (
                (campaign.get("metrics") or {}).get(
                    "mcda_convergence_direction_distribution"
                ) or {}
            ),
            "winner_distribution": (
                (campaign.get("metrics") or {}).get("winner_distribution") or {}
            ),
            "latencies": latencies,
            "per_flow": per_flow,
        },
        "aggregate": {
            "expected_cases": expected_per_scenario * 2,
            "reported_cases": len(rows),
            "passed_cases": sum(row.get("passed") is True for row in rows),
            "checks_passed": sum(checks.values()),
            "checks_total": len(checks),
            "replication_ready": replication_ready,
        },
    }


def _interval_text(interval: Dict[str, Any]) -> str:
    if interval.get("estimate") is None:
        return "-"
    return (
        f"{interval['estimate']:.3f} "
        f"[{interval['low']:.3f}, {interval['high']:.3f}]"
    )


def markdown(report: Dict[str, Any]) -> str:
    metrics = report["metrics"]
    aggregate = report["aggregate"]
    lines = [
        "| fluxo | benignos | ataques | TN | TP | detecção média ms | agente média ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for flow, row in metrics["per_flow"].items():
        lines.append(
            f"| {flow} | {row['benign_runs']} | {row['ddos_runs']} | "
            f"{row['TN']} | {row['TP']} | "
            f"{row['detection_latency_ms']['mean']} | "
            f"{row['agentic_consensus_latency_ms']['mean']} |"
        )
    lines.extend([
        "",
        (
            f"TP={metrics['TP']} TN={metrics['TN']} FP={metrics['FP']} "
            f"FN={metrics['FN']} precision={metrics['precision']} "
            f"recall={metrics['recall']} specificity={metrics['specificity']} "
            f"f1={metrics['f1']}"
        ),
        (
            "sensibilidade IC95%="
            f"{_interval_text(metrics['sensitivity_ci95'])} "
            "especificidade IC95%="
            f"{_interval_text(metrics['specificity_ci95'])}"
        ),
        (
            "agente-MCDA@autoridade="
            f"{metrics['agent_to_mcda_exact_run_rate']} "
            "agente-MCDA@domínio="
            f"{metrics['agent_to_mcda_exact_domain_rate']} "
            "MCDA-convergência="
            f"{metrics['mcda_bounded_convergence_rate']}"
        ),
        (
            "ordem-MCDA="
            f"{metrics['mcda_convergence_direction_distribution']}"
        ),
        (
            f"replication_ready={str(aggregate['replication_ready']).lower()} "
            f"cases={aggregate['passed_cases']}/{aggregate['expected_cases']} "
            f"checks={aggregate['checks_passed']}/{aggregate['checks_total']}"
        ),
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--pilot-report", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--bootstrap-resamples", type=int,
        default=None, help="reamostragens para o IC da média (padrão: manifesto)",
    )
    args = parser.parse_args()
    if args.bootstrap_resamples is not None and args.bootstrap_resamples < 100:
        parser.error("--bootstrap-resamples deve ser >= 100")
    report = evaluate(
        args.root,
        pilot_report=args.pilot_report,
        bootstrap_resamples=args.bootstrap_resamples,
    )
    output = args.output or (args.root / "replication-summary")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix(".json").write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    table = markdown(report)
    output.with_suffix(".md").write_text(table, encoding="utf-8")
    print(table, end="")
    return 0 if report["aggregate"]["replication_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
