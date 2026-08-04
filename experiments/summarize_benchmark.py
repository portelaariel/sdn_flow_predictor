#!/usr/bin/env python3
"""Resume uma ou mais execuções do benchmark FlowPredictor."""

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def read_timestamp(path: Path) -> Optional[int]:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def read_timeline(path: Path) -> Iterable[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def packet_loss_percent(path: Path) -> Optional[float]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    matches = re.findall(r"([0-9]+(?:\.[0-9]+)?)% packet loss", text)
    return float(matches[-1]) if matches else None


def iperf_bps(path: Path) -> Optional[float]:
    payload = read_json(path, {})
    end = payload.get("end", {}) if isinstance(payload, dict) else {}
    for key in ("sum", "sum_received", "sum_sent"):
        value = end.get(key, {}).get("bits_per_second")
        if isinstance(value, (int, float)):
            return float(value)
    return None


def summarize_run(run_dir: Path) -> Dict[str, Any]:
    metadata = read_json(run_dir / "metadata.json", {}) or {}
    workload = read_json(run_dir / "workload_status.json", {}) or {}
    flow = metadata.get("flow", "")
    expected_attack = metadata.get("scenario") == "ddos"
    collaborative = str(metadata.get("mode", "")).startswith("collaborative-")
    attack_start_ns = read_timestamp(run_dir / "attack_start_ns.txt")
    detection_ns = None
    decisions = set()
    all_decisions = set()
    max_score = None
    confirming_domains = set()
    action_domains = set()
    baseline_action_domains = set()
    coordinator = None
    claimed_ns = None
    mitigation_attempted = False
    mitigation_executed = False
    mitigation_reason = None
    endpoint_errors = 0
    seen_anomalies = set()
    seen_spikes = set()
    seen_drops = set()
    baseline_spikes = set()
    attack_spikes = set()
    baseline_mitigate = False

    for row in read_timeline(run_dir / "timeline.ndjson"):
        if row.get("error"):
            endpoint_errors += 1
            continue
        cid = row.get("status", {}).get("cid") or row.get("port")
        for anomaly in row.get("anomalies", []):
            anomaly_id = anomaly.get("anomaly_id")
            if anomaly_id:
                seen_anomalies.add(anomaly_id)
            timestamp = anomaly.get("ts_detect_ns")
            in_attack = bool(
                expected_attack and attack_start_ns is not None
                and isinstance(timestamp, int) and timestamp >= attack_start_ns
            )
            if anomaly.get("kind") == "THROUGHPUT_SPIKE":
                if anomaly_id:
                    seen_spikes.add(anomaly_id)
                    (attack_spikes if in_attack else baseline_spikes).add(anomaly_id)
                if in_attack and isinstance(timestamp, int):
                    detection_ns = timestamp if detection_ns is None else min(detection_ns, timestamp)
            elif anomaly.get("kind") == "THROUGHPUT_DROP" and anomaly_id:
                seen_drops.add(anomaly_id)
            mitigation = anomaly.get("mitigation", {})
            if mitigation.get("attempted"):
                if in_attack or not expected_attack:
                    action_domains.add(str(cid))
                    mitigation_attempted = True
                else:
                    baseline_action_domains.add(str(cid))
            if mitigation.get("executed"):
                if in_attack or not expected_attack:
                    mitigation_executed = True
                    mitigation_reason = mitigation.get("reason")
            elif ((in_attack or not expected_attack)
                  and mitigation.get("reason") and mitigation_reason is None):
                mitigation_reason = mitigation.get("reason")

        for decision in row.get("collaboration", {}).get("decisions", []):
            if flow and decision.get("flow") != flow:
                continue
            state = decision.get("decision")
            if state:
                all_decisions.add(state)
            claim = decision.get("claim") or {}
            decision_ts = (claim.get("claimed_ns")
                           or decision.get("evaluated_ns")
                           or row.get("sampled_ns"))
            in_attack = bool(
                expected_attack and attack_start_ns is not None
                and isinstance(decision_ts, int) and decision_ts >= attack_start_ns
            )
            if state and (in_attack or not expected_attack):
                decisions.add(state)
            if state == "MITIGATE" and expected_attack and not in_attack:
                baseline_mitigate = True
            if expected_attack and not in_attack:
                continue
            score = decision.get("score")
            if isinstance(score, (int, float)):
                max_score = score if max_score is None else max(max_score, score)
            confirming_domains.update(decision.get("confirming_domains", []))
            if claim.get("coordinator"):
                coordinator = claim["coordinator"]
            if isinstance(claim.get("claimed_ns"), int):
                claimed_ns = (claim["claimed_ns"] if claimed_ns is None
                              else min(claimed_ns, claim["claimed_ns"]))
            mitigation = decision.get("mitigation") or {}
            if mitigation.get("attempted"):
                mitigation_attempted = True
                action_domains.add(str(cid))
            if mitigation.get("executed"):
                mitigation_executed = True
                mitigation_reason = mitigation.get("reason")
            elif mitigation.get("reason") and mitigation_reason is None:
                mitigation_reason = mitigation["reason"]

    detection_latency_ms = (
        round((detection_ns - attack_start_ns) / 1e6, 3)
        if detection_ns is not None and attack_start_ns is not None else None
    )
    consensus_latency_ms = (
        round((claimed_ns - detection_ns) / 1e6, 3)
        if claimed_ns is not None and detection_ns is not None else None
    )
    ping_before_loss = packet_loss_percent(run_dir / "ping_before.txt")
    ping_after_loss = packet_loss_percent(run_dir / "ping_after.txt")
    baseline_bps = iperf_bps(run_dir / "baseline.json")
    attack_bps = iperf_bps(run_dir / "attack.json")
    detected_attack = (
        ("MITIGATE" in decisions if collaborative else bool(attack_spikes))
        if expected_attack
        else ("MITIGATE" in decisions if collaborative else bool(seen_spikes))
    )
    invalid_reasons = []
    if workload.get("valid") is not True:
        invalid_reasons.append(workload.get("reason") or "workload não validado")
    if ping_before_loss is None:
        invalid_reasons.append("ping inicial sem métrica")
    if ping_after_loss is None:
        invalid_reasons.append("ping final sem métrica")
    if baseline_bps is None:
        invalid_reasons.append("baseline sem vazão medida")
    if expected_attack and attack_bps is None:
        invalid_reasons.append("ataque sem vazão medida")
    if expected_attack and attack_start_ns is None:
        invalid_reasons.append("início do ataque sem timestamp")
    if endpoint_errors:
        invalid_reasons.append(f"{endpoint_errors} erro(s) nas APIs dos preditores")
    measurement_valid = not invalid_reasons
    contamination_reasons = []
    if expected_attack and baseline_spikes:
        contamination_reasons.append(
            f"{len(baseline_spikes)} spike(s) detectado(s) durante o baseline"
        )
    if expected_attack and baseline_action_domains:
        contamination_reasons.append(
            "mitigação acionada durante o baseline por "
            f"{len(baseline_action_domains)} domínio(s)"
        )
    if expected_attack and baseline_mitigate:
        contamination_reasons.append("decisão MITIGATE anterior ao ataque")
    classification = (
        "INVALID" if not measurement_valid
        else "CONTAMINATED" if contamination_reasons
        else "TP" if expected_attack and detected_attack
        else "FN" if expected_attack
        else "FP" if detected_attack
        else "TN"
    )
    return {
        "run": run_dir.name,
        "mode": metadata.get("mode"),
        "scenario": metadata.get("scenario"),
        "flow": flow,
        "decisions": sorted(decisions),
        "all_decisions": sorted(all_decisions),
        "max_score": max_score,
        "confirming_domains": sorted(confirming_domains),
        "coordinator": coordinator,
        "action_domains": sorted(action_domains),
        "baseline_action_domains": sorted(baseline_action_domains),
        "mitigation_attempted": mitigation_attempted,
        "mitigation_executed": mitigation_executed,
        "mitigation_reason": mitigation_reason,
        "anomalies": len(seen_anomalies),
        "spike_anomalies": len(seen_spikes),
        "drop_anomalies": len(seen_drops),
        "baseline_spike_anomalies": len(baseline_spikes),
        "attack_spike_anomalies": len(attack_spikes),
        "detection_latency_ms": detection_latency_ms,
        "consensus_latency_ms": consensus_latency_ms,
        "ping_before_loss_percent": ping_before_loss,
        "ping_after_loss_percent": ping_after_loss,
        "baseline_bps": baseline_bps,
        "attack_bps": attack_bps,
        "endpoint_errors": endpoint_errors,
        "expected_attack": expected_attack,
        "detected_attack": detected_attack,
        "measurement_valid": measurement_valid,
        "invalid_reasons": invalid_reasons,
        "contamination_reasons": contamination_reasons,
        "classification": classification,
    }


def aggregate_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = {name: sum(row.get("classification") == name for row in rows)
              for name in ("TP", "TN", "FP", "FN", "CONTAMINATED", "INVALID")}
    precision_denominator = counts["TP"] + counts["FP"]
    recall_denominator = counts["TP"] + counts["FN"]
    precision = (counts["TP"] / precision_denominator
                 if precision_denominator else None)
    recall = counts["TP"] / recall_denominator if recall_denominator else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision is not None and recall is not None and precision + recall else None)
    return {
        **counts,
        "precision": (None if precision is None else round(precision, 6)),
        "recall": (None if recall is None else round(recall, 6)),
        "f1": (None if f1 is None else round(f1, 6)),
    }


