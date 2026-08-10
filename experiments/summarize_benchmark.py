#!/usr/bin/env python3
"""Resume uma ou mais execuções do benchmark FlowPredictor."""

import argparse
import json
import re
import statistics
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
    run_started_ns = metadata.get("started_ns")
    if not isinstance(run_started_ns, int):
        run_started_ns = None
    collaborative = (
        str(metadata.get("mode", "")).startswith("collaborative-")
        or metadata.get("mode") == "agentic-live"
    )
    live_mode = metadata.get("mode") in {"collaborative-live", "agentic-live"}
    agentic_enabled = (
        metadata.get("agentic_enabled") is True
        or metadata.get("agentic_shadow") is True
    )
    agentic_mode = str(metadata.get(
        "agentic_mode", "shadow" if metadata.get("agentic_shadow") else "disabled"
    ))
    attack_disrupted = workload.get("attack_disrupted") is True
    attack_start_ns = read_timestamp(run_dir / "attack_start_ns.txt")
    detection_ns = None
    decisions = set()
    all_decisions = set()
    max_score = None
    confirming_domains = set()
    action_domains = set()
    baseline_action_domains = set()
    coordinator = None
    mcda_claimed_ns = None
    mcda_mitigate_ns = None
    agentic_claimed_ns = None
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
    agentic_decisions = set()
    agentic_mitigate_votes = set()
    agentic_comparisons: Dict[str, tuple] = {}
    agentic_active_domains = set()
    agentic_events: Dict[str, Dict[str, Any]] = {}
    agentic_waiting_by_domain: Dict[str, int] = {}
    agentic_expired_by_domain: Dict[str, int] = {}
    agentic_authorized_domains = set()
    agentic_would_execute_domains = set()
    agentic_claim_winners = set()
    agentic_actuation_violation = False
    initial_waiting_by_domain: Dict[str, int] = {}
    initial_expired_by_domain: Dict[str, int] = {}

    for path in run_dir.glob("initial-agent-*.json"):
        initial = read_json(path, {}) or {}
        initial_cid = initial.get("cid")
        if initial_cid is None:
            continue
        initial_cid = str(initial_cid)
        initial_waiting_by_domain[initial_cid] = int(
            initial.get("waiting_events", 0)
        )
        initial_expired_by_domain[initial_cid] = int(
            initial.get("expired_proposals", 0)
        )

    for row in read_timeline(run_dir / "timeline.ndjson"):
        if row.get("error"):
            endpoint_errors += 1
            continue
        cid = row.get("status", {}).get("cid") or row.get("port")
        agentic = row.get("agentic", {})
        if not isinstance(agentic, dict):
            agentic = {}
        expected_authoritative = agentic_mode in {
            "authority-dry-run", "authority-live"
        }
        expected_actuation = agentic_mode == "authority-live"
        if (agentic.get("requested") is True
                and agentic.get("active") is True
                and agentic.get("mode") == agentic_mode
                and agentic.get("authoritative") is expected_authoritative
                and agentic.get("actuation_enabled", False) is expected_actuation):
            agent_cid = agentic.get("cid") or cid
            if agent_cid is not None:
                agentic_active_domains.add(str(agent_cid))
                agentic_waiting_by_domain[str(agent_cid)] = max(
                    agentic_waiting_by_domain.get(str(agent_cid), 0),
                    int(agentic.get("waiting_events", 0)),
                )
                agentic_expired_by_domain[str(agent_cid)] = max(
                    agentic_expired_by_domain.get(str(agent_cid), 0),
                    int(agentic.get("expired_proposals", 0)),
                )
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
                    if mitigation.get("reason"):
                        mitigation_reason = mitigation.get("reason")
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
            if state == "MITIGATE" and isinstance(decision_ts, int):
                mcda_mitigate_ns = (
                    decision_ts if mcda_mitigate_ns is None
                    else min(mcda_mitigate_ns, decision_ts)
                )
            if claim.get("coordinator"):
                coordinator = claim["coordinator"]
            if isinstance(claim.get("claimed_ns"), int):
                mcda_claimed_ns = (
                    claim["claimed_ns"] if mcda_claimed_ns is None
                    else min(mcda_claimed_ns, claim["claimed_ns"])
                )
            mitigation = decision.get("mitigation") or {}
            if mitigation.get("attempted"):
                mitigation_attempted = True
                action_domains.add(str(cid))
                if mitigation.get("reason"):
                    mitigation_reason = mitigation.get("reason")
            if mitigation.get("executed"):
                mitigation_executed = True
                mitigation_reason = mitigation.get("reason")
            elif mitigation.get("reason") and mitigation_reason is None:
                mitigation_reason = mitigation["reason"]

        agent_cid = str(agentic.get("cid") or cid)
        current_decisions = agentic.get("decisions", [])
        decision_events = agentic.get("decision_events", [])
        if not isinstance(current_decisions, list):
            current_decisions = []
        if not isinstance(decision_events, list):
            decision_events = []
        for decision in current_decisions + decision_events:
            if not isinstance(decision, dict):
                continue
            if flow and decision.get("flow") != flow:
                continue
            transition_ns = decision.get(
                "state_entered_ns", decision.get("evaluated_ns")
            )
            if (run_started_ns is not None and isinstance(transition_ns, int)
                    and transition_ns < run_started_ns):
                continue
            state = decision.get("decision")
            if state:
                agentic_decisions.add(state)
            agentic_mitigate_votes.update(decision.get("mitigate_votes", []))
            authority = decision.get("authority") or {}
            if authority.get("authorized"):
                agentic_authorized_domains.add(agent_cid)
            authority_claim = authority.get("claim") or {}
            if authority_claim.get("won"):
                agentic_claim_winners.add(agent_cid)
                if authority_claim.get("coordinator"):
                    coordinator = authority_claim["coordinator"]
                if isinstance(authority_claim.get("claimed_ns"), int):
                    agentic_claimed_ns = (
                        authority_claim["claimed_ns"]
                        if agentic_claimed_ns is None
                        else min(agentic_claimed_ns,
                                 authority_claim["claimed_ns"])
                    )
            execution = decision.get("execution") or {}
            if execution.get("would_execute"):
                agentic_would_execute_domains.add(agent_cid)
            if execution.get("attempted") or execution.get("executed"):
                agentic_actuation_violation = True
                if agentic_mode == "authority-live":
                    action_domains.add(agent_cid)
                    mitigation_attempted = (
                        mitigation_attempted or bool(execution.get("attempted"))
                    )
                    mitigation_executed = (
                        mitigation_executed or bool(execution.get("executed"))
                    )
                    if execution.get("reason"):
                        mitigation_reason = execution["reason"]
            comparison = decision.get("legacy_comparison", {})
            if comparison.get("available") and isinstance(comparison.get("matches"), bool):
                evaluated_ns = int(decision.get("evaluated_ns", row.get("sampled_ns", 0)))
                previous = agentic_comparisons.get(agent_cid)
                if previous is None or evaluated_ns >= previous[0]:
                    agentic_comparisons[agent_cid] = (
                        evaluated_ns, comparison["matches"]
                    )
            event_id = decision.get("event_id")
            if not event_id:
                event_id = (
                    f"{agent_cid}:{decision.get('flow')}:{state}:"
                    f"{decision.get('state_entered_ns', decision.get('evaluated_ns'))}:"
                    f"{','.join(str(v) for v in decision.get('window_ids', []))}"
                )
            agentic_events.setdefault(str(event_id), decision)

    detection_latency_ms = (
        round((detection_ns - attack_start_ns) / 1e6, 3)
        if detection_ns is not None and attack_start_ns is not None else None
    )
    primary_claimed_ns = (
        agentic_claimed_ns if agentic_mode == "authority-live"
        else mcda_claimed_ns
    )
    consensus_latency_ms = (
        round((primary_claimed_ns - detection_ns) / 1e6, 3)
        if primary_claimed_ns is not None and detection_ns is not None else None
    )
    mcda_endpoint_ns = mcda_claimed_ns or mcda_mitigate_ns
    mcda_consensus_latency_ms = (
        round((mcda_endpoint_ns - detection_ns) / 1e6, 3)
        if mcda_endpoint_ns is not None and detection_ns is not None else None
    )
    agreement_events = [
        event for event in agentic_events.values()
        if event.get("decision") == "AGREED"
        and isinstance(event.get("state_entered_ns", event.get("evaluated_ns")), int)
    ]
    if detection_ns is not None:
        agreement_events = [
            event for event in agreement_events
            if int(event.get("state_entered_ns", event.get("evaluated_ns")))
            >= detection_ns
        ]
    elif expected_attack and attack_start_ns is not None:
        agreement_events = [
            event for event in agreement_events
            if int(event.get("state_entered_ns", event.get("evaluated_ns")))
            >= attack_start_ns
        ]
    first_agreement = min(
        agreement_events,
        key=lambda event: int(
            event.get("state_entered_ns", event.get("evaluated_ns"))
        ),
        default=None,
    )
    agentic_agreement_ns = (
        int(first_agreement.get("state_entered_ns", first_agreement.get("evaluated_ns")))
        if first_agreement is not None else None
    )
    first_proposal_ns = (
        first_agreement.get("first_proposal_ns")
        if first_agreement is not None else None
    )
    last_proposal_ns = (
        first_agreement.get("last_proposal_ns")
        if first_agreement is not None else None
    )
    proposal_timestamps_ns: Dict[str, int] = {}
    if first_agreement is not None:
        proposal_times = [
            item.get("created_ns") for item in first_agreement.get("proposals", [])
            if isinstance(item, dict) and isinstance(item.get("created_ns"), int)
        ]
        if first_proposal_ns is None and proposal_times:
            first_proposal_ns = min(proposal_times)
        if last_proposal_ns is None and proposal_times:
            last_proposal_ns = max(proposal_times)
        proposal_timestamps_ns = {
            str(item["cid"]): int(item["created_ns"])
            for item in first_agreement.get("proposals", [])
            if (isinstance(item, dict) and item.get("cid") is not None
                and isinstance(item.get("created_ns"), int))
        }

    def latency_ms(end_ns: Any, start_ns: Any) -> Optional[float]:
        if not isinstance(end_ns, int) or not isinstance(start_ns, int):
            return None
        if end_ns < start_ns:
            return None
        return round((end_ns - start_ns) / 1e6, 3)

    agentic_consensus_latency_ms = latency_ms(agentic_agreement_ns, detection_ns)
    agentic_attack_to_consensus_latency_ms = latency_ms(
        agentic_agreement_ns, attack_start_ns
    )
    agentic_first_proposal_latency_ms = latency_ms(first_proposal_ns, detection_ns)
    agentic_proposal_collection_latency_ms = latency_ms(
        last_proposal_ns, first_proposal_ns
    )
    agentic_deliberation_latency_ms = latency_ms(
        agentic_agreement_ns, last_proposal_ns
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
    if (expected_attack and attack_bps is None
            and not (live_mode and attack_disrupted and mitigation_executed)):
        invalid_reasons.append("ataque sem vazão medida")
    if expected_attack and attack_start_ns is None:
        invalid_reasons.append("início do ataque sem timestamp")
    if endpoint_errors:
        invalid_reasons.append(f"{endpoint_errors} erro(s) nas APIs dos preditores")
    if (live_mode and "MITIGATE" in decisions and not mitigation_executed):
        invalid_reasons.append(
            "mitigação live não executada"
            + (f": {mitigation_reason}" if mitigation_reason else "")
        )
    if live_mode and attack_disrupted and not mitigation_executed:
        invalid_reasons.append(
            "iperf interrompido sem mitigação live confirmada"
        )
    if (live_mode and mitigation_executed and ping_after_loss is not None
            and ping_after_loss <= 0.0):
        invalid_reasons.append(
            "FlowBlocker confirmou execução, mas o ping não observou perda"
        )
    agentic_shadow = agentic_enabled and agentic_mode == "shadow"
    try:
        expected_agent_domains = max(1, int(metadata.get("controller_sets", 1)))
    except (TypeError, ValueError):
        expected_agent_domains = 1
    if agentic_enabled and len(agentic_active_domains) < expected_agent_domains:
        invalid_reasons.append(
            f"agentes {agentic_mode} ativos em "
            f"{len(agentic_active_domains)}/{expected_agent_domains} domínio(s)"
        )
    if (agentic_enabled and expected_attack and attack_spikes
            and not agentic_decisions):
        invalid_reasons.append(
            f"anomalia de ataque sem decisão registrada pelos agentes {agentic_mode}"
        )
    if agentic_mode == "authority-dry-run" and agentic_actuation_violation:
        invalid_reasons.append("authority-dry-run tentou ou executou atuação")
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
        "mcda_consensus_latency_ms": mcda_consensus_latency_ms,
        "ping_before_loss_percent": ping_before_loss,
        "ping_after_loss_percent": ping_after_loss,
        "baseline_bps": baseline_bps,
        "attack_bps": attack_bps,
        "attack_disrupted": attack_disrupted,
        "endpoint_errors": endpoint_errors,
        "expected_attack": expected_attack,
        "detected_attack": detected_attack,
        "measurement_valid": measurement_valid,
        "invalid_reasons": invalid_reasons,
        "contamination_reasons": contamination_reasons,
        "classification": classification,
        "agentic_shadow": agentic_shadow,
        "agentic_enabled": agentic_enabled,
        "agentic_mode": agentic_mode,
        "agentic_active_domains": sorted(agentic_active_domains),
        "agentic_expected_domains": expected_agent_domains,
        "agentic_decisions": sorted(agentic_decisions),
        "agentic_mitigate_votes": sorted(agentic_mitigate_votes),
        "agentic_agreement_ns": agentic_agreement_ns,
        "agentic_first_proposal_ns": first_proposal_ns,
        "agentic_last_proposal_ns": last_proposal_ns,
        "agentic_proposal_timestamps_ns": proposal_timestamps_ns,
        "agentic_required_votes": (
            first_agreement.get("required_votes")
            if first_agreement is not None else None
        ),
        "agentic_quorum_reached": first_agreement is not None,
        "agentic_first_proposal_latency_ms": agentic_first_proposal_latency_ms,
        "agentic_proposal_collection_latency_ms": (
            agentic_proposal_collection_latency_ms
        ),
        "agentic_deliberation_latency_ms": agentic_deliberation_latency_ms,
        "agentic_consensus_latency_ms": agentic_consensus_latency_ms,
        "agentic_attack_to_consensus_latency_ms": (
            agentic_attack_to_consensus_latency_ms
        ),
        "agentic_agreement_events": sum(
            event.get("decision") == "AGREED" for event in agentic_events.values()
        ),
        "agentic_disagreement_events": sum(
            event.get("decision") in {
                "DISAGREED", "VETOED", "MODEL_MISMATCH", "TOPOLOGY_MISMATCH",
            }
            for event in agentic_events.values()
        ),
        "agentic_waiting_events": sum(
            max(0, value - initial_waiting_by_domain.get(cid, 0))
            for cid, value in agentic_waiting_by_domain.items()
        ),
        "agentic_expired_proposals": sum(
            max(0, value - initial_expired_by_domain.get(cid, 0))
            for cid, value in agentic_expired_by_domain.items()
        ),
        "agentic_agent_agreement": bool(first_agreement),
        "agentic_authorized_domains": sorted(agentic_authorized_domains),
        "agentic_claim_winners": sorted(agentic_claim_winners),
        "agentic_would_execute_domains": sorted(agentic_would_execute_domains),
        "agentic_actuation_violation": agentic_actuation_violation,
        "agentic_mcda_comparison_domains": sorted(agentic_comparisons),
        "agentic_matches_mcda": (
            all(value[1] for value in agentic_comparisons.values())
            if agentic_comparisons else None
        ),
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
    agentic_candidates = [
        row for row in rows
        if row.get("agentic_enabled") or row.get("agentic_shadow")
        and row.get("measurement_valid")
        and not row.get("contamination_reasons")
        and row.get("attack_spike_anomalies", 0) > 0
    ]
    agentic_comparable = [
        row for row in agentic_candidates
        if isinstance(row.get("agentic_matches_mcda"), bool)
    ]

    def rate(numerator: int, denominator: int) -> Optional[float]:
        return round(numerator / denominator, 6) if denominator else None

    def distribution(field: str) -> Dict[str, Any]:
        values = [
            float(row[field]) for row in agentic_candidates
            if isinstance(row.get(field), (int, float))
        ]
        if not values:
            return {"n": 0, "mean": None, "median": None,
                    "sample_stddev": None, "min": None, "max": None}
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

    agreed_runs = sum(
        row.get("agentic_agent_agreement") is True for row in agentic_candidates
    )
    matching_runs = sum(
        row.get("agentic_matches_mcda") is True for row in agentic_comparable
    )
    return {
        **counts,
        "precision": (None if precision is None else round(precision, 6)),
        "recall": (None if recall is None else round(recall, 6)),
        "f1": (None if f1 is None else round(f1, 6)),
        "agentic": {
            "candidate_runs": len(agentic_candidates),
            "agreed_runs": agreed_runs,
            "agent_to_agent_agreement_rate": rate(
                agreed_runs, len(agentic_candidates)
            ),
            "mcda_comparable_runs": len(agentic_comparable),
            "mcda_matching_runs": matching_runs,
            "agent_to_mcda_agreement_rate": rate(
                matching_runs, len(agentic_comparable)
            ),
            "waiting_events": sum(
                row.get("agentic_waiting_events", 0) for row in agentic_candidates
            ),
            "expired_proposals": sum(
                row.get("agentic_expired_proposals", 0)
                for row in agentic_candidates
            ),
            "consensus_latency_ms": distribution(
                "agentic_consensus_latency_ms"
            ),
            "attack_to_consensus_latency_ms": distribution(
                "agentic_attack_to_consensus_latency_ms"
            ),
            "proposal_collection_latency_ms": distribution(
                "agentic_proposal_collection_latency_ms"
            ),
        },
    }


def markdown_table(rows: List[Dict[str, Any]]) -> str:
    headers = [
        "run", "mode", "scenario", "classe", "decisão", "score", "confirmações",
        "coordenador", "ações", "executada", "detecção ms", "MCDA ms", "agente ms",
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
            row.get("mcda_consensus_latency_ms"),
            row.get("agentic_consensus_latency_ms"),
            row.get("baseline_spike_anomalies"),
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
        ("agentic: "
         f"AGREED={metrics['agentic']['agreed_runs']}/"
         f"{metrics['agentic']['candidate_runs']} "
         f"agente-agente={metrics['agentic']['agent_to_agent_agreement_rate']} "
         f"agente-MCDA={metrics['agentic']['agent_to_mcda_agreement_rate']} "
         f"latência média={metrics['agentic']['consensus_latency_ms']['mean']} ms"),
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
