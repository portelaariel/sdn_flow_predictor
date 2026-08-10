#!/usr/bin/env python3
"""Guard-rails reutilizáveis para a futura autoridade dos agentes.

Este módulo não chama o FlowBlocker. Ele transforma uma decisão deliberativa
em uma autorização verificável e, separadamente, oferece um claim atômico no
ETCD. Enquanto o runtime permanecer em shadow mode, nenhuma dessas operações
é conectada ao plano de dados.
"""

import json
import math
from typing import Any, Dict, Iterable, Optional

from agent_protocol import agent_flow_hash


# Compatibilidade com propostas geradas antes da soma integral do TTL. Ao
# adicionar uma duração float a epoch_ns, o Python podia arredondar até centenas
# de nanos. A tolerância só vale para a duração declarada; a idade real da
# observação continua limitada sem qualquer margem.
PROPOSAL_TTL_ROUNDING_TOLERANCE_NS = 1024


def _deny(code: str, reason: str, *, flow: Optional[str] = None) -> Dict[str, Any]:
    return {
        "authorized": False,
        "code": code,
        "reason": reason,
        "flow": flow,
    }


def evaluate_agentic_authority(
    decision: Dict[str, Any], *, now_ns_value: int,
    expected_flow: Optional[str] = None,
    expected_window_ids: Optional[Iterable[int]] = None,
    max_proposal_age_ns: int = 12_000_000_000,
) -> Dict[str, Any]:
    """Valida novamente uma decisão antes de qualquer futura atuação.

    A deliberação e a autorização são fronteiras diferentes. Mesmo um payload
    marcado como AGREED precisa provar quórum, topologia, modelo, episódio e
    validade temporal. Isso impede que um estado forjado ou antigo atravesse o
    caminho de mitigação quando o modo autoritativo for implementado.
    """
    if not isinstance(decision, dict):
        return _deny("decision_missing", "decisão agentic ausente")
    flow = decision.get("flow")
    if not isinstance(flow, str) or "->" not in flow:
        return _deny("invalid_flow", "decisão sem fluxo canônico")
    if expected_flow is not None and flow != expected_flow:
        return _deny("flow_mismatch", "decisão pertence a outro fluxo", flow=flow)
    state = decision.get("decision")
    if state != "AGREED":
        return _deny(
            "decision_not_agreed",
            f"estado {state or 'ausente'} não autoriza mitigação",
            flow=flow,
        )
    event_id = decision.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        return _deny(
            "event_identity_missing", "AGREED sem identidade auditável",
            flow=flow,
        )

    source = str(decision.get("source_cid") or "")
    destination = str(decision.get("destination_cid") or "")
    if not source or not destination:
        return _deny("topology_unknown", "origem ou destino sem domínio", flow=flow)
    expected_relevant = sorted({source, destination})
    relevant = sorted({str(value) for value in decision.get("relevant_domains", [])})
    if relevant != expected_relevant:
        return _deny(
            "topology_mismatch", "domínios relevantes divergem da topologia",
            flow=flow,
        )

    try:
        required_votes = int(decision.get("required_votes"))
    except (TypeError, ValueError):
        return _deny("invalid_quorum", "quórum ausente ou inválido", flow=flow)
    votes = sorted({str(value) for value in decision.get("mitigate_votes", [])})
    if required_votes < 1 or required_votes > len(relevant):
        return _deny("invalid_quorum", "quórum fora dos limites", flow=flow)
    if len(votes) < required_votes or not set(votes).issubset(relevant):
        return _deny("quorum_not_reached", "votos MITIGATE insuficientes", flow=flow)

    windows = sorted({int(value) for value in decision.get("window_ids", [])})
    if not windows:
        return _deny("episode_missing", "decisão sem janela de observação", flow=flow)
    if expected_window_ids is not None:
        expected_windows = {int(value) for value in expected_window_ids}
        if not expected_windows.intersection(windows):
            return _deny(
                "episode_mismatch", "decisão pertence a outro episódio", flow=flow
            )

    proposals = decision.get("proposals")
    if not isinstance(proposals, list):
        return _deny("proposals_missing", "decisão sem propostas auditáveis", flow=flow)
    by_cid: Dict[str, Dict[str, Any]] = {}
    for proposal in proposals:
        if not isinstance(proposal, dict):
            return _deny("invalid_proposal", "proposta não é objeto", flow=flow)
        cid = str(proposal.get("cid") or "")
        if cid in by_cid:
            return _deny(
                "duplicate_domain", f"mais de uma proposta para {cid}", flow=flow
            )
        by_cid[cid] = proposal
    if not set(relevant).issubset(by_cid):
        return _deny(
            "missing_relevant_proposal", "nem todos os domínios reportaram",
            flow=flow,
        )

    model_ids = set()
    expirations = []
    for cid in relevant:
        proposal = by_cid[cid]
        if proposal.get("flow") != flow:
            return _deny("proposal_flow_mismatch", "proposta de outro fluxo", flow=flow)
        if (proposal.get("source_cid") != source
                or proposal.get("destination_cid") != destination):
            return _deny(
                "proposal_topology_mismatch", "propostas divergem da topologia",
                flow=flow,
            )
        if sorted(proposal.get("relevant_domains", [])) != relevant:
            return _deny(
                "proposal_topology_mismatch", "proposta possui domínios divergentes",
                flow=flow,
            )
        expected_role = (
            "LOCAL" if source == destination == cid
            else "SOURCE" if source == cid
            else "DESTINATION" if destination == cid
            else "OBSERVER"
        )
        if proposal.get("role") != expected_role:
            return _deny(
                "proposal_role_mismatch", "papel não corresponde à topologia",
                flow=flow,
            )
        try:
            proposal_window = int(proposal.get("window_id"))
            observation_ns = int(proposal.get("observation_ns"))
            created_ns = int(proposal.get("created_ns"))
            expires_ns = int(proposal.get("expires_ns"))
        except (TypeError, ValueError):
            return _deny("invalid_proposal_time", "janela ou TTL inválido", flow=flow)
        if proposal_window not in windows:
            return _deny(
                "proposal_episode_mismatch", "proposta fora do episódio decidido",
                flow=flow,
            )
        if not (0 < observation_ns <= created_ns <= int(now_ns_value)):
            return _deny(
                "invalid_proposal_time", "ordem temporal da proposta é inválida",
                flow=flow,
            )
        if expires_ns <= created_ns:
            return _deny("invalid_proposal_time", "TTL da proposta é inválido", flow=flow)
        if expires_ns <= int(now_ns_value):
            return _deny("proposal_expired", "proposta expirada", flow=flow)
        if int(now_ns_value) - observation_ns > int(max_proposal_age_ns):
            return _deny(
                "proposal_stale", "idade da observação excede o limite", flow=flow
            )
        if (expires_ns - created_ns
                > int(max_proposal_age_ns) + PROPOSAL_TTL_ROUNDING_TOLERANCE_NS):
            return _deny(
                "proposal_ttl_exceeds_limit",
                "TTL declarado excede o limite",
                flow=flow,
            )
        expirations.append(expires_ns)
        model_id = proposal.get("model_id")
        if not isinstance(model_id, str) or not model_id:
            return _deny("model_missing", "proposta sem identidade de modelo", flow=flow)
        model_ids.add(model_id)
        if cid in votes and proposal.get("proposal") != "MITIGATE":
            return _deny(
                "vote_payload_mismatch", "voto não corresponde à proposta",
                flow=flow,
            )
        if proposal.get("proposal") in {"VETO", "NORMAL"}:
            return _deny(
                "blocking_proposal", "veto ou classificação normal presente",
                flow=flow,
            )
    if len(model_ids) != 1:
        return _deny("model_mismatch", "agentes usam modelos diferentes", flow=flow)

    return {
        "authorized": True,
        "code": "authorized",
        "reason": "AGREED válido e fresco",
        "flow": flow,
        "source_cid": source,
        "destination_cid": destination,
        "relevant_domains": relevant,
        "mitigate_votes": votes,
        "required_votes": required_votes,
        "window_ids": windows,
        "model_id": next(iter(model_ids)),
        "expires_ns": min(expirations),
        "decision_event_id": event_id,
    }


