#!/usr/bin/env python3
"""Treina Holt + detector robusto offline a partir de um ou mais CSVs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from offline_model import OfflineModel, transform_value


MAD_K = 1.4826


@dataclass(frozen=True)
class Observation:
    series: str
    order: Tuple[int, object, int]
    value_bps: float
    is_attack: bool


def percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise ValueError("não é possível calcular percentil de uma lista vazia")
    ordered = sorted(float(value) for value in values)
    position = max(0.0, min(1.0, quantile)) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _parse_grid(raw: str, name: str, allow_zero: bool) -> List[float]:
    try:
        values = sorted({float(item.strip()) for item in raw.split(",") if item.strip()})
    except ValueError as exc:
        raise ValueError(f"{name} contém um valor não numérico") from exc
    lower = 0.0 if allow_zero else 1e-12
    if (not values
            or any(not math.isfinite(value) or value < lower or value > 1.0
                   for value in values)):
        interval = "[0, 1]" if allow_zero else "(0, 1]"
        raise ValueError(f"{name} deve conter valores no intervalo {interval}")
    return values


def _resolve_inputs(inputs: Iterable[str]) -> List[Path]:
    paths: List[Path] = []
    for raw in inputs:
        candidate = Path(raw).expanduser()
        if candidate.is_dir():
            paths.extend(sorted(path for path in candidate.rglob("*.csv") if path.is_file()))
        elif candidate.is_file():
            paths.append(candidate)
        else:
            raise ValueError(f"entrada não encontrada: {candidate}")
    unique = list(dict.fromkeys(path.resolve() for path in paths))
    if not unique:
        raise ValueError("nenhum CSV foi encontrado")
    return unique


def _dataset_digest(paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _timestamp_key(raw: str, row_index: int) -> Tuple[int, object, int]:
    text = (raw or "").strip()
    if not text:
        return (2, row_index, row_index)
    try:
        return (0, float(text), row_index)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return (1, parsed.timestamp(), row_index)
    except ValueError:
        # Formatos proprietários continuam utilizáveis na ordem original.
        return (2, row_index, row_index)


def load_observations(
    inputs: Iterable[str],
    value_column: str,
    value_scale: float,
    series_columns: Sequence[str],
    timestamp_column: Optional[str],
    label_column: Optional[str],
    normal_labels: Sequence[str],
) -> Tuple[List[Observation], Dict[str, object]]:
    paths = _resolve_inputs(inputs)
    normal = {label.strip().casefold() for label in normal_labels if label.strip()}
    if label_column and not normal:
        raise ValueError("ao usar --label-column, informe ao menos um --normal-label")
    if not math.isfinite(value_scale) or value_scale <= 0.0:
        raise ValueError("--value-scale deve ser positivo e finito")

    observations: List[Observation] = []
    skipped = 0
    label_counts: Dict[str, int] = {}
    global_index = 0

    for path_index, path in enumerate(paths):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            sample = handle.read(8192)
            handle.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            reader = csv.DictReader(handle, dialect=dialect)
            headers = reader.fieldnames or []
            required = [value_column, *series_columns]
            if timestamp_column:
                required.append(timestamp_column)
            if label_column:
                required.append(label_column)
            missing = [column for column in required if column not in headers]
            if missing:
                raise ValueError(
                    f"{path}: colunas ausentes {missing}; disponíveis: {headers}"
                )

            for row_index, row in enumerate(reader, start=2):
                global_index += 1
                try:
                    value = float((row.get(value_column) or "").strip()) * value_scale
                    if not math.isfinite(value) or value < 0.0:
                        raise ValueError
                except ValueError:
                    skipped += 1
                    continue

                parts = [(row.get(column) or "").strip() for column in series_columns]
                series = "|".join(parts) if parts else f"file:{path_index}"
                if not series or any(not part for part in parts):
                    skipped += 1
                    continue

                raw_label = (row.get(label_column) or "").strip() if label_column else "normal"
                normalized_label = raw_label.casefold()
                is_attack = bool(label_column) and normalized_label not in normal
                label_counts[raw_label or "<empty>"] = label_counts.get(raw_label or "<empty>", 0) + 1

                timestamp = row.get(timestamp_column, "") if timestamp_column else ""
                order = _timestamp_key(timestamp, global_index)
                observations.append(Observation(series, order, value, is_attack))

    if not observations:
        raise ValueError("nenhuma observação numérica válida foi encontrada")

    metadata: Dict[str, object] = {
        "files": [path.name for path in paths],
        "dataset_sha256": _dataset_digest(paths),
        "rows_skipped": skipped,
        "label_counts": label_counts,
    }
    preparation = []
    for path in paths:
        sidecar = Path(f"{path}.metadata.json")
        if not sidecar.is_file():
            continue
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"metadados de preparação inválidos em {sidecar}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"metadados de preparação devem ser um objeto: {sidecar}")
        if payload.get("output_file") not in (None, path.name):
            raise ValueError(f"metadados de preparação não correspondem a {path.name}")
        preparation.append(payload)
    if preparation:
        metadata["preparation"] = preparation
    return observations, metadata


def _group_observations(observations: Sequence[Observation]) -> Dict[str, List[Observation]]:
    grouped: Dict[str, List[Observation]] = {}
    for observation in observations:
        grouped.setdefault(observation.series, []).append(observation)
    for rows in grouped.values():
        rows.sort(key=lambda row: row.order)
    return grouped


def _normal_runs(grouped: Dict[str, List[Observation]]) -> List[List[float]]:
    runs: List[List[float]] = []
    for rows in grouped.values():
        current: List[float] = []
        for row in rows:
            if row.is_attack:
                if len(current) >= 2:
                    runs.append(current)
                current = []
            else:
                current.append(row.value_bps)
        if len(current) >= 2:
            runs.append(current)
    return runs


def holt_residuals(
    values: Sequence[float], alpha: float, beta: float, transform: str
) -> List[float]:
    if len(values) < 2:
        return []
    level = transform_value(values[0], transform)
    trend = 0.0
    residuals: List[float] = []
    for value in values[1:]:
        transformed = transform_value(value, transform)
        prediction = level + trend
        residuals.append(transformed - prediction)
        previous_level = level
        level = alpha * transformed + (1.0 - alpha) * (level + trend)
        trend = beta * (level - previous_level) + (1.0 - beta) * trend
    return residuals


def fit_holt(
    runs: Sequence[Sequence[float]],
    alphas: Sequence[float],
    betas: Sequence[float],
    transform: str,
) -> Tuple[float, float, float]:
    best: Optional[Tuple[float, float, float]] = None
    for alpha in alphas:
        for beta in betas:
            residuals = [
                residual
                for run in runs
                for residual in holt_residuals(run, alpha, beta, transform)
            ]
            if not residuals:
                continue
            mse = sum(residual * residual for residual in residuals) / len(residuals)
            candidate = (mse, alpha, beta)
            if best is None or candidate < best:
                best = candidate
    if best is None:
        raise ValueError("não há sequências normais suficientes para ajustar Holt")
    return best[1], best[2], best[0]


def robust_center_scale(residuals: Sequence[float], minimum_scale: float) -> Tuple[float, float]:
    if not residuals:
        raise ValueError("não há resíduos normais para calibrar o detector")
    center = statistics.median(residuals)
    deviations = [abs(value - center) for value in residuals]
    mad_scale = MAD_K * statistics.median(deviations)
    # O percentil protege contra MAD=0 em tráfego quase constante.
    percentile_scale = percentile(deviations, 0.90) / 1.6448536269514722
    scale = max(float(minimum_scale), mad_scale, percentile_scale)
    return center, scale


def _simulate_residuals(
    grouped: Dict[str, List[Observation]], alpha: float, beta: float, transform: str
) -> List[Tuple[float, bool]]:
    scored: List[Tuple[float, bool]] = []
    for rows in grouped.values():
        level: Optional[float] = None
        trend = 0.0
        for row in rows:
            transformed = transform_value(row.value_bps, transform)
            if level is None:
                # Ataque antes de qualquer baseline não deve inicializar o modelo.
                if not row.is_attack:
                    level = transformed
                continue
            prediction = level + trend
            scored.append((transformed - prediction, row.is_attack))
            # Assim como no runtime, anomalias rotuladas não contaminam o estado.
            if not row.is_attack:
                previous_level = level
                level = alpha * transformed + (1.0 - alpha) * (level + trend)
                trend = beta * (level - previous_level) + (1.0 - beta) * trend
    return scored


def _classification_metrics(
    scores: Sequence[Tuple[float, bool]],
    threshold: float,
    direction: str = "absolute",
) -> Dict[str, float]:
    tp = fp = tn = fn = 0
    for z_score, is_attack in scores:
        if direction == "spike":
            predicted = z_score > threshold
        elif direction == "drop":
            predicted = z_score < -threshold
        elif direction == "absolute":
            predicted = abs(z_score) > threshold
        else:
            raise ValueError(f"direção de threshold inválida: {direction}")
        if predicted and is_attack:
            tp += 1
        elif predicted:
            fp += 1
        elif is_attack:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
    fpr = fp / (fp + tn) if fp + tn else 0.0
    return {
        "true_positives": tp,
        "false_positives": fp,
        "true_negatives": tn,
        "false_negatives": fn,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "false_positive_rate": round(fpr, 6),
    }


def choose_threshold(
    normalized_scores: Sequence[Tuple[float, bool]],
    explicit_threshold: Optional[float],
    normal_quantile: float,
    direction: str = "spike",
) -> Tuple[float, Dict[str, float]]:
    if explicit_threshold is not None:
        if not math.isfinite(explicit_threshold) or not 1.0 <= explicit_threshold <= 20.0:
            raise ValueError("threshold deve estar no intervalo [1, 20]")
        threshold = float(explicit_threshold)
        return threshold, _classification_metrics(normalized_scores, threshold, direction)

    attack_count = sum(1 for _, is_attack in normalized_scores if is_attack)
    if attack_count:
        best: Optional[Tuple[Tuple[float, float, float, float], float, Dict[str, float]]] = None
        for step in range(8, 41):  # 2.00 .. 10.00
            threshold = step / 4.0
            metrics = _classification_metrics(normalized_scores, threshold, direction)
            rank = (
                metrics["f1"],
                metrics["precision"],
                -metrics["false_positive_rate"],
                metrics["recall"],
            )
            if best is None or rank > best[0]:
                best = (rank, threshold, metrics)
        assert best is not None
        return best[1], best[2]

    if direction == "spike":
        normal_scores = [score for score, is_attack in normalized_scores
                         if not is_attack and score > 0.0]
    else:
        normal_scores = [-score for score, is_attack in normalized_scores
                         if not is_attack and score < 0.0]
    if not normal_scores:
        normal_scores = [0.0]
    threshold = max(2.5, min(10.0, percentile(normal_scores, normal_quantile)))
    return threshold, _classification_metrics(normalized_scores, threshold, direction)


def choose_drop_threshold(
    normalized_scores: Sequence[Tuple[float, bool]],
    explicit_threshold: Optional[float],
    normal_quantile: float,
) -> Tuple[float, Dict[str, float]]:
    normal_scores = [score for score, is_attack in normalized_scores if not is_attack]
    negative_magnitudes = [-score for score in normal_scores if score < 0.0]
    if explicit_threshold is not None:
        if not math.isfinite(explicit_threshold) or not 1.0 <= explicit_threshold <= 20.0:
            raise ValueError("--drop-z-threshold deve estar no intervalo [1, 20]")
        threshold = float(explicit_threshold)
    elif negative_magnitudes:
        threshold = max(2.5, min(20.0, percentile(negative_magnitudes, normal_quantile)))
    else:
        threshold = 20.0

    alerts = sum(1 for score in normal_scores if score < -threshold)
    normal_count = len(normal_scores)
    return threshold, {
        "normal_samples": normal_count,
        "drop_alerts": alerts,
        "false_positive_rate": round(alerts / normal_count if normal_count else 0.0, 6),
        "normal_quantile": normal_quantile,
    }


def train_model(
    observations: Sequence[Observation],
    metadata: Dict[str, object],
    transform: str,
    alphas: Sequence[float],
    betas: Sequence[float],
    minimum_scale: float,
    explicit_threshold: Optional[float],
    normal_quantile: float,
    input_config: Dict[str, object],
    explicit_drop_threshold: Optional[float] = None,
    drop_normal_quantile: float = 0.999,
) -> OfflineModel:
    grouped = _group_observations(observations)
    normal_count = sum(1 for row in observations if not row.is_attack)
    attack_count = len(observations) - normal_count
    if normal_count < 20:
        raise ValueError("o treinamento requer ao menos 20 observações normais")

    runs = _normal_runs(grouped)
    alpha, beta, mse = fit_holt(runs, alphas, betas, transform)
    calibration_residuals = [
        residual
        for run in runs
        for residual in holt_residuals(run, alpha, beta, transform)
    ]
    if len(calibration_residuals) < 10:
        raise ValueError("o treinamento requer ao menos 10 resíduos normais consecutivos")
    center, scale = robust_center_scale(calibration_residuals, minimum_scale)

    simulated = _simulate_residuals(grouped, alpha, beta, transform)
    normalized = [((residual - center) / scale, is_attack)
                  for residual, is_attack in simulated]
    spike_threshold, spike_metrics = choose_threshold(
        normalized, explicit_threshold, normal_quantile, direction="spike"
    )
    drop_threshold, drop_metrics = choose_drop_threshold(
        normalized, explicit_drop_threshold, drop_normal_quantile
    )

    training = {
        "rows_total": len(observations),
        "rows_normal": normal_count,
        "rows_attack": attack_count,
        "rows_skipped": metadata.get("rows_skipped", 0),
        "series": len(grouped),
        "normal_runs": len(runs),
        "dataset_sha256": metadata.get("dataset_sha256"),
        "source_files": metadata.get("files", []),
        "label_counts": metadata.get("label_counts", {}),
        "preparation": metadata.get("preparation", []),
        "input": input_config,
        "holt_mse_transformed": mse,
        "residual_samples": len(calibration_residuals),
        "metrics": spike_metrics,
        "metrics_by_kind": {
            "THROUGHPUT_SPIKE": spike_metrics,
            "THROUGHPUT_DROP": drop_metrics,
        },
        "metrics_note": "calibração no próprio dataset; use outro dataset para avaliação final",
    }
    return OfflineModel(
        alpha=alpha,
        beta=beta,
        transform=transform,
        residual_center=center,
        residual_scale=scale,
        z_threshold=spike_threshold,
        created_at=datetime.now(timezone.utc).isoformat(),
        drop_z_threshold=drop_threshold,
        training=training,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Treina um artefato Holt + resíduos robustos para inferência online sem warmup."
    )
    parser.add_argument("inputs", nargs="+", help="arquivos CSV ou diretórios com CSVs")
    parser.add_argument("--output", required=True, help="arquivo JSON de saída")
    parser.add_argument("--value-column", default="observed_bps",
                        help="coluna com a taxa observada")
    parser.add_argument("--value-scale", type=float, default=1.0,
                        help="multiplicador; use 8 para converter bytes/s em bps")
    parser.add_argument("--series-columns", default="flow_key",
                        help="colunas que identificam uma série, separadas por vírgula")
    parser.add_argument("--timestamp-column", default="timestamp",
                        help="coluna temporal; informe vazio para preservar a ordem do CSV")
    parser.add_argument("--sample-interval-s", type=float, default=2.0,
                        help="intervalo temporal já aplicado ao dataset")
    parser.add_argument("--label-column", default=None,
                        help="coluna do rótulo; ausente significa dataset somente normal")
    parser.add_argument("--normal-label", action="append", dest="normal_labels",
                        help="rótulo normal/benigno; pode ser repetido")
    parser.add_argument("--transform", choices=("log1p", "identity"), default="log1p")
    parser.add_argument("--alpha-grid", default="0.1,0.2,0.35,0.5,0.7,0.9")
    parser.add_argument("--beta-grid", default="0.0,0.05,0.1,0.2,0.35,0.5")
    parser.add_argument("--minimum-scale", type=float, default=0.05,
                        help="piso robusto para a escala do resíduo transformado")
    parser.add_argument("--normal-quantile", type=float, default=0.995,
                        help="quantil usado sem rótulos de ataque")
    parser.add_argument("--spike-z-threshold", "--z-threshold",
                        dest="spike_z_threshold", type=float, default=None,
                        help="threshold positivo fixo; por padrão é calibrado por F1")
    parser.add_argument("--drop-z-threshold", type=float, default=None,
                        help="threshold negativo fixo; por padrão usa quantil benigno")
    parser.add_argument("--drop-normal-quantile", type=float, default=0.999,
                        help="quantil benigno para calibrar quedas (padrão: 0.999)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    normal_labels = args.normal_labels or ["false", "normal", "benign", "0"]
    series_columns = [column.strip() for column in args.series_columns.split(",")
                      if column.strip()]
    timestamp_column = args.timestamp_column.strip() or None
    try:
        alphas = _parse_grid(args.alpha_grid, "--alpha-grid", allow_zero=False)
        betas = _parse_grid(args.beta_grid, "--beta-grid", allow_zero=True)
        if not math.isfinite(args.normal_quantile) or not 0.5 <= args.normal_quantile < 1.0:
            raise ValueError("--normal-quantile deve estar no intervalo [0.5, 1)")
        if (not math.isfinite(args.drop_normal_quantile)
                or not 0.5 <= args.drop_normal_quantile < 1.0):
            raise ValueError("--drop-normal-quantile deve estar no intervalo [0.5, 1)")
        if not math.isfinite(args.minimum_scale) or args.minimum_scale <= 0.0:
            raise ValueError("--minimum-scale deve ser positivo")
        if not math.isfinite(args.sample_interval_s) or args.sample_interval_s <= 0.0:
            raise ValueError("--sample-interval-s deve ser positivo")
        observations, metadata = load_observations(
            args.inputs,
            args.value_column,
            args.value_scale,
            series_columns,
            timestamp_column,
            args.label_column,
            normal_labels,
        )
        model = train_model(
            observations,
            metadata,
            args.transform,
            alphas,
            betas,
            args.minimum_scale,
            args.spike_z_threshold,
            args.normal_quantile,
            {
                "value_column": args.value_column,
                "value_scale": args.value_scale,
                "series_columns": series_columns,
                "timestamp_column": timestamp_column,
                "sample_interval_s": args.sample_interval_s,
                "label_column": args.label_column,
                "normal_labels": normal_labels,
                "drop_normal_quantile": args.drop_normal_quantile,
            },
            explicit_drop_threshold=args.drop_z_threshold,
            drop_normal_quantile=args.drop_normal_quantile,
        )
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(model.to_dict(), indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
    except ValueError as exc:
        parser.error(str(exc))

    metrics = model.training.get("metrics", {})
    print(f"model: {output}")
    print(f"rows: {model.training['rows_total']} "
          f"(normal={model.training['rows_normal']}, attack={model.training['rows_attack']})")
    print(f"holt: alpha={model.alpha} beta={model.beta} transform={model.transform}")
    print(f"detector: center={model.residual_center:.6f} "
          f"scale={model.residual_scale:.6f} "
          f"spike_z_threshold={model.spike_z_threshold:.2f} "
          f"drop_z_threshold={model.effective_drop_z_threshold:.2f}")
    if model.training["rows_attack"]:
        print(f"calibration: precision={metrics.get('precision', 0):.3f} "
              f"recall={metrics.get('recall', 0):.3f} f1={metrics.get('f1', 0):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
