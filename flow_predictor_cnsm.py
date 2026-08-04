#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FlowPredictor (CNSM) — Predição de Vazão + Detecção de Anomalias + Mitigação Autônoma
======================================================================================

Arquitetura (consistente com o testbed CNSM):
  - Coleta:    Ryu ofctl_rest (OF 1.0)  -> /stats/switches, /stats/port/{dpid}, /stats/flow/{dpid}
  - Predição:  Holt calibrado offline (nível + tendência) por série temporal
  - Anomalia:  z-score robusto usando resíduos calibrados offline + heurística de surto de fluxos
  - Mitigação: FlowBlocker  -> POST /flowblocker/service  {"src_ip": ..., "dst_ip": ...}
  - Estado:    ETCD (opcional) -> flowpredictor/state/<cid>  (visibilidade multi-domínio)
  - Feedback:  POST /predictor/feedback ajusta sensibilidade (threshold adaptativo)

Independente de topologia: os DPIDs são descobertos dinamicamente via /stats/switches;
as séries por porta e por fluxo (nw_src -> nw_dst) são criadas sob demanda.

ENV (mesmo padrão dos demais serviços):
  RYU_BASE_URL          http://192.168.10.10:8080
  FLOWBLOCKER_URL       http://192.168.10.30:7070   (base, sem path)
  PORT                  6060
  CONTROLLER_ID         192.168.10.10
  ETCD_ENDPOINTS        192.168.253.11:2379,...     (opcional)
  POLL_INTERVAL_S       2.0
  HISTORY_WINDOW        120        # amostras retidas por série
  Z_THRESHOLD           4.0        # sensibilidade inicial (adaptativa via feedback)
  MIN_RATE_BPS          50000      # ignora séries abaixo disso (ruído)
  FLOW_IDLE_RESET_SAMPLES 2        # zeros consecutivos antes de reiniciar um fluxo
  AUTO_MITIGATE         true|false
  DRY_RUN               true|false (loga a mitigação sem executar)
  MITIGATION_COOLDOWN_S 60
  ANOMALY_EVENT_COOLDOWN_S 60      # agrupa alertas repetidos da mesma série/tipo
  WHITELIST_IPS         10.0.0.254,...  (nunca bloquear)
  WARMUP_SAMPLES        15         # fallback adaptativo quando não há modelo offline
  OFFLINE_MODEL_PATH               # artefato JSON criado por train_offline_model.py
  OFFLINE_MODEL_REQUIRED false      # falha startup se o artefato não puder ser carregado
  ONLINE_MODEL_ADAPTATION false     # permite adaptar a calibração offline por série

  # Consenso multi-domínio (opt-in; requer ETCD compartilhado):
  COLLABORATION_ENABLED  false
  COLLAB_EXPECTED_DOMAINS 2
  COLLAB_MIN_DOMAINS     2
  COLLAB_WINDOW_S        4
  COLLAB_EVIDENCE_TTL_S  12
  COLLAB_CLAIM_TTL_S     60

  # Persistência do histórico (dataset offline p/ LSTM/GRU, RMSE/MAE, gráficos):
  EXPORT_ENABLED        true|false (default true)
  EXPORT_DIR            prediction_history
  EXPORT_PREFIXES       flow:      # csv; use "flow:,port:" p/ incluir portas
  EXPORT_FLUSH_EVERY    10         # flush a cada N linhas por série
