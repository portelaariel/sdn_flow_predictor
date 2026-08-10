#!/usr/bin/env python3
"""Valida um ensaio authority-live sem confiar apenas no resumo principal."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


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


def evaluate(run_dir: Path) -> Dict[str, Any]:
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
        for decision in (
            list(collaboration.get("decision_events") or [])
            + list(collaboration.get("decisions") or [])
        ):
            if not isinstance(decision, dict) or decision.get("flow") != flow:
                continue
            evaluated_ns = int(decision.get("evaluated_ns", 0) or 0)
            if evaluated_ns < start_ns:
                continue
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
    else:
        scenario_checks = {
            "benchmark_tn": result.get("classification") == "TN",
            "no_agent_agreement": not agreed,
            "no_agent_authorization": not authorized,
            "no_agent_execution": not executions,
            "no_flowblocker_request": blocker_requests == 0,
            "no_drop_rule": not drop_files,
        }
    checks = {**common, **scenario_checks}
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "authority-live-canary",
        "run": run_dir.name,
        "scenario": scenario,
        "flow": flow,
        "classification": result.get("classification"),
        "active_domains": sorted(active_domains),
        "authorized_domains": sorted(str(value) for value in authorized_domains),
        "winner_domains": sorted(str(value) for value in winner_domains),
        "execution_domains": sorted(str(value) for value in execution_domains),
        "flowblocker_requests": blocker_requests,
        "drop_rule_files": drop_files,
        "checks": checks,
        "aggregate": {
            "passed": sum(checks.values()),
            "total": len(checks),
            "failed": sum(not value for value in checks.values()),
            "safe": all(checks.values()),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.run_dir)
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
        "aggregate": report["aggregate"],
    }, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if report["aggregate"]["safe"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
