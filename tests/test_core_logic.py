import ast
import unittest
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from offline_model import OfflineModel, inverse_transform_value, transform_value


ROOT = Path(__file__).resolve().parents[1]


def load_definitions(relative_path, names, namespace):
    """Load selected pure definitions without importing service dependencies."""
    source = (ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=relative_path)
    selected = [
        node for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names
    ]
    module = ast.Module(body=selected, type_ignores=[])
    exec(compile(module, relative_path, "exec"), namespace)
    return namespace


class PredictorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.symbols = load_definitions(
            "flow_predictor_cnsm.py",
            {"HoltPredictor", "ResidualAnomalyDetector", "SeriesState"},
            {
                "Optional": Optional,
                "Tuple": Tuple,
                "Dict": Dict,
                "Any": Any,
                "deque": deque,
                "transform_value": transform_value,
                "inverse_transform_value": inverse_transform_value,
                "OfflineModel": OfflineModel,
                "ONLINE_MODEL_ADAPTATION": False,
                "HISTORY_WINDOW": 120,
                "Z_THRESHOLD": 4.0,
                "WARMUP_SAMPLES": 15,
                "MIN_RATE_BPS": 1.0,
                "CONTROLLER_ID": "test-controller",
                "_exporter": None,
                "uuid": uuid,
                "now_ns": lambda: 1,
            },
        )

    def test_holt_predictor_tracks_level_and_trend(self):
        predictor = self.symbols["HoltPredictor"](alpha=0.5, beta=0.5)
        self.assertEqual(predictor.predict(), 0.0)
        predictor.update(10.0)
        self.assertEqual(predictor.predict(), 10.0)
        predictor.update(14.0)
        self.assertGreater(predictor.predict(3), predictor.predict(1))

    def test_detector_rejects_outlier_from_baseline(self):
        detector = self.symbols["ResidualAnomalyDetector"](
            window=10, z_threshold=4.0, warmup=3
        )
        for value in (0.0, 0.0, 0.0):
            self.assertEqual(detector.score(value), (0.0, False))
        baseline_size = len(detector.residuals)
        z_score, is_anomaly = detector.score(10.0)
        self.assertTrue(is_anomaly)
        self.assertGreater(z_score, detector.z_threshold)
        self.assertEqual(len(detector.residuals), baseline_size)

    def test_offline_detector_is_ready_without_warmup(self):
        detector = self.symbols["ResidualAnomalyDetector"](
            window=10,
            z_threshold=4.0,
            warmup=0,
            fixed_center=0.0,
            fixed_scale=0.1,
        )
        self.assertTrue(detector.ready)
        self.assertEqual(detector.score(0.0), (0.0, False))
        z_score, is_anomaly = detector.score(1.0)
        self.assertTrue(is_anomaly)
        self.assertGreater(z_score, detector.z_threshold)

    def test_detector_uses_independent_spike_and_drop_thresholds(self):
        detector = self.symbols["ResidualAnomalyDetector"](
            window=10,
            z_threshold=4.0,
            drop_z_threshold=8.0,
            warmup=0,
            fixed_center=0.0,
            fixed_scale=1.0,
        )
        self.assertEqual(detector.score(5.0), (5.0, True))
        self.assertEqual(detector.score(-5.0), (-5.0, False))
        self.assertEqual(detector.score(-9.0), (-9.0, True))

    def test_series_uses_offline_model_and_does_not_absorb_attack(self):
        model = OfflineModel(
            alpha=0.35,
            beta=0.1,
            transform="log1p",
            residual_center=0.0,
            residual_scale=0.1,
            z_threshold=4.0,
            created_at="test",
        )
        series = self.symbols["SeriesState"](
            "flow:1:10.0.0.1->10.0.0.4",
            {"type": "flow", "dpid": 1, "nw_src": "10.0.0.1", "nw_dst": "10.0.0.4"},
            model,
        )

        self.assertIsNone(series.ingest(0, 0.0))
        self.assertIsNone(series.ingest(12_500, 1.0))       # 100 kbit/s: prime
        self.assertIsNone(series.ingest(25_000, 2.0))       # baseline estável
        samples_before_attack = series.predictor.n
        anomaly = series.ingest(1_275_000, 3.0)             # 10 Mbit/s: spike

        self.assertIsNotNone(anomaly)
        self.assertEqual(anomaly["kind"], "THROUGHPUT_SPIKE")
        self.assertEqual(anomaly["detection_mode"], "offline")
        self.assertEqual(series.predictor.n, samples_before_attack)

    def test_repeated_anomalies_are_aggregated_during_cooldown(self):
        class FakeMitigator:
            def __init__(self):
                self.calls = 0

            def maybe_mitigate(self, anomaly):
                self.calls += 1
                return {"attempted": True, "executed": False, "reason": "test"}

        symbols = load_definitions(
            "flow_predictor_cnsm.py",
            {"PredictorEngine"},
            {
                "Any": Any,
                "Dict": Dict,
                "List": List,
                "Optional": Optional,
                "Tuple": Tuple,
                "OfflineModel": OfflineModel,
                "deque": deque,
                "threading": __import__("threading"),
                "Mitigator": FakeMitigator,
                "now_ns": lambda: 1,
                "EVENT_COOLDOWN_S": 60.0,
                "CONTROLLER_ID": "test-controller",
                "_metric": lambda *_args: None,
                "_etcd": None,
            },
        )
        engine = symbols["PredictorEngine"]()

        def anomaly(anomaly_id, timestamp_ns, observed_bps):
            return {
                "anomaly_id": anomaly_id,
                "kind": "THROUGHPUT_SPIKE",
                "key": "flow:1:10.0.0.1->10.0.0.4",
                "meta": {"type": "flow"},
                "observed_bps": observed_bps,
                "z_score": observed_bps / 100.0,
                "ts_detect_ns": timestamp_ns,
            }

        self.assertTrue(engine.register_anomaly(anomaly("first", 1_000_000_000, 100.0), True))
        self.assertFalse(engine.register_anomaly(anomaly("duplicate", 2_000_000_000, 200.0), True))
        self.assertEqual(len(engine.anomalies), 1)
        self.assertEqual(engine.anomalies[0]["suppressed_count"], 1)
        self.assertEqual(engine.anomalies[0]["peak_observed_bps"], 200.0)
        self.assertEqual(engine.anomalies_suppressed, 1)
        self.assertEqual(engine.mitigator.calls, 1)

        self.assertTrue(engine.register_anomaly(anomaly("next", 63_000_000_000, 150.0), True))
        self.assertEqual(len(engine.anomalies), 2)
        self.assertEqual(engine.mitigator.calls, 2)


