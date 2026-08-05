import unittest

from agent_protocol import validate_agent_proposal
from domain_agent import DomainAgent, domain_role


class DomainAgentTests(unittest.TestCase):
    NOW_NS = 10_000_000_000

    @staticmethod
    def evidence(*, model_id="holt:model-a", z_score=15.0,
                 persistence=2, window_id=2, ts_ns=9_500_000_000):
        return {
            "flow": "10.0.0.1->10.0.0.8",
            "src_ip": "10.0.0.1",
            "dst_ip": "10.0.0.8",
            "window_id": window_id,
            "ts_ns": ts_ns,
            "observed_bps": 100_000_000.0,
            "predicted_bps": 1_000_000.0,
            "z_score": z_score,
            "threshold": 5.0,
            "persistence_windows": persistence,
            "model_id": model_id,
            "model_reliability": 0.92,
        }

    @staticmethod
    def agent(cid, required_votes=2):
        return DomainAgent(
            cid,
            proposal_threshold=0.65,
            persistence_windows=3,
            rate_ratio_max=10.0,
            proposal_ttl_s=12.0,
            required_votes=required_votes,
            negotiation_window_s=4.0,
        )

    def proposal(self, cid, role, *, model_id="holt:model-a",
                 veto_reason=None, relevant=None, z_score=15.0,
                 source_cid="domain-0", destination_cid="domain-1",
                 window_id=2, ts_ns=9_500_000_000,
                 created_ns=None):
        if role == "LOCAL":
            source_cid = destination_cid = cid
        elif role == "UNKNOWN":
            source_cid = destination_cid = None
        return self.agent(cid).build_proposal(
            self.evidence(
                model_id=model_id,
                z_score=z_score,
                window_id=window_id,
                ts_ns=ts_ns,
            ),
            role=role,
            relevant_domains=(relevant or ["domain-0", "domain-1"]),
            source_cid=source_cid,
            destination_cid=destination_cid,
            created_ns=self.NOW_NS if created_ns is None else created_ns,
            veto_reason=veto_reason,
        )

    def test_topology_roles_distinguish_source_destination_and_local(self):
        self.assertEqual(
            domain_role("domain-0", "domain-0", "domain-1"),
            ("SOURCE", ["domain-0", "domain-1"]),
        )
        self.assertEqual(
            domain_role("domain-1", "domain-0", "domain-1")[0],
            "DESTINATION",
        )
        self.assertEqual(
            domain_role("domain-0", "domain-0", "domain-0"),
            ("LOCAL", ["domain-0"]),
        )

    def test_holt_evidence_becomes_a_valid_mitigation_proposal(self):
        proposal = self.proposal("domain-0", "SOURCE")

        self.assertEqual(proposal["proposal"], "MITIGATE")
        self.assertEqual(proposal["belief"], "DDOS_LIKELY")
        self.assertGreaterEqual(proposal["confidence"], 0.65)
        self.assertEqual(validate_agent_proposal(proposal), proposal)

    def test_unknown_topology_waits_instead_of_authorizing_action(self):
        proposal = self.agent("domain-0").build_proposal(
            self.evidence(),
            role="UNKNOWN",
            relevant_domains=[],
            source_cid=None,
            destination_cid=None,
            created_ns=self.NOW_NS,
        )

        self.assertEqual(proposal["proposal"], "WAIT")
        self.assertEqual(proposal["belief"], "TOPOLOGY_UNKNOWN")

    def test_two_relevant_agents_must_agree(self):
        coordinator = self.agent("domain-0")
        proposals = [
            self.proposal(
                "domain-0", "SOURCE", created_ns=self.NOW_NS - 200_000_000
            ),
            self.proposal(
                "domain-1", "DESTINATION", created_ns=self.NOW_NS - 50_000_000
            ),
        ]

        decision = coordinator.decide(
            proposals,
            flow="10.0.0.1->10.0.0.8",
            now_ns_value=self.NOW_NS + 1,
        )

        self.assertEqual(decision["decision"], "AGREED")
        self.assertEqual(decision["mitigate_votes"], ["domain-0", "domain-1"])
        self.assertEqual(decision["required_votes"], 2)
        self.assertEqual(decision["source_cid"], "domain-0")
        self.assertEqual(decision["destination_cid"], "domain-1")
        self.assertEqual(decision["first_proposal_ns"], self.NOW_NS - 200_000_000)
        self.assertEqual(decision["last_proposal_ns"], self.NOW_NS - 50_000_000)
        self.assertEqual(decision["proposal_collection_latency_ms"], 150.0)
        self.assertEqual(decision["decision_after_last_proposal_ms"], 50.0)

    def test_missing_peer_proposal_waits(self):
        decision = self.agent("domain-0").decide(
            [self.proposal("domain-0", "SOURCE")],
            flow="10.0.0.1->10.0.0.8",
            now_ns_value=self.NOW_NS + 1,
        )

        self.assertEqual(decision["decision"], "WAITING_PROPOSALS")
        self.assertEqual(decision["missing_domains"], ["domain-1"])

    def test_veto_and_model_mismatch_prevent_agreement(self):
        veto = self.proposal(
            "domain-1", "DESTINATION", veto_reason="destino em whitelist"
        )
        decision = self.agent("domain-0").decide(
            [self.proposal("domain-0", "SOURCE"), veto],
            flow="10.0.0.1->10.0.0.8",
            now_ns_value=self.NOW_NS + 1,
        )
        self.assertEqual(decision["decision"], "VETOED")

        mismatch = self.agent("domain-0").decide(
            [
                self.proposal("domain-0", "SOURCE"),
                self.proposal(
                    "domain-1", "DESTINATION", model_id="holt:model-b"
                ),
            ],
            flow="10.0.0.1->10.0.0.8",
            now_ns_value=self.NOW_NS + 1,
        )
        self.assertEqual(mismatch["decision"], "MODEL_MISMATCH")

    def test_local_flow_requires_only_its_responsible_agent(self):
        proposal = self.proposal(
            "domain-0", "LOCAL", relevant=["domain-0"]
        )
        decision = self.agent("domain-0", required_votes=2).decide(
            [proposal],
            flow="10.0.0.1->10.0.0.8",
            now_ns_value=self.NOW_NS + 1,
        )

        self.assertEqual(decision["decision"], "AGREED")
        self.assertEqual(decision["required_votes"], 1)

    def test_expired_proposals_do_not_participate(self):
        decision = self.agent("domain-0").decide(
            [self.proposal("domain-0", "SOURCE")],
            flow="10.0.0.1->10.0.0.8",
            now_ns_value=self.NOW_NS + 13_000_000_000,
        )
        self.assertEqual(decision["decision"], "NO_PROPOSALS")

    def test_protocol_rejects_forged_agent_identity(self):
        proposal = self.proposal("domain-0", "SOURCE")
        proposal["agent_id"] = "sdn-domain:domain-1"
        with self.assertRaisesRegex(ValueError, "agent_id"):
            validate_agent_proposal(proposal)

    def test_conflicting_ordered_topology_never_agrees(self):
        proposals = [
            self.proposal("domain-0", "SOURCE"),
            self.proposal(
                "domain-1",
                "SOURCE",
                source_cid="domain-1",
                destination_cid="domain-0",
            ),
        ]

        decision = self.agent("domain-0").decide(
            proposals,
            flow="10.0.0.1->10.0.0.8",
            now_ns_value=self.NOW_NS + 1,
        )

        self.assertEqual(decision["decision"], "TOPOLOGY_MISMATCH")

        forged_role = self.proposal("domain-1", "DESTINATION")
        forged_role["role"] = "SOURCE"
        with self.assertRaisesRegex(ValueError, "role"):
            validate_agent_proposal(forged_role)

    def test_negotiation_uses_observation_time_not_retry_time(self):
        proposals = [
            self.proposal(
                "domain-0", "SOURCE", window_id=2,
                ts_ns=5_000_000_000, created_ns=self.NOW_NS,
            ),
            self.proposal(
                "domain-1", "DESTINATION", window_id=3,
                ts_ns=9_500_000_000, created_ns=self.NOW_NS,
            ),
        ]

        decision = self.agent("domain-0").decide(
            proposals,
            flow="10.0.0.1->10.0.0.8",
            now_ns_value=self.NOW_NS + 1,
        )

        self.assertEqual(decision["decision"], "WAITING_WINDOW")

    def test_configured_vote_quorum_is_effective_after_all_agents_report(self):
        decision = self.agent("domain-0", required_votes=1).decide(
            [
                self.proposal("domain-0", "SOURCE"),
                self.proposal("domain-1", "DESTINATION", z_score=5.1),
            ],
            flow="10.0.0.1->10.0.0.8",
            now_ns_value=self.NOW_NS + 1,
        )

        self.assertEqual(decision["decision"], "AGREED")
        self.assertEqual(decision["required_votes"], 1)

    def test_observer_window_does_not_change_the_decided_episode(self):
        decision = self.agent("domain-0").decide(
            [
                self.proposal("domain-0", "SOURCE"),
                self.proposal("domain-1", "DESTINATION"),
                self.proposal(
                    "domain-2", "OBSERVER", window_id=1,
                    ts_ns=1_000_000_000,
                ),
            ],
            flow="10.0.0.1->10.0.0.8",
            now_ns_value=self.NOW_NS + 1,
        )

        self.assertEqual(decision["decision"], "AGREED")
        self.assertEqual(decision["window_ids"], [2])
        self.assertIn("domain-2", decision["participating_domains"])

        waiting = self.agent("domain-0").decide(
            [
                self.proposal("domain-0", "SOURCE"),
                self.proposal(
                    "domain-2", "OBSERVER", window_id=1,
                    ts_ns=1_000_000_000,
                ),
            ],
            flow="10.0.0.1->10.0.0.8",
            now_ns_value=self.NOW_NS + 1,
        )
        self.assertEqual(waiting["decision"], "WAITING_PROPOSALS")
        self.assertEqual(waiting["window_ids"], [2])


if __name__ == "__main__":
    unittest.main()
