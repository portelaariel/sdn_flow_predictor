import json
import tempfile
import unittest
from pathlib import Path

from experiments.monitor_predictors import flow_anomalies
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

    def test_summary_correlates_detection_claim_and_mitigation(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-1"
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
            }
            (run_dir / "timeline.ndjson").write_text(
                json.dumps(row) + "\n", encoding="utf-8"
            )

            summary = summarize_run(run_dir)

            self.assertEqual(summary["decisions"], ["MITIGATE"])
            self.assertEqual(summary["coordinator"], "domain-1")
            self.assertEqual(summary["detection_latency_ms"], 500.0)
            self.assertEqual(summary["consensus_latency_ms"], 500.0)
            self.assertEqual(summary["ping_after_loss_percent"], 100.0)
            self.assertTrue(summary["mitigation_executed"])
            self.assertEqual(summary["classification"], "TP")
            self.assertEqual(aggregate_metrics([summary])["f1"], 1.0)
            self.assertIn("collaborative-live", markdown_table([summary]))

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


if __name__ == "__main__":
    unittest.main()
