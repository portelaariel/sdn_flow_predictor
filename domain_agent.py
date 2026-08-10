#!/usr/bin/env python3
"""Agente deliberativo e determinístico de um domínio SDN.

O agente interpreta uma evidência Holt local, emite uma proposta estruturada e
funde propostas dos domínios relevantes. Nesta primeira fase ele opera somente
em *shadow mode*: não possui qualquer método de atuação no plano de dados.
"""

import math
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

from agent_protocol import latest_valid_proposals, validate_agent_proposal
from collaborative_decision import clip01


DEFAULT_LOCAL_AGENT_WEIGHTS = {
    "severity": 0.35,
    "rate_ratio": 0.20,
    "persistence": 0.20,
    "model_reliability": 0.15,
    "topology": 0.10,
}


def domain_role(cid: str, source_cid: Optional[str],
                destination_cid: Optional[str]) -> Tuple[str, List[str]]:
    """Resolve o papel do agente e os domínios que precisam negociar."""
    source = str(source_cid or "").strip()
    destination = str(destination_cid or "").strip()
    relevant = sorted({value for value in (source, destination) if value})
    if not source or not destination:
        return "UNKNOWN", relevant
    if source == cid and destination == cid:
        return "LOCAL", relevant
    if source == cid:
        return "SOURCE", relevant
    if destination == cid:
        return "DESTINATION", relevant
    return "OBSERVER", relevant