def claim_agentic_mitigation(
    etcd: Any, authorization: Dict[str, Any], *, coordinator: str,
    now_ns_value: int, ttl_s: float = 60.0,
) -> Dict[str, Any]:
    """Disputa um claim próprio dos agentes; falhas nunca degradam para ação."""
    if not authorization.get("authorized"):
        return {
            "won": False,
            "degraded": False,
            "coordinator": None,
            "reason": "decisão não autorizada",
        }
    flow = authorization["flow"]
    if int(authorization.get("expires_ns", 0)) <= int(now_ns_value):
        return {
            "won": False,
            "degraded": False,
            "coordinator": None,
            "reason": "autorização expirada antes do claim",
        }
    if coordinator not in authorization.get("relevant_domains", []):
        return {
            "won": False,
            "degraded": False,
            "coordinator": None,
            "reason": "coordenador não pertence aos domínios relevantes",
        }
    key = f"flowpredictor/agent-mitigation-claim/{agent_flow_hash(flow)}"
    ttl = max(1, int(math.ceil(float(ttl_s))))
    payload = {
        "flow": flow,
        "coordinator": coordinator,
        "claimed_ns": int(now_ns_value),
        "decision_event_id": authorization.get("decision_event_id"),
        "window_ids": list(authorization.get("window_ids", [])),
    }
    try:
        lease = etcd.lease(ttl)
        won, _responses = etcd.transaction(
            compare=[etcd.transactions.version(key) == 0],
            success=[etcd.transactions.put(
                key, json.dumps(payload, sort_keys=True), lease.id
            )],
            failure=[],
        )
        if won:
            owner = payload
        else:
            raw, _metadata = etcd.get(key)
            owner = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        claimed_ns = int(owner.get("claimed_ns", now_ns_value))
        return {
            "won": bool(won),
            "degraded": False,
            "coordinator": owner.get("coordinator", "unknown"),
            "claimed_ns": claimed_ns,
            "expires_ns": claimed_ns + ttl * 1_000_000_000,
            "key": key,
            "reason": "claim adquirido" if won else "claim pertencente a outro agente",
        }
    except Exception as exc:
        return {
            "won": False,
            "degraded": True,
            "coordinator": None,
            "key": key,
            "reason": f"ETCD indisponível: {exc}",
        }
