#!/usr/bin/env python3
"""Injeta falhas determinísticas no protocolo agentic sem tocar no dataplane."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_authority import (
    claim_agentic_mitigation,
    evaluate_agentic_authority,
)
from domain_agent import DomainAgent


NOW_NS = 10_000_000_000
FLOW = "10.0.0.1->10.0.0.8"
DOMAINS = ["domain-0", "domain-1"]


def new_agent(cid: str = "domain-0") -> DomainAgent:
    return DomainAgent(
        cid,
        proposal_threshold=0.65,
        persistence_windows=3,
        rate_ratio_max=10.0,
        proposal_ttl_s=12.0,
        required_votes=2,
        negotiation_window_s=4.0,
    )


def evidence(*, model_id: str = "holt:model-a", z_score: float = 15.0,
             window_id: int = 2, ts_ns: int = 9_500_000_000) -> Dict[str, Any]:
    return {
        "flow": FLOW,
        "src_ip": "10.0.0.1",
        "dst_ip": "10.0.0.8",
        "window_id": window_id,
        "ts_ns": ts_ns,
        "observed_bps": 100_000_000.0,
        "predicted_bps": 1_000_000.0,
        "z_score": z_score,
        "threshold": 5.0,
        "persistence_windows": 3,
        "model_id": model_id,
        "model_reliability": 0.92,
    }


def proposal(cid: str, role: str, *, model_id: str = "holt:model-a",
             z_score: float = 15.0, veto_reason: Optional[str] = None,
             source_cid: str = "domain-0", destination_cid: str = "domain-1",
             created_ns: int = NOW_NS, window_id: int = 2) -> Dict[str, Any]:
    return new_agent(cid).build_proposal(
        evidence(model_id=model_id, z_score=z_score, window_id=window_id),
        role=role,
        relevant_domains=sorted({source_cid, destination_cid}),
        source_cid=source_cid,
        destination_cid=destination_cid,
        created_ns=created_ns,
        veto_reason=veto_reason,
    )


def decide(rows: List[Dict[str, Any]], *, now_ns_value: int = NOW_NS + 1) -> Dict[str, Any]:
    result = new_agent().decide(rows, flow=FLOW, now_ns_value=now_ns_value)
    windows = ",".join(str(value) for value in result.get("window_ids", [])) or "none"
    result["event_id"] = f"fault:{windows}:{result['decision']}:{now_ns_value}"
    result["state_entered_ns"] = now_ns_value
    return result


class FakeEtcd:
    class Lease:
        def __init__(self, lease_id: int):
            self.id = lease_id

    class Version:
        def __init__(self, key: str):
            self.key = key

        def __eq__(self, value: object) -> tuple:
            return ("version", self.key, value)

    class Transactions:
        def version(self, key: str) -> "FakeEtcd.Version":
            return FakeEtcd.Version(key)

        @staticmethod
        def put(key: str, value: str, lease_id: int) -> tuple:
            return ("put", key, value, lease_id)

    def __init__(self):
        self.values: Dict[str, bytes] = {}
        self.transactions = self.Transactions()
        self.next_lease = 1

    def lease(self, _ttl: int) -> "FakeEtcd.Lease":
        lease = self.Lease(self.next_lease)
        self.next_lease += 1
        return lease

    def transaction(self, *, compare: list, success: list, failure: list) -> tuple:
        _kind, key, expected_version = compare[0]
        won = (expected_version == 0 and key not in self.values)
        operations = success if won else failure
        for kind, operation_key, value, _lease_id in operations:
            if kind == "put":
                self.values[operation_key] = value.encode("utf-8")
        return won, []

    def get(self, key: str) -> tuple:
        return self.values.get(key), None


class FailingEtcd:
    class Transactions:
        @staticmethod
        def version(_key: str) -> None:
            raise ConnectionError("falha injetada")

    transactions = Transactions()

    @staticmethod
    def lease(_ttl: int) -> None:
        raise ConnectionError("falha injetada")


def _case(name: str, expected_decision: str, decision: Optional[Dict[str, Any]],
          expected_authorized: bool, *, authorization: Optional[Dict[str, Any]] = None,
          extra_ok: bool = True, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    evaluation_ns = (
        int(decision.get("evaluated_ns", NOW_NS + 1)) + 1
        if isinstance(decision, dict) else NOW_NS + 2
    )
    auth = authorization or evaluate_agentic_authority(
        decision, now_ns_value=evaluation_ns, expected_flow=FLOW
    )
    observed_decision = (
        decision.get("decision") if isinstance(decision, dict) else "NO_DECISION"
    )
    passed = (
        observed_decision == expected_decision
        and auth["authorized"] is expected_authorized
        and extra_ok
    )
    return {
        "name": name,
        "expected_decision": expected_decision,
        "observed_decision": observed_decision,
        "expected_authorized": expected_authorized,
        "authorized": auth["authorized"],
        "authorization_code": auth["code"],
        "passed": passed,
        "details": details or {},
    }


def run_suite() -> Dict[str, Any]:
    source = proposal("domain-0", "SOURCE")
    destination = proposal("domain-1", "DESTINATION")
    agreed = decide([source, destination])
    rows = [_case("valid_agreement", "AGREED", agreed, True)]

    waiting = decide([source])
    rows.append(_case(
        "missing_agent", "WAITING_PROPOSALS", waiting, False
    ))

    expired = decide([source, destination], now_ns_value=NOW_NS + 13_000_000_000)
    rows.append(_case("expired_proposals", "NO_PROPOSALS", expired, False))

    model_mismatch = decide([
        source,
        proposal("domain-1", "DESTINATION", model_id="holt:model-b"),
    ])
    rows.append(_case(
        "model_mismatch", "MODEL_MISMATCH", model_mismatch, False
    ))

    reverse_topology = proposal(
        "domain-1", "SOURCE", source_cid="domain-1", destination_cid="domain-0"
    )
    topology_mismatch = decide([source, reverse_topology])
    rows.append(_case(
        "topology_mismatch", "TOPOLOGY_MISMATCH", topology_mismatch, False
    ))

    veto = decide([
        source,
        proposal(
            "domain-1", "DESTINATION", veto_reason="whitelist injetada"
        ),
    ])
    rows.append(_case("whitelist_veto", "VETOED", veto, False))

    disagreement = decide([
        source,
        proposal("domain-1", "DESTINATION", z_score=2.0),
    ])
    rows.append(_case("normal_disagreement", "DISAGREED", disagreement, False))

    duplicate_source = proposal(
        "domain-0", "SOURCE", created_ns=NOW_NS + 100_000_000
    )
    deduplicated = decide(
        [source, duplicate_source, destination], now_ns_value=NOW_NS + 200_000_000
    )
    duplicate_ok = (
        len(deduplicated.get("proposals", [])) == 2
        and deduplicated.get("participating_domains") == DOMAINS
    )
    rows.append(_case(
        "duplicate_proposal_collapsed", "AGREED", deduplicated, True,
        extra_ok=duplicate_ok,
        details={"proposals_after_deduplication": len(deduplicated.get("proposals", []))},
    ))

    wrong_episode_auth = evaluate_agentic_authority(
        agreed,
        now_ns_value=NOW_NS + 2,
        expected_flow=FLOW,
        expected_window_ids=[99],
    )
    rows.append(_case(
        "old_episode_isolation", "AGREED", agreed, False,
        authorization=wrong_episode_auth,
    ))

    stale_auth = evaluate_agentic_authority(
        agreed,
        now_ns_value=min(item["expires_ns"] for item in agreed["proposals"]),
        expected_flow=FLOW,
    )
    rows.append(_case(
        "stale_agreement_rejected", "AGREED", agreed, False,
        authorization=stale_auth,
    ))

    forged_quorum = json.loads(json.dumps(agreed))
    forged_quorum["required_votes"] = 2
    forged_quorum["mitigate_votes"] = ["domain-0"]
    rows.append(_case(
        "forged_quorum_rejected", "AGREED", forged_quorum, False
    ))

    forged_duplicate = json.loads(json.dumps(agreed))
    forged_duplicate["proposals"].append(
        json.loads(json.dumps(forged_duplicate["proposals"][0]))
    )
    rows.append(_case(
        "forged_duplicate_domain_rejected", "AGREED", forged_duplicate, False
    ))

    forged_role = json.loads(json.dumps(agreed))
    forged_role["proposals"][0]["role"] = "DESTINATION"
    rows.append(_case(
        "forged_topology_role_rejected", "AGREED", forged_role, False
    ))

    missing_event = json.loads(json.dumps(agreed))
    missing_event.pop("event_id")
    rows.append(_case(
        "missing_event_identity_rejected", "AGREED", missing_event, False
    ))

    future_proposal = json.loads(json.dumps(agreed))
    future_proposal["proposals"][0]["created_ns"] = NOW_NS + 1_000_000_000
    rows.append(_case(
        "future_proposal_rejected", "AGREED", future_proposal, False
    ))

    authorization = evaluate_agentic_authority(
        agreed, now_ns_value=NOW_NS + 2, expected_flow=FLOW
    )
    failing_claim = claim_agentic_mitigation(
        FailingEtcd(), authorization,
        coordinator="domain-0", now_ns_value=NOW_NS + 3,
    )
    rows.append(_case(
        "etcd_unavailable_fails_closed", "AGREED", agreed, True,
        authorization=authorization,
        extra_ok=(not failing_claim["won"] and failing_claim["degraded"]),
        details={"claims": [failing_claim], "expected_claim_winners": 0},
    ))

    etcd = FakeEtcd()
    claim_a = claim_agentic_mitigation(
        etcd, authorization, coordinator="domain-0", now_ns_value=NOW_NS + 3
    )
    claim_b = claim_agentic_mitigation(
        etcd, authorization, coordinator="domain-1", now_ns_value=NOW_NS + 4
    )
    single_winner = sum((claim_a["won"], claim_b["won"])) == 1
    rows.append(_case(
        "atomic_claim_single_winner", "AGREED", agreed, True,
        authorization=authorization,
        extra_ok=(single_winner and claim_a["coordinator"] == claim_b["coordinator"]),
        details={
            "claims": [claim_a, claim_b], "expected_claim_winners": 1,
        },
    ))

    expired_claim = claim_agentic_mitigation(
        FakeEtcd(), authorization, coordinator="domain-0",
        now_ns_value=authorization["expires_ns"],
    )
    rows.append(_case(
        "authorization_expires_before_claim", "AGREED", agreed, True,
        authorization=authorization,
        extra_ok=(not expired_claim["won"] and not expired_claim["degraded"]),
        details={"claims": [expired_claim], "expected_claim_winners": 0},
    ))

    negative_rows = [row for row in rows if not row["expected_authorized"]]
    unsafe_authorizations = sum(row["authorized"] for row in negative_rows)
    claim_cases = [row for row in rows if "claims" in row["details"]]
    claim_invariant_violations = sum(
        sum(bool(claim.get("won")) for claim in row["details"]["claims"])
        != row["details"]["expected_claim_winners"]
        for row in claim_cases
    )
    passed = sum(row["passed"] for row in rows)
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "offline_fault_injection",
        "dataplane_touched": False,
        "cases": rows,
        "aggregate": {
            "total": len(rows),
            "passed": passed,
            "failed": len(rows) - passed,
            "negative_cases": len(negative_rows),
            "unsafe_authorizations": unsafe_authorizations,
            "claim_cases": len(claim_cases),
            "claim_invariant_violations": claim_invariant_violations,
            "safe": (
                passed == len(rows)
                and unsafe_authorizations == 0
                and claim_invariant_violations == 0
            ),
        },
    }


def markdown_report(payload: Dict[str, Any]) -> str:
    lines = [
        "| cenário | decisão observada | autorização | código | claims vencedores | resultado |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in payload["cases"]:
        claims = row["details"].get("claims", [])
        claim_summary = (
            f"{sum(bool(claim.get('won')) for claim in claims)}/"
            f"{len(claims)}" if claims else "-"
        )
        lines.append(
            f"| {row['name']} | {row['observed_decision']} | "
            f"{row['authorized']} | {row['authorization_code']} | "
            f"{claim_summary} | "
            f"{'PASS' if row['passed'] else 'FAIL'} |"
        )
    aggregate = payload["aggregate"]
    lines.extend([
        "",
        (f"PASS={aggregate['passed']}/{aggregate['total']} "
         f"unsafe_authorizations={aggregate['unsafe_authorizations']} "
         f"claim_invariant_violations={aggregate['claim_invariant_violations']} "
         f"safe={aggregate['safe']} dataplane_touched=False"),
    ])
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path,
        help="prefixo opcional para os relatórios .json e .md",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = run_suite()
    report = markdown_report(payload)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.with_suffix(".json").write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        args.output.with_suffix(".md").write_text(report, encoding="utf-8")
    if args.quiet:
        aggregate = payload["aggregate"]
        print(
            f"agentic_faults: {aggregate['passed']}/{aggregate['total']} passed; "
            f"unsafe_authorizations={aggregate['unsafe_authorizations']}; "
            f"claim_invariant_violations={aggregate['claim_invariant_violations']}"
        )
    else:
        print(report, end="")
    return 0 if payload["aggregate"]["safe"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
