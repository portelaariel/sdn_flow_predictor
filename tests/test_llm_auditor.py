import json
import tempfile
import unittest
from pathlib import Path

from llm_auditor.cli import apply_llm, main
from llm_auditor.core import (
    audit_run,
    evaluation_evidence,
    extract_decision_events,
    group_decision_events,
)
from llm_auditor.ollama import (
    EXPLANATION_SCHEMA,
    OllamaAuditClient,
    OllamaAuditError,
)


FLOW = "10.0.0.1->10.0.0.8"
DOMAINS = ["domain-0", "domain-1"]


def agreed_event(cid, timestamp, *, won):
    return {
        "event_id": f"{cid}:agreement:{timestamp}",
        "flow": FLOW,
        "decision": "AGREED",
        "state_entered_ns": timestamp,
        "mode": "authority-dry-run",
        "relevant_domains": DOMAINS,
        "participating_domains": DOMAINS,
        "required_votes": 2,
        "mitigate_votes": DOMAINS,
        "model_ids": ["model-sha"],
        "authority": {
            "authorized": True,
            "code": "authorized_POLICY_DECISION",
            "claim": {"won": won, "coordinator": "domain-0"},
        },
        "execution": {
            "attempted": False,
            "executed": False,
            "would_execute": won,
            "reason": (
                "authority-dry-run: claim adquirido; FlowBlocker desconectado"
                if won else
                "authority-dry-run: decisão autorizada, mas outro agente coordena"
            ),
        },
    }


def timeline_row(cid, timestamp, agent_events=None, mcda_events=None):
    return {
        "sampled_ns": timestamp + 10,
        "status": {"cid": cid},
        "agentic": {"cid": cid, "decision_events": agent_events or []},
        "collaboration": {
            "cid": cid, "decision_events": mcda_events or []
        },
    }


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