"""

import os
import json
import math
import time
import uuid
import glob
import hashlib
import atexit
import logging
import threading
from datetime import datetime
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify, request

from collaborative_decision import (
    canonical_flow_key,
    clip01,
    load_collaboration_weights,
    score_collaborative_evidence,
)
from offline_model import (
    OfflineModel,
    inverse_transform_value,
    load_offline_model,
    transform_value,
)

# ---------------- Configuração via ENV ----------------
RYU_BASE_URL      = os.environ.get("RYU_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
FLOWBLOCKER_URL   = os.environ.get("FLOWBLOCKER_URL", "http://127.0.0.1:7070").rstrip("/")
PORT              = int(os.environ.get("PORT", "6060"))
CONTROLLER_ID     = os.environ.get("CONTROLLER_ID", "unset_cid")
ETCD_ENDPOINTS    = os.environ.get("ETCD_ENDPOINTS", "").strip()
POLL_INTERVAL_S   = float(os.environ.get("POLL_INTERVAL_S", "2.0"))
HISTORY_WINDOW    = int(os.environ.get("HISTORY_WINDOW", "120"))
Z_THRESHOLD       = float(os.environ.get("Z_THRESHOLD", "4.0"))
MIN_RATE_BPS      = float(os.environ.get("MIN_RATE_BPS", "50000"))
FLOW_IDLE_RESET_SAMPLES = int(os.environ.get("FLOW_IDLE_RESET_SAMPLES", "2"))
AUTO_MITIGATE     = os.environ.get("AUTO_MITIGATE", "true").lower() == "true"
DRY_RUN           = os.environ.get("DRY_RUN", "false").lower() == "true"
COOLDOWN_S        = float(os.environ.get("MITIGATION_COOLDOWN_S", "60"))
EVENT_COOLDOWN_S  = float(os.environ.get("ANOMALY_EVENT_COOLDOWN_S", "60"))
WHITELIST_IPS     = {ip.strip() for ip in os.environ.get("WHITELIST_IPS", "").split(",") if ip.strip()}
WARMUP_SAMPLES    = int(os.environ.get("WARMUP_SAMPLES", "15"))
FLOW_SURGE_WARMUP = int(os.environ.get("FLOW_SURGE_WARMUP_SAMPLES", str(WARMUP_SAMPLES)))
REQUEST_TIMEOUT_S = float(os.environ.get("REQUEST_TIMEOUT_S", "5.0"))
OFFLINE_MODEL_PATH = os.environ.get("OFFLINE_MODEL_PATH", "").strip()
OFFLINE_MODEL_REQUIRED = os.environ.get("OFFLINE_MODEL_REQUIRED", "false").lower() == "true"
ONLINE_MODEL_ADAPTATION = os.environ.get("ONLINE_MODEL_ADAPTATION", "false").lower() == "true"

# --- Fusão colaborativa multi-domínio (desativada por padrão) ---
COLLABORATION_ENABLED = os.environ.get("COLLABORATION_ENABLED", "false").lower() == "true"
COLLAB_WINDOW_S = float(os.environ.get("COLLAB_WINDOW_S", "4.0"))
COLLAB_EVIDENCE_TTL_S = float(os.environ.get("COLLAB_EVIDENCE_TTL_S", "12.0"))
COLLAB_CLAIM_TTL_S = float(os.environ.get("COLLAB_CLAIM_TTL_S", "60.0"))
COLLAB_EVALUATION_INTERVAL_S = float(
    os.environ.get("COLLAB_EVALUATION_INTERVAL_S", "0.5")
)
COLLAB_EXPECTED_DOMAINS = int(os.environ.get("COLLAB_EXPECTED_DOMAINS", "2"))
COLLAB_MIN_DOMAINS = int(os.environ.get("COLLAB_MIN_DOMAINS", "2"))
COLLAB_PERSISTENCE_WINDOWS = int(os.environ.get("COLLAB_PERSISTENCE_WINDOWS", "3"))
COLLAB_SUSPECT_THRESHOLD = float(os.environ.get("COLLAB_SUSPECT_THRESHOLD", "0.40"))
COLLAB_ALERT_THRESHOLD = float(os.environ.get("COLLAB_ALERT_THRESHOLD", "0.60"))
COLLAB_DECISION_THRESHOLD = float(os.environ.get("COLLAB_DECISION_THRESHOLD", "0.80"))
COLLAB_RATE_RATIO_MAX = float(os.environ.get("COLLAB_RATE_RATIO_MAX", "10.0"))

COLLAB_WEIGHTS = load_collaboration_weights(os.environ.get("COLLAB_WEIGHTS_JSON", ""))

if not math.isfinite(EVENT_COOLDOWN_S) or EVENT_COOLDOWN_S < 0.0:
    raise ValueError("ANOMALY_EVENT_COOLDOWN_S deve ser não negativo e finito")
if FLOW_IDLE_RESET_SAMPLES < 1:
    raise ValueError("FLOW_IDLE_RESET_SAMPLES deve ser um inteiro positivo")
if (not math.isfinite(COLLAB_WINDOW_S) or COLLAB_WINDOW_S <= 0.0
        or not math.isfinite(COLLAB_EVIDENCE_TTL_S)
        or COLLAB_EVIDENCE_TTL_S < COLLAB_WINDOW_S
        or not math.isfinite(COLLAB_CLAIM_TTL_S) or COLLAB_CLAIM_TTL_S <= 0.0
        or not math.isfinite(COLLAB_EVALUATION_INTERVAL_S)
        or COLLAB_EVALUATION_INTERVAL_S <= 0.0):
    raise ValueError("janelas/TTLs colaborativos devem ser positivos e o TTL cobrir a janela")
if (COLLAB_EXPECTED_DOMAINS < 1 or COLLAB_MIN_DOMAINS < 1
        or COLLAB_PERSISTENCE_WINDOWS < 1
        or (COLLABORATION_ENABLED
            and COLLAB_MIN_DOMAINS > COLLAB_EXPECTED_DOMAINS)):
    raise ValueError("quantidades de domínios/persistência colaborativas são inválidas")
if not (0.0 <= COLLAB_SUSPECT_THRESHOLD <= COLLAB_ALERT_THRESHOLD
        <= COLLAB_DECISION_THRESHOLD <= 1.0):
    raise ValueError("thresholds MCDA devem ser ordenados dentro de [0, 1]")
if not math.isfinite(COLLAB_RATE_RATIO_MAX) or COLLAB_RATE_RATIO_MAX <= 1.0:
    raise ValueError("COLLAB_RATE_RATIO_MAX deve ser finito e maior que um")

# --- Persistência do histórico de predição (aditivo; não afeta a lógica online) ---
EXPORT_ENABLED     = os.environ.get("EXPORT_ENABLED", "true").lower() == "true"
EXPORT_DIR         = os.environ.get("EXPORT_DIR", "prediction_history")
EXPORT_PREFIXES    = tuple(p.strip() for p in
                           os.environ.get("EXPORT_PREFIXES", "flow:").split(",") if p.strip())
EXPORT_FLUSH_EVERY = int(os.environ.get("EXPORT_FLUSH_EVERY", "10"))  # flush a cada N linhas/série

# ---------------- Logging (padrão [METRICS] do projeto) ----------------
logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s:FlowPredictor:%(asctime)s - %(message)s")
logger = logging.getLogger("FlowPredictor")


def _load_runtime_model() -> Tuple[Optional[OfflineModel], Optional[str]]:
    if not OFFLINE_MODEL_PATH:
        if OFFLINE_MODEL_REQUIRED:
            raise RuntimeError("OFFLINE_MODEL_REQUIRED=true, mas OFFLINE_MODEL_PATH está vazio")
        logger.warning("Modelo offline não configurado; usando detector adaptativo com warmup")
        return None, None
    try:
        model = load_offline_model(OFFLINE_MODEL_PATH)
        logger.info(
            "Modelo offline carregado: path=%s alpha=%s beta=%s "
            "spike_threshold=%s drop_threshold=%s priming_samples=%s",
            OFFLINE_MODEL_PATH, model.alpha, model.beta,
            model.spike_z_threshold, model.effective_drop_z_threshold,
            model.series_priming_samples,
        )
        input_config = model.training.get("input", {})
        trained_interval = (input_config.get("sample_interval_s")
                            if isinstance(input_config, dict) else None)
        if trained_interval is not None:
            try:
                relative_error = (abs(float(trained_interval) - POLL_INTERVAL_S)
                                  / max(POLL_INTERVAL_S, 1e-9))
                if relative_error > 0.25:
                    logger.warning(
                        "Intervalo do modelo (%ss) difere do polling online (%ss); "
                        "reampostre o dataset ou ajuste POLL_INTERVAL_S",
                        trained_interval, POLL_INTERVAL_S,
                    )
            except (TypeError, ValueError):
                logger.warning("sample_interval_s inválido na proveniência do modelo")
        return model, None
    except ValueError as exc:
        if OFFLINE_MODEL_REQUIRED:
            raise RuntimeError(f"modelo offline obrigatório inválido: {exc}") from exc
        logger.error("Falha ao carregar modelo offline: %s. Usando fallback adaptativo.", exc)
        return None, str(exc)


_offline_model, _offline_model_error = _load_runtime_model()


def now_ns() -> int:
    return time.time_ns()


def _metric(tag: str, msg: str) -> None:
    logger.info(f"[METRICS][{tag}] {msg}")


def blocked_flow_pairs(flows: List[Dict[str, Any]]) -> set:
    """Identifica pares cobertos por uma regra DROP OpenFlow 1.0.

    O Ryu representa DROP como uma lista de ações vazia. O par inteiro é
    excluído da coleta para que a própria regra de mitigação não seja tratada
    como tráfego encaminhado e não realimente o detector.
    """
    blocked = set()
    for flow in flows:
        if flow.get("actions") != []:
            continue
        match = flow.get("match", {})
        src, dst = match.get("nw_src"), match.get("nw_dst")
        if src and dst:
            blocked.add((src, dst))
    return blocked


# ---------------- ETCD opcional (mesma degradação graciosa do FlowBlocker) ----------------
_etcd = None
if ETCD_ENDPOINTS:
    try:
        import etcd3

        def _parse_hp(url: str) -> Tuple[str, int]:
            u = url.strip()
            if "://" in u:
                u = u.split("://", 1)[1]
            if ":" in u:
                h, p = u.split(":", 1)
                return h, int(p)
            return u, 2379

        _h, _p = _parse_hp(ETCD_ENDPOINTS.split(",")[0])
        _etcd = etcd3.client(host=_h, port=_p, timeout=5)
        # A construção do cliente é lazy; o status confirma que a colaboração
        # não ficará ativa sobre um endpoint apenas configurado, porém inacessível.
        _etcd.status()
        logger.info(f"ETCD client inicializado em {_h}:{_p}")
    except Exception as e:
        logger.error(f"Falha ao iniciar etcd3: {e}. Operando apenas em memória.")
        _etcd = None


# =====================================================================
# 1) PREDIÇÃO — Holt (nível + tendência), leve e adequado a streaming
# =====================================================================
class HoltPredictor:
    """
    Suavização exponencial dupla (Holt). Escolhida por:
      - custo O(1) por amostra (escala para milhares de séries);
      - captura nível E tendência (melhor que EWMA puro em rampas de vazão);
      - sem dependências pesadas (funciona em qualquer topologia/hardware).
    A interface (update/predict) é pluggável: pode ser trocada por ARIMA/LSTM
    sem alterar o restante do módulo.
    """

    def __init__(self, alpha: float = 0.35, beta: float = 0.10,
                 transform: str = "identity"):
        self.alpha = alpha
        self.beta = beta
        self.transform = transform
        self.level: Optional[float] = None
        self.trend: float = 0.0
        self.n = 0

    def update(self, value: float) -> None:
        transformed = transform_value(value, self.transform)
        if self.level is None:
            self.level = transformed
            self.trend = 0.0
        else:
            prev_level = self.level
            self.level = (self.alpha * transformed
                          + (1 - self.alpha) * (self.level + self.trend))
            self.trend = self.beta * (self.level - prev_level) + (1 - self.beta) * self.trend
        self.n += 1

    def predict_transformed(self, horizon: int = 1) -> float:
        if self.level is None:
            return transform_value(0.0, self.transform)
        return self.level + horizon * self.trend

    def predict(self, horizon: int = 1) -> float:
        if self.level is None:
            return 0.0
        return inverse_transform_value(self.predict_transformed(horizon), self.transform)

    def residual(self, observed: float, predicted: float) -> float:
        """Resíduo no espaço em que o modelo offline foi calibrado."""
        return (transform_value(observed, self.transform)
                - transform_value(predicted, self.transform))

    @property
    def trend_bps(self) -> float:
        if self.level is None:
            return 0.0
        return self.predict(1) - self.predict(0)


# =====================================================================
# 2) DETECÇÃO — z-score robusto (MAD) sobre resíduos de predição
# =====================================================================
class ResidualAnomalyDetector:
    """
    Mantém janela de resíduos (observado - predito). Usa mediana + MAD
    (Median Absolute Deviation) para robustez contra outliers passados —
    um pico anômalo anterior não "envenena" a estatística como faria com
    média/desvio-padrão simples.
    """

    MAD_K = 1.4826  # fator de consistência para distribuição normal

    def __init__(self, window: int, z_threshold: float, warmup: int,
                 fixed_center: Optional[float] = None,
                 fixed_scale: Optional[float] = None,
                 online_adaptation: bool = False,
                 drop_z_threshold: Optional[float] = None):
        self.residuals: deque = deque(maxlen=window)
        # z_threshold permanece como alias do lado positivo para não quebrar
        # clientes e modelos legados que usavam um único valor simétrico.
        self.z_threshold = z_threshold
        self.drop_z_threshold = (z_threshold if drop_z_threshold is None
                                 else drop_z_threshold)
        self.warmup = warmup
        self.fixed_center = fixed_center
        self.fixed_scale = fixed_scale
        self.online_adaptation = online_adaptation

    @property
    def offline_calibrated(self) -> bool:
        return self.fixed_center is not None and self.fixed_scale is not None

    @property
    def ready(self) -> bool:
        return self.offline_calibrated or len(self.residuals) >= self.warmup

    def _center_scale(self) -> Tuple[float, float]:
        use_offline = (self.offline_calibrated
                       and (not self.online_adaptation
                            or len(self.residuals) < max(3, self.warmup)))
        if use_offline:
            return float(self.fixed_center), max(float(self.fixed_scale), 1e-6)

        data = sorted(self.residuals)
        median = data[len(data) // 2]
        mad = sorted(abs(x - median) for x in data)[len(data) // 2]
        return median, max(self.MAD_K * mad, 1e-6)

    def score(self, residual: float) -> Tuple[float, bool]:
        """Retorna (z_score, is_anomaly) usando calibração offline ou warmup."""
        if not self.ready:
            self.residuals.append(residual)
            return 0.0, False

        median, sigma = self._center_scale()
        z = (residual - median) / sigma

        is_anom = z > self.z_threshold or z < -self.drop_z_threshold
        # Resíduos anômalos NÃO entram na janela (evita mascarar ataques prolongados)
        if not is_anom and (not self.offline_calibrated or self.online_adaptation):
            self.residuals.append(residual)
        return z, is_anom

    def threshold_for(self, z_score: float) -> float:
        return self.z_threshold if z_score >= 0.0 else self.drop_z_threshold


# =====================================================================
# 2.5) PERSISTÊNCIA — exporta cada amostra para CSV (dataset offline)
# =====================================================================
class PredictionHistoryExporter:
    """
    Persistência incremental do histórico de predição — 1 CSV por série.

    Propriedades garantidas:
      - ADITIVA: chamada após o history.append(); nunca altera a lógica online.
      - SINCRONIZADA: cada linha corresponde exatamente a uma amostra da série
        (mesmo ts/observado/predito do deque e mesmo resíduo visto pelo detector).
      - INCREMENTAL: append-only; header escrito uma única vez por arquivo.
      - LEVE: file handles mantidos abertos em cache (1 open por série na vida
        do processo) com flush a cada EXPORT_FLUSH_EVERY linhas.
      - À PROVA DE FALHA: qualquer exceção de I/O é logada e a predição segue.

    Nome do arquivo: flow:1:10.0.0.1->10.0.0.4  ->  flow_1_10.0.0.1_10.0.0.4.csv
    """

    HEADER = ["timestamp", "datetime_iso", "flow_key", "dpid", "src_ip", "dst_ip",
              "observed_bps", "predicted_bps", "prediction_error", "absolute_error",
              "residual", "z_score", "is_anomaly"]

    def __init__(self, directory: str, prefixes: Tuple[str, ...] = ("flow:",),
                 flush_every: int = 10, enabled: bool = True):
        self.directory = directory
        self.prefixes = prefixes
        self.flush_every = max(1, flush_every)
        self.enabled = enabled
        self.records_written = 0
        self._files: Dict[str, Any] = {}      # key -> file handle (cache)
        self._pending: Dict[str, int] = {}    # key -> linhas desde o último flush
        self._lock = threading.Lock()
        if self.enabled:
            try:
                os.makedirs(self.directory, exist_ok=True)
                logger.info(f"Exporter ativo: dir={self.directory} "
                            f"prefixes={self.prefixes} flush_every={self.flush_every}")
            except OSError as e:
                logger.error(f"Exporter desabilitado (mkdir falhou): {e}")
                self.enabled = False

    @staticmethod
    def _filename_for(key: str) -> str:
        safe = key.replace("->", "_").replace(":", "_").replace("/", "_")
        return f"{safe}.csv"

    def record(self, key: str, meta: Dict[str, Any], ts: float,
               observed_bps: float, predicted_bps: float, residual: float,
               z_score: Optional[float], is_anomaly: bool) -> None:
        """Persiste UMA amostra. z_score=None (warm-up) é gravado como vazio."""
        if not self.enabled or not key.startswith(self.prefixes):
            return
        try:
            err = observed_bps - predicted_bps
            row = ",".join([
                f"{ts:.3f}",
                datetime.fromtimestamp(ts).isoformat(timespec="milliseconds"),
                key,
                str(meta.get("dpid", "")),
                str(meta.get("nw_src", "")),
                str(meta.get("nw_dst", "")),
                f"{observed_bps:.1f}",
                f"{predicted_bps:.1f}",
                f"{err:.1f}",
                f"{abs(err):.1f}",
                f"{residual:.1f}",
                ("" if z_score is None else f"{z_score:.4f}"),
                str(is_anomaly),
            ]) + "\n"

            with self._lock:
                fh = self._files.get(key)
                if fh is None:
                    path = os.path.join(self.directory, self._filename_for(key))
                    write_header = not os.path.exists(path) or os.path.getsize(path) == 0
                    fh = open(path, "a", buffering=8192)
                    if write_header:
                        fh.write(",".join(self.HEADER) + "\n")
                    self._files[key] = fh
                    self._pending[key] = 0
                fh.write(row)
                self._pending[key] += 1
                self.records_written += 1
                if self._pending[key] >= self.flush_every:
                    fh.flush()
                    self._pending[key] = 0
        except Exception as e:
            # I/O nunca pode derrubar o pipeline de predição
            logger.error(f"Exporter: falha ao gravar amostra de {key}: {e}")

    def status(self) -> Dict[str, Any]:
        with self._lock:
            n_disk = len(glob.glob(os.path.join(self.directory, "*.csv"))) \
                     if self.enabled and os.path.isdir(self.directory) else 0
            return {
                "enabled": self.enabled,
                "directory": self.directory,
                "files": n_disk,
                "series_active": len(self._files),
                "records_written": self.records_written,
                "prefixes": list(self.prefixes),
                "flush_every": self.flush_every,
            }

    def close(self) -> None:
        """Flush + close de todos os handles (registrado via atexit)."""
        with self._lock:
            for fh in self._files.values():
                try:
                    fh.flush()
                    fh.close()
                except Exception:
                    pass
            self._files.clear()


# Instância única do módulo (None quando desabilitado)
_exporter: Optional[PredictionHistoryExporter] = (
    PredictionHistoryExporter(EXPORT_DIR, EXPORT_PREFIXES,
                              EXPORT_FLUSH_EVERY, EXPORT_ENABLED)
    if EXPORT_ENABLED else None
)
if _exporter:
    atexit.register(_exporter.close)


# =====================================================================
# 3) SÉRIE TEMPORAL — encapsula contadores, taxa, preditor e detector
# =====================================================================
class SeriesState:
    """Uma série por chave (porta ou fluxo). Converte contadores cumulativos em taxa (bps)."""

    def __init__(self, key: str, meta: Dict[str, Any],
                 offline_model: Optional[OfflineModel] = None):
        self.key = key
        self.meta = meta                          # {"type": "flow"/"port", "dpid":.., "nw_src":.., ...}
        self.last_bytes: Optional[int] = None
        self.last_ts: Optional[float] = None
        self.rate_bps: float = 0.0
        self.predicted_bps: float = 0.0
        self.model_residual: float = 0.0
        self.idle_samples: int = 0
        self.detection_mode = "offline" if offline_model else "adaptive"
        self.series_priming_samples = (
            offline_model.series_priming_samples if offline_model else 0
        )
        if offline_model:
            self.predictor = HoltPredictor(
                offline_model.alpha,
                offline_model.beta,
                offline_model.transform,
            )
            self.detector = ResidualAnomalyDetector(
                HISTORY_WINDOW,
                offline_model.z_threshold,
                warmup=WARMUP_SAMPLES,
                fixed_center=offline_model.residual_center,
                fixed_scale=offline_model.residual_scale,
                online_adaptation=ONLINE_MODEL_ADAPTATION,
                drop_z_threshold=offline_model.effective_drop_z_threshold,
            )
        else:
            self.predictor = HoltPredictor()
            self.detector = ResidualAnomalyDetector(
                HISTORY_WINDOW, Z_THRESHOLD, WARMUP_SAMPLES
            )
        self.history: deque = deque(maxlen=HISTORY_WINDOW)   # (ts, observado, predito)

    def ingest(self, byte_count: int, ts: float) -> Optional[Dict[str, Any]]:
        """
        Pré-processamento + predição + detecção em um passo.
        Retorna dict de anomalia (ou None).
        """
        # --- Pré-processamento: delta de contadores, tolerante a reset/rollover ---
        if self.last_bytes is None:
            self.last_bytes, self.last_ts = byte_count, ts
            return None

        dt = ts - self.last_ts
        if dt <= 0:
            return None
        delta = byte_count - self.last_bytes
        if delta < 0:                             # contador resetou (flow reinstalado, switch reiniciado)
            delta = byte_count
        self.last_bytes, self.last_ts = byte_count, ts

        self.rate_bps = (delta * 8.0) / dt

        # Taxa zero não é uma queda anômala. Uma única amostra vazia pode ser
        # apenas a fronteira entre duas rajadas observada em fases diferentes
        # por coletores multi-domínio; preserva-se o nível durante uma pequena
        # graça e só então o fluxo é considerado encerrado.
        if (self.detection_mode == "offline"
                and self.meta.get("type") == "flow"
                and self.rate_bps <= 0.0):
            self.idle_samples += 1
            if self.idle_samples >= FLOW_IDLE_RESET_SAMPLES:
                self.predictor.level = None
                self.predictor.trend = 0.0
                self.predictor.n = 0
            self.predicted_bps = 0.0
            self.model_residual = 0.0
            self.history.append((ts, self.rate_bps, self.predicted_bps))
            if _exporter:
                _exporter.record(
                    self.key, self.meta, ts,
                    self.rate_bps, self.predicted_bps, 0.0,
                    None, False,
                )
            return None
        self.idle_samples = 0

        # O threshold e a distribuição dos resíduos continuam inteiramente
        # offline. As observações declaradas pelo artefato apenas alinham o
        # nível Holt da série; intervalos parciais abaixo do piso não podem
        # participar desse alinhamento.
        if (self.detection_mode == "offline"
                and self.predictor.n < self.series_priming_samples
                and self.rate_bps < MIN_RATE_BPS):
            self.predicted_bps = 0.0
            self.model_residual = 0.0
            self.history.append((ts, self.rate_bps, self.predicted_bps))
            if _exporter:
                _exporter.record(
                    self.key, self.meta, ts,
                    self.rate_bps, self.predicted_bps, 0.0,
                    None, False,
                )
            return None

        if (self.detection_mode == "offline"
                and self.predictor.n < self.series_priming_samples):
            self.predicted_bps = self.rate_bps
            self.predictor.update(self.rate_bps)
            self.history.append((ts, self.rate_bps, self.predicted_bps))
            if _exporter:
                _exporter.record(
                    self.key, self.meta, ts,
                    self.rate_bps, self.predicted_bps, 0.0,
                    None, False,
                )
            return None

        # --- Predição feita ANTES do update (predição genuína de 1 passo à frente) ---
        self.predicted_bps = self.predictor.predict(horizon=1)
        residual = self.rate_bps - self.predicted_bps
        self.model_residual = self.predictor.residual(self.rate_bps, self.predicted_bps)
        self.history.append((ts, self.rate_bps, self.predicted_bps))

        # --- Detecção ---
        # Chamada ÚNICA ao score(): a alimentação da janela é idêntica ao fluxo
        # anterior (a decisão de incluir o resíduo é interna ao detector); o que
        # muda é apenas que o z fica disponível para persistência em todo caso.
        detector_ready = self.detector.ready
        z, is_anom_stat = self.detector.score(self.model_residual)

        # Anomalia só é REPORTADA acima do piso de ruído (comportamento original)
        below_floor = max(self.rate_bps, self.predicted_bps) < MIN_RATE_BPS
        is_anomaly = is_anom_stat and not below_floor

        # Ataques detectados não atualizam Holt no modo offline: isso evita que
        # um DDoS prolongado seja absorvido como o novo nível normal. O fallback
        # preserva o comportamento adaptativo anterior.
        if self.detection_mode == "adaptive" or not is_anomaly:
            self.predictor.update(self.rate_bps)

        # --- Persistência (aditiva; sincronizada com o history.append acima) ---
        if _exporter:
            _exporter.record(self.key, self.meta, ts,
                             self.rate_bps, self.predicted_bps, residual,
                             (z if detector_ready else None), is_anomaly)

        if not is_anomaly:
            return None

        kind = "THROUGHPUT_SPIKE" if z > 0 else "THROUGHPUT_DROP"
        return {
            "anomaly_id": uuid.uuid4().hex[:12],
            "kind": kind,
            "key": self.key,
            "meta": self.meta,
            "observed_bps": round(self.rate_bps, 1),
            "predicted_bps": round(self.predicted_bps, 1),
            "z_score": round(z, 2),
            "threshold": self.detector.threshold_for(z),
            "spike_z_threshold": self.detector.z_threshold,
            "drop_z_threshold": self.detector.drop_z_threshold,
            "model_residual": round(self.model_residual, 8),
            "detection_mode": self.detection_mode,
            "ts_detect_ns": now_ns(),
            "cid": CONTROLLER_ID,
        }


# =====================================================================
# 4) COLETOR — descobre DPIDs e ingere stats do Ryu periodicamente
# =====================================================================
class Collector:
    def __init__(self, engine: "PredictorEngine"):
        self.engine = engine
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.flow_count_hist: Dict[int, deque] = {}   # por dpid: nº de fluxos distintos

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()

    # -------- HTTP helpers --------
    def _get(self, path: str) -> Optional[Any]:
        try:
            r = requests.get(f"{RYU_BASE_URL}{path}", timeout=REQUEST_TIMEOUT_S)
            if r.status_code == 200:
                return r.json()
            logger.warning(f"GET {path} -> {r.status_code}")
        except requests.exceptions.RequestException as e:
            logger.warning(f"GET {path} falhou: {e}")
        return None

    # -------- Loop principal --------
    def _loop(self):
        logger.info(f"Coletor iniciado (intervalo={POLL_INTERVAL_S}s, Ryu={RYU_BASE_URL})")
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._collect_once()
            except Exception as e:
                logger.error(f"Erro no ciclo de coleta: {e}")
            elapsed = time.time() - t0
            _metric("COLLECT_CYCLE", f"cid={CONTROLLER_ID} elapsed_ms={elapsed*1000:.1f} "
                                     f"series={len(self.engine.series)} ts_ns={now_ns()}")
            self._stop.wait(max(0.1, POLL_INTERVAL_S - elapsed))

    def _collect_once(self):
        dpids = self._get("/stats/switches") or []
        ts = time.time()

        for dpid in dpids:
            # ---- Portas (visão agregada por enlace) ----
            pstats = self._get(f"/stats/port/{dpid}") or {}
            for p in pstats.get(str(dpid), []):
                port_no = p.get("port_no")
                if port_no in (None, "LOCAL", 65534):
                    continue
                key = f"port:{dpid}:{port_no}"
                meta = {"type": "port", "dpid": dpid, "port_no": port_no}
                total = int(p.get("rx_bytes", 0)) + int(p.get("tx_bytes", 0))
                self.engine.ingest(key, meta, total, ts)

            # ---- Fluxos IPv4 (visão fina src->dst; base da mitigação) ----
            fstats = self._get(f"/stats/flow/{dpid}") or {}
            flows = fstats.get(str(dpid), [])
            blocked_pairs = blocked_flow_pairs(flows)
            for f in flows:
                m = f.get("match", {})
                nw_src, nw_dst = m.get("nw_src"), m.get("nw_dst")
                if not nw_src or not nw_dst or (nw_src, nw_dst) in blocked_pairs:
                    continue
                key = f"flow:{dpid}:{nw_src}->{nw_dst}"
                meta = {"type": "flow", "dpid": dpid, "nw_src": nw_src, "nw_dst": nw_dst}
                self.engine.ingest(key, meta, int(f.get("byte_count", 0)), ts)

            # ---- Heurística de surto de fluxos (indício de scan/DDoS) ----
            forwarding_rules = sum(1 for flow in flows if flow.get("actions") != [])
            self._check_flow_surge(dpid, forwarding_rules)

    def _check_flow_surge(self, dpid: int, n_flows: int):
        hist = self.flow_count_hist.setdefault(dpid, deque(maxlen=HISTORY_WINDOW))
        if len(hist) >= FLOW_SURGE_WARMUP:
            baseline = sorted(hist)[len(hist) // 2]
            if baseline >= 1 and n_flows > max(baseline * 3, baseline + 20):
                self.engine.register_anomaly({
                    "anomaly_id": uuid.uuid4().hex[:12],
                    "kind": "NEW_FLOW_SURGE",
                    "key": f"dpid:{dpid}",
                    "meta": {"type": "dpid", "dpid": dpid},
                    "observed_flows": n_flows,
                    "baseline_flows": baseline,
                    "ts_detect_ns": now_ns(),
                    "cid": CONTROLLER_ID,
                }, mitigable=False)  # surto exige investigação; sem bloqueio cego
        hist.append(n_flows)


# =====================================================================
# 5) MITIGADOR — aciona o FlowBlocker de forma autônoma e segura
# =====================================================================
class Mitigator:
    """
    Guard-rails de segurança antes de qualquer bloqueio autônomo:
      1. AUTO_MITIGATE precisa estar habilitado;
      2. só mitiga anomalias do tipo THROUGHPUT_SPIKE em séries de FLUXO
         (existe um par src/dst inequívoco — nunca bloqueia porta inteira);
      3. respeita whitelist (infra, gateways, DNS...);
      4. cooldown por par (src,dst) — evita tempestade de POSTs;
      5. DRY_RUN permite validar o comportamento sem impacto real.
    """

    def __init__(self):
        self.last_action: Dict[str, float] = {}
        self.lock = threading.Lock()

    def maybe_mitigate(self, anomaly: Dict[str, Any]) -> Dict[str, Any]:
        result = {"attempted": False, "executed": False, "reason": ""}

        if not AUTO_MITIGATE:
            result["reason"] = "AUTO_MITIGATE desabilitado"
            return result
        if anomaly.get("kind") != "THROUGHPUT_SPIKE":
            result["reason"] = f"kind={anomaly.get('kind')} não mitigável automaticamente"
            return result

        meta = anomaly.get("meta", {})
        if meta.get("type") != "flow":
            result["reason"] = "anomalia em porta/agregado: sem par src/dst para bloquear"
            return result

        src_ip, dst_ip = meta.get("nw_src"), meta.get("nw_dst")
        if not src_ip or not dst_ip:
            result["reason"] = "src/dst ausentes"
            return result
        if src_ip in WHITELIST_IPS or dst_ip in WHITELIST_IPS:
            result["reason"] = "IP em whitelist"
            return result

        pair = f"{src_ip}->{dst_ip}"
        with self.lock:
            last = self.last_action.get(pair, 0.0)
            if time.time() - last < COOLDOWN_S:
                result["reason"] = f"cooldown ativo ({COOLDOWN_S}s) para {pair}"
                return result
            self.last_action[pair] = time.time()

        result["attempted"] = True
        payload = {"src_ip": src_ip, "dst_ip": dst_ip,
                   "policy_id": f"auto-{anomaly['anomaly_id']}"}
        url = f"{FLOWBLOCKER_URL}/flowblocker/service"

        if DRY_RUN:
            _metric("MITIGATION_DRYRUN", f"anomaly={anomaly['anomaly_id']} "
                                         f"would_block={pair} url={url} ts_ns={now_ns()}")
            result["executed"] = False
            result["reason"] = "DRY_RUN"
            return result

        try:
            ts_send = now_ns()
            r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT_S)
            _metric("MITIGATION_APPLY", f"anomaly={anomaly['anomaly_id']} pair={pair} "
                                        f"status={r.status_code} ts_send_ns={ts_send} "
                                        f"latency_ms={(now_ns()-ts_send)/1e6:.1f}")
            result["executed"] = (r.status_code == 200)
            result["reason"] = f"FlowBlocker HTTP {r.status_code}"
            result["flowblocker_response"] = (r.json() if r.status_code == 200 else r.text)
        except requests.exceptions.RequestException as e:
            logger.error(f"Falha ao acionar FlowBlocker: {e}")
            result["reason"] = f"exceção: {e}"
        return result


class CollaborativeDecisionManager:
    """Publica evidências compactas e coordena uma decisão MCDA via ETCD.

    O ETCD transporta somente candidatos anômalos, com TTL. A telemetria bruta
    continua local. Uma transação por fluxo funciona como *claim* distribuído:
    somente seu vencedor pode chamar o FlowBlocker.
    """

    def __init__(self, engine: Any):
        self.engine = engine
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.local_candidates: Dict[str, Dict[str, Any]] = {}
        self.dirty_flows = set()
        self.decisions: Dict[str, Dict[str, Any]] = {}
        self.claims: Dict[str, Dict[str, Any]] = {}
        self.mitigation_results: Dict[str, Dict[str, Any]] = {}
        self.claim_leases: Dict[str, Any] = {}
        self.last_logged_state: Dict[str, str] = {}
        self.errors = 0
        self.evidence_published = 0
        self.claims_won = 0
        self.claims_lost = 0
        self.started_ns = now_ns()
        self.thread = threading.Thread(
            target=self._run,
            name=f"collaborative-mcda-{CONTROLLER_ID}",
            daemon=True,
        )
        self.thread.start()

    def _model_identity(self) -> Tuple[str, float]:
        model = self.engine.offline_model
        if model is None:
            return "adaptive", 0.5
        # O mesmo dataset pode originar contratos e calibrações diferentes.
        # A colaboração só combina evidências com todos os parâmetros de
        # inferência idênticos, sem depender do timestamp de geração.
        identity = {
            "dataset_sha256": model.training.get("dataset_sha256"),
            "schema_version": model.schema_version,
            "model_type": model.model_type,
            "alpha": model.alpha,
            "beta": model.beta,
            "transform": model.transform,
            "residual_center": model.residual_center,
            "residual_scale": model.residual_scale,
            "spike_z_threshold": model.spike_z_threshold,
            "drop_z_threshold": model.effective_drop_z_threshold,
            "series_priming_samples": model.series_priming_samples,
            "flow_idle_reset_samples": FLOW_IDLE_RESET_SAMPLES,
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        model_id = f"{model.model_type}:{digest}"
        metrics = model.training.get("metrics", {})
        try:
            reliability = float(metrics.get("precision", 0.5))
        except (AttributeError, TypeError, ValueError):
            reliability = 0.5
        return str(model_id), clip01(reliability)

    @staticmethod
    def _flow_hash(flow: str) -> str:
        return hashlib.sha256(flow.encode("utf-8")).hexdigest()[:24]

    def submit(self, anomaly: Dict[str, Any]) -> bool:
        """Agrega DPIDs locais sem contar o mesmo pacote várias vezes."""
        flow = canonical_flow_key(anomaly)
        if flow is None:
            return False
        event_ts = int(anomaly["ts_detect_ns"])
        window_ns = max(1, int(COLLAB_WINDOW_S * 1e9))
        window_id = event_ts // window_ns
        model_id, reliability = self._model_identity()
        dpid = anomaly.get("meta", {}).get("dpid")

        with self.lock:
            previous = self.local_candidates.get(flow)
            if previous is not None and window_id < previous["window_id"]:
                return False
            if previous is None or window_id > previous["window_id"]:
                consecutive = (previous is not None
                               and window_id == previous["window_id"] + 1)
                persistence = (previous["evidence"]["persistence_windows"] + 1
                               if consecutive else 1)
                evidence = {
                    "schema_version": 1,
                    "cid": CONTROLLER_ID,
                    "flow": flow,
                    "src_ip": anomaly["meta"]["nw_src"],
                    "dst_ip": anomaly["meta"]["nw_dst"],
                    "window_id": window_id,
                    "ts_ns": event_ts,
                    "observed_bps": float(anomaly.get("observed_bps", 0.0)),
                    "predicted_bps": float(anomaly.get("predicted_bps", 0.0)),
                    "z_score": float(anomaly.get("z_score", 0.0)),
                    "threshold": float(anomaly.get("threshold", Z_THRESHOLD)),
                    "persistence_windows": persistence,
                    "flow_specificity": 1.0,
                    "dpids": ([] if dpid is None else [dpid]),
                    "model_id": model_id,
                    "model_reliability": reliability,
                    "detection_mode": self.engine.detection_mode,
                }
                self.local_candidates[flow] = {
                    "window_id": window_id,
                    "evidence": evidence,
                }
            else:
                evidence = previous["evidence"]
                evidence["ts_ns"] = max(event_ts, int(evidence["ts_ns"]))
                if dpid is not None and dpid not in evidence["dpids"]:
                    evidence["dpids"].append(dpid)
                    evidence["dpids"].sort()
                # Mantém a visão local mais forte; não soma taxas observadas em
                # switches diferentes, pois eles podem ver os mesmos pacotes.
                if float(anomaly.get("z_score", 0.0)) > float(evidence["z_score"]):
                    evidence["z_score"] = float(anomaly.get("z_score", 0.0))
                    evidence["threshold"] = float(anomaly.get("threshold", Z_THRESHOLD))
                    evidence["observed_bps"] = float(anomaly.get("observed_bps", 0.0))
                    evidence["predicted_bps"] = float(anomaly.get("predicted_bps", 0.0))
            self.dirty_flows.add(flow)
        self.wake.set()
        return True

    def _run(self):
        while True:
            self.wake.wait(COLLAB_EVALUATION_INTERVAL_S)
            self.wake.clear()
            try:
                self._flush_evidence()
                self._evaluate_candidates()
            except Exception as exc:  # thread deve sobreviver a falhas transitórias
                self.errors += 1
                logger.error("Falha no coordenador colaborativo: %s", exc)

    def _flush_evidence(self):
        with self.lock:
            flows = list(self.dirty_flows)
            self.dirty_flows.clear()
            rows = []
            for flow in flows:
                if flow not in self.local_candidates:
                    continue
                evidence = dict(self.local_candidates[flow]["evidence"])
                evidence["dpids"] = list(evidence["dpids"])
                rows.append((flow, evidence))
        for index, (flow, evidence) in enumerate(rows):
            try:
                lease = _etcd.lease(max(1, int(math.ceil(COLLAB_EVIDENCE_TTL_S))))
                key = (f"flowpredictor/evidence/{self._flow_hash(flow)}/"
                       f"{evidence['window_id']}/{CONTROLLER_ID}")
                _etcd.put(key, json.dumps(evidence, sort_keys=True), lease=lease)
                self.evidence_published += 1
                _metric("COLLAB_EVIDENCE", f"cid={CONTROLLER_ID} flow={flow} "
                                             f"window={evidence['window_id']} "
                                             f"z={evidence['z_score']:.2f}")
            except Exception:
                with self.lock:
                    self.dirty_flows.update(row[0] for row in rows[index:])
                raise

    def _read_evidence(self, flow: str) -> List[Dict[str, Any]]:
        prefix = f"flowpredictor/evidence/{self._flow_hash(flow)}/"
        rows = []
        for raw, _metadata in _etcd.get_prefix(prefix):
            try:
                item = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
                if item.get("flow") == flow:
                    rows.append(item)
            except (AttributeError, TypeError, ValueError):
                logger.warning("Evidência colaborativa inválida sob %s", prefix)
        return rows

    def _evaluate_candidates(self):
        cutoff_ns = now_ns() - int(COLLAB_EVIDENCE_TTL_S * 1e9)
        with self.lock:
            stale = [flow for flow, row in self.local_candidates.items()
                     if int(row["evidence"]["ts_ns"]) < cutoff_ns]
            for flow in stale:
                self.local_candidates.pop(flow, None)
                self.dirty_flows.discard(flow)
            flows = list(self.local_candidates)

        for flow in flows:
            evaluated_ns = now_ns()
            decision = score_collaborative_evidence(
                self._read_evidence(flow),
                now_ns_value=evaluated_ns,
                expected_domains=COLLAB_EXPECTED_DOMAINS,
                min_domains=COLLAB_MIN_DOMAINS,
                weights=COLLAB_WEIGHTS,
                freshness_s=COLLAB_EVIDENCE_TTL_S,
                persistence_windows=COLLAB_PERSISTENCE_WINDOWS,
                rate_ratio_max=COLLAB_RATE_RATIO_MAX,
                suspect_threshold=COLLAB_SUSPECT_THRESHOLD,
                alert_threshold=COLLAB_ALERT_THRESHOLD,
                decision_threshold=COLLAB_DECISION_THRESHOLD,
            )
            decision.update({"flow": flow, "evaluated_ns": evaluated_ns})
            if decision["decision"] == "MITIGATE":
                claim = self._claim_mitigation(flow, decision)
                decision["claim"] = claim
                if claim.get("won"):
                    previous_action = self.mitigation_results.get(flow)
                    if (previous_action is None
                            or previous_action.get("claimed_ns") != claim.get("claimed_ns")):
                        result = self.engine.mitigate_collaborative(flow, decision)
                        previous_action = {
                            "claimed_ns": claim.get("claimed_ns"),
                            "result": result,
                        }
                        self.mitigation_results[flow] = previous_action
                    decision["mitigation"] = previous_action["result"]
                else:
                    decision["mitigation"] = {
                        "attempted": False,
                        "executed": False,
                        "reason": ("decisão executada pelo coordenador "
                                   f"{claim.get('coordinator', 'desconhecido')}"),
                    }

            with self.lock:
                previous_state = self.last_logged_state.get(flow)
                self.decisions[flow] = decision
                if previous_state != decision["decision"]:
                    self.last_logged_state[flow] = decision["decision"]
                    _metric("COLLAB_DECISION", f"flow={flow} decision={decision['decision']} "
                                                f"score={decision['score']:.3f} "
                                                f"domains={decision['confirming_domains']}")
            self.engine.apply_collaborative_decision(flow, decision)

    def _claim_mitigation(self, flow: str, decision: Dict[str, Any]) -> Dict[str, Any]:
        current = self.claims.get(flow)
        current_ns = now_ns()
        if current is not None and current_ns < int(current.get("expires_ns", 0)):
            return dict(current)

        key = f"flowpredictor/mitigation-claim/{self._flow_hash(flow)}"
        ttl = max(1, int(math.ceil(COLLAB_CLAIM_TTL_S)))
        payload = {
            "flow": flow,
            "coordinator": CONTROLLER_ID,
            "claimed_ns": current_ns,
            "score": decision["score"],
            "confirming_domains": decision["confirming_domains"],
        }
        try:
            lease = _etcd.lease(ttl)
            won, _responses = _etcd.transaction(
                compare=[_etcd.transactions.version(key) == 0],
                success=[_etcd.transactions.put(
                    key, json.dumps(payload, sort_keys=True), lease.id
                )],
                failure=[],
            )
            if won:
                self.claim_leases[flow] = lease
                claim = {
                    "won": True,
                    "coordinator": CONTROLLER_ID,
                    "claimed_ns": current_ns,
                    "key": key,
                    "expires_ns": current_ns + ttl * 1_000_000_000,
                    "degraded": False,
                }
                self.claims_won += 1
            else:
                raw, _metadata = _etcd.get(key)
                owner = json.loads(raw.decode("utf-8")) if raw else {}
                claimed_ns = int(owner.get("claimed_ns", current_ns))
                claim = {
                    "won": False,
                    "coordinator": owner.get("coordinator", "unknown"),
                    "claimed_ns": claimed_ns,
                    "key": key,
                    "expires_ns": claimed_ns + ttl * 1_000_000_000,
                    "degraded": False,
                }
                self.claims_lost += 1
        except Exception as exc:
            # Se a transação falhar mas a leitura das evidências funcionou, uma
            # eleição determinística preserva o modo degradado sem ação dupla.
            coordinator = min(decision["confirming_domains"])
            claim = {
                "won": coordinator == CONTROLLER_ID,
                "coordinator": coordinator,
                "claimed_ns": current_ns,
                "key": key,
                "expires_ns": current_ns + ttl * 1_000_000_000,
                "degraded": True,
                "error": str(exc),
            }
            logger.error("Claim ETCD falhou para %s; coordenador determinístico=%s: %s",
                         flow, coordinator, exc)

        self.claims[flow] = claim
        _metric("COLLAB_CLAIM", f"flow={flow} coordinator={claim['coordinator']} "
                                 f"won={claim['won']} degraded={claim['degraded']}")
        return dict(claim)

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            decisions = sorted(
                self.decisions.values(),
                key=lambda row: int(row.get("evaluated_ns", 0)),
                reverse=True,
            )[:50]
            return {
                "requested": COLLABORATION_ENABLED,
                "active": True,
                "cid": CONTROLLER_ID,
                "uptime_s": round((now_ns() - self.started_ns) / 1e9, 1),
                "local_candidates": len(self.local_candidates),
                "evidence_published": self.evidence_published,
                "claims_won": self.claims_won,
                "claims_lost": self.claims_lost,
                "errors": self.errors,
                "config": {
                    "expected_domains": COLLAB_EXPECTED_DOMAINS,
                    "min_domains": COLLAB_MIN_DOMAINS,
                    "window_s": COLLAB_WINDOW_S,
                    "evidence_ttl_s": COLLAB_EVIDENCE_TTL_S,
                    "claim_ttl_s": COLLAB_CLAIM_TTL_S,
                    "persistence_windows": COLLAB_PERSISTENCE_WINDOWS,
                    "suspect_threshold": COLLAB_SUSPECT_THRESHOLD,
                    "alert_threshold": COLLAB_ALERT_THRESHOLD,
                    "decision_threshold": COLLAB_DECISION_THRESHOLD,
                    "weights": COLLAB_WEIGHTS,
                },
                "decisions": decisions,
            }


# =====================================================================
# 6) MOTOR — orquestra séries, anomalias, mitigação, feedback e ETCD
# =====================================================================
class PredictorEngine:
    def __init__(self, offline_model: Optional[OfflineModel] = None):
        self.series: Dict[str, SeriesState] = {}
        self.anomalies: deque = deque(maxlen=500)
        self.lock = threading.RLock()
        self.mitigator = Mitigator()
        self.offline_model = offline_model
        self.detection_mode = "offline" if offline_model else "adaptive"
        self.feedback_stats = {"true_positive": 0, "false_positive": 0}
        self.active_anomaly_events: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.anomalies_suppressed = 0
        self.started_ns = now_ns()
        self.collaboration = (CollaborativeDecisionManager(self)
                              if COLLABORATION_ENABLED and _etcd is not None else None)
        if COLLABORATION_ENABLED and self.collaboration is None:
            logger.error("Colaboração solicitada sem ETCD; mantendo decisão local como fallback")

    # ---- ingestão (chamada pelo Collector) ----
    def ingest(self, key: str, meta: Dict[str, Any], byte_count: int, ts: float):
        with self.lock:
            s = self.series.get(key)
            if s is None:
                s = SeriesState(key, meta, self.offline_model)
                self.series[key] = s
            anomaly = s.ingest(byte_count, ts)
        if anomaly:
            self.register_anomaly(anomaly, mitigable=True)

    # ---- registro + resposta autônoma ----
    def register_anomaly(self, anomaly: Dict[str, Any], mitigable: bool) -> bool:
        """Registra um evento novo ou agrega uma repetição ao evento ativo.

        A decisão estatística continua sendo executada em toda amostra. Apenas o
        evento, o log e a mitigação são deduplicados, evitando uma tempestade de
        alertas durante um ataque sustentado.
        """
        collaborative_candidate = (
            mitigable and self.collaboration is not None
            and canonical_flow_key(anomaly) is not None
        )
        event_key = (str(anomaly["kind"]), str(anomaly["key"]))
        event_ts = int(anomaly["ts_detect_ns"])
        with self.lock:
            previous = self.active_anomaly_events.get(event_key)
            event_age_ns = (None if previous is None else
                            event_ts - int(previous["last_seen_ns"]))
            if (previous is not None
                    and 0 <= event_age_ns <= EVENT_COOLDOWN_S * 1e9):
                previous["last_seen_ns"] = event_ts
                previous["suppressed_count"] += 1
                self.anomalies_suppressed += 1

                if "observed_bps" in anomaly:
                    observed = anomaly["observed_bps"]
                    previous["latest_observed_bps"] = observed
                    peak = previous.get("peak_observed_bps", observed)
                    previous["peak_observed_bps"] = (
                        max(peak, observed) if anomaly["kind"] == "THROUGHPUT_SPIKE"
                        else min(peak, observed)
                    )
                if "observed_flows" in anomaly:
                    observed_flows = anomaly["observed_flows"]
                    previous["latest_observed_flows"] = observed_flows
                    previous["peak_observed_flows"] = max(
                        previous.get("peak_observed_flows", observed_flows),
                        observed_flows,
                    )
                if "z_score" in anomaly:
                    z_score = anomaly["z_score"]
                    previous["latest_z_score"] = z_score
                    if abs(z_score) > abs(previous.get("peak_z_score", z_score)):
                        previous["peak_z_score"] = z_score

                suppressed_count = previous["suppressed_count"]
                original_id = previous["anomaly_id"]
            else:
                anomaly["first_seen_ns"] = event_ts
                anomaly["last_seen_ns"] = event_ts
                anomaly["suppressed_count"] = 0
                if "observed_bps" in anomaly:
                    anomaly["peak_observed_bps"] = anomaly["observed_bps"]
                if "observed_flows" in anomaly:
                    anomaly["peak_observed_flows"] = anomaly["observed_flows"]
                if "z_score" in anomaly:
                    anomaly["peak_z_score"] = anomaly["z_score"]
                self.active_anomaly_events[event_key] = anomaly
                previous = None

        if previous is not None:
            if collaborative_candidate:
                self.collaboration.submit(anomaly)
            # Confirma a primeira agregação e depois em lotes de dez; o contador
            # e o evento da API são atualizados em toda amostra sem poluir logs/ETCD.
            if suppressed_count == 1 or suppressed_count % 10 == 0:
                _metric("ANOMALY_SUPPRESS", f"kind={event_key[0]} key={event_key[1]} "
                                            f"original_id={original_id} "
                                            f"count={suppressed_count} ts_ns={event_ts}")
                self._publish_etcd()
            return False

        _metric("ANOMALY_DETECT", f"id={anomaly['anomaly_id']} kind={anomaly['kind']} "
                                  f"key={anomaly['key']} ts_ns={anomaly['ts_detect_ns']}")
        if collaborative_candidate:
            anomaly["mitigation"] = {
                "attempted": False,
                "executed": False,
                "reason": "aguardando decisão colaborativa",
            }
        else:
            anomaly["mitigation"] = (
                self.mitigator.maybe_mitigate(anomaly)
                if mitigable else {"attempted": False, "reason": "não mitigável"}
            )
        with self.lock:
            self.anomalies.appendleft(anomaly)
        if collaborative_candidate:
            self.collaboration.submit(anomaly)
        self._publish_etcd()
        return True

    def mitigate_collaborative(self, flow: str,
                               decision: Dict[str, Any]) -> Dict[str, Any]:
        """Executa a ação somente depois de este domínio vencer o claim global."""
        with self.lock:
            target = next((item for item in self.anomalies
                           if canonical_flow_key(item) == flow), None)
        if target is None:
            return {
                "attempted": False,
                "executed": False,
                "reason": "evento local não encontrado para o fluxo colaborativo",
            }
        result = self.mitigator.maybe_mitigate(target)
        with self.lock:
            for item in self.anomalies:
                if canonical_flow_key(item) == flow:
                    item["mitigation"] = result
        _metric("COLLAB_MITIGATION", f"flow={flow} score={decision['score']:.3f} "
                                      f"attempted={result.get('attempted')} "
                                      f"executed={result.get('executed')}")
        return result

    def apply_collaborative_decision(self, flow: str,
                                     decision: Dict[str, Any]) -> None:
        """Anexa a justificativa MCDA aos eventos locais correspondentes."""
        with self.lock:
            for item in self.anomalies:
                if canonical_flow_key(item) == flow:
                    item["collaboration"] = decision
                    if "mitigation" in decision:
                        item["mitigation"] = decision["mitigation"]

    def collaboration_snapshot(self) -> Dict[str, Any]:
        if self.collaboration is not None:
            return self.collaboration.snapshot()
        return {
            "requested": COLLABORATION_ENABLED,
            "active": False,
            "reason": ("desativada por configuração" if not COLLABORATION_ENABLED
                       else "ETCD indisponível; decisão local ativa"),
            "cid": CONTROLLER_ID,
            "decisions": [],
        }

    # ---- feedback loop: ajusta a sensibilidade da série afetada ----
    def apply_feedback(self, anomaly_id: str, verdict: str) -> Dict[str, Any]:
        """
        false_positive -> aumenta o threshold do lado afetado (menos sensível)
        true_positive  -> reduz levemente o threshold da série (mais sensível)
        Ajuste multiplicativo com limites próprios do modo de detecção.
        """
        with self.lock:
            target = next((a for a in self.anomalies if a["anomaly_id"] == anomaly_id), None)
            if target is None:
                return {"ok": False, "error": "anomaly_id não encontrado"}
            self.feedback_stats[verdict] = self.feedback_stats.get(verdict, 0) + 1

            factor = 1.25 if verdict == "false_positive" else 0.95
            key = target["key"]
            s = self.series.get(key)
            if s:
                lower, upper = ((1.0, 20.0) if s.detection_mode == "offline"
                                else (2.5, 10.0))
                threshold_attr = ("drop_z_threshold"
                                  if target.get("kind") == "THROUGHPUT_DROP"
                                  else "z_threshold")
                current = getattr(s.detector, threshold_attr)
                new_thr = min(upper, max(lower, current * factor))
                setattr(s.detector, threshold_attr, new_thr)
            else:
                new_thr = None
                threshold_attr = None
            target["feedback"] = verdict

        _metric("FEEDBACK", f"anomaly={anomaly_id} verdict={verdict} "
                            f"threshold_kind={threshold_attr} new_threshold={new_thr} "
                            f"ts_ns={now_ns()}")
        return {"ok": True, "anomaly_id": anomaly_id, "verdict": verdict,
                "threshold_kind": threshold_attr, "new_threshold": new_thr}

    # ---- snapshots para API / ETCD ----
    def snapshot_predictions(self, top: int = 50) -> List[Dict[str, Any]]:
        with self.lock:
            rows = [{
                "key": s.key, "meta": s.meta,
                "observed_bps": round(s.rate_bps, 1),
                "predicted_next_bps": round(s.predictor.predict(1), 1),
                "predicted_5step_bps": round(s.predictor.predict(5), 1),
                "trend_bps": round(s.predictor.trend_bps, 1),
                "samples": s.predictor.n,
                "idle_samples": s.idle_samples,
                "z_threshold": s.detector.z_threshold,
                "spike_z_threshold": s.detector.z_threshold,
                "drop_z_threshold": s.detector.drop_z_threshold,
                "detector_ready": s.detector.ready,
                "detection_mode": s.detection_mode,
            } for s in self.series.values()]
        rows.sort(key=lambda r: r["observed_bps"], reverse=True)
        return rows[:top]

    def _publish_etcd(self):
        if not _etcd:
            return
        try:
            state = {
                "cid": CONTROLLER_ID,
                "ts_ns": now_ns(),
                "n_series": len(self.series),
                "detection_mode": self.detection_mode,
                "recent_anomalies": list(self.anomalies)[:20],
                "anomalies_suppressed": self.anomalies_suppressed,
                "feedback_stats": self.feedback_stats,
                "collaboration": {
                    "requested": COLLABORATION_ENABLED,
                    "active": self.collaboration is not None,
                },
            }
            _etcd.put(f"flowpredictor/state/{CONTROLLER_ID}", json.dumps(state, default=str))
            _metric("ETCD_WRITE", f"cid={CONTROLLER_ID} key=flowpredictor/state/{CONTROLLER_ID} "
                                  f"ts_write_ns={now_ns()}")
        except Exception as e:
            logger.error(f"Falha ao publicar estado no ETCD: {e}")


# =====================================================================
# 7) API REST (Flask) — mesmo padrão dos demais serviços
# =====================================================================
app = Flask(__name__)
engine = PredictorEngine(_offline_model)
collector = Collector(engine)


@app.route("/")
def index():
    return f"FlowPredictor Service is running on Controller {CONTROLLER_ID}"


@app.route("/predictor/status", methods=["GET"])
def status():
    collaboration = engine.collaboration_snapshot()
    with engine.lock:
        return jsonify({
            "cid": CONTROLLER_ID,
            "uptime_s": round((now_ns() - engine.started_ns) / 1e9, 1),
            "series_tracked": len(engine.series),
            "anomalies_recorded": len(engine.anomalies),
            "anomalies_suppressed": engine.anomalies_suppressed,
            "feedback_stats": engine.feedback_stats,
            "model": model_status_payload(),
            "collaboration": {
                "requested": collaboration["requested"],
                "active": collaboration["active"],
                "reason": collaboration.get("reason"),
                "local_candidates": collaboration.get("local_candidates", 0),
                "claims_won": collaboration.get("claims_won", 0),
                "claims_lost": collaboration.get("claims_lost", 0),
                "errors": collaboration.get("errors", 0),
            },
            "config": {
                "poll_interval_s": POLL_INTERVAL_S,
                "z_threshold_default": Z_THRESHOLD,
                "spike_z_threshold_default": Z_THRESHOLD,
                "warmup_samples_fallback": WARMUP_SAMPLES,
                "flow_surge_warmup_samples": FLOW_SURGE_WARMUP,
                "min_rate_bps": MIN_RATE_BPS,
                "flow_idle_reset_samples": FLOW_IDLE_RESET_SAMPLES,
                "auto_mitigate": AUTO_MITIGATE,
                "dry_run": DRY_RUN,
                "cooldown_s": COOLDOWN_S,
                "event_cooldown_s": EVENT_COOLDOWN_S,
                "online_model_adaptation": ONLINE_MODEL_ADAPTATION,
                "whitelist": sorted(WHITELIST_IPS),
                "etcd_enabled": _etcd is not None,
                "collaboration_enabled": COLLABORATION_ENABLED,
                "collaboration_active": engine.collaboration is not None,
            },
        }), 200


def model_status_payload() -> Dict[str, Any]:
    if _offline_model:
        payload = _offline_model.status()
        payload["online_adaptation"] = ONLINE_MODEL_ADAPTATION
        return payload
    return {
        "loaded": False,
        "mode": "adaptive",
        "warmup_samples": WARMUP_SAMPLES,
        "load_error": _offline_model_error,
    }


@app.route("/predictor/model", methods=["GET"])
def model_status():
    return jsonify(model_status_payload()), 200


@app.route("/predictor/predictions", methods=["GET"])
def predictions():
    top = int(request.args.get("top", 50))
    return jsonify({"cid": CONTROLLER_ID, "predictions": engine.snapshot_predictions(top)}), 200


@app.route("/predictor/predictions/<path:key>", methods=["GET"])
def prediction_detail(key: str):
    with engine.lock:
        s = engine.series.get(key)
        if not s:
            return jsonify({"error": f"série '{key}' não encontrada"}), 404
        return jsonify({
            "key": s.key, "meta": s.meta,
            "observed_bps": s.rate_bps,
            "detection_mode": s.detection_mode,
            "detector_ready": s.detector.ready,
            "idle_samples": s.idle_samples,
            "model_residual": s.model_residual,
            "forecast": {f"h{h}": round(s.predictor.predict(h), 1) for h in (1, 3, 5, 10)},
            "history": [{"ts": t, "observed": o, "predicted": p} for t, o, p in s.history],
        }), 200


@app.route("/predictor/anomalies", methods=["GET"])
def anomalies():
    limit = int(request.args.get("limit", 50))
    with engine.lock:
        return jsonify({"cid": CONTROLLER_ID,
                        "anomalies": list(engine.anomalies)[:limit]}), 200


@app.route("/predictor/collaboration", methods=["GET"])
def collaboration_status():
    """Estado, critérios e decisões recentes do consenso multi-domínio."""
    return jsonify(engine.collaboration_snapshot()), 200


@app.route("/predictor/export/status", methods=["GET"])
def export_status():
    """Estatísticas da persistência do histórico de predição."""
    if _exporter is None:
        return jsonify({"enabled": False, "directory": EXPORT_DIR,
                        "files": 0, "records_written": 0}), 200
    return jsonify(_exporter.status()), 200


@app.route("/predictor/feedback", methods=["POST"])
def feedback():
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "JSON inválido"}), 400
    anomaly_id = (payload or {}).get("anomaly_id")
    verdict = (payload or {}).get("verdict")
    if not anomaly_id or verdict not in ("true_positive", "false_positive"):
        return jsonify({"error": "requer anomaly_id e verdict "
                                 "('true_positive'|'false_positive')"}), 400
    result = engine.apply_feedback(anomaly_id, verdict)
    return jsonify(result), (200 if result.get("ok") else 404)


@app.route("/predictor/config", methods=["POST"])
def update_config():
    """Ajuste em runtime de parâmetros seguros (sem restart)."""
    global AUTO_MITIGATE, DRY_RUN, MIN_RATE_BPS, COOLDOWN_S, EVENT_COOLDOWN_S
    payload = request.get_json(force=True, silent=True) or {}
    try:
        requested_event_cooldown = (float(payload["event_cooldown_s"])
                                    if "event_cooldown_s" in payload else None)
    except (TypeError, ValueError):
        return jsonify({"error": "event_cooldown_s deve ser numérico"}), 400
    if (requested_event_cooldown is not None
            and (not math.isfinite(requested_event_cooldown)
                 or requested_event_cooldown < 0.0)):
        return jsonify({"error": "event_cooldown_s deve ser não negativo e finito"}), 400
    changed = {}
    if "auto_mitigate" in payload:
        AUTO_MITIGATE = bool(payload["auto_mitigate"]); changed["auto_mitigate"] = AUTO_MITIGATE
    if "dry_run" in payload:
        DRY_RUN = bool(payload["dry_run"]); changed["dry_run"] = DRY_RUN
    if "min_rate_bps" in payload:
        MIN_RATE_BPS = float(payload["min_rate_bps"]); changed["min_rate_bps"] = MIN_RATE_BPS
    if "cooldown_s" in payload:
        COOLDOWN_S = float(payload["cooldown_s"]); changed["cooldown_s"] = COOLDOWN_S
    if requested_event_cooldown is not None:
        EVENT_COOLDOWN_S = requested_event_cooldown
        changed["event_cooldown_s"] = EVENT_COOLDOWN_S
    _metric("CONFIG_UPDATE", f"cid={CONTROLLER_ID} changed={changed} ts_ns={now_ns()}")
    return jsonify({"ok": True, "changed": changed}), 200


# ---------------- Main ----------------
if __name__ == "__main__":
    logger.info(f"FlowPredictor iniciando (cid={CONTROLLER_ID}, Ryu={RYU_BASE_URL}, "
                f"FlowBlocker={FLOWBLOCKER_URL}, detection_mode={engine.detection_mode}, "
                f"auto_mitigate={AUTO_MITIGATE}, dry_run={DRY_RUN}, "
                f"collaboration_active={engine.collaboration is not None})")
    collector.start()
    app.run(host="0.0.0.0", port=PORT, debug=False)
