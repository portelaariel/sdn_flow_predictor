import csv
import json
import tempfile
import unittest
from pathlib import Path

from offline_model import OfflineModel, load_offline_model
from train_offline_model import (
    load_observations,
    train_model,
)


class OfflineModelTests(unittest.TestCase):
    def test_rejects_invalid_scale(self):
        payload = {
            "schema_version": 1,
            "model_type": "holt_residual",
            "created_at": "test",
            "holt": {"alpha": 0.35, "beta": 0.1, "transform": "log1p"},
            "detector": {
                "residual_center": 0.0,
                "residual_scale": 0.0,
                "z_threshold": 4.0,
            },
            "training": {},
        }
        with self.assertRaisesRegex(ValueError, "residual_scale"):
            OfflineModel.from_dict(payload)

    def test_training_builds_reloadable_calibrated_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / "traffic.csv"
            with dataset.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=("timestamp", "flow_key", "observed_bps", "label"),
                )
                writer.writeheader()
                for flow_index in range(2):
                    flow = f"flow-{flow_index}"
                    base = 100_000.0 * (flow_index + 1)
                    for sample in range(60):
                        # Oscilação determinística pequena para produzir escala não nula.
                        value = base * (1.0 + ((sample % 7) - 3) * 0.005)
                        writer.writerow({
                            "timestamp": sample,
                            "flow_key": flow,
                            "observed_bps": value,
                            "label": "BENIGN",
                        })
                    for sample in range(60, 68):
                        writer.writerow({
                            "timestamp": sample,
                            "flow_key": flow,
                            "observed_bps": base * 80.0,
                            "label": "DDoS",
                        })

            observations, metadata = load_observations(
                [str(dataset)],
                value_column="observed_bps",
                value_scale=1.0,
                series_columns=["flow_key"],
                timestamp_column="timestamp",
                label_column="label",
                normal_labels=["BENIGN"],
            )
            model = train_model(
                observations,
                metadata,
                transform="log1p",
                alphas=[0.2, 0.35, 0.5],
                betas=[0.0, 0.1, 0.2],
                minimum_scale=0.01,
                explicit_threshold=None,
                normal_quantile=0.995,
                input_config={"test": True},
            )

            self.assertEqual(model.training["rows_normal"], 120)
            self.assertEqual(model.training["rows_attack"], 16)
            self.assertGreaterEqual(model.training["metrics"]["recall"], 0.9)
            self.assertGreaterEqual(model.training["metrics"]["precision"], 0.9)

            artifact = Path(directory) / "model.json"
            artifact.write_text(json.dumps(model.to_dict()), encoding="utf-8")
            loaded = load_offline_model(str(artifact))
            self.assertEqual(loaded.alpha, model.alpha)
            self.assertEqual(loaded.training["dataset_sha256"], metadata["dataset_sha256"])


if __name__ == "__main__":
    unittest.main()