def markdown_table(rows: List[Dict[str, Any]]) -> str:
    headers = [
        "run", "mode", "scenario", "classe", "decisão", "score", "confirmações",
        "coordenador", "ações", "executada", "detecção ms", "consenso ms",
        "spikes base", "perda ping %", "erros API", "problema",
    ]
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        values = [
            row.get("run"), row.get("mode"), row.get("scenario"),
            row.get("classification"),
            ",".join(row.get("decisions", [])) or "-",
            row.get("max_score"), len(row.get("confirming_domains", [])),
            row.get("coordinator") or "-", len(row.get("action_domains", [])),
            row.get("mitigation_executed"), row.get("detection_latency_ms"),
            row.get("consensus_latency_ms"), row.get("baseline_spike_anomalies"),
            row.get("ping_after_loss_percent"), row.get("endpoint_errors"),
            "; ".join(row.get("invalid_reasons", [])
                      + row.get("contamination_reasons", [])) or "-",
        ]
        lines.append("| " + " | ".join("-" if value is None else str(value)
                                        for value in values) + " |")
    metrics = aggregate_metrics(rows)
    lines.extend([
        "",
        (f"TP={metrics['TP']} TN={metrics['TN']} FP={metrics['FP']} "
         f"FN={metrics['FN']} CONTAMINATED={metrics['CONTAMINATED']} "
         f"INVALID={metrics['INVALID']} "
         f"precision={metrics['precision']} "
         f"recall={metrics['recall']} f1={metrics['f1']}"),
    ])
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", type=Path, nargs="+", help="diretórios de resultado")
    parser.add_argument("--output", type=Path, help="prefixo opcional para summary.json/.md")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    rows = [summarize_run(path) for path in args.runs]
    payload = json.dumps(
        {"runs": rows, "aggregate": aggregate_metrics(rows)},
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    ) + "\n"
    table = markdown_table(rows)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.with_suffix(".json").write_text(payload, encoding="utf-8")
        args.output.with_suffix(".md").write_text(table, encoding="utf-8")
    print(table, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
