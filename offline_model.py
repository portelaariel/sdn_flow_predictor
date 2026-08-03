#!/usr/bin/env python3
"""Contrato e validação do artefato de detecção treinado offline."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict


SCHEMA_VERSION = 1
MODEL_TYPE = "holt_residual"
SUPPORTED_TRANSFORMS = {"identity", "log1p"}


def transform_value(value: float, transform: str) -> float:
    """Transforma uma taxa não negativa para o espaço usado pelo modelo."""
    value = max(0.0, float(value))
    if transform == "identity":
        return value
    if transform == "log1p":
        return math.log1p(value)
    raise ValueError(f"transformação não suportada: {transform}")


def inverse_transform_value(value: float, transform: str) -> float:
    """Converte a previsão do espaço do modelo novamente para bps."""
    if transform == "identity":
        return max(0.0, float(value))
    if transform == "log1p":
        # Evita overflow quando um artefato inválido ou uma tendência extrema
        # produzir um expoente fora da faixa numérica útil.
        return max(0.0, math.expm1(min(float(value), 709.0)))
    raise ValueError(f"transformação não suportada: {transform}")


def _finite_float(value: Any, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} deve ser numérico") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} deve ser finito")
    return parsed


@dataclass(frozen=True)
class OfflineModel:
    """Parâmetros imutáveis carregados por todas as séries online."""

    alpha: float
    beta: float
    transform: str
    residual_center: float
    residual_scale: float
    z_threshold: float
    created_at: str
    training: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    model_type: str = MODEL_TYPE

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "OfflineModel":
        if not isinstance(payload, dict):
            raise ValueError("o artefato deve conter um objeto JSON")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version incompatível: esperado {SCHEMA_VERSION}, "
                f"recebido {payload.get('schema_version')!r}"
            )
        if payload.get("model_type") != MODEL_TYPE:
            raise ValueError(f"model_type incompatível: {payload.get('model_type')!r}")

        holt = payload.get("holt")
        detector = payload.get("detector")
        if not isinstance(holt, dict) or not isinstance(detector, dict):
            raise ValueError("o artefato requer os objetos 'holt' e 'detector'")

        alpha = _finite_float(holt.get("alpha"), "holt.alpha")
        beta = _finite_float(holt.get("beta"), "holt.beta")
        if not 0.0 < alpha <= 1.0:
            raise ValueError("holt.alpha deve estar no intervalo (0, 1]")
        if not 0.0 <= beta <= 1.0:
            raise ValueError("holt.beta deve estar no intervalo [0, 1]")

        transform = str(holt.get("transform", "log1p"))
        if transform not in SUPPORTED_TRANSFORMS:
            raise ValueError(f"holt.transform não suportado: {transform!r}")

        center = _finite_float(detector.get("residual_center"),
                               "detector.residual_center")
        scale = _finite_float(detector.get("residual_scale"),
                              "detector.residual_scale")
        threshold = _finite_float(detector.get("z_threshold"),
                                  "detector.z_threshold")
        if scale <= 0.0:
            raise ValueError("detector.residual_scale deve ser positivo")
        if not 1.0 <= threshold <= 20.0:
            raise ValueError("detector.z_threshold deve estar no intervalo [1, 20]")

        training = payload.get("training", {})
        if not isinstance(training, dict):
            raise ValueError("training deve ser um objeto JSON")

        return cls(
            alpha=alpha,
            beta=beta,
            transform=transform,
            residual_center=center,
            residual_scale=scale,
            z_threshold=threshold,
            created_at=str(payload.get("created_at", "unknown")),
            training=training,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "model_type": self.model_type,
            "created_at": self.created_at,
            "holt": {
                "alpha": self.alpha,
                "beta": self.beta,
                "transform": self.transform,
            },
            "detector": {
                "residual_center": self.residual_center,
                "residual_scale": self.residual_scale,
                "z_threshold": self.z_threshold,
                "score": "absolute_robust_z",
            },
            "training": self.training,
        }

    def status(self) -> Dict[str, Any]:
        metrics = self.training.get("metrics", {})
        input_config = self.training.get("input", {})
        return {
            "loaded": True,
            "mode": "offline",
            "model_type": self.model_type,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "transform": self.transform,
            "alpha": self.alpha,
            "beta": self.beta,
            "z_threshold": self.z_threshold,
            "training_rows": self.training.get("rows_total"),
            "training_series": self.training.get("series"),
            "sample_interval_s": (input_config.get("sample_interval_s")
                                  if isinstance(input_config, dict) else None),
            "dataset_sha256": self.training.get("dataset_sha256"),
            "metrics": metrics,
        }


def load_offline_model(path: str) -> OfflineModel:
    model_path = Path(path)
    try:
        payload = json.loads(model_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"não foi possível ler o modelo {model_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON inválido no modelo {model_path}: {exc}") from exc
    return OfflineModel.from_dict(payload)
