#!/usr/bin/env python3
"""Contrato serializável das propostas trocadas pelos agentes de domínio.

O protocolo é propositalmente pequeno e determinístico. Mensagens livres ou
respostas de um LLM nunca chegam ao caminho de mitigação: qualquer proposta
precisa satisfazer este contrato antes de participar de uma negociação.
"""

import hashlib
import math
from typing import Any, Dict, Iterable, List, Optional


AGENT_SCHEMA_VERSION = 1
AGENT_ROLES = {"SOURCE", "DESTINATION", "LOCAL", "OBSERVER", "UNKNOWN"}
AGENT_PROPOSALS = {"MITIGATE", "WAIT", "NORMAL", "ABSTAIN", "VETO"}
AGENT_CRITERIA = {
    "severity", "rate_ratio", "persistence", "model_reliability", "topology",
}
AGENT_BELIEFS = {
    "DDOS_LIKELY",
    "ANOMALY_UNCERTAIN",
    "NORMAL_TRAFFIC",
    "NOT_RESPONSIBLE",
    "POLICY_CONFLICT",
    "TOPOLOGY_UNKNOWN",
}
AGENT_PROPOSAL_FIELDS = {
    "schema_version", "agent_id", "cid", "flow", "src_ip", "dst_ip",
    "source_cid", "destination_cid", "window_id", "model_id", "role",
    "belief", "proposal", "confidence", "criteria", "observed_bps",
    "predicted_bps", "z_score", "threshold", "persistence_windows",
    "relevant_domains", "observation_ns", "created_ns", "expires_ns",
    "reason",
}
BELIEFS_BY_PROPOSAL = {
    "MITIGATE": {"DDOS_LIKELY"},
    "WAIT": {"ANOMALY_UNCERTAIN", "TOPOLOGY_UNKNOWN"},
    "NORMAL": {"NORMAL_TRAFFIC"},
    "ABSTAIN": {"NOT_RESPONSIBLE"},
    "VETO": {"POLICY_CONFLICT"},
}


def agent_flow_hash(flow: str) -> str:
    """Retorna a identidade curta usada nas chaves ETCD."""
    return hashlib.sha256(flow.encode("utf-8")).hexdigest()[:24]


def _finite_number(value: Any, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} deve ser numérico") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} deve ser finito")
    return parsed


