#!/usr/bin/env python3
"""Aggregate an authority-dry-run campaign into a promotion gate."""

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
    if not isinstance(expected_cases, list):
        expected_cases = []
    minimums = manifest.get("minimums") or {}
    if not isinstance(minimums, dict):
        minimums = {}
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
        report = read_json(root / case_id / "authority-summary.json")
        row = {
            "case_id": case_id,
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
                "claim_winner_domains": report.get("claim_winner_domains") or [],
                "would_execute_domains": report.get("would_execute_domains") or [],
                "flowblocker_requests": int(report.get("flowblocker_requests", 0) or 0),
                "drop_rules": len(report.get("drop_rule_files") or []),
                "git_commit": report.get("git_commit"),
                "model_sha256": report.get("model_sha256"),
                "detection_latency_ms": report.get("detection_latency_ms"),
                "agentic_consensus_latency_ms": report.get(
                    "agentic_consensus_latency_ms"
                ),
            })
        else:
            row.update({
                "scenario": None,
                "flow": None,
                "classification": "MISSING",
                "safe": False,
                "failed_checks": ["authority_summary_missing"],
                "authorized_domains": [],
                "claim_winner_domains": [],
                "would_execute_domains": [],
                "flowblocker_requests": 0,
                "drop_rules": 0,
                "git_commit": None,
                "model_sha256": None,
                "detection_latency_ms": None,
                "agentic_consensus_latency_ms": None,
            })
        row["identity_matches_manifest"] = (
            row["scenario"] == row["expected_scenario"]
            and row["flow"] == row["expected_flow"]
        )
        row["passed"] = bool(row["safe"] and row["identity_matches_manifest"])
        row["processes_succeeded"] = (
            row["benchmark_exit_code"] == 0 and row["evaluator_exit_code"] == 0
        )
        row["passed"] = bool(row["passed"] and row["processes_succeeded"])
        rows.append(row)

    unexpected = sorted(
        path.parent.name
        for path in root.glob("*/authority-summary.json")
        if path.parent.name not in expected_ids
    )
    ddos_rows = [row for row in rows if row["expected_scenario"] == "ddos"]
    benign_rows = [row for row in rows if row["expected_scenario"] == "benign"]
    classifications = Counter(str(row["classification"]) for row in rows)
    tp = classifications["TP"]
    tn = classifications["TN"]
    fp = classifications["FP"]
    fn = classifications["FN"]
    precision = rate(tp, tp + fp)
    recall = rate(tp, tp + fn)
    f1 = (
        round(2 * precision * recall / (precision + recall), 6)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    commits = {row["git_commit"] for row in rows if row.get("git_commit")}
    models = {row["model_sha256"] for row in rows if row.get("model_sha256")}
    winner_distribution = Counter(
        winner
        for row in ddos_rows
        for winner in row.get("claim_winner_domains", [])
    )
    distinct_flows = {
        row["expected_flow"] for row in rows if row.get("expected_flow")
    }
    checks = {
        "manifest_valid": bool(expected_cases) and len(expected_ids) == len(expected_cases),
        "minimums_valid": minimums_valid,
        "all_cases_reported": len(rows) == len(expected_cases)
        and all(row["report_present"] for row in rows),
        "no_unexpected_reports": not unexpected,
        "all_case_gates_safe": bool(rows) and all(row["passed"] for row in rows),
        "all_case_processes_succeeded": bool(rows) and all(
            row["processes_succeeded"] for row in rows
        ),
        "minimum_ddos_runs": len(ddos_rows) >= minimum_ddos,
        "minimum_benign_runs": len(benign_rows) >= minimum_benign,
        "minimum_distinct_flows": (
            len(distinct_flows) >= minimum_flows
        ),
        "all_ddos_detected_and_authorized": bool(ddos_rows) and all(
            row["classification"] == "TP"
            and len(row["authorized_domains"]) >= 2
            and len(row["claim_winner_domains"]) == 1
            and row["would_execute_domains"] == row["claim_winner_domains"]
            for row in ddos_rows
        ),
        "all_benign_rejected": bool(benign_rows) and all(
            row["classification"] == "TN"
            and not row["authorized_domains"]
            and not row["claim_winner_domains"]
            and not row["would_execute_domains"]
            for row in benign_rows
        ),
        "zero_agentic_actuation": all(
            "agent_never_actuated" not in row["failed_checks"] for row in rows
        ),
        "zero_flowblocker_requests": sum(
            row["flowblocker_requests"] for row in rows
        ) == 0,
        "zero_drop_rules": sum(row["drop_rules"] for row in rows) == 0,
        "single_git_commit": len(commits) == 1,
        "single_model_artifact": len(models) == 1,
    }
    detection_values = [
        float(row["detection_latency_ms"]) for row in ddos_rows
        if isinstance(row.get("detection_latency_ms"), (int, float))
    ]
    consensus_values = [
        float(row["agentic_consensus_latency_ms"]) for row in ddos_rows
        if isinstance(row.get("agentic_consensus_latency_ms"), (int, float))
    ]
    promotion_ready = all(checks.values())
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "authority-dry-run-promotion-campaign",
        "root": str(root),
        "checks": checks,
        "cases": rows,
        "unexpected_reports": unexpected,
        "metrics": {
            "TP": tp,
            "TN": tn,
            "FP": fp,
            "FN": fn,
            "precision": precision,
            "recall": recall,
            "specificity": rate(tn, tn + fp),
            "f1": f1,
            "detection_latency_ms": distribution(detection_values),
            "agentic_consensus_latency_ms": distribution(consensus_values),
            "claim_winner_distribution": dict(sorted(winner_distribution.items())),
        },
        "aggregate": {
            "expected": len(expected_cases),
            "evaluated": sum(row["report_present"] for row in rows),
            "passed": sum(row["passed"] for row in rows),
            "failed": sum(not row["passed"] for row in rows),
            "checks_passed": sum(checks.values()),
            "checks_total": len(checks),
            "promotion_ready": promotion_ready,
        },
    }


def markdown(report: Dict[str, Any]) -> str:
    lines = [
        "| caso | cenário | fluxo | classe | gate | autorizados | vencedor | requests | DROP |",
        "| --- | --- | --- | --- | --- | ---: | --- | ---: | ---: |",
    ]
    for row in report["cases"]:
        lines.append(
            f"| {row['case_id']} | {row['expected_scenario']} | "
            f"{row['expected_flow']} | {row['classification']} | "
            f"{'PASS' if row['passed'] else 'FAIL'} | "
            f"{len(row['authorized_domains'])} | "
            f"{','.join(row['claim_winner_domains']) or '-'} | "
            f"{row['flowblocker_requests']} | {row['drop_rules']} |"
        )
    aggregate = report["aggregate"]
    metrics = report["metrics"]
    lines.extend([
        "",
        (
            f"TP={metrics['TP']} TN={metrics['TN']} FP={metrics['FP']} "
            f"FN={metrics['FN']} precision={metrics['precision']} "
            f"recall={metrics['recall']} specificity={metrics['specificity']} "
            f"f1={metrics['f1']}"
        ),
        (
            f"promotion_ready={str(aggregate['promotion_ready']).lower()} "
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
    return 0 if report["aggregate"]["promotion_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
