import ast
import unittest
from collections import deque
from pathlib import Path
from typing import Optional, Tuple


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
            {"HoltPredictor", "ResidualAnomalyDetector"},
            {"Optional": Optional, "Tuple": Tuple, "deque": deque},
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
