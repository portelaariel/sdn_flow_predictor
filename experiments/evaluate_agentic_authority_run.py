#!/usr/bin/env python3
"""Validate an authority-dry-run benchmark without trusting its headline."""

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
    rows = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
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
    benchmark = read_json(run_dir / "summary.json", {}) or {}
    benchmark_run = ((benchmark.get("runs") or [{}])[0]
                     if isinstance(benchmark, dict) else {})
    flow = str(metadata.get("flow") or "")
    attack_start_ns = read_ns(run_dir / "attack_start_ns.txt")
    try:
        expected_domains = max(1, int(metadata.get("controller_sets", 2) or 2))
    except (TypeError, ValueError):
        expected_domains = 0
    rows = timeline(run_dir / "timeline.ndjson")
    active_domains = set()
    events: Dict[str, Dict[str, Any]] = {}
    endpoint_errors = 0
    for row in rows:
        if row.get("error"):
            endpoint_errors += 1
            continue
        cid = str((row.get("status") or {}).get("cid") or row.get("port") or "")
        agent = row.get("agentic") or {}
        if (agent.get("requested") is True
                and agent.get("active") is True
                and agent.get("mode") == "authority-dry-run"
                and agent.get("authoritative") is True
                and agent.get("actuation_enabled") is False):
            active_cid = str(agent.get("cid") or cid)
            if active_cid:
                active_domains.add(active_cid)
        for event in (
            list(agent.get("decision_events", []))
            + list(agent.get("decisions", []))
        ):
            if not isinstance(event, dict) or event.get("flow") != flow:
                continue
            entered_ns = int(event.get("state_entered_ns", 0) or 0)
            if entered_ns < attack_start_ns:
                continue
            copy = dict(event)
            copy["observed_by"] = str(agent.get("cid") or cid)
            event_id = str(copy.get("event_id") or f"{cid}:{entered_ns}")
            events[event_id] = copy

    agreed = [event for event in events.values() if event.get("decision") == "AGREED"]
    authorized = [
        event for event in agreed
        if (event.get("authority") or {}).get("authorized") is True
    ]
    winners = [
        event for event in authorized
        if (((event.get("authority") or {}).get("claim") or {}).get("won") is True)
    ]
    winner_domains = {event.get("observed_by") for event in winners}
    authorized_domains = {
        event.get("observed_by") for event in authorized
    }
    claim_records = [
        (event.get("authority") or {}).get("claim")
        for event in authorized
    ]
    claim_coordinators = {
        claim.get("coordinator")
        for claim in claim_records
        if isinstance(claim, dict) and claim.get("coordinator")
    }
    would_execute = {
        event.get("observed_by") for event in authorized
        if (event.get("execution") or {}).get("would_execute") is True
    }
    attempted_or_executed = [
        event for event in events.values()
        if ((event.get("execution") or {}).get("attempted")
            or (event.get("execution") or {}).get("executed"))
    ]
    degraded_claims = [
        event for event in authorized
        if (((event.get("authority") or {}).get("claim") or {}).get("degraded"))
    ]
    fresh_authorized = [
        event for event in authorized
        if event.get("proposals")
        and all(
            isinstance(proposal, dict)
            and int(proposal.get("observation_ns", 0) or 0) >= attack_start_ns
            and int(proposal.get("created_ns", 0) or 0) >= attack_start_ns
            for proposal in event.get("proposals", [])
        )
    ]
    mcda_records = [
        (event.get("observed_by"), (
            event.get("legacy_comparison") or {}
        ).get("matches"))
        for event in authorized
        if (event.get("legacy_comparison") or {}).get("available") is True
    ]
    mcda_by_domain = {domain: matches for domain, matches in mcda_records}
    blocker_requests = 0
    for path in run_dir.glob("flow-blocker-*.log"):
        blocker_requests += sum(
            "Service request to block traffic" in line
            for line in path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        )
    src, dst = flow.split("->", 1) if "->" in flow else ("", "")
    drop_rules = []
    for path in run_dir.glob("ovs-flows-s*.txt"):
        content = path.read_text(encoding="utf-8", errors="replace")
        if (f"nw_src={src}" in content and f"nw_dst={dst}" in content
                and "actions=drop" in content):
            drop_rules.append(str(path))

    checks = {
        "metadata_is_authority_dry_run": (
            metadata.get("mode") == "collaborative-dry-run"
            and metadata.get("agentic_enabled") is True
            and metadata.get("agentic_mode") == "authority-dry-run"
        ),
        "attack_timestamp_valid": attack_start_ns > 0,
        "workload_valid": workload.get("valid") is True,
        "benchmark_tp": benchmark_run.get("classification") == "TP",
        "no_endpoint_errors": endpoint_errors == 0,
        "all_agents_active": len(active_domains) == expected_domains,
        "all_agents_authorized": len(authorized_domains) == expected_domains,
        "fresh_evidence_only": (
            len(fresh_authorized) == len(authorized)
            and len({
                event.get("observed_by") for event in fresh_authorized
            }) == expected_domains
        ),
        "all_authorizations_have_claim": (
            len(claim_records) == len(authorized)
            and all(isinstance(claim, dict) for claim in claim_records)
        ),
        "single_claim_winner": len(winners) == 1 and len(winner_domains) == 1,
        "claim_owner_consistent": claim_coordinators == winner_domains,
        "winner_matches_would_execute": would_execute == winner_domains,
        "claim_not_degraded": not degraded_claims,
        "agent_never_actuated": not attempted_or_executed,
        "mcda_comparison_available": (
            len(mcda_by_domain) == expected_domains
            and len(mcda_records) == len(authorized)
        ),
        "agent_matches_mcda": (
            bool(mcda_records) and all(matches for _domain, matches in mcda_records)
        ),
        "no_flowblocker_request": blocker_requests == 0,
        "no_drop_rule": not drop_rules,
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "authority-dry-run",
        "run": run_dir.name,
        "flow": flow,
        "active_domains": sorted(active_domains),
        "agreed_events": len(agreed),
        "authorized_domains": sorted(str(value) for value in authorized_domains),
        "claim_winner_domains": sorted(str(value) for value in winner_domains),
        "would_execute_domains": sorted(str(value) for value in would_execute),
        "flowblocker_requests": blocker_requests,
        "drop_rule_files": drop_rules,
        "checks": checks,
        "aggregate": {
            "passed": sum(checks.values()),
            "total": len(checks),
            "failed": sum(not value for value in checks.values()),
            "safe": all(checks.values()),
        },
    }


def print_report(report: Dict[str, Any]) -> None:
    print(json.dumps({
        "mode": report["mode"],
        "run": report["run"],
        "flow": report["flow"],
        "authorized_domains": report["authorized_domains"],
        "claim_winner_domains": report["claim_winner_domains"],
        "would_execute_domains": report["would_execute_domains"],
        "flowblocker_requests": report["flowblocker_requests"],
        "drop_rules": len(report["drop_rule_files"]),
        "aggregate": report["aggregate"],
    }, indent=2, sort_keys=True, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.run_dir)
    output = args.output or (args.run_dir / "authority-summary.json")
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print_report(report)
    return 0 if report["aggregate"]["safe"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