def validate_agent_proposal(payload: Dict[str, Any], *,
                            now_ns_value: Optional[int] = None) -> Dict[str, Any]:
    """Valida e normaliza uma proposta recebida de outro domínio.

    Uma cópia canônica é devolvida para impedir que referências mutáveis do
    chamador alterem a mensagem depois da validação.
    """
    if not isinstance(payload, dict):
        raise ValueError("proposta deve ser um objeto JSON")
    if payload.get("schema_version") != AGENT_SCHEMA_VERSION:
        raise ValueError("schema_version de proposta não suportado")
    unknown_fields = set(payload) - AGENT_PROPOSAL_FIELDS
    if unknown_fields:
        raise ValueError(f"campos de proposta desconhecidos: {sorted(unknown_fields)}")
    missing_fields = AGENT_PROPOSAL_FIELDS - set(payload)
    if missing_fields:
        raise ValueError(f"campos de proposta ausentes: {sorted(missing_fields)}")

    required_text = ("agent_id", "cid", "flow", "model_id", "role",
                     "belief", "proposal")
    text = {}
    for name in required_text:
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} deve ser texto não vazio")
        text[name] = value.strip()

    if "->" not in text["flow"]:
        raise ValueError("flow deve usar o formato src->dst")
    src_ip = str(payload.get("src_ip", "")).strip()
    dst_ip = str(payload.get("dst_ip", "")).strip()
    expected_src, expected_dst = text["flow"].split("->", 1)
    if not src_ip or not dst_ip or (src_ip, dst_ip) != (expected_src, expected_dst):
        raise ValueError("src_ip/dst_ip devem corresponder ao fluxo canônico")
    if text["agent_id"] != f"sdn-domain:{text['cid']}":
        raise ValueError("agent_id não corresponde ao cid")
    if text["role"] not in AGENT_ROLES:
        raise ValueError("role inválido")
    if text["belief"] not in AGENT_BELIEFS:
        raise ValueError("belief inválido")
    if text["proposal"] not in AGENT_PROPOSALS:
        raise ValueError("proposal inválido")
    if text["belief"] not in BELIEFS_BY_PROPOSAL[text["proposal"]]:
        raise ValueError("belief não corresponde à proposta")

    source_cid = payload.get("source_cid", "")
    destination_cid = payload.get("destination_cid", "")
    if not isinstance(source_cid, str) or not isinstance(destination_cid, str):
        raise ValueError("source_cid/destination_cid devem ser texto")
    source_cid = source_cid.strip()
    destination_cid = destination_cid.strip()

    try:
        window_id = int(payload.get("window_id"))
        observation_ns = int(payload.get("observation_ns"))
        created_ns = int(payload.get("created_ns"))
        expires_ns = int(payload.get("expires_ns"))
        persistence = int(payload.get("persistence_windows"))
    except (TypeError, ValueError) as exc:
        raise ValueError("timestamps, janela e persistência devem ser inteiros") from exc
    if window_id < 0 or observation_ns <= 0 or created_ns <= 0:
        raise ValueError("timestamps e window_id devem ser positivos")
    if created_ns < observation_ns:
        raise ValueError("created_ns não pode preceder observation_ns")
    if expires_ns <= created_ns:
        raise ValueError("expires_ns deve ser posterior a created_ns")
    if persistence < 1:
        raise ValueError("persistence_windows deve ser positivo")
    if now_ns_value is not None and expires_ns <= int(now_ns_value):
        raise ValueError("proposta expirada")

    confidence = _finite_number(payload.get("confidence"), "confidence")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence deve estar em [0, 1]")
    observed = _finite_number(payload.get("observed_bps"), "observed_bps")
    predicted = _finite_number(payload.get("predicted_bps"), "predicted_bps")
    z_score = _finite_number(payload.get("z_score"), "z_score")
    threshold = _finite_number(payload.get("threshold"), "threshold")
    if observed < 0.0 or predicted < 0.0 or threshold <= 0.0:
        raise ValueError("taxas devem ser não negativas e threshold deve ser positivo")
    if text["proposal"] == "MITIGATE" and z_score < threshold:
        raise ValueError("MITIGATE exige confirmação do limiar Holt")

    criteria_raw = payload.get("criteria")
    if not isinstance(criteria_raw, dict) or not criteria_raw:
        raise ValueError("criteria deve ser um objeto não vazio")
    if set(criteria_raw) != AGENT_CRITERIA:
        raise ValueError("conjunto de critérios agentic inválido")
    criteria = {}
    for name, value in criteria_raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError("nome de critério inválido")
        parsed = _finite_number(value, f"criteria.{name}")
        if not 0.0 <= parsed <= 1.0:
            raise ValueError(f"criteria.{name} deve estar em [0, 1]")
        criteria[name] = parsed

    relevant_raw = payload.get("relevant_domains", [])
    if not isinstance(relevant_raw, list):
        raise ValueError("relevant_domains deve ser uma lista")
    if any(not isinstance(value, str) or not value.strip()
           for value in relevant_raw):
        raise ValueError("relevant_domains deve conter textos não vazios")
    relevant_domains = sorted({value.strip() for value in relevant_raw})
    expected_relevant = sorted({
        value for value in (source_cid, destination_cid) if value
    })
    if relevant_domains != expected_relevant:
        raise ValueError("relevant_domains diverge da topologia ordenada")
    if not source_cid or not destination_cid:
        expected_role = "UNKNOWN"
    elif source_cid == text["cid"] and destination_cid == text["cid"]:
        expected_role = "LOCAL"
    elif source_cid == text["cid"]:
        expected_role = "SOURCE"
    elif destination_cid == text["cid"]:
        expected_role = "DESTINATION"
    else:
        expected_role = "OBSERVER"
    if text["role"] != expected_role:
        raise ValueError("role não corresponde à topologia e ao cid")
    if text["role"] == "OBSERVER" and text["proposal"] not in {"ABSTAIN", "VETO"}:
        raise ValueError("observador só pode abster ou vetar")
    if text["role"] == "UNKNOWN" and text["proposal"] not in {"WAIT", "VETO"}:
        raise ValueError("topologia desconhecida só pode aguardar ou vetar")

    reason = payload.get("reason", "")
    if not isinstance(reason, str):
        raise ValueError("reason deve ser texto")

    return {
        "schema_version": AGENT_SCHEMA_VERSION,
        **text,
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "source_cid": source_cid,
        "destination_cid": destination_cid,
        "window_id": window_id,
        "observation_ns": observation_ns,
        "created_ns": created_ns,
        "expires_ns": expires_ns,
        "confidence": round(confidence, 6),
        "criteria": {name: round(value, 6) for name, value in criteria.items()},
        "observed_bps": observed,
        "predicted_bps": predicted,
        "z_score": z_score,
        "threshold": threshold,
        "persistence_windows": persistence,
        "relevant_domains": relevant_domains,
        "reason": reason,
    }


def latest_valid_proposals(proposals: Iterable[Dict[str, Any]], *,
                           now_ns_value: int,
                           flow: Optional[str] = None,
                           max_observation_age_ns: Optional[int] = None
                           ) -> List[Dict[str, Any]]:
    """Seleciona a proposta válida mais recente de cada agente/domínio."""
    latest: Dict[str, Dict[str, Any]] = {}
    for raw in proposals:
        try:
            item = validate_agent_proposal(raw, now_ns_value=now_ns_value)
        except ValueError:
            continue
        if flow is not None and item["flow"] != flow:
            continue
        if max_observation_age_ns is not None:
            age_ns = int(now_ns_value) - item["observation_ns"]
            if age_ns < 0 or age_ns > int(max_observation_age_ns):
                continue
        previous = latest.get(item["cid"])
        if previous is None or item["created_ns"] > previous["created_ns"]:
            latest[item["cid"]] = item
    return sorted(latest.values(), key=lambda item: item["cid"])