class FlowRuleTests(unittest.TestCase):
    def test_flowblocker_builds_openflow10_drop(self):
        symbols = load_definitions(
            "flow_blocker/flow_blocker_cnsm.py",
            {"build_block_flow"},
            {"Dict": dict, "Any": object, "FLOW_PRIORITY": 32768,
             "IDLE_TIMEOUT": 0, "HARD_TIMEOUT": 0},
        )
        flow = symbols["build_block_flow"](1, "10.0.0.1", "10.0.0.4")
        self.assertEqual(flow["priority"], 32768)
        self.assertEqual(flow["actions"], [])
        self.assertEqual(
            flow["match"],
            {"dl_type": 2048, "nw_src": "10.0.0.1", "nw_dst": "10.0.0.4"},
        )

    def test_simpleswitch_rules_are_disjoint_and_below_drop_priority(self):
        symbols = load_definitions(
            "rest_client/Simpleswitch_cnsm.py",
            {"build_flow", "build_ipv4_flow", "build_arp_flow"},
            {
                "SEND_OUT": None,
                "FORWARD_PRIORITY": 3000,
                "ETH_TYPE_IP": 0x0800,
                "ETH_TYPE_ARP": 0x0806,
                "IPV4_IDLE_TIMEOUT": 30,
                "IPV4_HARD_TIMEOUT": 0,
                "ARP_IDLE_TIMEOUT": 5,
                "ARP_HARD_TIMEOUT": 5,
            },
        )
        ipv4_flow = symbols["build_ipv4_flow"](
            1, "10.0.0.1", "10.0.0.4", 1, 2, 99
        )
        arp_flow = symbols["build_arp_flow"](
            1, "00:00:00:00:00:01", "00:00:00:00:00:04", 1, 2, 99
        )
        self.assertEqual(ipv4_flow["match"]["dl_type"], 0x0800)
        self.assertEqual(arp_flow["match"]["dl_type"], 0x0806)
        self.assertLess(ipv4_flow["priority"], 32768)
        self.assertEqual(ipv4_flow["idle_timeout"], 30)


if __name__ == "__main__":
    unittest.main()