class DomainAgent:
    """Transforma observações locais em propostas e negocia um consenso."""

    def __init__(self, cid: str, *, proposal_threshold: float = 0.65,
                 persistence_windows: int = 3, rate_ratio_max: float = 10.0,
                 proposal_ttl_s: float = 12.0,
                 required_votes: int = 2,
                 negotiation_window_s: float = 4.0):
        if not cid:
            raise ValueError("cid do agente não pode ser vazio")
        if not 0.0 <= proposal_threshold <= 1.0:
            raise ValueError("proposal_threshold deve estar em [0, 1]")
        if persistence_windows < 1 or required_votes < 1:
            raise ValueError("persistência e votos devem ser positivos")
        if not math.isfinite(rate_ratio_max) or rate_ratio_max <= 1.0:
            raise ValueError("rate_ratio_max deve ser maior que um")
        if (not math.isfinite(proposal_ttl_s) or proposal_ttl_s <= 0.0
                or not math.isfinite(negotiation_window_s)
                or negotiation_window_s <= 0.0):
            raise ValueError("TTL e janela de negociação devem ser positivos")
        if proposal_ttl_s < negotiation_window_s:
            raise ValueError("TTL deve cobrir a janela de negociação")

        self.cid = cid
        self.agent_id = f"sdn-domain:{cid}"
        self.proposal_threshold = float(proposal_threshold)
        self.persistence_windows = int(persistence_windows)
        self.rate_ratio_max = float(rate_ratio_max)
        self.proposal_ttl_s = float(proposal_ttl_s)
        self.required_votes = int(required_votes)
        self.negotiation_window_s = float(negotiation_window_s)
        self.weights = dict(DEFAULT_LOCAL_AGENT_WEIGHTS)
        self.lock = threading.RLock()
        self.states: Dict[str, Dict[str, Any]] = {}

    def _criteria(self, evidence: Dict[str, Any], role: str) -> Dict[str, float]:
        z_score = max(0.0, float(evidence.get("z_score", 0.0)))
        threshold = max(1e-9, float(evidence.get("threshold", 1.0)))
        observed = max(0.0, float(evidence.get("observed_bps", 0.0)))
        predicted = max(1.0, float(evidence.get("predicted_bps", 0.0)))
        ratio = max(1.0, observed / predicted)
        topology = {
            "SOURCE": 1.0,
            "DESTINATION": 1.0,
            "LOCAL": 1.0,
            "UNKNOWN": 0.5,
            "OBSERVER": 0.0,
        }[role]
        return {
            "severity": clip01((z_score / threshold - 1.0) / 2.0),
            "rate_ratio": clip01(math.log(ratio) / math.log(self.rate_ratio_max)),
            "persistence": clip01(
                float(evidence.get("persistence_windows", 1))
                / self.persistence_windows
            ),
            "model_reliability": clip01(
                float(evidence.get("model_reliability", 0.5))
            ),
            "topology": topology,
        }

    def build_proposal(self, evidence: Dict[str, Any], *, role: str,
                       relevant_domains: List[str],
                       source_cid: Optional[str],
                       destination_cid: Optional[str], created_ns: int,
                       veto_reason: Optional[str] = None) -> Dict[str, Any]:
        """Interpreta uma evidência Holt e produz uma proposta validada."""
        if role not in {"SOURCE", "DESTINATION", "LOCAL", "OBSERVER", "UNKNOWN"}:
            raise ValueError("papel topológico inválido")
        flow = str(evidence.get("flow", ""))
        if "->" not in flow:
            raise ValueError("evidência sem fluxo canônico")
        criteria = self._criteria(evidence, role)
        confidence = clip01(sum(
            criteria[name] * weight for name, weight in self.weights.items()
        ))
        z_score = float(evidence.get("z_score", 0.0))
        threshold = float(evidence.get("threshold", 1.0))
        created_ns = int(created_ns)
        # Somar float a um epoch em nanos (~1e18) perde os bits menos
        # significativos e pode alongar o TTL em centenas de nanos. Converta a
        # duração primeiro e mantenha a soma integral de ponta a ponta.
        proposal_ttl_ns = int(self.proposal_ttl_s * 1e9)

        if veto_reason:
            proposal, belief, reason = "VETO", "POLICY_CONFLICT", veto_reason
        elif role == "OBSERVER":
            proposal, belief, reason = (
                "ABSTAIN", "NOT_RESPONSIBLE",
                "domínio não é origem nem destino do fluxo",
            )
        elif role == "UNKNOWN" or not relevant_domains:
            proposal, belief, reason = (
                "WAIT", "TOPOLOGY_UNKNOWN",
                "aguardando resolução dos domínios de origem e destino",
            )
        elif z_score < threshold:
            proposal, belief, reason = (
                "NORMAL", "NORMAL_TRAFFIC", "limiar Holt não confirmado",
            )
        elif confidence >= self.proposal_threshold:
            proposal, belief, reason = (
                "MITIGATE", "DDOS_LIKELY",
                "evidência local e contexto superam o limiar do agente",
            )
        else:
            proposal, belief, reason = (
                "WAIT", "ANOMALY_UNCERTAIN",
                "anomalia local ainda não possui confiança suficiente",
            )

        proposal_payload = validate_agent_proposal({
            "schema_version": 1,
            "agent_id": self.agent_id,
            "cid": self.cid,
            "flow": flow,
            "src_ip": evidence.get("src_ip", ""),
            "dst_ip": evidence.get("dst_ip", ""),
            "source_cid": str(source_cid or ""),
            "destination_cid": str(destination_cid or ""),
            "window_id": int(evidence.get("window_id", 0)),
            "model_id": str(evidence.get("model_id", "unknown")),
            "role": role,
            "belief": belief,
            "proposal": proposal,
            "confidence": confidence,
            "criteria": criteria,
            "observed_bps": float(evidence.get("observed_bps", 0.0)),
            "predicted_bps": float(evidence.get("predicted_bps", 0.0)),
            "z_score": z_score,
            "threshold": threshold,
            "persistence_windows": int(evidence.get("persistence_windows", 1)),
            "relevant_domains": relevant_domains,
            "observation_ns": int(evidence.get("ts_ns", created_ns)),
            "created_ns": created_ns,
            "expires_ns": created_ns + proposal_ttl_ns,
            "reason": reason,
        })
        with self.lock:
            self.states[flow] = {
                "state": "PROPOSING",
                "updated_ns": created_ns,
                "proposal": proposal,
                "belief": belief,
                "confidence": proposal_payload["confidence"],
            }
        return proposal_payload

    def decide(self, proposals: Iterable[Dict[str, Any]], *, flow: str,
               now_ns_value: int) -> Dict[str, Any]:
        """Funde as propostas recentes sem realizar qualquer ação externa."""
        current = latest_valid_proposals(
            proposals,
            now_ns_value=now_ns_value,
            flow=flow,
            max_observation_age_ns=int(self.proposal_ttl_s * 1e9),
        )
        base = {
            "flow": flow,
            "evaluated_ns": int(now_ns_value),
            "participating_domains": [item["cid"] for item in current],
            "window_ids": [],
            "observation_start_ns": None,
            "observation_end_ns": None,
            "relevant_domains": [],
            "required_votes": self.required_votes,
            "mitigate_votes": [],
            "proposals": [
                {
                    "flow": item["flow"],
                    "cid": item["cid"],
                    "role": item["role"],
                    "proposal": item["proposal"],
                    "belief": item["belief"],
                    "confidence": item["confidence"],
                    "reason": item["reason"],
                    "source_cid": item["source_cid"],
                    "destination_cid": item["destination_cid"],
                    "observation_ns": item["observation_ns"],
                    "window_id": item["window_id"],
                    "model_id": item["model_id"],
                    "created_ns": item["created_ns"],
                    "expires_ns": item["expires_ns"],
                    "relevant_domains": list(item["relevant_domains"]),
                }
                for item in current
            ],
            "confidence": 0.0,
            "first_proposal_ns": None,
            "last_proposal_ns": None,
            "proposal_collection_latency_ms": None,
            "decision_after_last_proposal_ms": None,
        }
        if not current:
            return self._record_decision(base, "NO_PROPOSALS", "sem proposta recente")

        topology_pairs = {
            (item["source_cid"], item["destination_cid"])
            for item in current
            if item["source_cid"] and item["destination_cid"]
        }
        topology_unknown = any(
            not item["source_cid"] or not item["destination_cid"]
            for item in current
        )
        if topology_unknown or len(topology_pairs) != 1:
            state = ("WAITING_TOPOLOGY" if topology_unknown and not topology_pairs
                     else "TOPOLOGY_MISMATCH")
            reason = ("topologia ainda não resolvida" if state == "WAITING_TOPOLOGY"
                      else "agentes possuem visões topológicas incompatíveis")
            return self._record_decision(base, state, reason)
        source_cid, destination_cid = next(iter(topology_pairs))
        base["source_cid"] = source_cid
        base["destination_cid"] = destination_cid
        relevant = sorted({source_cid, destination_cid})
        base["relevant_domains"] = relevant
        required = min(self.required_votes, len(relevant))
        base["required_votes"] = required

        by_cid = {item["cid"]: item for item in current}
        relevant_rows = [by_cid[cid] for cid in relevant if cid in by_cid]
        # Observadores são auditáveis em participating_domains/proposals, mas
        # não pertencem à identidade temporal do episódio decidido pelos
        # agentes responsáveis pela origem e pelo destino.
        if relevant_rows:
            base["window_ids"] = sorted({
                item["window_id"] for item in relevant_rows
            })
            base["observation_start_ns"] = min(
                item["observation_ns"] for item in relevant_rows
            )
            base["observation_end_ns"] = max(
                item["observation_ns"] for item in relevant_rows
            )
            proposal_times = [item["created_ns"] for item in relevant_rows]
            base["first_proposal_ns"] = min(proposal_times)
            base["last_proposal_ns"] = max(proposal_times)
            base["proposal_collection_latency_ms"] = round(
                (base["last_proposal_ns"] - base["first_proposal_ns"]) / 1e6,
                3,
            )
            base["decision_after_last_proposal_ms"] = round(
                (base["evaluated_ns"] - base["last_proposal_ns"]) / 1e6,
                3,
            )
        missing = sorted(set(relevant) - set(by_cid))
        if missing:
            base["missing_domains"] = missing
            return self._record_decision(
                base, "WAITING_PROPOSALS",
                f"aguardando proposta de {', '.join(missing)}",
            )

        models = sorted({item["model_id"] for item in relevant_rows})
        base["model_ids"] = models
        if len(models) != 1:
            return self._record_decision(
                base, "MODEL_MISMATCH", "agentes usam modelos incompatíveis"
            )

        # O instante da observação Holt define simultaneidade. created_ns é o
        # momento de transporte e pode ser renovado por retry, portanto não
        # pode tornar uma evidência antiga compatível com uma nova.
        timestamps = [item["observation_ns"] for item in relevant_rows]
        if max(timestamps) - min(timestamps) > self.negotiation_window_s * 1e9:
            return self._record_decision(
                base, "WAITING_WINDOW", "propostas fora da mesma janela de negociação"
            )

        vetoes = [item["cid"] for item in relevant_rows
                  if item["proposal"] == "VETO"]
        if vetoes:
            base["veto_domains"] = sorted(vetoes)
            return self._record_decision(
                base, "VETOED", "ao menos um domínio relevante aplicou veto"
            )

        mitigate = sorted(item["cid"] for item in relevant_rows
                          if item["proposal"] == "MITIGATE")
        base["mitigate_votes"] = mitigate
        base["confidence"] = round(
            min(item["confidence"] for item in relevant_rows), 6
        )
        if any(item["proposal"] == "NORMAL" for item in relevant_rows):
            return self._record_decision(
                base, "DISAGREED", "ao menos um domínio classificou o fluxo como normal"
            )
        if len(mitigate) >= required:
            return self._record_decision(
                base, "AGREED", "quórum configurado propõe mitigação"
            )
        return self._record_decision(
            base, "WAITING", "há propostas pendentes, abstidas ou inconclusivas"
        )

    def _record_decision(self, base: Dict[str, Any], state: str,
                         reason: str) -> Dict[str, Any]:
        result = {**base, "decision": state, "reason": reason}
        flow = result["flow"]
        with self.lock:
            self.states[flow] = {
                "state": state,
                "updated_ns": result["evaluated_ns"],
                "confidence": result.get("confidence", 0.0),
                "reason": reason,
            }
        return result

    def snapshot_states(self) -> Dict[str, Dict[str, Any]]:
        with self.lock:
            return {flow: dict(state) for flow, state in self.states.items()}

    def forget(self, flow: str) -> None:
        """Remove o estado transitório quando o episódio local expira."""
        with self.lock:
            self.states.pop(flow, None)
