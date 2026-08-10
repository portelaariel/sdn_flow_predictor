import unittest

from agent_authority import claim_agentic_mitigation
from experiments.run_agentic_fault_suite import run_suite


class AgentAuthoritySafetyTests(unittest.TestCase):
    def test_fault_matrix_has_no_unsafe_authorization(self):
        payload = run_suite()
        aggregate = payload["aggregate"]

        self.assertGreaterEqual(aggregate["total"], 16)
        self.assertEqual(aggregate["passed"], aggregate["total"])
        self.assertEqual(aggregate["failed"], 0)
        self.assertEqual(aggregate["unsafe_authorizations"], 0)
        self.assertEqual(aggregate["claim_invariant_violations"], 0)
        self.assertTrue(aggregate["safe"])
        self.assertFalse(payload["dataplane_touched"])

        cases = {row["name"]: row for row in payload["cases"]}
        self.assertFalse(cases["missing_agent"]["authorized"])
        self.assertFalse(cases["old_episode_isolation"]["authorized"])
        self.assertFalse(cases["stale_agreement_rejected"]["authorized"])
        self.assertFalse(cases["forged_quorum_rejected"]["authorized"])
        self.assertTrue(cases["atomic_claim_single_winner"]["passed"])

    def test_unauthorized_decision_never_touches_etcd(self):
        class ExplodingEtcd:
            def __getattr__(self, _name):
                raise AssertionError("ETCD não deveria ser acessado")

        claim = claim_agentic_mitigation(
            ExplodingEtcd(),
            {"authorized": False, "flow": "10.0.0.1->10.0.0.8"},
            coordinator="domain-0",
            now_ns_value=10_000_000_000,
        )

        self.assertFalse(claim["won"])
        self.assertFalse(claim["degraded"])
        self.assertEqual(claim["reason"], "decisão não autorizada")


if __name__ == "__main__":
    unittest.main()
