import csv
import tempfile
import unittest
from pathlib import Path

from evaluate_offline_model import evaluate
from offline_model import OfflineModel
from prepare_cicddos2019 import aggregate_cic_files, write_compact_csv
from train_offline_model import Observation, load_observations


class CicDdosPreparationTests(unittest.TestCase):
    def test_aggregates_flow_bytes_into_two_second_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "DrDoS_UDP.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                # Espaços nos cabeçalhos reproduzem uma variação comum do CICFlowMeter.
                writer = csv.DictWriter(
                    handle,
                    fieldnames=(
                        " Timestamp",
                        " Flow Duration",
                        " Total Length of Fwd Packets",
                        " Total Length of Bwd Packets",
                        " Label",
                    ),
                )
                writer.writeheader()
                writer.writerow({
                    " Timestamp": "2018-12-01 00:00:00",
                    " Flow Duration": "4000000",
                    " Total Length of Fwd Packets": "200",
                    " Total Length of Bwd Packets": "200",
                    " Label": "BENIGN",
                })
                writer.writerow({
                    " Timestamp": "2018-12-01 00:00:04",
                    " Flow Duration": "4000000",
                    " Total Length of Fwd Packets": "400",
                    " Total Length of Bwd Packets": "400",
                    " Label": "DrDoS_UDP",
                })
                writer.writerow({
                    " Timestamp": "invalid",
                    " Flow Duration": "1000",
                    " Total Length of Fwd Packets": "1",
                    " Total Length of Bwd Packets": "1",
                    " Label": "BENIGN",
                })

            series_windows, summary = aggregate_cic_files(
                [str(source)], window_s=2.0, series_mode="aggregate", progress_every=0
            )
            self.assertEqual(summary["rows_total"], 3)
            self.assertEqual(summary["rows_valid"], 2)
            self.assertEqual(summary["rows_skipped"], 1)
            self.assertEqual(len(series_windows["aggregate"]), 4)

            windows = series_windows["aggregate"]
            ordered = [windows[index] for index in sorted(windows)]
            self.assertAlmostEqual(ordered[0].bits / 2.0, 800.0)
            self.assertAlmostEqual(ordered[1].bits / 2.0, 800.0)
            self.assertAlmostEqual(ordered[2].bits / 2.0, 1600.0)
            self.assertAlmostEqual(ordered[3].bits / 2.0, 1600.0)
            self.assertEqual(ordered[0].attack_records, 0)
            self.assertEqual(ordered[2].attack_records, 1)

            compact = Path(directory) / "compact.csv"
            write_compact_csv(str(compact), series_windows, 2.0, "cic:udp")
            with compact.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["label"] for row in rows],
                             ["BENIGN", "BENIGN", "ATTACK", "ATTACK"])
            self.assertEqual({row["flow_key"] for row in rows}, {"cic:udp"})

    def test_filters_other_attacks_and_reverse_direction(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "UDP.csv"
            fieldnames = (
                "Source IP",
                "Destination IP",
                "Timestamp",
                "Flow Duration",
                "Total Length of Fwd Packets",
                "Total Length of Bwd Packets",
                "Inbound",
                "Label",
            )
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                base = {
                    "Timestamp": "2018-11-03 10:00:00",
                    "Flow Duration": "1000",
                    "Total Length of Fwd Packets": "100",
                    "Total Length of Bwd Packets": "0",
                }
                writer.writerow({**base, "Source IP": "client", "Destination IP": "dns",
                                 "Inbound": "0", "Label": "BENIGN"})
                writer.writerow({**base, "Source IP": "attacker", "Destination IP": "victim",
                                 "Inbound": "1", "Label": "UDP"})
                writer.writerow({**base, "Source IP": "victim", "Destination IP": "attacker",
                                 "Inbound": "0", "Label": "UDP"})
                writer.writerow({**base, "Source IP": "attacker", "Destination IP": "victim",
                                 "Inbound": "1", "Label": "MSSQL"})

            series_windows, summary = aggregate_cic_files(
                [str(source)],
                attack_labels=["UDP"],
                attack_inbound_only=True,
                progress_every=0,
            )
            self.assertEqual(summary["rows_valid"], 2)
            self.assertEqual(summary["rows_filtered"], 2)
            self.assertEqual(set(series_windows), {"client->dns", "attacker->victim"})

    def test_held_out_evaluation_uses_runtime_state_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            compact = Path(directory) / "validation.csv"
            with compact.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=("timestamp", "flow_key", "observed_bps", "label"),
                )
                writer.writeheader()
                for index, value in enumerate((1000, 1000, 1000, 1_000_000)):
                    writer.writerow({
                        "timestamp": index * 2,
                        "flow_key": "cic:udp",
                        "observed_bps": value,
                        "label": "ATTACK" if index == 3 else "BENIGN",
                    })

            observations, _ = load_observations(
                [str(compact)],
                value_column="observed_bps",
                value_scale=1.0,
                series_columns=["flow_key"],
                timestamp_column="timestamp",
                label_column="label",
                normal_labels=["BENIGN"],
            )
            model = OfflineModel(
                alpha=0.35,
                beta=0.1,
                transform="log1p",
                residual_center=0.0,
                residual_scale=0.05,
                z_threshold=4.0,
                created_at="test",
            )
            metrics = evaluate(model, observations)
            ddos = metrics["ddos_throughput_spike"]
            self.assertEqual(ddos["true_positives"], 1)
            self.assertEqual(ddos["false_positives"], 0)
            self.assertEqual(metrics["initial_rows_unscored"], 2)
            self.assertEqual(metrics["priming_rows_unscored"], 2)
            self.assertEqual(metrics["below_floor_rows_unscored"], 0)
            self.assertEqual(metrics["attack_rows_unscored"], 0)
            self.assertEqual(metrics["series_priming_samples"], 2)

    def test_held_out_evaluation_uses_independent_drop_threshold(self):
        observations = [
            # A queda de 1000 para 500 gera aproximadamente z=-13, abaixo do
            # limiar de pico, mas ainda dentro do limiar específico de queda.
            Observation("flow", (0, index, index), value, False)
            for index, value in enumerate((1000.0, 1000.0, 500.0))
        ]
        model = OfflineModel(
            alpha=0.35,
            beta=0.1,
            transform="log1p",
            residual_center=0.0,
            residual_scale=0.05,
            z_threshold=4.0,
            drop_z_threshold=20.0,
            created_at="test",
        )
        metrics = evaluate(model, observations)
        self.assertEqual(metrics["drop_detections"], 0)
        self.assertEqual(metrics["drop_z_threshold"], 20.0)


if __name__ == "__main__":
    unittest.main()