class LLMAuditorTests(unittest.TestCase):
    def write_run(self, root, rows, *, scenario="ddos", classification="TP"):
        root = Path(root)
        (root / "metadata.json").write_text(json.dumps({
            "scenario": scenario,
            "flow": FLOW,
            "mode": "collaborative-dry-run",
            "agentic_mode": "authority-dry-run",
            "started_ns": 100,
            "git_commit": "abc123",
        }), encoding="utf-8")
        (root / "summary.json").write_text(json.dumps({
            "runs": [{
                "classification": classification,
                "attack_disrupted": False,
            }]
        }), encoding="utf-8")
        (root / "timeline.ndjson").write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        if scenario == "ddos":
            (root / "attack_start_ns.txt").write_text("100\n", encoding="utf-8")

    def valid_rows(self):
        suspect = {
            "event_id": "mcda:suspect", "flow": FLOW,
            "decision": "SUSPECT", "evaluated_ns": 150,
        }
        waiting = {
            "event_id": "agent:waiting", "flow": FLOW,
            "decision": "WAITING_PROPOSALS", "evaluated_ns": 160,
            "missing_domains": ["domain-1"],
        }
        mitigate = {
            "event_id": "mcda:mitigate", "flow": FLOW,
            "decision": "MITIGATE", "evaluated_ns": 180,
            "participating_domains": DOMAINS,
        }
        return [
            timeline_row("domain-0", 150, [waiting], [suspect]),
            timeline_row("domain-0", 200, [agreed_event("domain-0", 200, won=True)],
                         [mitigate]),
            timeline_row("domain-1", 210, [agreed_event("domain-1", 210, won=False)]),
        ]

    def test_extracts_both_layers_and_deduplicates_event_id(self):
        rows = self.valid_rows()
        rows.append(rows[-1])
        events = extract_decision_events(rows, flow=FLOW)
        self.assertEqual(len(events), 5)
        self.assertEqual(
            {event["_audit"]["layer"] for event in events},
            {"agentic", "mcda"},
        )

    def test_groups_same_flow_and_splits_on_large_gap(self):
        first = agreed_event("domain-0", 100, won=True)
        second = agreed_event("domain-0", int(20e9), won=True)
        for event in (first, second):
            event["_audit"] = {
                "layer": "agentic", "observed_by": "domain-0",
                "sampled_ns": event["state_entered_ns"],
            }
        episodes = group_decision_events([first, second], gap_s=15)
        self.assertEqual(len(episodes), 2)

    def test_authority_dry_run_is_consistent_and_not_applicable(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_run(tmp, self.valid_rows())
            report = audit_run(Path(tmp))
        self.assertEqual(report["events_seen"], 5)
        self.assertEqual(len(report["episodes"]), 1)
        episode = report["episodes"][0]
        self.assertEqual(episode["protocol_consistency"], "CONSISTENT")
        self.assertEqual(episode["scenario_correctness"], "CORRECT")
        self.assertEqual(episode["execution_status"], "DRY_RUN_SUPPRESSED")
        self.assertEqual(
            episode["operational_effectiveness"], "NOT_APPLICABLE"
        )
        self.assertEqual(episode["claim_winners"], ["domain-0"])
        self.assertEqual(
            [item["state"] for item in episode["transitions"]],
            ["SUSPECT", "WAITING_PROPOSALS", "MITIGATE", "AGREED"],
        )

    def test_two_claim_winners_are_inconsistent(self):
        rows = self.valid_rows()
        rows[-1] = timeline_row(
            "domain-1", 210, [agreed_event("domain-1", 210, won=True)]
        )
        with tempfile.TemporaryDirectory() as tmp:
            self.write_run(tmp, rows)
            episode = audit_run(Path(tmp))["episodes"][0]
        self.assertEqual(episode["protocol_consistency"], "INCONSISTENT")
        check = next(item for item in episode["checks"]
                     if item["name"] == "single_atomic_claim_winner")
        self.assertEqual(check["status"], "FAIL")

    def test_benign_run_without_events_is_valid_empty_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_run(tmp, [], scenario="benign", classification="TN")
            report = audit_run(Path(tmp))
        self.assertEqual(report["run"]["audit_status"], "NO_AUDITABLE_EVENTS")
        self.assertEqual(report["run"]["scenario_correctness"], "CORRECT")
        self.assertEqual(report["episodes"], [])

    def test_false_positive_is_scenario_incorrect(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_run(
                tmp, self.valid_rows(), scenario="benign", classification="FP"
            )
            episode = audit_run(Path(tmp))["episodes"][0]
        self.assertEqual(episode["scenario_correctness"], "INCORRECT")

    def test_veto_is_consistent_when_veto_evidence_is_present(self):
        vetoed = {
            "event_id": "agent:vetoed", "flow": FLOW,
            "decision": "VETOED", "evaluated_ns": 200,
            "veto_domains": ["domain-1"],
            "proposals": [{"cid": "domain-1", "proposal": "VETO"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            self.write_run(tmp, [timeline_row("domain-0", 200, [vetoed])])
            episode = audit_run(Path(tmp))["episodes"][0]
        self.assertEqual(episode["decision_stage"], "FINAL")
        self.assertEqual(episode["protocol_consistency"], "CONSISTENT")

    def test_mcda_normal_is_consistent_without_confirming_domains(self):
        normal = {
            "event_id": "mcda:normal", "flow": FLOW,
            "decision": "NORMAL", "evaluated_ns": 200,
            "confirming_domains": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            self.write_run(
                tmp, [timeline_row("domain-0", 200, mcda_events=[normal])],
                scenario="benign", classification="TN",
            )
            episode = audit_run(Path(tmp))["episodes"][0]
        self.assertEqual(episode["protocol_consistency"], "CONSISTENT")
        self.assertEqual(episode["scenario_correctness"], "CORRECT")

    def test_evaluation_evidence_hides_deterministic_oracle(self):
        record = {
            "protocol_consistency": "CONSISTENT",
            "scenario_correctness": "CORRECT",
            "operational_effectiveness": "NOT_APPLICABLE",
            "checks": [{"status": "PASS"}],
            "decision_stage": "FINAL",
            "laboratory_context": {
                "scenario": "ddos", "classification": "TP",
                "ground_truth_available": True,
            },
        }
        evidence = evaluation_evidence(record)
        self.assertNotIn("protocol_consistency", evidence)
        self.assertNotIn("checks", evidence)
        self.assertNotIn("decision_stage", evidence)
        self.assertNotIn("execution_status", evidence)
        self.assertNotIn("source_event_ids", evidence)
        self.assertNotIn("classification", evidence["laboratory_context"])

    def test_ollama_request_uses_schema_and_reproducible_parameters(self):
        captured = {}
        explanation = {
            "summary": "Consenso válido em dry-run.",
            "supporting_evidence": ["Quórum 2/2."],
            "contradicting_evidence": [],
            "missing_information": [],
        }

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse({
                "model": "qwen3.5:9b",
                "message": {"content": json.dumps(explanation)},
                "eval_count": 20,
            })

        client = OllamaAuditClient(
            base_url="http://127.0.0.1:12434", opener=opener
        )
        response = client.explain({"protocol_consistency": "CONSISTENT"})
        payload = captured["payload"]
        self.assertEqual(captured["url"], "http://127.0.0.1:12434/api/chat")
        self.assertEqual(payload["format"], EXPLANATION_SCHEMA)
        self.assertEqual(payload["options"]["temperature"], 0.0)
        self.assertEqual(payload["options"]["seed"], 42)
        self.assertEqual(payload["options"]["num_ctx"], 4096)
        self.assertEqual(payload["keep_alive"], 0)
        self.assertEqual(response["result"], explanation)

    def test_ollama_rejects_non_json_content(self):
        client = OllamaAuditClient(opener=lambda request, timeout: FakeResponse({
            "message": {"content": "not-json"}
        }))
        with self.assertRaises(OllamaAuditError):
            client.explain({"protocol_consistency": "CONSISTENT"})

    def test_evaluate_mode_records_comparison_with_oracle(self):
        report = {"episodes": [{
            "protocol_consistency": "CONSISTENT",
            "scenario_correctness": "CORRECT",
            "decision_stage": "FINAL",
            "execution_status": "DRY_RUN_SUPPRESSED",
            "operational_effectiveness": "NOT_APPLICABLE",
            "checks": [],
            "laboratory_context": {"scenario": "ddos", "classification": "TP"},
        }]}

        class Client:
            @staticmethod
            def evaluate(evidence):
                return {"result": {
                    "protocol_consistency": "CONSISTENT",
                    "scenario_correctness": "CORRECT",
                    "decision_stage": "FINAL",
                    "execution_status": "DRY_RUN_SUPPRESSED",
                    "operational_effectiveness": "NOT_APPLICABLE",
                    "confidence": 1.0,
                    "summary": "ok",
                    "supporting_evidence": [],
                    "contradicting_evidence": [],
                    "missing_information": [],
                }}

        apply_llm(report, mode="evaluate", client=Client())
        self.assertTrue(report["episodes"][0]["llm_vs_deterministic"]["all_match"])

    def test_cli_writes_audit_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            self.write_run(run_dir, self.valid_rows())
            output = Path(tmp) / "audit.json"
            status = main([
                str(run_dir), "--mode", "audit", "--output", str(output)
            ])
            self.assertEqual(status, 0)
            self.assertTrue(output.is_file())
            self.assertTrue(output.with_suffix(".md").is_file())
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["episodes"][0]["protocol_consistency"],
                             "CONSISTENT")


if __name__ == "__main__":
    unittest.main()
