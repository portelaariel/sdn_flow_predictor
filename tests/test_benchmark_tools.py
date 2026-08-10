import json
import tempfile
import threading
import unittest
from pathlib import Path

from experiments.evaluate_agentic_authority_run import (
    evaluate as evaluate_authority_run,
)
from experiments.evaluate_agentic_authority_campaign import (
    evaluate as evaluate_authority_campaign,
)
from experiments.evaluate_agentic_runtime_faults import evaluate
from experiments.monitor_predictors import (
    flow_anomalies,
    flow_predictions,
    retain_new_agentic_events,
    retain_new_decision_events,
)
from experiments.run_mininet_workload import (
    domain_table_has_hosts,
    wait_for_attack_release,
)
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

    def test_monitor_writes_each_agentic_transition_only_once(self):
        seen = set()
        payload = {
            "decision_events": [
                {"event_id": "event-2", "decision": "AGREED"},
                {"event_id": "event-1", "decision": "WAITING_PROPOSALS"},
            ],
            "decisions": [{"decision": "AGREED"}],
        }

        first = retain_new_agentic_events(payload, seen)
        second = retain_new_agentic_events(payload, seen)

        self.assertEqual(
            [row["event_id"] for row in first["decision_events"]],
            ["event-2", "event-1"],
        )
        self.assertEqual(second["decision_events"], [])
        self.assertEqual(second["decisions"], payload["decisions"])

    def test_monitor_writes_each_mcda_transition_only_once(self):
        seen = set()
        payload = {
            "decision_events": [{
                "event_id": "mcda:domain-0:flow:2:MITIGATE:10",
                "decision": "MITIGATE",
                "window_ids": [2],
            }],
            "decisions": [{"decision": "SUSPECT", "window_ids": [3]}],
        }

        first = retain_new_decision_events(payload, seen)
        second = retain_new_decision_events(payload, seen)

        self.assertEqual(len(first["decision_events"]), 1)
        self.assertEqual(second["decision_events"], [])
        self.assertEqual(second["decisions"], payload["decisions"])

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

    def test_attack_gate_announces_readiness_and_waits_for_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = Path(tmp)
            result = []
            thread = threading.Thread(
                target=lambda: result.append(wait_for_attack_release(gate, 2.0))
            )
            thread.start()
            for _attempt in range(100):
                if (gate / "attack.ready.json").exists():
                    break
                threading.Event().wait(0.01)
            self.assertTrue((gate / "attack.ready.json").exists())
            self.assertTrue(thread.is_alive())
            (gate / "attack.release").touch()
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [None])
            self.assertTrue((gate / "attack.released.json").exists())

    def test_runtime_fault_report_requires_fail_closed_and_fresh_recovery(self):
        flow = "10.0.0.1->10.0.0.8"

        def agent_payload(events, errors=0):
            return {
                "active": True,
                "mode": "shadow",
                "authoritative": False,
                "errors": errors,
                "decisions": [],
                "decision_events": events,
            }

        def event(cid, decision, entered_ns, proposals=None):
            return {
                "event_id": f"{cid}:{decision}:{entered_ns}",
                "flow": flow,
                "decision": decision,
                "state_entered_ns": entered_ns,
                "participating_domains": ["domain-0", "domain-1"],
                "proposals": proposals or [],
            }

        proposals = [
            {"cid": cid, "observation_ns": 120, "created_ns": 125}
            for cid in ("domain-0", "domain-1")
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows_by_episode = {
                "missing-agent": [
                    {"port": "6060", "status": {"cid": "domain-0"},
                     "agentic": agent_payload([
                         event("domain-0", "WAITING_PROPOSALS", 120)
                     ])},
                ],
                "missing-agent-recovery": [
                    {"port": str(6060 + index), "status": {"cid": f"domain-{index}"},
                     "agentic": agent_payload([
                         event(f"domain-{index}", "AGREED", 130, proposals)
                     ])}
                    for index in range(2)
                ],
                "etcd-partition": [
                    {"port": "6060", "status": {"cid": "domain-0"},
                     "agentic": agent_payload([], errors=0)},
                    {"port": "6060", "status": {"cid": "domain-0"},
                     "agentic": agent_payload([], errors=2)},
                ],
                "etcd-recovery": [
                    {"port": str(6060 + index), "status": {"cid": f"domain-{index}"},
                     "agentic": agent_payload([
                         event(f"domain-{index}", "AGREED", 140, proposals)
                     ])}
                    for index in range(2)
                ],
            }
            for name, rows in rows_by_episode.items():
                episode = root / name
                episode.mkdir()
                (episode / "attack_start_ns.txt").write_text("100\n", encoding="utf-8")
                (episode / "workload_status.json").write_text(
                    json.dumps({"valid": True}), encoding="utf-8"
                )
                (episode / "timeline.ndjson").write_text(
                    "".join(json.dumps(row) + "\n" for row in rows),
                    encoding="utf-8",
                )
            (root / "flowblocker-requests.log").write_text("", encoding="utf-8")

            report = evaluate(root, flow, 2)

            self.assertTrue(report["aggregate"]["safe"])
            self.assertEqual(report["aggregate"]["passed"], 4)
            self.assertTrue(report["global_checks"]["no_flowblocker_request"])

            stale = rows_by_episode["etcd-recovery"][0]["agentic"]["decision_events"][0]
            stale["proposals"][0]["observation_ns"] = 99
            (root / "etcd-recovery" / "timeline.ndjson").write_text(
                "".join(
                    json.dumps(row) + "\n"
                    for row in rows_by_episode["etcd-recovery"]
                ),
                encoding="utf-8",
            )
            unsafe = evaluate(root, flow, 2)
            self.assertFalse(unsafe["aggregate"]["safe"])

    def test_authority_dry_run_requires_one_claim_and_zero_actuation(self):
        flow = "10.0.0.1->10.0.0.8"
        attack_ns = 1_000_000_000
        domains = ("domain-0", "domain-1")
        proposals = [
            {
                "cid": cid,
                "observation_ns": attack_ns + 100_000_000,
                "created_ns": attack_ns + 200_000_000,
            }
            for cid in domains
        ]

        def row(cid, won):
            decision = {
                "event_id": f"{cid}:agreement",
                "flow": flow,
                "decision": "AGREED",
                "state_entered_ns": attack_ns + 500_000_000,
                "proposals": proposals,
                "authority": {
                    "authorized": True,
                    "claim": {
                        "won": won,
                        "degraded": False,
                        "coordinator": "domain-0",
                    },
                },
                "execution": {
                    "attempted": False,
                    "executed": False,
                    "would_execute": won,
                },
                "legacy_comparison": {
                    "available": True,
                    "matches": True,
                },
            }
            return {
                "sampled_ns": attack_ns + 600_000_000,
                "status": {"cid": cid},
                "agentic": {
                    "requested": True,
                    "active": True,
                    "mode": "authority-dry-run",
                    "authoritative": True,
                    "actuation_enabled": False,
                    "cid": cid,
                    "decisions": [decision],
                    "decision_events": [],
                },
            }

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "metadata.json").write_text(json.dumps({
                "mode": "collaborative-dry-run",
                "scenario": "ddos",
                "flow": flow,
                "controller_sets": 2,
                "agentic_enabled": True,
                "agentic_mode": "authority-dry-run",
            }), encoding="utf-8")
            (run_dir / "attack_start_ns.txt").write_text(
                f"{attack_ns}\n", encoding="utf-8"
            )
            (run_dir / "workload_status.json").write_text(
                json.dumps({"valid": True}), encoding="utf-8"
            )
            (run_dir / "summary.json").write_text(json.dumps({
                "runs": [{"classification": "TP"}],
            }), encoding="utf-8")
            rows = [row("domain-0", True), row("domain-1", False)]
            (run_dir / "timeline.ndjson").write_text(
                "".join(json.dumps(item) + "\n" for item in rows),
                encoding="utf-8",
            )
            for switch in ("s1", "s2", "s3", "s4"):
                (run_dir / f"ovs-flows-{switch}.txt").write_text(
                    "actions=NORMAL\n", encoding="utf-8"
                )
            (run_dir / "flow-blocker-0.log").write_text("", encoding="utf-8")

            report = evaluate_authority_run(run_dir)
            self.assertTrue(report["aggregate"]["safe"])
            self.assertEqual(report["claim_winner_domains"], ["domain-0"])
            self.assertEqual(report["would_execute_domains"], ["domain-0"])

            # A comparação é evidência experimental, não parte do gate de
            # autoridade. Se a decisão corrente do MCDA já avançou, o
            # avaliador deve correlacionar o episódio preservado no histórico.
            for item in rows:
                decision = item["agentic"]["decisions"][0]
                decision["window_ids"] = [2]
                decision["legacy_comparison"] = {
                    "available": False,
                    "matches": None,
                    "reason": "decisão MCDA pertence a outro episódio",
                }
                cid = item["status"]["cid"]
                item["collaboration"] = {
                    "cid": cid,
                    "decisions": [{
                        "flow": flow,
                        "decision": "SUSPECT",
                        "evaluated_ns": attack_ns + 800_000_000,
                        "window_ids": [3],
                    }],
                    "decision_events": [{
                        "flow": flow,
                        "decision": "MITIGATE",
                        "evaluated_ns": attack_ns + 400_000_000,
                        "window_ids": [2],
                    }],
                }
            (run_dir / "timeline.ndjson").write_text(
                "".join(json.dumps(item) + "\n" for item in rows),
                encoding="utf-8",
            )
            historical_mcda = evaluate_authority_run(run_dir)
            self.assertTrue(historical_mcda["aggregate"]["safe"])
            self.assertTrue(
                historical_mcda["checks"]["mcda_comparison_available"]
            )
            self.assertTrue(historical_mcda["checks"]["agent_matches_mcda"])

            rows[0]["agentic"]["decisions"][0]["execution"]["attempted"] = True
            (run_dir / "timeline.ndjson").write_text(
                "".join(json.dumps(item) + "\n" for item in rows),
                encoding="utf-8",
            )
            unsafe = evaluate_authority_run(run_dir)
            self.assertFalse(unsafe["aggregate"]["safe"])
            self.assertFalse(unsafe["checks"]["agent_never_actuated"])

            rows[0]["agentic"]["decisions"][0]["execution"]["attempted"] = False
            rows[1]["agentic"]["decisions"][0]["authority"]["claim"][
                "coordinator"
            ] = "domain-1"
            (run_dir / "timeline.ndjson").write_text(
                "".join(json.dumps(item) + "\n" for item in rows),
                encoding="utf-8",
            )
            split_brain = evaluate_authority_run(run_dir)
            self.assertFalse(split_brain["aggregate"]["safe"])
            self.assertFalse(split_brain["checks"]["claim_owner_consistent"])

    def test_authority_dry_run_benign_forbids_authorization(self):
        flow = "10.0.0.1->10.0.0.8"
        started_ns = 1_000_000_000

        def agent_row(cid):
            return {
                "sampled_ns": started_ns + 100_000_000,
                "status": {"cid": cid},
                "agentic": {
                    "requested": True,
                    "active": True,
                    "mode": "authority-dry-run",
                    "authoritative": True,
                    "actuation_enabled": False,
                    "cid": cid,
                    "decisions": [],
                    "decision_events": [],
                },
            }

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "metadata.json").write_text(json.dumps({
                "mode": "collaborative-dry-run",
                "scenario": "benign",
                "flow": flow,
                "controller_sets": 2,
                "started_ns": started_ns,
                "agentic_enabled": True,
                "agentic_mode": "authority-dry-run",
            }), encoding="utf-8")
            (run_dir / "workload_status.json").write_text(
                json.dumps({"valid": True}), encoding="utf-8"
            )
            (run_dir / "summary.json").write_text(json.dumps({
                "runs": [{"classification": "TN"}],
            }), encoding="utf-8")
            rows = [agent_row("domain-0"), agent_row("domain-1")]
            (run_dir / "timeline.ndjson").write_text(
                "".join(json.dumps(item) + "\n" for item in rows),
                encoding="utf-8",
            )
            (run_dir / "ovs-flows-s1.txt").write_text(
                "actions=NORMAL\n", encoding="utf-8"
            )

            report = evaluate_authority_run(run_dir)
            self.assertTrue(report["aggregate"]["safe"])
            self.assertTrue(report["checks"]["benchmark_tn"])
            self.assertTrue(report["checks"]["no_agent_authorization"])

            forged = {
                "event_id": "forged-benign-agreement",
                "flow": flow,
                "decision": "AGREED",
                "state_entered_ns": started_ns + 50_000_000,
                "authority": {
                    "authorized": True,
                    "claim": {"won": True, "coordinator": "domain-0"},
                },
                "execution": {
                    "attempted": False,
                    "executed": False,
                    "would_execute": True,
                },
            }
            rows[0]["agentic"]["decisions"] = [forged]
            (run_dir / "timeline.ndjson").write_text(
                "".join(json.dumps(item) + "\n" for item in rows),
                encoding="utf-8",
            )
            unsafe = evaluate_authority_run(run_dir)
            self.assertFalse(unsafe["aggregate"]["safe"])
            self.assertFalse(unsafe["checks"]["no_agent_authorization"])
            self.assertFalse(unsafe["checks"]["no_would_execute"])

    def test_authority_campaign_requires_positive_and_negative_cases(self):
        flow = "10.0.0.1->10.0.0.8"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = {
                "minimums": {"ddos": 1, "benign": 1, "distinct_flows": 1},
                "cases": [
                    {"case_id": "01-ddos", "scenario": "ddos", "flow": flow},
                    {"case_id": "01-benign", "scenario": "benign", "flow": flow},
                ],
            }
            (root / "campaign-manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            ddos = {
                "scenario": "ddos",
                "flow": flow,
                "classification": "TP",
                "authorized_domains": ["domain-0", "domain-1"],
                "claim_winner_domains": ["domain-0"],
                "would_execute_domains": ["domain-0"],
                "flowblocker_requests": 0,
                "drop_rule_files": [],
                "git_commit": "commit-a",
                "model_sha256": "model-a",
                "detection_latency_ms": 500.0,
                "agentic_consensus_latency_ms": 1000.0,
                "checks": {"agent_never_actuated": True},
                "aggregate": {"safe": True},
            }
            benign = {
                **ddos,
                "scenario": "benign",
                "classification": "TN",
                "authorized_domains": [],
                "claim_winner_domains": [],
                "would_execute_domains": [],
                "detection_latency_ms": None,
                "agentic_consensus_latency_ms": None,
            }
            for case_id, payload in (("01-ddos", ddos), ("01-benign", benign)):
                case_dir = root / case_id
                case_dir.mkdir()
                (case_dir / "authority-summary.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )
                (case_dir / "benchmark-exit-code.txt").write_text(
                    "0\n", encoding="utf-8"
                )
                (case_dir / "evaluator-exit-code.txt").write_text(
                    "0\n", encoding="utf-8"
                )

            report = evaluate_authority_campaign(root)
            self.assertTrue(report["aggregate"]["promotion_ready"])
            self.assertEqual(report["metrics"]["TP"], 1)
            self.assertEqual(report["metrics"]["TN"], 1)
            self.assertEqual(report["metrics"]["f1"], 1.0)

            benign["authorized_domains"] = ["domain-0"]
            (root / "01-benign" / "authority-summary.json").write_text(
                json.dumps(benign), encoding="utf-8"
            )
            unsafe = evaluate_authority_campaign(root)
            self.assertFalse(unsafe["aggregate"]["promotion_ready"])
            self.assertFalse(unsafe["checks"]["all_benign_rejected"])

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
            (run_dir / "initial-agent-6061.json").write_text(json.dumps({
                "cid": "domain-1", "waiting_events": 1,
                "expired_proposals": 0,
            }), encoding="utf-8")
            (run_dir / "initial-agent-6060.json").write_text(json.dumps({
                "cid": "domain-0", "waiting_events": 1,
                "expired_proposals": 1,
            }), encoding="utf-8")
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
                    "decisions": [],
                    "waiting_events": 1,
                    "expired_proposals": 0,
                    "decision_events": [{
                        "flow": "10.0.0.1->10.0.0.8",
                        "decision": "AGREED",
                        "event_id": "domain-1:agreement-1",
                        "evaluated_ns": 2_000_000_000,
                        "state_entered_ns": 2_000_000_000,
                        "first_proposal_ns": 1_600_000_000,
                        "last_proposal_ns": 1_800_000_000,
                        "required_votes": 2,
                        "proposals": [
                            {"cid": "domain-0", "created_ns": 1_600_000_000},
                            {"cid": "domain-1", "created_ns": 1_800_000_000},
                        ],
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
                    "decision_events": [],
                    "waiting_events": 2,
                    "expired_proposals": 3,
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
            self.assertTrue(summary["agentic_agent_agreement"])
            self.assertEqual(summary["agentic_first_proposal_latency_ms"], 100.0)
            self.assertEqual(
                summary["agentic_proposal_collection_latency_ms"], 200.0
            )
            self.assertEqual(summary["agentic_deliberation_latency_ms"], 200.0)
            self.assertEqual(summary["agentic_consensus_latency_ms"], 500.0)
            self.assertEqual(summary["agentic_required_votes"], 2)
            self.assertTrue(summary["agentic_quorum_reached"])
            self.assertEqual(summary["agentic_proposal_timestamps_ns"], {
                "domain-0": 1_600_000_000,
                "domain-1": 1_800_000_000,
            })
            self.assertEqual(
                summary["agentic_attack_to_consensus_latency_ms"], 1000.0
            )
            self.assertEqual(summary["agentic_waiting_events"], 1)
            self.assertEqual(summary["agentic_expired_proposals"], 2)
            metrics = aggregate_metrics([summary])
            self.assertEqual(metrics["f1"], 1.0)
            self.assertEqual(metrics["agentic"]["agreed_runs"], 1)
            self.assertEqual(
                metrics["agentic"]["agent_to_agent_agreement_rate"], 1.0
            )
            self.assertEqual(
                metrics["agentic"]["agent_to_mcda_agreement_rate"], 1.0
            )
            table = markdown_table([summary])
            self.assertIn("collaborative-live", table)
            self.assertIn("agentic: AGREED=1/1", table)

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
