#!/usr/bin/env python3
"""Gera explicações em linguagem natural para decisões dos agentes.

Este script NUNCA participa do caminho de mitigação. Ele roda depois que
uma execução (benchmark ou campanha) já terminou, lê os eventos de decisão
gravados em timeline.ndjson e produz um parecer por decisão AGREED/VETO/
WAITING_QUORUM, explicando o motivo e apontando se a decisão parece
coerente com a evidência disponível.

"Coerente/incoerente" aqui significa: a decisão é consistente com os
próprios critérios do sistema (severidade, corroboração, quórum, modelo)?
Não é uma avaliação de acerto/erro absoluto — isso só existe quando o
cenário do benchmark já tem rótulo conhecido (benign/ddos), e nesse caso
o rótulo é passado como contexto extra, não como verdade adicional pro
sistema em produção.

Uso:
    python3 experiments/explain_agentic_decisions.py \
        experiments/results/<execução> \
        --provider null \
        --output experiments/results/<execução>/agentic_explanations.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def read_ndjson(path: Path) -> List[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def extract_decision_events(timeline_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    for row in timeline_rows:
        for section in ("agentic", "collaboration"):
            block = row.get(section)
            if not isinstance(block, dict):
                continue
            for event in block.get("decision_events", []) or []:
                event_id = event.get("event_id")
                if not event_id or event_id in seen:
                    continue
                enriched = dict(event)
                enriched["_section"] = section
                seen[event_id] = enriched
    return list(seen.values())


def load_scenario_ground_truth(run_dir: Path) -> Optional[Dict[str, Any]]:
    metadata = read_json(run_dir / "metadata.json")
    if isinstance(metadata, dict):
        return {
            "scenario": metadata.get("scenario"),
            "mode": metadata.get("mode"),
            "flow": metadata.get("flow"),
        }
    return None


@dataclass
class Explanation:
    event_id: str
    flow: str
    decision: str
    narrative: str
    assessment: str
    provider: str
    raw: Dict[str, Any] = field(default_factory=dict)


class LLMClient(ABC):
    name: str = "abstract"

    @abstractmethod
    def explain(self, decision_event: Dict[str, Any],
                ground_truth: Optional[Dict[str, Any]]) -> Explanation:
        raise NotImplementedError


class NullLLMClient(LLMClient):
    name = "null"

    def explain(self, decision_event: Dict[str, Any],
                ground_truth: Optional[Dict[str, Any]]) -> Explanation:
        flow = str(decision_event.get("flow", "desconhecido"))
        state = str(decision_event.get("decision", "desconhecido"))
        score = decision_event.get("score")
        domains = decision_event.get("confirming_domains")
        mitigation = decision_event.get("mitigation", {}) or {}

        parts = [f"Decisão {state} para o fluxo {flow}."]
        if score is not None:
            parts.append(f"Score de {score}.")
        if domains is not None:
            parts.append(f"Confirmado por {domains} domínio(s).")
        if mitigation.get("executed"):
            parts.append("Mitigação executada de fato.")
        elif mitigation.get("attempted"):
            parts.append(
                f"Mitigação tentada mas não executada ({mitigation.get('reason')})."
            )
        else:
            parts.append("Nenhuma mitigação foi tentada.")

        assessment = "indeterminado"
        if ground_truth and ground_truth.get("scenario"):
            attacked = ground_truth["scenario"] == "ddos"
            executed = bool(mitigation.get("executed") or mitigation.get("attempted"))
            if attacked == executed:
                assessment = "coerente"
            else:
                assessment = "incoerente"

        return Explanation(
            event_id=str(decision_event.get("event_id")),
            flow=flow,
            decision=state,
            narrative=" ".join(parts),
            assessment=assessment,
            provider=self.name,
            raw=decision_event,
        )


def build_explain_prompt(decision_event: Dict[str, Any],
                          ground_truth: Optional[Dict[str, Any]]) -> str:
    context = json.dumps(decision_event, ensure_ascii=False, indent=2)
    gt_line = ""
    if ground_truth and ground_truth.get("scenario"):
        gt_line = (
            "\nContexto de laboratório (não visível ao sistema em "
            f"produção): cenário real era '{ground_truth['scenario']}'."
        )
    return (
        "Você é um auditor técnico de um sistema SDN de mitigação de "
        "DDoS multi-domínio. Abaixo está o registro estruturado de uma "
        "decisão tomada por agentes determinísticos (não LLM) que "
        "negociaram sobre um fluxo de rede.\n\n"
        f"{context}\n{gt_line}\n\n"
        "IMPORTANTE: 'CORROBORATED', 'WAITING', 'WAITING_PROPOSALS' e "
        "estados semelhantes SÃO estados intermediários válidos do "
        "protocolo (o sistema está funcionando como projetado, apenas "
        "ainda não atingiu quórum/consenso) - NÃO são decisões erradas. "
        "Só classifique como 'correta' ou 'incorreta' quando a decisão "
        "for final (ex: AGREED, ou uma mitigação de fato tentada/"
        "executada). Para estados intermediários, use sempre "
        "'impossível avaliar' e explique que a decisão ainda está em "
        "andamento, não que está errada.\n\n"
        "Escreva, em português, um parecer curto (3-5 frases) no "
        "formato: 'A decisão <X> foi tomada por <motivo>, e foi "
        "<correta/incorreta/impossível avaliar> pelos seguintes "
        "motivos: <motivos>.' Baseie-se apenas na evidência fornecida, "
        "sem inventar dados que não estão no registro."
    )


class AnthropicLLMClient(LLMClient):
    name = "anthropic"

    def __init__(self, model: str = "claude-sonnet-4-6",
                 api_key_env: str = "ANTHROPIC_API_KEY"):
        self.model = model
        self.api_key = os.environ.get(api_key_env)
        if not self.api_key:
            raise RuntimeError(
                f"variável de ambiente {api_key_env} não definida; "
                "exporte a chave antes de usar --provider anthropic"
            )

    def explain(self, decision_event: Dict[str, Any],
                ground_truth: Optional[Dict[str, Any]]) -> Explanation:
        import anthropic  # type: ignore

        client = anthropic.Anthropic(api_key=self.api_key)
        prompt = build_explain_prompt(decision_event, ground_truth)
        response = client.messages.create(
            model=self.model,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        narrative = "".join(
            block.text for block in response.content
            if getattr(block, "type", None) == "text"
        )
        assessment = "indeterminado"
        lowered = narrative.lower()
        if "incorreta" in lowered:
            assessment = "incoerente"
        elif "correta" in lowered:
            assessment = "coerente"

        return Explanation(
            event_id=str(decision_event.get("event_id")),
            flow=str(decision_event.get("flow")),
            decision=str(decision_event.get("decision")),
            narrative=narrative.strip(),
            assessment=assessment,
            provider=self.name,
            raw=decision_event,
        )


class OllamaLLMClient(LLMClient):
    name = "ollama"

    def __init__(self, model: str = "gpt-oss:20b",
                 base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url.rstrip("/")

    def explain(self, decision_event: Dict[str, Any],
                ground_truth: Optional[Dict[str, Any]]) -> Explanation:
        import urllib.request

        prompt = build_explain_prompt(decision_event, ground_truth)
        payload = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
        }).encode("utf-8")

        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                body = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"falha ao chamar Ollama em {self.base_url} (modelo "
                f"'{self.model}' foi baixado com 'ollama pull {self.model}'?): {exc}"
            ) from exc

        raw_narrative = body.get("message", {}).get("content", "").strip()
        # Alguns modelos (ex: deepseek-r1) sempre emitem um bloco de
        # raciocinio interno entre <think>...</think> antes da resposta
        # final. Removemos isso do parecer, mas guardamos em separado
        # para quem quiser auditar o raciocinio bruto depois.
        thinking_match = re.search(r"<think>(.*?)</think>", raw_narrative,
                                    re.DOTALL)
        thinking = thinking_match.group(1).strip() if thinking_match else None
        narrative = re.sub(r"<think>.*?</think>", "", raw_narrative,
                            flags=re.DOTALL).strip()

        assessment = "indeterminado"
        lowered = narrative.lower()
        if "incorreta" in lowered:
            assessment = "incoerente"
        elif "correta" in lowered:
            assessment = "coerente"

        return Explanation(
            event_id=str(decision_event.get("event_id")),
            flow=str(decision_event.get("flow")),
            decision=str(decision_event.get("decision")),
            narrative=narrative,
            assessment=assessment,
            provider=self.name,
            raw={**decision_event, "_thinking": thinking} if thinking else decision_event,
        )


PROVIDERS = {
    "null": NullLLMClient,
    "anthropic": AnthropicLLMClient,
    "ollama": OllamaLLMClient,
}


def render_markdown(explanations: List[Explanation], run_dir: Path) -> str:
    lines = [f"# Explicações de decisões agentic — {run_dir.name}", ""]
    if not explanations:
        lines.append("Nenhum decision_event encontrado neste timeline.")
        return "\n".join(lines)
    for exp in explanations:
        lines.append(f"## {exp.flow} — {exp.decision} ({exp.assessment})")
        lines.append(f"*event_id: `{exp.event_id}` · provedor: `{exp.provider}`*")
        lines.append("")
        lines.append(exp.narrative)
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path,
                         help="pasta experiments/results/<execução>")
    parser.add_argument("--provider", choices=sorted(PROVIDERS), default="null")
    parser.add_argument("--model", default=None,
                         help="claude-sonnet-4-6 (anthropic) ou gpt-oss:20b/"
                              "qwen3:14b etc (ollama); usa o default de cada "
                              "provedor se omitido")
    parser.add_argument("--ollama-url", default="http://localhost:11434",
                         help="endereço do servidor Ollama local")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    timeline_path = args.run_dir / "timeline.ndjson"
    if not timeline_path.exists():
        print(f"timeline.ndjson não encontrado em {args.run_dir}", file=sys.stderr)
        return 2

    rows = read_ndjson(timeline_path)
    events = extract_decision_events(rows)
    ground_truth = load_scenario_ground_truth(args.run_dir)

    client: LLMClient
    if args.provider == "anthropic":
        kwargs = {"model": args.model} if args.model else {}
        client = AnthropicLLMClient(**kwargs)
    elif args.provider == "ollama":
        kwargs = {"model": args.model} if args.model else {}
        client = OllamaLLMClient(base_url=args.ollama_url, **kwargs)
    else:
        client = NullLLMClient()

    explanations = [client.explain(event, ground_truth) for event in events]

    output_path = args.output or (args.run_dir / "agentic_explanations.md")
    output_path.write_text(render_markdown(explanations, args.run_dir),
                            encoding="utf-8")

    json_path = output_path.with_suffix(".json")
    json_path.write_text(
        json.dumps([exp.__dict__ for exp in explanations], ensure_ascii=False,
                    indent=2, default=lambda o: o),
        encoding="utf-8",
    )

    print(f"{len(explanations)} decisão(ões) explicada(s) via '{client.name}'")
    print(f"markdown: {output_path}")
    print(f"json:     {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
