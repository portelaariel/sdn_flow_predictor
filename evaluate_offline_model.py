#!/usr/bin/env python3
"""Avalia um artefato offline em um CSV temporal que não participou do treino."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from offline_model import (
    OfflineModel,
    inverse_transform_value,
    load_offline_model,
    transform_value,
)
from train_offline_model import Observation, load_observations


def _group(observations: Sequence[Observation]) -> Dict[str, List[Observation]]:
    grouped: Dict[str, List[Observation]] = {}
    for observation in observations:
        grouped.setdefault(observation.series, []).append(observation)
    for rows in grouped.values():
        rows.sort(key=lambda row: row.order)
    return grouped


def evaluate(
    model: OfflineModel,
    observations: Sequence[Observation],
    min_rate_bps: float = 0.0,
) -> Dict[str, object]:
    if not math.isfinite(min_rate_bps) or min_rate_bps < 0.0:
        raise ValueError("--min-rate-bps deve ser não negativo e finito")

    any_counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    spike_counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    rows_unscored = 0
    priming_rows_unscored = 0
    below_floor_rows_unscored = 0
    attack_rows_unscored = 0
    spike_detections = drop_detections = 0

    grouped = _group(observations)
    for rows in grouped.values():
        level: Optional[float] = None
        trend = 0.0
        primed = 0
        for row in rows:
            transformed = transform_value(row.value_bps, model.transform)
            if primed < model.series_priming_samples:
                # Igual ao runtime: observações subpiso não podem alinhar o
                # nível; as demais atualizam Holt sem serem classificadas.
                rows_unscored += 1
                if row.value_bps < min_rate_bps:
                    below_floor_rows_unscored += 1
                    if row.is_attack:
                        attack_rows_unscored += 1
                        any_counts["fn"] += 1
                        spike_counts["fn"] += 1
                    continue
                priming_rows_unscored += 1
                if level is None:
                    level = transformed
                else:
                    previous_level = level
                    level = model.alpha * transformed + (1.0 - model.alpha) * (level + trend)
                    trend = model.beta * (level - previous_level) + (1.0 - model.beta) * trend
                primed += 1
                if row.is_attack:
                    attack_rows_unscored += 1
                    any_counts["fn"] += 1
                    spike_counts["fn"] += 1
                continue

            prediction = level + trend
            residual = transformed - prediction
            z_score = (residual - model.residual_center) / model.residual_scale
            predicted_bps = inverse_transform_value(prediction, model.transform)
            above_floor = max(row.value_bps, predicted_bps) >= min_rate_bps
            predicted_spike = (
                z_score > model.spike_z_threshold and above_floor
            )
            predicted_drop = (
                z_score < -model.effective_drop_z_threshold and above_floor
            )
            predicted_anomaly = predicted_spike or predicted_drop

            if predicted_anomaly:
                if predicted_spike:
                    spike_detections += 1
                else:
                    drop_detections += 1

            for predicted, counts in (
                (predicted_anomaly, any_counts),
                (predicted_spike, spike_counts),
            ):
                if predicted and row.is_attack:
                    counts["tp"] += 1
                elif predicted:
                    counts["fp"] += 1
                elif row.is_attack:
                    counts["fn"] += 1
                else:
                    counts["tn"] += 1

            # O runtime protege o estado com a própria decisão, sem conhecer o rótulo.
            if not predicted_anomaly:
                previous_level = level
                level = model.alpha * transformed + (1.0 - model.alpha) * (level + trend)
                trend = model.beta * (level - previous_level) + (1.0 - model.beta) * trend

    def classification(counts: Dict[str, int]) -> Dict[str, object]:
        tp, fp, tn, fn = (counts[key] for key in ("tp", "fp", "tn", "fn"))
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
        false_positive_rate = fp / (fp + tn) if fp + tn else 0.0
        return {
            "true_positives": tp,
            "false_positives": fp,
            "true_negatives": tn,
            "false_negatives": fn,
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(f1, 6),
            "false_positive_rate": round(false_positive_rate, 6),
        }

    return {
        "rows_total": len(observations),
        "rows_scored": len(observations) - rows_unscored,
        "rows_unscored": rows_unscored,
        # Alias conservado para consumidores dos relatórios v1/v2.
        "initial_rows_unscored": rows_unscored,
        "priming_rows_unscored": priming_rows_unscored,
        "below_floor_rows_unscored": below_floor_rows_unscored,
        "attack_rows_unscored": attack_rows_unscored,
        "series_priming_samples": model.series_priming_samples,
        "series": len(grouped),
        "ddos_throughput_spike": classification(spike_counts),
        "all_throughput_anomalies": classification(any_counts),
        "spike_detections": spike_detections,
        "drop_detections": drop_detections,
        "spike_z_threshold": model.spike_z_threshold,
        "drop_z_threshold": model.effective_drop_z_threshold,
        "min_rate_bps": min_rate_bps,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Avalia o modelo Holt offline em um CSV temporal independente."
    )
    parser.add_argument("model", help="artefato JSON produzido pelo treinamento")
    parser.add_argument("inputs", nargs="+", help="CSVs compactos de validação")
    parser.add_argument("--value-column", default="observed_bps")
    parser.add_argument("--value-scale", type=float, default=1.0)
    parser.add_argument("--series-columns", default="flow_key")
    parser.add_argument("--timestamp-column", default="timestamp")
    parser.add_argument("--label-column", default="label")
    parser.add_argument("--normal-label", action="append", dest="normal_labels")
    parser.add_argument("--min-rate-bps", type=float, default=0.0,
                        help="mesmo piso de alerta utilizado no runtime")
    parser.add_argument("--output", help="grava também as métricas em JSON")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    series_columns = [
        column.strip() for column in args.series_columns.split(",") if column.strip()
    ]
    timestamp_column = args.timestamp_column.strip() or None
    label_column = args.label_column.strip() or None
    try:
        if not label_column:
            raise ValueError("--label-column é obrigatório para avaliação")
        model = load_offline_model(args.model)
        observations, metadata = load_observations(
            args.inputs,
            args.value_column,
            args.value_scale,
            series_columns,
            timestamp_column,
            label_column,
            args.normal_labels or ["BENIGN"],
        )
        metrics = evaluate(model, observations, args.min_rate_bps)
        report = {
            "model": str(Path(args.model).expanduser()),
            "validation_files": metadata.get("files", []),
            "validation_sha256": metadata.get("dataset_sha256"),
            "validation_preparation": metadata.get("preparation", []),
            "metrics": metrics,
        }
        if args.output:
            output = Path(args.output).expanduser()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                              encoding="utf-8")
    except ValueError as exc:
        parser.error(str(exc))

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
