import json
import tempfile
import unittest
from pathlib import Path

from experiments.monitor_predictors import flow_anomalies, flow_predictions
from experiments.run_mininet_workload import domain_table_has_hosts
from experiments.summarize_benchmark import (
    aggregate_metrics,
    markdown_table,
    summarize_run,
)


class BenchmarkToolTests(unittest.TestCase):
    @staticmethod
    def write_iperf(path, bits_per_second):
        path.write_text(json.dumps({
            "end": {"sum": {"bits_per_second": bits_per_second}}
        }), encoding="utf-8")

    def test_flow_anomalies_selects_only_requested_pair(self):
        payload = {
            "anomalies": [
                {"anomaly_id": "wanted", "meta": {
                    "nw_src": "10.0.0.1", "nw_dst": "10.0.0.8"}},
                {"anomaly_id": "other", "meta": {
                    "nw_src": "10.0.0.2", "nw_dst": "10.0.0.8"}},
            ]
        }
        selected = flow_anomalies(payload, "10.0.0.1->10.0.0.8")
        self.assertEqual([row["anomaly_id"] for row in selected], ["wanted"])

        prediction_payload = {
            "predictions": [
                {"key": "wanted", "meta": {
                    "nw_src": "10.0.0.1", "nw_dst": "10.0.0.8"}},
                {"key": "other", "meta": {
                    "nw_src": "10.0.0.2", "nw_dst": "10.0.0.8"}},
            ]
        }
        predictions = flow_predictions(
            prediction_payload, "10.0.0.1->10.0.0.8"
        )
        self.assertEqual([row["key"] for row in predictions], ["wanted"])

    def test_domain_table_requires_both_mitigation_endpoints(self):
        payload = {"hosts": {"10.0.0.1": {"dpid": 1}}}
        self.assertFalse(domain_table_has_hosts(
            payload, "10.0.0.1", "10.0.0.8"
        ))
        payload["hosts"]["10.0.0.8"] = {"dpid": 4}
        self.assertTrue(domain_table_has_hosts(
            payload, "10.0.0.1", "10.0.0.8", 1, 4
        ))
        self.assertFalse(domain_table_has_hosts(
            payload, "10.0.0.1", "10.0.0.8", 1, 3
        ))

    def test_agentic_run_is_invalid_when_agents_or_decisions_are_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "inactive-agentic-ddos"
            run_dir.mkdir()
            (run_dir / "metadata.json").write_text(json.dumps({
                "mode": "collaborative-dry-run",
                "scenario": "ddos",
                "flow": "10.0.0.1->10.0.0.8",
                "controller_sets": 2,
                "agentic_shadow": True,
            }), encoding="utf-8")
            (run_dir / "attack_start_ns.txt").write_text(
                "1000000000\n", encoding="utf-8"
            )
            (run_dir / "workload_status.json").write_text(
                json.dumps({"valid": True}), encoding="utf-8"
            )
            for name in ("ping_before.txt", "ping_after.txt"):
                (run_dir / name).write_text(
                    "3 packets transmitted, 3 received, 0% packet loss\n",
                    encoding="utf-8",
                )
            self.write_iperf(run_dir / "baseline.json", 1_000_000)
            self.write_iperf(run_dir / "attack.json", 100_000_000)
            rows = [
                {
                    "sampled_ns": 1_500_000_000,
                    "port": str(6060 + index),
                    "status": {"cid": f"domain-{index}"},
                    "agentic": {
                        "requested": True,
                        "active": False,
                        "mode": "shadow",
                        "authoritative": False,
                        "cid": f"domain-{index}",
                        "decisions": [],
                    },
                    "anomalies": ([{
                        "anomaly_id": "attack-a",
                        "kind": "THROUGHPUT_SPIKE",
                        "ts_detect_ns": 1_500_000_000,
                        "mitigation": {
                            "attempted": False,
                            "executed": False,
                            "reason": "aguardando decisão colaborativa",
                        },
                    }] if index == 0 else []),
                    "collaboration": {"decisions": []},
                }
                for index in range(2)
            ]
            (run_dir / "timeline.ndjson").write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )

            summary = summarize_run(run_dir)

            self.assertEqual(summary["classification"], "INVALID")
            self.assertEqual(summary["agentic_active_domains"], [])
            self.assertIn(
                "agentes shadow ativos em 0/2 domínio(s)",
                summary["invalid_reasons"],
            )
            self.assertIn(
                "anomalia de ataque sem decisão registrada pelos agentes shadow",
                summary["invalid_reasons"],
            )

            # Se Holt não detectar nada, ausência de negociação é um FN real,
            # não uma medição inválida: o agente só recebe candidatos anômalos.
            rows[0]["anomalies"] = []
            for row in rows:
                row["agentic"]["active"] = True
            (run_dir / "timeline.ndjson").write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            no_detection = summarize_run(run_dir)
            self.assertEqual(no_detection["classification"], "FN")
            self.assertEqual(no_detection["invalid_reasons"], [])

    def test_live_mitigation_failure_is_invalid_with_real_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "failed-live"
            run_dir.mkdir()
            (run_dir / "metadata.json").write_text(json.dumps({
                "mode": "collaborative-live",
                "scenario": "ddos",
                "flow": "10.0.0.1->10.0.0.8",
            }), encoding="utf-8")
            (run_dir / "attack_start_ns.txt").write_text(
                "1000000000\n", encoding="utf-8"
            )
            (run_dir / "workload_status.json").write_text(
                json.dumps({"valid": True}), encoding="utf-8"
            )
            for name in ("ping_before.txt", "ping_after.txt"):
                (run_dir / name).write_text(
                    "3 packets transmitted, 3 received, 0% packet loss\n",
                    encoding="utf-8",
                )
            self.write_iperf(run_dir / "baseline.json", 1_000_000)
            self.write_iperf(run_dir / "attack.json", 100_000_000)
            row = {
                "sampled_ns": 2_100_000_000,
                "status": {"cid": "domain-0"},
                "anomalies": [{
                    "anomaly_id": "a1",
                    "kind": "THROUGHPUT_SPIKE",
                    "ts_detect_ns": 1_500_000_000,
                    "mitigation": {
                        "attempted": False,
                        "executed": False,
                        "reason": "aguardando decisão colaborativa",
                    },
                }],
                "collaboration": {"decisions": [{
                    "flow": "10.0.0.1->10.0.0.8",
                    "decision": "MITIGATE",
                    "score": 0.95,
                    "confirming_domains": ["domain-0", "domain-1"],
                    "claim": {
                        "coordinator": "domain-0",
                        "claimed_ns": 2_000_000_000,
                        "won": True,
                    },
                    "mitigation": {
                        "attempted": True,
                        "executed": False,
                        "reason": "FlowBlocker HTTP 404",
                    },
                }]},
            }
            (run_dir / "timeline.ndjson").write_text(
                json.dumps(row) + "\n", encoding="utf-8"
            )

            summary = summarize_run(run_dir)

            self.assertEqual(summary["classification"], "INVALID")
            self.assertTrue(summary["detected_attack"])
            self.assertEqual(summary["mitigation_reason"], "FlowBlocker HTTP 404")
            self.assertIn(
                "mitigação live não executada: FlowBlocker HTTP 404",
                summary["invalid_reasons"],
            )

    def test_summary_correlates_detection_claim_and_mitigation(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-1"
            run_dir.mkdir()
            (run_dir / "metadata.json").write_text(json.dumps({
                "mode": "collaborative-live",
                "scenario": "ddos",
                "flow": "10.0.0.1->10.0.0.8",
                "controller_sets": 2,
                "agentic_shadow": True,
            }), encoding="utf-8")
            (run_dir / "attack_start_ns.txt").write_text(
                "1000000000\n", encoding="utf-8"
            )
            (run_dir / "workload_status.json").write_text(
                json.dumps({"valid": True}), encoding="utf-8"
            )
            (run_dir / "ping_before.txt").write_text(
                "3 packets transmitted, 3 received, 0% packet loss\n",
                encoding="utf-8",
            )
            (run_dir / "ping_after.txt").write_text(
                "5 packets transmitted, 0 received, 100% packet loss\n",
                encoding="utf-8",
            )
            self.write_iperf(run_dir / "baseline.json", 1_000_000)
            self.write_iperf(run_dir / "attack.json", 100_000_000)
            row = {
                "sampled_ns": 2_100_000_000,
                "port": "6061",
                "status": {"cid": "domain-1"},
                "anomalies": [{
                    "anomaly_id": "a1",
                    "kind": "THROUGHPUT_SPIKE",
                    "ts_detect_ns": 1_500_000_000,
                    "mitigation": {"attempted": True, "executed": True,
                                   "reason": "FlowBlocker HTTP 200"},
                }],
                "collaboration": {"decisions": [{
                    "flow": "10.0.0.1->10.0.0.8",
                    "decision": "MITIGATE",
                    "score": 0.95,
                    "confirming_domains": ["domain-0", "domain-1"],
                    "claim": {"coordinator": "domain-1",
                              "claimed_ns": 2_000_000_000, "won": True},
                    "mitigation": {"attempted": True, "executed": True,
                                   "reason": "FlowBlocker HTTP 200"},
                }]},
                "agentic": {
                    "requested": True,
                    "active": True,
                    "mode": "shadow",
                    "authoritative": False,
                    "cid": "domain-1",
                    "decisions": [{
                        "flow": "10.0.0.1->10.0.0.8",
                        "decision": "AGREED",
                        "mitigate_votes": ["domain-0", "domain-1"],
                        "legacy_comparison": {
                            "available": True, "matches": True,
                        },
                    }],
                },
            }
            peer_row = {
                "sampled_ns": 2_100_000_000,
                "port": "6060",
                "status": {"cid": "domain-0"},
                "anomalies": [],
                "collaboration": {"decisions": []},
                "agentic": {
                    "requested": True,
                    "active": True,
                    "mode": "shadow",
                    "authoritative": False,
                    "cid": "domain-0",
                    "decisions": [],
                },
            }
            (run_dir / "timeline.ndjson").write_text(
                json.dumps(row) + "\n" + json.dumps(peer_row) + "\n",
                encoding="utf-8",
            )

            summary = summarize_run(run_dir)

            self.assertEqual(summary["decisions"], ["MITIGATE"])
            self.assertEqual(summary["coordinator"], "domain-1")
            self.assertEqual(summary["detection_latency_ms"], 500.0)
            self.assertEqual(summary["consensus_latency_ms"], 500.0)
            self.assertEqual(summary["baseline_spike_anomalies"], 0)
            self.assertEqual(summary["attack_spike_anomalies"], 1)
            self.assertEqual(summary["ping_after_loss_percent"], 100.0)
            self.assertTrue(summary["mitigation_executed"])
            self.assertEqual(summary["classification"], "TP")
            self.assertEqual(summary["agentic_decisions"], ["AGREED"])
            self.assertEqual(
                summary["agentic_active_domains"], ["domain-0", "domain-1"]
            )
            self.assertEqual(
                summary["agentic_mitigate_votes"], ["domain-0", "domain-1"]
            )
            self.assertTrue(summary["agentic_matches_mcda"])
            self.assertEqual(aggregate_metrics([summary])["f1"], 1.0)
            self.assertIn("collaborative-live", markdown_table([summary]))

    def test_live_drop_may_interrupt_iperf_when_execution_and_loss_are_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "successful-live-disruption"
            run_dir.mkdir()
            (run_dir / "metadata.json").write_text(json.dumps({
                "mode": "collaborative-live",
                "scenario": "ddos",
                "flow": "10.0.0.1->10.0.0.8",
            }), encoding="utf-8")
            (run_dir / "attack_start_ns.txt").write_text(
                "1000000000\n", encoding="utf-8"
            )
            (run_dir / "workload_status.json").write_text(json.dumps({
                "valid": True,
                "expect_disruption": True,
                "attack_disrupted": True,
                "attack_exit_code": 1,
                "attack_bps": None,
            }), encoding="utf-8")
            (run_dir / "ping_before.txt").write_text(
                "3 packets transmitted, 3 received, 0% packet loss\n",
                encoding="utf-8",
            )
            (run_dir / "ping_after.txt").write_text(
                "5 packets transmitted, 0 received, 100% packet loss\n",
                encoding="utf-8",
            )
            self.write_iperf(run_dir / "baseline.json", 1_000_000)
            row = {
                "sampled_ns": 2_100_000_000,
                "status": {"cid": "domain-0"},
                "anomalies": [{
                    "anomaly_id": "attack",
                    "kind": "THROUGHPUT_SPIKE",
                    "ts_detect_ns": 1_500_000_000,
                    "mitigation": {
                        "attempted": True,
                        "executed": True,
                        "reason": "FlowBlocker HTTP 200",
                    },
                }],
                "collaboration": {"decisions": [{
                    "flow": "10.0.0.1->10.0.0.8",
                    "decision": "MITIGATE",
                    "score": 0.95,
                    "confirming_domains": ["domain-0", "domain-1"],
                    "claim": {
                        "coordinator": "domain-0",
                        "claimed_ns": 2_000_000_000,
                        "won": True,
                    },
                    "mitigation": {
                        "attempted": True,
                        "executed": True,
                        "reason": "FlowBlocker HTTP 200",
                    },
                }]},
            }
            (run_dir / "timeline.ndjson").write_text(
                json.dumps(row) + "\n", encoding="utf-8"
            )

            summary = summarize_run(run_dir)

            self.assertEqual(summary["classification"], "TP")
            self.assertTrue(summary["attack_disrupted"])
            self.assertTrue(summary["mitigation_executed"])
            self.assertEqual(summary["ping_after_loss_percent"], 100.0)
            self.assertEqual(summary["invalid_reasons"], [])

            (run_dir / "ping_after.txt").write_text(
                "5 packets transmitted, 5 received, 0% packet loss\n",
                encoding="utf-8",
            )
            no_loss = summarize_run(run_dir)
            self.assertEqual(no_loss["classification"], "INVALID")
            self.assertIn(
                "FlowBlocker confirmou execução, mas o ping não observou perda",
                no_loss["invalid_reasons"],
            )

            (run_dir / "ping_after.txt").write_text(
                "5 packets transmitted, 0 received, 100% packet loss\n",
                encoding="utf-8",
            )
            row["anomalies"][0]["mitigation"]["executed"] = False
            row["anomalies"][0]["mitigation"]["reason"] = "FlowBlocker HTTP 500"
            row["collaboration"]["decisions"][0]["mitigation"]["executed"] = False
            row["collaboration"]["decisions"][0]["mitigation"]["reason"] = (
                "FlowBlocker HTTP 500"
            )
            (run_dir / "timeline.ndjson").write_text(
                json.dumps(row) + "\n", encoding="utf-8"
            )
            unconfirmed = summarize_run(run_dir)
            self.assertEqual(unconfirmed["classification"], "INVALID")
            self.assertIn(
                "iperf interrompido sem mitigação live confirmada",
                unconfirmed["invalid_reasons"],
            )

    def test_missing_traffic_is_invalid_instead_of_true_negative(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "failed-benign"
            run_dir.mkdir()
            (run_dir / "metadata.json").write_text(json.dumps({
                "mode": "collaborative-dry-run",
                "scenario": "benign",
                "flow": "10.0.0.1->10.0.0.8",
            }), encoding="utf-8")
            (run_dir / "workload_status.json").write_text(json.dumps({
                "valid": False,
                "reason": "switches sem conexão OpenFlow",
            }), encoding="utf-8")

            summary = summarize_run(run_dir)
            metrics = aggregate_metrics([summary])

            self.assertEqual(summary["classification"], "INVALID")
            self.assertFalse(summary["measurement_valid"])
            self.assertIn("switches sem conexão OpenFlow",
                          summary["invalid_reasons"])
            self.assertEqual(metrics["INVALID"], 1)
            self.assertEqual(metrics["TN"], 0)

    def test_pre_attack_spike_contaminates_run_without_negative_latency(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "contaminated-ddos"
            run_dir.mkdir()
            (run_dir / "metadata.json").write_text(json.dumps({
                "mode": "local-dry-run",
                "scenario": "ddos",
                "flow": "10.0.0.1->10.0.0.8",
            }), encoding="utf-8")
            (run_dir / "workload_status.json").write_text(
                json.dumps({"valid": True}), encoding="utf-8"
            )
            (run_dir / "attack_start_ns.txt").write_text(
                "2000000000\n", encoding="utf-8"
            )
            for name in ("ping_before.txt", "ping_after.txt"):
                (run_dir / name).write_text(
                    "3 packets transmitted, 3 received, 0% packet loss\n",
                    encoding="utf-8",
                )
            self.write_iperf(run_dir / "baseline.json", 1_000_000)
            self.write_iperf(run_dir / "attack.json", 100_000_000)
            rows = [
                {
                    "sampled_ns": 1_600_000_000,
                    "port": "6060",
                    "status": {"cid": "domain-0"},
                    "anomalies": [{
                        "anomaly_id": "baseline-fp",
                        "kind": "THROUGHPUT_SPIKE",
                        "ts_detect_ns": 1_500_000_000,
                        "mitigation": {"attempted": True, "executed": False},
                    }],
                    "collaboration": {"decisions": []},
                },
                {
                    "sampled_ns": 2_600_000_000,
                    "port": "6060",
                    "status": {"cid": "domain-0"},
                    "anomalies": [{
                        "anomaly_id": "attack-tp",
                        "kind": "THROUGHPUT_SPIKE",
                        "ts_detect_ns": 2_500_000_000,
                        "mitigation": {"attempted": True, "executed": False},
                    }],
                    "collaboration": {"decisions": []},
                },
            ]
            (run_dir / "timeline.ndjson").write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )

            summary = summarize_run(run_dir)
            metrics = aggregate_metrics([summary])

            self.assertEqual(summary["classification"], "CONTAMINATED")
            self.assertEqual(summary["baseline_spike_anomalies"], 1)
            self.assertEqual(summary["attack_spike_anomalies"], 1)
            self.assertEqual(summary["detection_latency_ms"], 500.0)
            self.assertEqual(summary["baseline_action_domains"], ["domain-0"])
            self.assertEqual(summary["action_domains"], ["domain-0"])
            self.assertEqual(metrics["CONTAMINATED"], 1)
            self.assertEqual(metrics["TP"], 0)

    def test_collaborative_benign_drop_does_not_become_ddos_false_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "benign-drop"
            run_dir.mkdir()
            (run_dir / "metadata.json").write_text(json.dumps({
                "mode": "collaborative-dry-run",
                "scenario": "benign",
                "flow": "10.0.0.1->10.0.0.8",
            }), encoding="utf-8")
            (run_dir / "workload_status.json").write_text(
                json.dumps({"valid": True}), encoding="utf-8"
            )
            for name in ("ping_before.txt", "ping_after.txt"):
                (run_dir / name).write_text(
                    "3 packets transmitted, 3 received, 0% packet loss\n",
                    encoding="utf-8",
                )
            self.write_iperf(run_dir / "baseline.json", 1_000_000)
            (run_dir / "timeline.ndjson").write_text(json.dumps({
                "sampled_ns": 2_000_000_000,
                "port": "6061",
                "status": {"cid": "domain-1"},
                "anomalies": [{
                    "anomaly_id": "normal-flow-end",
                    "kind": "THROUGHPUT_DROP",
                    "ts_detect_ns": 1_500_000_000,
                    "mitigation": {"attempted": False, "executed": False},
                }],
                "collaboration": {"decisions": []},
            }) + "\n", encoding="utf-8")

            summary = summarize_run(run_dir)

            self.assertEqual(summary["classification"], "TN")
            self.assertEqual(summary["drop_anomalies"], 1)
            self.assertEqual(summary["spike_anomalies"], 0)


if __name__ == "__main__":
    unittest.main()
