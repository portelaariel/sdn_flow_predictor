#!/usr/bin/env python3
"""Valida um ensaio authority-live sem confiar apenas no resumo principal."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


MCDA_EPISODE_DEFINITION = "bounded-episode-window-v2"


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return default


def read_ns(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, TypeError, ValueError):
        return 0


def timeline(path: Path) -> List[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def window_ids(payload: Dict[str, Any]) -> set[int]:
    """Return valid, non-negative window identifiers from a decision."""
    values = set()
    for value in payload.get("window_ids", []):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            values.add(parsed)
    return values


def preceding_window_distance(
    candidate_windows: set[int], agent_windows: set[int]
) -> Optional[int]:
    """Measure how many windows an earlier MCDA decision precedes the agent.

    Zero means that both decisions share at least one window. A positive value
    is only returned when every candidate window is before every agent window;
    mixed or future identities are rejected instead of being coerced into the
    same episode.
    """
    if not candidate_windows or not agent_windows:
        return None
    if candidate_windows & agent_windows:
        return 0
    candidate_last = max(candidate_windows)
    agent_first = min(agent_windows)
    if candidate_last >= agent_first:
        return None
    return agent_first - candidate_last


def evaluate(
    run_dir: Path, mcda_convergence_window_ms: float = 1000.0,
    mcda_episode_lookback_ms: float = 2000.0,
    mcda_max_preceding_windows: int = 1,
) -> Dict[str, Any]:
    metadata = read_json(run_dir / "metadata.json", {}) or {}
    workload = read_json(run_dir / "workload_status.json", {}) or {}
    summary = read_json(run_dir / "summary.json", {}) or {}
    result = ((summary.get("runs") or [{}])[0]
              if isinstance(summary, dict) else {})
    flow = str(metadata.get("flow") or "")
    scenario = str(metadata.get("scenario") or "")
    attack_ns = read_ns(run_dir / "attack_start_ns.txt")
    started_ns = int(metadata.get("started_ns", 0) or 0)
    start_ns = attack_ns if scenario == "ddos" else started_ns
    expected_domains = max(1, int(metadata.get("controller_sets", 2) or 2))
    active_domains = set()
    events: Dict[str, Dict[str, Any]] = {}
    endpoint_errors = 0
    mcda_actions = []
    mcda_claims = []
    mcda_events: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for row in timeline(run_dir / "timeline.ndjson"):
        if row.get("error"):
            endpoint_errors += 1
            continue
        cid = str((row.get("status") or {}).get("cid") or row.get("port") or "")
        agent = row.get("agentic") or {}
        agent_cid = str(agent.get("cid") or cid)
        if (agent.get("requested") is True
                and agent.get("active") is True
                and agent.get("mode") == "authority-live"
                and agent.get("authoritative") is True
                and agent.get("actuation_enabled") is True):
            active_domains.add(agent_cid)
        for event in (
            list(agent.get("decision_events") or [])
            + list(agent.get("decisions") or [])
        ):
            if not isinstance(event, dict) or event.get("flow") != flow:
                continue
            entered_ns = int(
                event.get("state_entered_ns", event.get("evaluated_ns", 0)) or 0
            )
            if entered_ns < start_ns:
                continue
            copy = dict(event)
            copy["observed_by"] = agent_cid
            event_id = str(copy.get("event_id") or f"{agent_cid}:{entered_ns}")
            events[event_id] = copy

        collaboration = row.get("collaboration") or {}
        mcda_cid = str(collaboration.get("cid") or cid)
        for decision in (
            list(collaboration.get("decision_events") or [])
            + list(collaboration.get("decisions") or [])
        ):
            if not isinstance(decision, dict) or decision.get("flow") != flow:
                continue
            evaluated_ns = int(decision.get("evaluated_ns", 0) or 0)
            if evaluated_ns < start_ns:
                continue
            mcda_key = (
                f"{decision.get('decision')}:{evaluated_ns}:"
                f"{','.join(str(value) for value in decision.get('window_ids', []))}"
            )
            mcda_events.setdefault(mcda_cid, {})[mcda_key] = decision
            mitigation = decision.get("mitigation") or {}
            if mitigation.get("attempted") or mitigation.get("executed"):
                mcda_actions.append(decision)
            if isinstance(decision.get("claim"), dict):
                mcda_claims.append(decision["claim"])

    agreed = [event for event in events.values()
              if event.get("decision") == "AGREED"]
    authorized = [event for event in agreed
                  if (event.get("authority") or {}).get("authorized") is True]
    winners = [event for event in authorized
               if (((event.get("authority") or {}).get("claim") or {})
                   .get("won") is True)]
    executions = [event for event in authorized
                  if (event.get("execution") or {}).get("executed") is True]
    authorized_domains = {event.get("observed_by") for event in authorized}
    winner_domains = {event.get("observed_by") for event in winners}
    execution_domains = {event.get("observed_by") for event in executions}
    winner_claims = [
        (event.get("authority") or {}).get("claim") or {}
        for event in winners
    ]
    mcda_comparisons = {}
    for event in authorized:
        domain = str(event.get("observed_by") or "")
        authority = event.get("authority") or {}
        frozen = authority.get("mcda_comparison")
        comparison = (
            frozen if isinstance(frozen, dict)
            else event.get("legacy_comparison") or {}
        )
        if (comparison.get("available") is True
                and isinstance(comparison.get("matches"), bool)):
            mcda_comparisons[domain] = {
                "matches": comparison["matches"],
                "basis": comparison.get("basis", "legacy_comparison"),
                "captured_ns": comparison.get("captured_ns"),
                "window_ids": list(event.get("window_ids") or []),
                "mcda": comparison.get("mcda"),
            }

    convergence_window_ns = max(1, int(mcda_convergence_window_ms * 1e6))
    episode_lookback_ns = max(1, int(mcda_episode_lookback_ms * 1e6))
    max_preceding_windows = max(0, int(mcda_max_preceding_windows))
    mcda_convergence = {}
    for event in authorized:
        domain = str(event.get("observed_by") or "")
        comparison = mcda_comparisons.get(domain)
        if not comparison:
            continue
        authority = event.get("authority") or {}
        authority_ns = int(
            authority.get("evaluated_ns")
            or comparison.get("captured_ns")
            or event.get("state_entered_ns")
            or event.get("evaluated_ns")
            or 0
        )
        agent_windows = window_ids(event)
        converged_ns = authority_ns if comparison["matches"] else None
        converged_decision = comparison.get("mcda") if converged_ns else None
        direction = "AT_AUTHORITY" if converged_ns is not None else "NOT_OBSERVED"
        window_relation = "OVERLAP" if converged_ns is not None else None
        preceding_distance = 0 if converged_ns is not None else None

        # The exploratory replication showed a legitimate ordering not covered
        # by v1: MCDA reached MITIGATE in the immediately preceding polling
        # window, while the agents completed authority in the next window. A
        # bounded lookback records that lead without accepting arbitrary old
        # decisions. The attack gate, canonical flow, time bound and adjacent
        # window identity must all agree.
        if converged_ns is None and authority_ns and agent_windows:
            early_candidates = []
            for candidate in mcda_events.get(domain, {}).values():
                candidate_ns = int(candidate.get("evaluated_ns", 0) or 0)
                distance = preceding_window_distance(
                    window_ids(candidate), agent_windows
                )
                if (
                    candidate.get("decision") == "MITIGATE"
                    and max(start_ns, authority_ns - episode_lookback_ns)
                    <= candidate_ns <= authority_ns
                    and distance is not None
                    and distance <= max_preceding_windows
                ):
                    early_candidates.append((candidate_ns, distance, candidate))
            if early_candidates:
                converged_ns, preceding_distance, converged_decision = max(
                    early_candidates, key=lambda item: item[0]
                )
                direction = (
                    "AT_AUTHORITY"
                    if converged_ns == authority_ns
                    else "BEFORE_AUTHORITY"
                )
                window_relation = (
                    "OVERLAP" if preceding_distance == 0 else "PRECEDING"
                )

        if converged_ns is None and authority_ns and agent_windows:
            candidates = [
                candidate
                for candidate in mcda_events.get(domain, {}).values()
                if candidate.get("decision") == "MITIGATE"
                and authority_ns <= int(candidate.get("evaluated_ns", 0) or 0)
                <= authority_ns + convergence_window_ns
                and agent_windows & window_ids(candidate)
            ]
            if candidates:
                converged_decision = min(
                    candidates,
                    key=lambda item: int(item.get("evaluated_ns", 0) or 0),
                )
                converged_ns = int(converged_decision["evaluated_ns"])
                direction = (
                    "AT_AUTHORITY"
                    if converged_ns == authority_ns
                    else "AFTER_AUTHORITY"
                )
                window_relation = "OVERLAP"
                preceding_distance = 0
        offset_ms = (
            round((converged_ns - authority_ns) / 1e6, 3)
            if converged_ns is not None else None
        )
        mcda_convergence[domain] = {
            "matches_at_authority": comparison["matches"],
            "authority_ns": authority_ns,
            "window_ids": sorted(agent_windows),
            "converged": converged_ns is not None,
            "converged_ns": converged_ns,
            "direction": direction,
            "window_relation": window_relation,
            "preceding_window_distance": preceding_distance,
            "offset_ms": offset_ms,
            "latency_ms": (
                max(0.0, offset_ms) if offset_ms is not None else None
            ),
            "lead_ms": (
                max(0.0, -offset_ms) if offset_ms is not None else None
            ),
            "mcda": converged_decision,
        }
    mcda_converged_within_bound = (
        len(mcda_convergence) == expected_domains
        and all(item["converged"] for item in mcda_convergence.values())
    )

    blocker_requests = 0
    for path in run_dir.glob("flow-blocker-*.log"):
        blocker_requests += sum(
            "Service request to block traffic" in line
            for line in path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        )
    src, dst = flow.split("->", 1) if "->" in flow else ("", "")
    drop_files = []
    for path in run_dir.glob("ovs-flows-s*.txt"):
        content = path.read_text(encoding="utf-8", errors="replace")
        if (f"nw_src={src}" in content and f"nw_dst={dst}" in content
                and "actions=drop" in content):
            drop_files.append(str(path))

    common = {
        "metadata_is_agentic_live": (
            metadata.get("mode") == "agentic-live"
            and metadata.get("agentic_enabled") is True
            and metadata.get("agentic_mode") == "authority-live"
        ),
        "workload_valid": workload.get("valid") is True,
        "no_endpoint_errors": endpoint_errors == 0,
        "all_agents_live": len(active_domains) == expected_domains,
        "mcda_never_actuated": not mcda_actions,
        "mcda_never_claimed": not mcda_claims,
    }
    if scenario == "ddos":
        scenario_checks = {
            "benchmark_tp": result.get("classification") == "TP",
            "all_agents_authorized": len(authorized_domains) == expected_domains,
            "single_agentic_winner": len(winners) == 1 and len(winner_domains) == 1,
            "single_agentic_executor": (
                len(executions) == 1 and execution_domains == winner_domains
            ),
            "claim_not_degraded": all(
                claim.get("degraded") is False for claim in winner_claims
            ),
            "execution_owned_by_agentic": all(
                (event.get("execution") or {}).get("owner") == "agentic"
                for event in executions
            ),
            "one_flowblocker_request": blocker_requests == 1,
            "drop_rule_present": bool(drop_files),
            "summary_confirms_execution": result.get("mitigation_executed") is True,
        }
        observational_checks = {
            "decision_time_mcda_available": (
                len(mcda_comparisons) == expected_domains
            ),
            "agent_matches_mcda_at_authority": (
                len(mcda_comparisons) == expected_domains
                and all(item["matches"] for item in mcda_comparisons.values())
            ),
            "mcda_converged_same_episode_within_bound": (
                mcda_converged_within_bound
            ),
        }
    else:
        scenario_checks = {
            "benchmark_tn": result.get("classification") == "TN",
            "no_agent_agreement": not agreed,
            "no_agent_authorization": not authorized,
            "no_agent_execution": not executions,
            "no_flowblocker_request": blocker_requests == 0,
            "no_drop_rule": not drop_files,
        }
        observational_checks = {}
    checks = {**common, **scenario_checks}
    return {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "authority-live-canary",
        "run": run_dir.name,
        "scenario": scenario,
        "flow": flow,
        "classification": result.get("classification"),
        "git_commit": metadata.get("git_commit"),
        "model_sha256": metadata.get("model_sha256"),
        "detection_latency_ms": result.get("detection_latency_ms"),
        "mcda_consensus_latency_ms": result.get("mcda_consensus_latency_ms"),
        "agentic_consensus_latency_ms": result.get(
            "agentic_consensus_latency_ms"
        ),
        "ping_after_loss_percent": result.get("ping_after_loss_percent"),
        "agentic_matches_mcda": (
            all(item["matches"] for item in mcda_comparisons.values())
            if mcda_comparisons else None
        ),
        "agentic_mcda_comparisons": {
            domain: mcda_comparisons[domain]
            for domain in sorted(mcda_comparisons)
        },
        "mcda_convergence_window_ms": round(
            convergence_window_ns / 1e6, 3
        ),
        "mcda_episode_definition": {
            "name": MCDA_EPISODE_DEFINITION,
            "lookback_ms": round(episode_lookback_ns / 1e6, 3),
            "max_preceding_windows": max_preceding_windows,
            "future_convergence_ms": round(convergence_window_ns / 1e6, 3),
        },
        "mcda_converged_within_bound": mcda_converged_within_bound,
        "mcda_convergence": {
            domain: mcda_convergence[domain]
            for domain in sorted(mcda_convergence)
        },
        "mcda_max_convergence_latency_ms": max(
            (
                item["latency_ms"] for item in mcda_convergence.values()
                if isinstance(item.get("latency_ms"), (int, float))
            ),
            default=None,
        ),
        "mcda_max_early_lead_ms": max(
            (
                item["lead_ms"] for item in mcda_convergence.values()
                if isinstance(item.get("lead_ms"), (int, float))
            ),
            default=None,
        ),
        "mcda_convergence_directions": {
            direction: sum(
                item.get("direction") == direction
                for item in mcda_convergence.values()
            )
            for direction in (
                "BEFORE_AUTHORITY", "AT_AUTHORITY", "AFTER_AUTHORITY",
                "NOT_OBSERVED",
            )
        },
        "active_domains": sorted(active_domains),
        "authorized_domains": sorted(str(value) for value in authorized_domains),
        "winner_domains": sorted(str(value) for value in winner_domains),
        "execution_domains": sorted(str(value) for value in execution_domains),
        "flowblocker_requests": blocker_requests,
        "drop_rule_files": drop_files,
        "checks": checks,
        "observational_checks": observational_checks,
        "aggregate": {
            "passed": sum(checks.values()),
            "total": len(checks),
            "failed": sum(not value for value in checks.values()),
            "safe": all(checks.values()),
            "observational_passed": sum(observational_checks.values()),
            "observational_total": len(observational_checks),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--mcda-convergence-window-ms", type=float, default=1000.0
    )
    parser.add_argument(
        "--mcda-episode-lookback-ms", type=float, default=2000.0
    )
    parser.add_argument(
        "--mcda-max-preceding-windows", type=int, default=1
    )
    args = parser.parse_args()
    if args.mcda_convergence_window_ms <= 0:
        parser.error("--mcda-convergence-window-ms deve ser positivo")
    if args.mcda_episode_lookback_ms <= 0:
        parser.error("--mcda-episode-lookback-ms deve ser positivo")
    if args.mcda_max_preceding_windows < 0:
        parser.error("--mcda-max-preceding-windows deve ser não negativo")
    report = evaluate(
        args.run_dir,
        args.mcda_convergence_window_ms,
        args.mcda_episode_lookback_ms,
        args.mcda_max_preceding_windows,
    )
    output = args.output or (args.run_dir / "agentic-live-summary.json")
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "run": report["run"],
        "scenario": report["scenario"],
        "classification": report["classification"],
        "authorized_domains": report["authorized_domains"],
        "winner_domains": report["winner_domains"],
        "execution_domains": report["execution_domains"],
        "flowblocker_requests": report["flowblocker_requests"],
        "drop_rules": len(report["drop_rule_files"]),
        "agentic_matches_mcda": report["agentic_matches_mcda"],
        "mcda_converged_within_bound": report["mcda_converged_within_bound"],
        "mcda_convergence_directions": report["mcda_convergence_directions"],
        "aggregate": report["aggregate"],
    }, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if report["aggregate"]["safe"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
