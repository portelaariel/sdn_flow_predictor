#!/usr/bin/env python3
"""Evaluate fail-closed behavior of live shadow agents under runtime faults."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List


FAULT_EPISODES = {"missing-agent", "etcd-partition"}
RECOVERY_EPISODES = {"missing-agent-recovery", "etcd-recovery"}


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


def timeline_rows(path: Path) -> List[Dict[str, Any]]:
    rows = []
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


def agent_events(rows: Iterable[Dict[str, Any]], flow: str,
                 since_ns: int) -> List[Dict[str, Any]]:
    events: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        cid = str((row.get("status") or {}).get("cid") or row.get("port") or "")
        for event in (row.get("agentic") or {}).get("decision_events", []):
            if not isinstance(event, dict) or event.get("flow") != flow:
                continue
            entered_ns = int(event.get("state_entered_ns", 0) or 0)
            if entered_ns < since_ns:
                continue
            copy = dict(event)
            copy["observed_by"] = cid
            key = str(copy.get("event_id") or f"{cid}:{entered_ns}:{copy.get('decision')}")
            events[key] = copy
    return sorted(events.values(), key=lambda row: int(row.get("state_entered_ns", 0)))


def fresh_claims(rows: Iterable[Dict[str, Any]], flow: str,
                 since_ns: int) -> List[Dict[str, Any]]:
    claims: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        for decision in (row.get("collaboration") or {}).get("decisions", []):
            if not isinstance(decision, dict) or decision.get("flow") != flow:
                continue
            claim = decision.get("claim") or {}
            claimed_ns = int(claim.get("claimed_ns", 0) or 0)
            if claimed_ns < since_ns:
                continue
            key = str(claim.get("key") or f"{claimed_ns}:{claim.get('coordinator')}")
            claims[key] = dict(claim)
    return list(claims.values())


def agent_errors(rows: Iterable[Dict[str, Any]]) -> List[int]:
    values = []
    for row in rows:
        value = (row.get("agentic") or {}).get("errors")
        if isinstance(value, int):
            values.append(value)
    return values


def first_agent_error_increase_ns(rows: Iterable[Dict[str, Any]],
                                  since_ns: int) -> int:
    baselines: Dict[str, int] = {}
    ordered = sorted(rows, key=lambda row: int(row.get("sampled_ns", 0) or 0))
    for row in ordered:
        cid = str((row.get("status") or {}).get("cid") or row.get("port") or "")
        value = (row.get("agentic") or {}).get("errors")
        if not isinstance(value, int):
            continue
        sampled_ns = int(row.get("sampled_ns", 0) or 0)
        baselines.setdefault(cid, value)
        if sampled_ns >= since_ns and value > baselines[cid]:
            return sampled_ns
    return 0


def shadow_invariant(rows: Iterable[Dict[str, Any]]) -> bool:
    observed = 0
    for row in rows:
        agent = row.get("agentic")
        if not isinstance(agent, dict) or not agent.get("active"):
            continue
        observed += 1
        if agent.get("mode") != "shadow" or agent.get("authoritative") is not False:
            return False
        for decision in agent.get("decisions", []):
            execution = decision.get("execution") or {}
            if execution.get("attempted") or execution.get("executed"):
                return False
    return observed > 0


def fresh_agreement(event: Dict[str, Any], attack_start_ns: int,
                    expected_domains: int) -> bool:
    if event.get("decision") != "AGREED":
        return False
    proposals = event.get("proposals") or []
    participating = set(event.get("participating_domains") or [])
    valid_proposals = [item for item in proposals if isinstance(item, dict)]
    proposal_domains = {str(item.get("cid") or "") for item in valid_proposals}
    if (len(participating) < expected_domains
            or len(valid_proposals) < expected_domains
            or len(proposal_domains - {""}) < expected_domains):
        return False
    return all(
        int(item.get("observation_ns", 0) or 0) >= attack_start_ns
        and int(item.get("created_ns", 0) or 0) >= attack_start_ns
        for item in valid_proposals
    )


def evaluate_episode(path: Path, *, name: str, flow: str,
                     expected_domains: int) -> Dict[str, Any]:
    rows = timeline_rows(path / "timeline.ndjson")
    start_ns = read_ns(path / "attack_start_ns.txt")
    fault_applied_ns = read_ns(path / "fault_applied_ns.txt")
    workload = read_json(path / "workload_status.json", {}) or {}
    events = agent_events(rows, flow, start_ns)
    decisions = sorted({str(event.get("decision")) for event in events})
    agreements = [event for event in events if event.get("decision") == "AGREED"]
    claims = fresh_claims(rows, flow, start_ns)
    errors = agent_errors(rows)
    latency_ms = None
    checks: Dict[str, bool] = {
        "workload_valid": workload.get("valid") is True,
        "shadow_non_authoritative": shadow_invariant(rows),
    }
    if name in FAULT_EPISODES:
        checks.update({
            "no_new_agent_agreement": not agreements,
            "no_new_mcda_claim": not claims,
        })
        if name == "missing-agent":
            checks["quorum_wait_observed"] = "WAITING_PROPOSALS" in decisions
            waiting_times = [
                int(event.get("state_entered_ns", 0) or 0)
                for event in events
                if event.get("decision") == "WAITING_PROPOSALS"
            ]
            if waiting_times and fault_applied_ns:
                latency_ms = round((min(waiting_times) - fault_applied_ns) / 1e6, 3)
        else:
            checks["etcd_error_observed"] = bool(errors) and max(errors) > min(errors)
            first_error_ns = first_agent_error_increase_ns(rows, start_ns)
            if first_error_ns and fault_applied_ns:
                latency_ms = round((first_error_ns - fault_applied_ns) / 1e6, 3)
    elif name in RECOVERY_EPISODES:
        fresh = [
            event for event in agreements
            if fresh_agreement(event, start_ns, expected_domains)
        ]
        agreeing_domains = {event.get("observed_by") for event in fresh}
        if fresh:
            latency_ms = round(
                (min(int(event["state_entered_ns"]) for event in fresh) - start_ns)
                / 1e6,
                3,
            )
        checks.update({
            "fresh_agreement": bool(fresh),
            "all_agents_recovered": len(agreeing_domains) >= expected_domains,
        })
    else:
        checks["known_episode"] = False
    return {
        "name": name,
        "path": str(path),
        "attack_start_ns": start_ns,
        "decisions": decisions,
        "agent_events": len(events),
        "agreements": len(agreements),
        "fresh_claims": len(claims),
        "agent_error_delta": (max(errors) - min(errors)) if errors else None,
        "fault_or_recovery_latency_ms": latency_ms,
        "checks": checks,
        "passed": all(checks.values()),
    }


def evaluate(root: Path, flow: str, expected_domains: int) -> Dict[str, Any]:
    names = [
        "missing-agent",
        "missing-agent-recovery",
        "etcd-partition",
        "etcd-recovery",
    ]
    episodes = [
        evaluate_episode(
            root / name,
            name=name,
            flow=flow,
            expected_domains=expected_domains,
        )
        for name in names
    ]
    blocker_log = root / "flowblocker-requests.log"
    blocker_requests = 0
    if blocker_log.exists():
        blocker_requests = sum(
            "Service request to block traffic" in line
            for line in blocker_log.read_text(encoding="utf-8", errors="replace").splitlines()
        )
    drop_rules = []
    src, dst = flow.split("->", 1)
    for path in root.glob("*/ovs-flows-s*.txt"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if (f"nw_src={src}" in text and f"nw_dst={dst}" in text
                and "actions=drop" in text):
            drop_rules.append(str(path))
    global_checks = {
        "no_flowblocker_request": blocker_requests == 0,
        "no_drop_rule": not drop_rules,
    }
    safe = all(item["passed"] for item in episodes) and all(global_checks.values())
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "flow": flow,
        "expected_domains": expected_domains,
        "mode": "shadow-runtime-fault-injection",
        "episodes": episodes,
        "global_checks": global_checks,
        "flowblocker_requests": blocker_requests,
        "drop_rule_files": drop_rules,
        "aggregate": {
            "safe": safe,
            "passed": sum(item["passed"] for item in episodes),
            "failed": sum(not item["passed"] for item in episodes),
            "total": len(episodes),
        },
    }


def print_summary(report: Dict[str, Any]) -> None:
    print("| episódio | resultado | decisões | eventos | claims | erros agente | latência ms |")
    print("| --- | --- | --- | ---: | ---: | ---: | ---: |")
    for row in report["episodes"]:
        decisions = ",".join(row["decisions"]) or "-"
        error_delta = row["agent_error_delta"]
        print(
            f"| {row['name']} | {'PASS' if row['passed'] else 'FAIL'} | "
            f"{decisions} | {row['agent_events']} | {row['fresh_claims']} | "
            f"{error_delta if error_delta is not None else '-'} | "
            f"{row['fault_or_recovery_latency_ms'] if row['fault_or_recovery_latency_ms'] is not None else '-'} |"
        )
    aggregate = report["aggregate"]
    print(
        f"runtime-fault-gate: {aggregate['passed']}/{aggregate['total']} "
        f"safe={str(aggregate['safe']).lower()} "
        f"flowblocker_requests={report['flowblocker_requests']} "
        f"drop_rules={len(report['drop_rule_files'])}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--flow", default="10.0.0.1->10.0.0.8")
    parser.add_argument("--expected-domains", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if "->" not in args.flow or args.expected_domains < 2:
        parser.error("fluxo inválido ou menos de dois domínios")
    report = evaluate(args.root, args.flow, args.expected_domains)
    output = args.output or (args.root / "summary.json")
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print_summary(report)
    return 0 if report["aggregate"]["safe"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
