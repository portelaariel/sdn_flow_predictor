#!/usr/bin/env python3
"""Agrega uma campanha authority-live multi-fluxo em um gate experimental."""

import argparse
import json
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return default


def read_exit_code(path: Path) -> Optional[int]:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, TypeError, ValueError):
        return None


def rate(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 6) if denominator else None


def distribution(values: List[float]) -> Dict[str, Any]:
    if not values:
        return {
            "n": 0, "mean": None, "median": None,
            "sample_stddev": None, "min": None, "max": None,
        }
    return {
        "n": len(values),
        "mean": round(statistics.mean(values), 3),
        "median": round(statistics.median(values), 3),
        "sample_stddev": (
            round(statistics.stdev(values), 3) if len(values) > 1 else None
        ),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


def evaluate(root: Path) -> Dict[str, Any]:
    manifest = read_json(root / "campaign-manifest.json", {}) or {}
    expected_cases = manifest.get("cases") or []
    minimums = manifest.get("minimums") or {}
    prerequisites = manifest.get("prerequisites") or {}
    try:
        expected_convergence_window_ms = float(
            manifest.get("mcda_convergence_window_ms", 1000.0)
        )
    except (TypeError, ValueError):
        expected_convergence_window_ms = 0.0
    if not isinstance(expected_cases, list):
        expected_cases = []
    if not isinstance(minimums, dict):
        minimums = {}
    if not isinstance(prerequisites, dict):
        prerequisites = {}
    try:
        minimum_ddos = int(minimums.get("ddos", 1))
        minimum_benign = int(minimums.get("benign", 1))
        minimum_flows = int(minimums.get("distinct_flows", 1))
        minimums_valid = min(minimum_ddos, minimum_benign, minimum_flows) >= 1
    except (TypeError, ValueError):
        minimum_ddos = minimum_benign = minimum_flows = len(expected_cases) + 1
        minimums_valid = False

    expected_ids = {
        str(case.get("case_id")) for case in expected_cases
        if isinstance(case, dict) and case.get("case_id")
    }
    rows = []
    for case in expected_cases:
        if not isinstance(case, dict) or not case.get("case_id"):
            continue
        case_id = str(case["case_id"])
        report = read_json(root / case_id / "agentic-live-summary.json")
        row = {
            "case_id": case_id,
            "pair_id": case.get("pair_id"),
            "expected_scenario": case.get("scenario"),
            "expected_flow": case.get("flow"),
            "report_present": isinstance(report, dict),
            "benchmark_exit_code": read_exit_code(
                root / case_id / "benchmark-exit-code.txt"
            ),
            "evaluator_exit_code": read_exit_code(
                root / case_id / "evaluator-exit-code.txt"
            ),
        }
        if isinstance(report, dict):
            row.update({
                "scenario": report.get("scenario"),
                "flow": report.get("flow"),
                "classification": report.get("classification"),
                "safe": (report.get("aggregate") or {}).get("safe") is True,
                "failed_checks": sorted(
                    name for name, passed in (report.get("checks") or {}).items()
                    if passed is not True
                ),
                "authorized_domains": report.get("authorized_domains") or [],
                "winner_domains": report.get("winner_domains") or [],
                "execution_domains": report.get("execution_domains") or [],
                "flowblocker_requests": int(
                    report.get("flowblocker_requests", 0) or 0
                ),
                "drop_rules": len(report.get("drop_rule_files") or []),
                "git_commit": report.get("git_commit"),
                "model_sha256": report.get("model_sha256"),
                "detection_latency_ms": report.get("detection_latency_ms"),
                "mcda_consensus_latency_ms": report.get(
                    "mcda_consensus_latency_ms"
                ),
                "agentic_consensus_latency_ms": report.get(
                    "agentic_consensus_latency_ms"
                ),
                "ping_after_loss_percent": report.get(
                    "ping_after_loss_percent"
                ),
                "agentic_matches_mcda": report.get("agentic_matches_mcda"),
                "agentic_mcda_comparisons": (
                    report.get("agentic_mcda_comparisons") or {}
                ),
                "mcda_converged_within_bound": report.get(
                    "mcda_converged_within_bound"
                ),
                "mcda_convergence_window_ms": report.get(
                    "mcda_convergence_window_ms"
                ),
                "mcda_max_convergence_latency_ms": report.get(
                    "mcda_max_convergence_latency_ms"
                ),
            })
        else:
            row.update({
                "scenario": None, "flow": None, "classification": "MISSING",
                "safe": False, "failed_checks": ["live_summary_missing"],
                "authorized_domains": [], "winner_domains": [],
                "execution_domains": [], "flowblocker_requests": 0,
                "drop_rules": 0, "git_commit": None, "model_sha256": None,
                "detection_latency_ms": None,
                "mcda_consensus_latency_ms": None,
                "agentic_consensus_latency_ms": None,
                "ping_after_loss_percent": None,
                "agentic_matches_mcda": None,
                "agentic_mcda_comparisons": {},
                "mcda_converged_within_bound": None,
                "mcda_convergence_window_ms": None,
                "mcda_max_convergence_latency_ms": None,
            })
        row["identity_matches_manifest"] = (
            row["scenario"] == row["expected_scenario"]
            and row["flow"] == row["expected_flow"]
        )
        row["processes_succeeded"] = (
            row["benchmark_exit_code"] == 0 and row["evaluator_exit_code"] == 0
        )
        row["passed"] = bool(
            row["safe"] and row["identity_matches_manifest"]
            and row["processes_succeeded"]
        )
        rows.append(row)

    unexpected = sorted(
        path.parent.name
        for path in root.glob("*/agentic-live-summary.json")
        if path.parent.name not in expected_ids
    )
    ddos_rows = [row for row in rows if row["expected_scenario"] == "ddos"]
    benign_rows = [row for row in rows if row["expected_scenario"] == "benign"]
    benign_by_pair = {row.get("pair_id"): row for row in benign_rows}
    classifications = Counter(str(row["classification"]) for row in rows)
    tp, tn = classifications["TP"], classifications["TN"]
    fp, fn = classifications["FP"], classifications["FN"]
    precision, recall = rate(tp, tp + fp), rate(tp, tp + fn)
    f1 = (
        round(2 * precision * recall / (precision + recall), 6)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    commits = {row["git_commit"] for row in rows if row.get("git_commit")}
    models = {row["model_sha256"] for row in rows if row.get("model_sha256")}
    distinct_flows = {row["expected_flow"] for row in rows if row.get("expected_flow")}
    winner_distribution = Counter(
        winner for row in ddos_rows for winner in row["winner_domains"]
    )
    prerequisites_valid = (
        prerequisites.get("promotion_ready") is True
        and prerequisites.get("canary_ready") is True
        and bool(prerequisites.get("promotion_commit"))
        and bool(prerequisites.get("model_sha256"))
    )
    checks = {
        "manifest_valid": bool(expected_cases) and len(expected_ids) == len(expected_cases),
        "prerequisites_valid": prerequisites_valid,
        "convergence_window_valid": expected_convergence_window_ms > 0,
        "minimums_valid": minimums_valid,
        "all_cases_reported": (
            len(rows) == len(expected_cases)
            and all(row["report_present"] for row in rows)
        ),
        "no_unexpected_reports": not unexpected,
        "all_case_gates_safe": bool(rows) and all(row["passed"] for row in rows),
        "all_case_processes_succeeded": bool(rows) and all(
            row["processes_succeeded"] for row in rows
        ),
        "minimum_ddos_runs": len(ddos_rows) >= minimum_ddos,
        "minimum_benign_runs": len(benign_rows) >= minimum_benign,
        "minimum_distinct_flows": len(distinct_flows) >= minimum_flows,
        "paired_negative_controls": bool(ddos_rows) and all(
            benign_by_pair.get(row.get("pair_id"), {}).get("passed") is True
            for row in ddos_rows
        ),
        "all_ddos_safely_mitigated": bool(ddos_rows) and all(
            row["classification"] == "TP"
            and len(row["authorized_domains"]) >= 2
            and len(row["winner_domains"]) == 1
            and row["execution_domains"] == row["winner_domains"]
            and row["flowblocker_requests"] == 1
            and row["drop_rules"] >= 1
            for row in ddos_rows
        ),
        "all_benign_rejected": bool(benign_rows) and all(
            row["classification"] == "TN"
            and not row["authorized_domains"]
            and not row["winner_domains"]
            and not row["execution_domains"]
            and row["flowblocker_requests"] == 0
            and row["drop_rules"] == 0
            for row in benign_rows
        ),
        "all_mcda_observers_converged": bool(ddos_rows) and all(
            row["mcda_converged_within_bound"] is True for row in ddos_rows
        ),
        "single_mcda_convergence_window": bool(ddos_rows) and all(
            row["mcda_convergence_window_ms"]
            == expected_convergence_window_ms
            for row in ddos_rows
        ),
        "one_flowblocker_request_per_attack": sum(
            row["flowblocker_requests"] for row in rows
        ) == len(ddos_rows),
        "zero_benign_drop_rules": sum(row["drop_rules"] for row in benign_rows) == 0,
        "single_git_commit": len(commits) == 1,
        "single_model_artifact": len(models) == 1,
    }

    def values(field: str) -> List[float]:
        return [
            float(row[field]) for row in ddos_rows
            if isinstance(row.get(field), (int, float))
        ]

    operational_checks = {
        name: passed for name, passed in checks.items()
        if name != "all_mcda_observers_converged"
    }
    operational_ready = all(operational_checks.values())
    comparative_ready = checks["all_mcda_observers_converged"]
    campaign_ready = operational_ready and comparative_ready
    comparison_rows = [
        comparison
        for row in ddos_rows
        for comparison in row["agentic_mcda_comparisons"].values()
        if isinstance(comparison, dict)
        and isinstance(comparison.get("matches"), bool)
    ]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "authority-live-multiflow-campaign",
        "root": str(root),
        "prerequisites": prerequisites,
        "mcda_convergence_window_ms": expected_convergence_window_ms,
        "checks": checks,
        "cases": rows,
        "unexpected_reports": unexpected,
        "metrics": {
            "TP": tp, "TN": tn, "FP": fp, "FN": fn,
            "precision": precision, "recall": recall,
            "specificity": rate(tn, tn + fp), "f1": f1,
            "agent_to_mcda_agreement_rate": rate(
                sum(row["agentic_matches_mcda"] is True for row in ddos_rows),
                len(ddos_rows),
            ),
            "agent_to_mcda_domain_agreement_rate": rate(
                sum(item["matches"] is True for item in comparison_rows),
                len(comparison_rows),
            ),
            "mcda_bounded_convergence_rate": rate(
                sum(
                    row["mcda_converged_within_bound"] is True
                    for row in ddos_rows
                ),
                len(ddos_rows),
            ),
            "detection_latency_ms": distribution(values("detection_latency_ms")),
            "mcda_consensus_latency_ms": distribution(
                values("mcda_consensus_latency_ms")
            ),
            "agentic_consensus_latency_ms": distribution(
                values("agentic_consensus_latency_ms")
            ),
            "mcda_convergence_latency_ms": distribution(
                values("mcda_max_convergence_latency_ms")
            ),
            "ping_after_loss_percent": distribution(
                values("ping_after_loss_percent")
            ),
            "winner_distribution": dict(sorted(winner_distribution.items())),
        },
        "aggregate": {
            "expected": len(expected_cases),
            "evaluated": sum(row["report_present"] for row in rows),
            "passed": sum(row["passed"] for row in rows),
            "failed": sum(not row["passed"] for row in rows),
            "checks_passed": sum(checks.values()),
            "checks_total": len(checks),
            "operational_ready": operational_ready,
            "comparative_ready": comparative_ready,
            "campaign_ready": campaign_ready,
        },
    }


def markdown(report: Dict[str, Any]) -> str:
    lines = [
        "| caso | cenário | fluxo | classe | gate | executor | requests | DROP | agente=MCDA@autoridade | MCDA convergiu |",
        "| --- | --- | --- | --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    for row in report["cases"]:
        lines.append(
            f"| {row['case_id']} | {row['expected_scenario']} | "
            f"{row['expected_flow']} | {row['classification']} | "
            f"{'PASS' if row['passed'] else 'FAIL'} | "
            f"{','.join(row['execution_domains']) or '-'} | "
            f"{row['flowblocker_requests']} | {row['drop_rules']} | "
            f"{row['agentic_matches_mcda'] if row['expected_scenario'] == 'ddos' else '-'} | "
            f"{row['mcda_converged_within_bound'] if row['expected_scenario'] == 'ddos' else '-'} |"
        )
    metrics, aggregate = report["metrics"], report["aggregate"]
    lines.extend([
        "",
        (
            f"TP={metrics['TP']} TN={metrics['TN']} FP={metrics['FP']} "
            f"FN={metrics['FN']} precision={metrics['precision']} "
            f"recall={metrics['recall']} specificity={metrics['specificity']} "
            f"f1={metrics['f1']} agente-MCDA@autoridade="
            f"{metrics['agent_to_mcda_agreement_rate']} "
            f"agente-MCDA@domínio="
            f"{metrics['agent_to_mcda_domain_agreement_rate']} "
            f"MCDA-convergência={metrics['mcda_bounded_convergence_rate']}"
        ),
        (
            f"campaign_ready={str(aggregate['campaign_ready']).lower()} "
            f"operational_ready={str(aggregate['operational_ready']).lower()} "
            f"comparative_ready={str(aggregate['comparative_ready']).lower()} "
            f"cases={aggregate['passed']}/{aggregate['expected']} "
            f"checks={aggregate['checks_passed']}/{aggregate['checks_total']}"
        ),
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.root)
    output = args.output or (args.root / "campaign-summary")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix(".json").write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    table = markdown(report)
    output.with_suffix(".md").write_text(table, encoding="utf-8")
    print(table, end="")
    return 0 if report["aggregate"]["campaign_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
