"""Normalização e verificação determinística de artefatos do CoMAS.

Este módulo não importa nem chama o runtime do CoMAS. Ele consome apenas os
artefatos imutáveis de uma execução concluída, preservando a LLM fora do
caminho de detecção, consenso, eleição de executor e mitigação.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


SCHEMA_VERSION = "1.0"
INTERMEDIATE_STATES = {
    "NO_EVIDENCE",
    "NO_PROPOSALS",
    "SUSPECT",
    "CORROBORATED",
    "WAITING",
    "WAITING_PROPOSALS",
    "WAITING_QUORUM",
    "WAITING_TOPOLOGY",
    "WAITING_WINDOW",
}
FINAL_STATES = {
    "AGREED",
    "DISAGREED",
    "MITIGATE",
    "MODEL_MISMATCH",
    "NORMAL",
    "TOPOLOGY_MISMATCH",
    "VETOED",
}


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return default


def read_ndjson(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def read_ns(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, TypeError, ValueError):
        return 0


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def event_time_ns(event: Dict[str, Any]) -> int:
    audit = event.get("_audit") or {}
    for value in (
        event.get("state_entered_ns"),
        event.get("evaluated_ns"),
        audit.get("sampled_ns"),
    ):
        timestamp = _integer(value)
        if timestamp > 0:
            return timestamp
    return 0


def extract_decision_events(
    timeline_rows: Iterable[Dict[str, Any]],
    *,
    flow: Optional[str] = None,
    minimum_ns: int = 0,
) -> List[Dict[str, Any]]:
    """Extrai transições imutáveis dos agentes e do MCDA, sem snapshots.

    ``monitor_predictors.py`` já evita repetir ``decision_events`` no arquivo,
    mas a deduplicação por ``event_id`` permanece para tolerar artefatos de
    versões anteriores.
    """
    seen: Dict[str, Dict[str, Any]] = {}
    for row_index, row in enumerate(timeline_rows):
        sampled_ns = _integer(row.get("sampled_ns"))
        status_cid = str((row.get("status") or {}).get("cid") or "")
        for section, layer in (("agentic", "agentic"),
                               ("collaboration", "mcda")):
            block = row.get(section)
            if not isinstance(block, dict):
                continue
            observed_by = str(
                block.get("cid") or status_cid or row.get("port") or "unknown"
            )
            events = block.get("decision_events") or []
            if not isinstance(events, list):
                continue
            for event_index, event in enumerate(events):
                if not isinstance(event, dict):
                    continue
                event_flow = str(event.get("flow") or "")
                if flow and event_flow != flow:
                    continue
                enriched = json.loads(json.dumps(event))
                enriched["_audit"] = {
                    "layer": layer,
                    "observed_by": observed_by,
                    "sampled_ns": sampled_ns,
                }
                timestamp = event_time_ns(enriched)
                if minimum_ns and timestamp and timestamp < minimum_ns:
                    continue
                event_id = str(event.get("event_id") or "")
                if not event_id:
                    event_id = (
                        f"missing:{layer}:{observed_by}:{event_flow}:"
                        f"{event.get('decision')}:{timestamp}:{row_index}:"
                        f"{event_index}"
                    )
                    enriched["event_id"] = event_id
                seen.setdefault(event_id, enriched)
    return sorted(
        seen.values(),
        key=lambda item: (event_time_ns(item), str(item.get("event_id"))),
    )


def _episode_identifier(flow: str, events: Sequence[Dict[str, Any]],
                        sequence: int) -> str:
    window_ids = sorted({
        _integer(window_id, -1)
        for event in events
        for window_id in (event.get("window_ids") or [])
        if _integer(window_id, -1) >= 0
    })
    window_part = (
        f"{window_ids[0]}-{window_ids[-1]}" if window_ids else "no-window"
    )
    safe_flow = re.sub(r"[^A-Za-z0-9_.-]+", "_", flow).strip("_")
    return f"{safe_flow}:{window_part}:{sequence}"


def group_decision_events(
    events: Iterable[Dict[str, Any]],
    *,
    gap_s: float = 15.0,
) -> List[Dict[str, Any]]:
    """Agrupa eventos do mesmo fluxo separados por no máximo ``gap_s``."""
    if gap_s <= 0:
        raise ValueError("gap_s deve ser positivo")
    by_flow: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_flow[str(event.get("flow") or "unknown")].append(event)

    episodes: List[Dict[str, Any]] = []
    gap_ns = int(gap_s * 1e9)
    for flow in sorted(by_flow):
        ordered = sorted(
            by_flow[flow],
            key=lambda item: (event_time_ns(item), str(item.get("event_id"))),
        )
        groups: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = []
        previous_ns = 0
        for event in ordered:
            timestamp = event_time_ns(event)
            if (current and previous_ns and timestamp
                    and timestamp - previous_ns > gap_ns):
                groups.append(current)
                current = []
            current.append(event)
            if timestamp:
                previous_ns = timestamp
        if current:
            groups.append(current)
        for sequence, group in enumerate(groups, start=1):
            episodes.append({
                "episode_id": _episode_identifier(flow, group, sequence),
                "flow": flow,
                "started_ns": min(
                    (event_time_ns(event) for event in group
                     if event_time_ns(event)),
                    default=0,
                ),
                "ended_ns": max(
                    (event_time_ns(event) for event in group), default=0
                ),
                "events": group,
            })
    return sorted(
        episodes,
        key=lambda item: (item["started_ns"], item["episode_id"]),
    )


def _run_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    runs = summary.get("runs") if isinstance(summary, dict) else None
    if isinstance(runs, list) and runs and isinstance(runs[0], dict):
        return runs[0]
    return summary if isinstance(summary, dict) else {}


def _check(name: str, status: str, evidence: Any) -> Dict[str, Any]:
    return {"name": name, "status": status, "evidence": evidence}


def _proposal_model_ids(event: Dict[str, Any]) -> List[str]:
    configured = [
        str(value) for value in (event.get("model_ids") or []) if value
    ]
    if configured:
        return sorted(set(configured))
    return sorted({
        str(proposal.get("model_id"))
        for proposal in (event.get("proposals") or [])
        if isinstance(proposal, dict) and proposal.get("model_id")
    })


def _scenario_correctness(metadata: Dict[str, Any],
                          summary: Dict[str, Any]) -> str:
    scenario = str(metadata.get("scenario") or "")
    classification = str(summary.get("classification") or "")
    if scenario not in {"benign", "ddos"}:
        return "UNKNOWN"
    if classification in {"TP", "TN"}:
        return "CORRECT"
    if classification in {"FP", "FN"}:
        return "INCORRECT"
    return "UNKNOWN"


def _decision_stage(events: Sequence[Dict[str, Any]]) -> str:
    states = {str(event.get("decision") or "") for event in events}
    if states & FINAL_STATES:
        return "FINAL"
    if states & INTERMEDIATE_STATES:
        return "INTERMEDIATE"
    return "NO_DECISION"


def _execution_status(events: Sequence[Dict[str, Any]], mode: str) -> str:
    agent_events = [
        event for event in events
        if (event.get("_audit") or {}).get("layer") == "agentic"
    ]
    executions = [
        event.get("execution") for event in agent_events
        if isinstance(event.get("execution"), dict)
    ]
    if any(item.get("executed") is True for item in executions):
        return "EXECUTED"
    if any(item.get("attempted") is True for item in executions):
        return "FAILED"
    winners = [
        event for event in agent_events
        if (((event.get("authority") or {}).get("claim") or {}).get("won")
            is True)
    ]
    if mode == "authority-dry-run" and winners:
        return "DRY_RUN_SUPPRESSED"
    if any(event.get("decision") == "AGREED" for event in agent_events):
        return "SKIPPED_OTHER_COORDINATOR"
    if _decision_stage(events) == "INTERMEDIATE":
        return "NOT_REQUESTED"
    return "UNKNOWN"


def _operational_effectiveness(execution_status: str,
                               summary: Dict[str, Any]) -> str:
    if execution_status in {
        "DRY_RUN_SUPPRESSED", "NOT_REQUESTED", "SKIPPED_OTHER_COORDINATOR"
    }:
        return "NOT_APPLICABLE"
    if execution_status == "FAILED":
        return "INEFFECTIVE"
    if execution_status == "EXECUTED":
        disrupted = summary.get("attack_disrupted")
        if disrupted is True:
            return "EFFECTIVE"
        if disrupted is False:
            return "INEFFECTIVE"
    return "UNKNOWN"


def _collapsed_transitions(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    transitions: List[Dict[str, Any]] = []
    seen: set = set()
    for event in sorted(events, key=event_time_ns):
        layer = str((event.get("_audit") or {}).get("layer") or "unknown")
        state = str(event.get("decision") or "UNKNOWN")
        key = (layer, state)
        if key in seen:
            continue
        seen.add(key)
        transitions.append({
            "layer": layer,
            "state": state,
            "event_id": str(event.get("event_id") or ""),
            "observed_by": str(
                (event.get("_audit") or {}).get("observed_by") or "unknown"
            ),
            "entered_ns": event_time_ns(event),
        })
    return transitions


def evaluate_episode(
    episode: Dict[str, Any],
    metadata: Dict[str, Any],
    summary: Dict[str, Any],
) -> Dict[str, Any]:
    """Produz um veredito reproduzível sem consultar uma LLM."""
    events = list(episode.get("events") or [])
    agent_events = [
        event for event in events
        if (event.get("_audit") or {}).get("layer") == "agentic"
    ]
    agreed = [event for event in agent_events
              if event.get("decision") == "AGREED"]
    authorized = [
        event for event in agreed
        if (event.get("authority") or {}).get("authorized") is True
    ]
    winners = [
        event for event in authorized
        if (((event.get("authority") or {}).get("claim") or {}).get("won")
            is True)
    ]
    authorized_non_winners = [
        event for event in authorized if event not in winners
    ]
    mode = str(
        metadata.get("agentic_mode")
        or next((event.get("mode") for event in agent_events
                 if event.get("mode")), "")
    )
    checks: List[Dict[str, Any]] = []
    required_unknown = False

    if agreed:
        quorum_rows = []
        for event in agreed:
            required = _integer(event.get("required_votes"), -1)
            votes = sorted(set(str(value)
                               for value in (event.get("mitigate_votes") or [])))
            quorum_rows.append({
                "event_id": event.get("event_id"),
                "required": required,
                "votes": votes,
                "satisfied": required > 0 and len(votes) >= required,
            })
        if any(row["required"] <= 0 for row in quorum_rows):
            quorum_status = "UNKNOWN"
            required_unknown = True
        else:
            quorum_status = (
                "PASS" if all(row["satisfied"] for row in quorum_rows)
                else "FAIL"
            )
        checks.append(_check(
            "agreed_has_required_quorum",
            quorum_status,
            {
                "events": len(quorum_rows),
                "satisfied": sum(row["satisfied"] for row in quorum_rows),
                "invalid_event_ids": [
                    row["event_id"] for row in quorum_rows
                    if not row["satisfied"]
                ],
            },
        ))

        models = [_proposal_model_ids(event) for event in agreed]
        if any(not values for values in models):
            model_status = "UNKNOWN"
            required_unknown = True
        else:
            model_status = "PASS" if all(len(values) == 1 for values in models) else "FAIL"
        checks.append(_check(
            "agreed_uses_one_model_per_event",
            model_status,
            {
                "events": len(models),
                "model_sets": sorted({tuple(values) for values in models}),
                "invalid_event_count": sum(len(values) != 1 for values in models),
            },
        ))

        if mode in {"authority-dry-run", "authority-live"}:
            authority_status = (
                "PASS" if len(authorized) == len(agreed) else "FAIL"
            )
            checks.append(_check(
                "agreed_authorized_by_authority_gate",
                authority_status,
                {"authorized": len(authorized), "agreed": len(agreed)},
            ))

            winner_status = "PASS" if len(winners) == 1 else "FAIL"
            checks.append(_check(
                "single_atomic_claim_winner",
                winner_status,
                [
                    str((event.get("_audit") or {}).get("observed_by")
                        or ((event.get("authority") or {}).get("claim") or {})
                        .get("coordinator") or "unknown")
                    for event in winners
                ],
            ))

            winner_execution = [event.get("execution") or {} for event in winners]
            checks.append(_check(
                "claim_winner_matches_would_execute",
                "PASS" if len(winner_execution) == 1
                and winner_execution[0].get("would_execute") is True else "FAIL",
                winner_execution,
            ))

            non_winner_executions = [
                event.get("execution") or {} for event in authorized_non_winners
            ]
            checks.append(_check(
                "non_winners_do_not_actuate",
                "PASS" if all(
                    execution.get("attempted") is not True
                    and execution.get("executed") is not True
                    and execution.get("would_execute") is not True
                    for execution in non_winner_executions
                ) else "FAIL",
                {"events": len(authorized_non_winners)},
            ))

            if mode == "authority-dry-run":
                all_executions = [event.get("execution") or {}
                                  for event in agent_events]
                checks.append(_check(
                    "dry_run_does_not_actuate",
                    "PASS" if all(
                        execution.get("attempted") is not True
                        and execution.get("executed") is not True
                        for execution in all_executions
                    ) else "FAIL",
                    {"execution_records": len(all_executions)},
                ))
    else:
        waiting = [event for event in agent_events
                   if event.get("decision") == "WAITING_PROPOSALS"]
        if waiting:
            checks.append(_check(
                "waiting_proposals_identifies_missing_domains",
                "PASS" if all(event.get("missing_domains") for event in waiting)
                else "FAIL",
                [event.get("missing_domains") for event in waiting],
            ))
        vetoed = [event for event in agent_events
                  if event.get("decision") == "VETOED"]
        if vetoed:
            has_veto = all(
                event.get("veto_domains")
                or any(
                    isinstance(proposal, dict)
                    and proposal.get("proposal") == "VETO"
                    for proposal in (event.get("proposals") or [])
                )
                for event in vetoed
            )
            checks.append(_check(
                "vetoed_has_veto_evidence",
                "PASS" if has_veto else "FAIL",
                [event.get("veto_domains") for event in vetoed],
            ))
        disagreed = [event for event in agent_events
                     if event.get("decision") == "DISAGREED"]
        if disagreed:
            has_normal = all(any(
                isinstance(proposal, dict)
                and proposal.get("proposal") == "NORMAL"
                for proposal in (event.get("proposals") or [])
            ) for event in disagreed)
            checks.append(_check(
                "disagreed_has_normal_proposal",
                "PASS" if has_normal else "FAIL",
                {"events": len(disagreed)},
            ))
        model_mismatch = [
            event for event in agent_events
            if event.get("decision") == "MODEL_MISMATCH"
        ]
        if model_mismatch:
            mismatch_present = all(
                len(_proposal_model_ids(event)) > 1
                for event in model_mismatch
            )
            checks.append(_check(
                "model_mismatch_has_distinct_models",
                "PASS" if mismatch_present else "FAIL",
                [_proposal_model_ids(event) for event in model_mismatch],
            ))
        mcda_normal = [event for event in events
                       if (event.get("_audit") or {}).get("layer") == "mcda"
                       and event.get("decision") == "NORMAL"]
        if mcda_normal:
            checks.append(_check(
                "mcda_normal_has_no_confirming_domain",
                "PASS" if all(not event.get("confirming_domains")
                              for event in mcda_normal) else "FAIL",
                {"events": len(mcda_normal)},
            ))
        mcda_mitigate = [event for event in events
                         if (event.get("_audit") or {}).get("layer") == "mcda"
                         and event.get("decision") == "MITIGATE"]
        if mcda_mitigate:
            quorum_evidence = []
            for event in mcda_mitigate:
                minimum = _integer(event.get("min_domains"), -1)
                confirming = set(event.get("confirming_domains") or [])
                quorum_evidence.append({
                    "minimum": minimum,
                    "confirming": len(confirming),
                    "satisfied": minimum > 0 and len(confirming) >= minimum,
                })
            mcda_unknown = any(row["minimum"] <= 0 for row in quorum_evidence)
            if mcda_unknown:
                mcda_status = "UNKNOWN"
                required_unknown = True
            else:
                mcda_status = (
                    "PASS" if all(row["satisfied"] for row in quorum_evidence)
                    else "FAIL"
                )
            checks.append(_check(
                "mcda_mitigate_has_required_domains",
                mcda_status,
                {
                    "events": len(mcda_mitigate),
                    "satisfied": sum(
                        row["satisfied"] for row in quorum_evidence
                    ),
                },
            ))
        intermediate = [
            event for event in events
            if str(event.get("decision") or "") in INTERMEDIATE_STATES
        ]
        if intermediate and not checks:
            checks.append(_check(
                "intermediate_state_is_non_terminal",
                "PASS",
                sorted({event.get("decision") for event in intermediate}),
            ))

    statuses = {check["status"] for check in checks}
    if "FAIL" in statuses:
        protocol_consistency = "INCONSISTENT"
    elif required_unknown or not checks:
        protocol_consistency = "INSUFFICIENT_EVIDENCE"
    else:
        protocol_consistency = "CONSISTENT"

    execution_status = _execution_status(events, mode)
    relevant_domains = sorted({
        str(domain)
        for event in events
        for domain in (
            list(event.get("relevant_domains") or [])
            + list(event.get("participating_domains") or [])
        )
        if domain
    })
    claim_winners = sorted({
        str((event.get("_audit") or {}).get("observed_by")
            or ((event.get("authority") or {}).get("claim") or {})
            .get("coordinator") or "unknown")
        for event in winners
    })
    state_counts = Counter(str(event.get("decision") or "UNKNOWN")
                           for event in events)
    layer_counts = Counter(str((event.get("_audit") or {}).get("layer")
                               or "unknown") for event in events)
    executions = [event.get("execution") or {} for event in agent_events
                  if isinstance(event.get("execution"), dict)]
    winner_executions = [event.get("execution") or {} for event in winners]
    if (mode == "authority-dry-run" and len(winners) == 1
            and winner_executions
            and winner_executions[0].get("would_execute") is True
            and winner_executions[0].get("attempted") is not True
            and winner_executions[0].get("executed") is not True):
        winner_behavior = "SELECTED_WOULD_EXECUTE_DRY_RUN_SUPPRESSED"
    elif any(item.get("executed") is True for item in winner_executions):
        winner_behavior = "EXECUTED"
    elif any(item.get("attempted") is True for item in winner_executions):
        winner_behavior = "ATTEMPTED_NOT_EXECUTED"
    elif winners:
        winner_behavior = "SELECTED_WITHOUT_EXECUTION_EVIDENCE"
    else:
        winner_behavior = "NO_CLAIM_WINNER"

    non_winner_executions = [
        event.get("execution") or {} for event in authorized_non_winners
    ]
    if authorized_non_winners and all(
        item.get("attempted") is not True
        and item.get("executed") is not True
        and item.get("would_execute") is not True
        for item in non_winner_executions
    ):
        non_winner_behavior = "ABSTAINED_OTHER_COORDINATOR"
    elif authorized_non_winners:
        non_winner_behavior = "UNEXPECTED_EXECUTION_EVIDENCE"
    else:
        non_winner_behavior = "NO_AUTHORIZED_NON_WINNER"

    return {
        "episode_id": episode["episode_id"],
        "flow": episode["flow"],
        "started_ns": episode["started_ns"],
        "ended_ns": episode["ended_ns"],
        "event_count": len(events),
        "event_counts_by_layer": dict(sorted(layer_counts.items())),
        "event_counts_by_state": dict(sorted(state_counts.items())),
        "transitions": _collapsed_transitions(events),
        "relevant_domains": relevant_domains,
        "model_ids": sorted({
            model_id for event in events
            for model_id in _proposal_model_ids(event)
        }),
        "required_votes": max(
            (_integer(event.get("required_votes")) for event in events),
            default=0,
        ),
        "quorum_reached": bool(agreed),
        "claim_winners": claim_winners,
        "execution_mode": mode or "unknown",
        "normalized_facts": {
            "agentic_agreed_events": len(agreed),
            "agentic_authorized_events": len(authorized),
            "atomic_claim_winner_events": len(winners),
            "authorized_non_winner_events": len(authorized_non_winners),
            "claim_winner_behavior": winner_behavior,
            "authorized_non_winner_behavior": non_winner_behavior,
            "attempted_execution_events": sum(
                item.get("attempted") is True for item in executions
            ),
            "executed_events": sum(
                item.get("executed") is True for item in executions
            ),
            "would_execute_events": sum(
                item.get("would_execute") is True for item in executions
            ),
        },
        "decision_stage": _decision_stage(events),
        "protocol_consistency": protocol_consistency,
        "scenario_correctness": _scenario_correctness(metadata, summary),
        "execution_status": execution_status,
        "operational_effectiveness": _operational_effectiveness(
            execution_status, summary
        ),
        "checks": checks,
        "source_event_ids": [str(event.get("event_id") or "")
                             for event in events],
        "laboratory_context": {
            "scenario": metadata.get("scenario"),
            "classification": summary.get("classification"),
            "ground_truth_available": metadata.get("scenario")
            in {"benign", "ddos"},
        },
    }


def audit_run(run_dir: Path, *, episode_gap_s: float = 15.0) -> Dict[str, Any]:
    """Audita deterministicamente uma execução concluída do CoMAS."""
    run_dir = Path(run_dir)
    metadata = read_json(run_dir / "metadata.json", {}) or {}
    summary_document = read_json(run_dir / "summary.json", {}) or {}
    summary = _run_summary(summary_document)
    timeline_path = run_dir / "timeline.ndjson"
    if not timeline_path.is_file():
        raise FileNotFoundError(f"timeline.ndjson não encontrado em {run_dir}")

    scenario = str(metadata.get("scenario") or "")
    minimum_ns = (
        read_ns(run_dir / "attack_start_ns.txt")
        if scenario == "ddos" else _integer(metadata.get("started_ns"))
    )
    events = extract_decision_events(
        read_ndjson(timeline_path),
        flow=str(metadata.get("flow") or "") or None,
        minimum_ns=minimum_ns,
    )
    grouped = group_decision_events(events, gap_s=episode_gap_s)
    episodes = [evaluate_episode(episode, metadata, summary)
                for episode in grouped]
    event_counts = Counter(str(event.get("decision") or "UNKNOWN")
                           for event in events)
    return {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "post-experiment",
        "run": {
            "directory": str(run_dir),
            "name": run_dir.name,
            "git_commit": metadata.get("git_commit"),
            "flow": metadata.get("flow"),
            "scenario": metadata.get("scenario"),
            "mode": metadata.get("mode"),
            "agentic_mode": metadata.get("agentic_mode"),
            "classification": summary.get("classification"),
            "scenario_correctness": _scenario_correctness(metadata, summary),
            "audit_status": (
                "AUDITED" if episodes else "NO_AUDITABLE_EVENTS"
            ),
        },
        "events_seen": len(events),
        "event_counts_by_state": dict(sorted(event_counts.items())),
        "episodes": episodes,
    }


def evaluation_evidence(record: Dict[str, Any]) -> Dict[str, Any]:
    """Remove o veredito/oráculo antes do modo experimental da LLM."""
    excluded = {
        "checks",
        "decision_stage",
        "execution_status",
        "operational_effectiveness",
        "protocol_consistency",
        "scenario_correctness",
        "source_event_ids",
    }
    evidence = {key: value for key, value in record.items()
                if key not in excluded}
    laboratory = dict(evidence.get("laboratory_context") or {})
    laboratory.pop("classification", None)
    evidence["laboratory_context"] = laboratory
    return evidence
