#!/usr/bin/env python3
"""Gera um pacote científico leve a partir de uma replicação agentic live.

O pacote não copia logs nem resultados brutos. Ele cria tabelas, figuras SVG,
um relatório metodológico e um inventário SHA-256 que referencia os arquivos
originais dentro do diretório da replicação.
"""

import argparse
import csv
import hashlib
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from experiments.evaluate_agentic_live_replication import find_campaign_summary
except ModuleNotFoundError:  # execução direta: python3 experiments/arquivo.py
    from evaluate_agentic_live_replication import find_campaign_summary


PACKAGE_SCHEMA_VERSION = 1
DEFAULT_OUTPUT_NAME = "research-artifact-v1"


def read_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"JSON inválido ou ausente: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"objeto JSON esperado em: {path}")
    return payload


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Path, root: Path) -> Dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def source_files(root: Path, output: Path) -> List[Path]:
    files = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            path.relative_to(output)
        except ValueError:
            files.append(path)
    return sorted(files)


def write_csv(path: Path, fields: Sequence[str], rows: Iterable[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def scalar(value: Any) -> Any:
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return value


def case_rows(campaign: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for item in campaign.get("cases") or []:
        if not isinstance(item, dict):
            continue
        rows.append({
            "case_id": item.get("case_id"),
            "scenario": item.get("expected_scenario"),
            "flow": item.get("expected_flow"),
            "classification": item.get("classification"),
            "passed": item.get("passed"),
            "detection_latency_ms": item.get("detection_latency_ms"),
            "mcda_consensus_latency_ms": item.get("mcda_consensus_latency_ms"),
            "agentic_consensus_latency_ms": item.get("agentic_consensus_latency_ms"),
            "agentic_matches_mcda": item.get("agentic_matches_mcda"),
            "mcda_converged_within_bound": item.get(
                "mcda_converged_within_bound"
            ),
            "winner_domains": ";".join(item.get("winner_domains") or []),
            "execution_domains": ";".join(item.get("execution_domains") or []),
            "flowblocker_requests": item.get("flowblocker_requests"),
            "drop_rules": item.get("drop_rules"),
        })
    return rows


def convergence_rows(campaign: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for item in campaign.get("cases") or []:
        if not isinstance(item, dict) or item.get("expected_scenario") != "ddos":
            continue
        convergence = item.get("mcda_convergence") or {}
        for cid, observation in sorted(convergence.items()):
            if not isinstance(observation, dict):
                continue
            rows.append({
                "case_id": item.get("case_id"),
                "flow": item.get("expected_flow"),
                "cid": cid,
                "converged": observation.get("converged"),
                "direction": observation.get("direction"),
                "offset_ms": observation.get("offset_ms"),
                "lead_ms": observation.get("lead_ms"),
                "latency_ms": observation.get("latency_ms"),
                "matches_at_authority": observation.get("matches_at_authority"),
                "window_relation": observation.get("window_relation"),
                "preceding_window_distance": observation.get(
                    "preceding_window_distance"
                ),
                "authority_windows": ";".join(
                    str(value) for value in observation.get("window_ids") or []
                ),
            })
    return rows


def summary_rows(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    metrics = summary.get("metrics") or {}
    aggregate = summary.get("aggregate") or {}
    selected = {
        "TP": metrics.get("TP"),
        "TN": metrics.get("TN"),
        "FP": metrics.get("FP"),
        "FN": metrics.get("FN"),
        "precision": metrics.get("precision"),
        "recall": metrics.get("recall"),
        "specificity": metrics.get("specificity"),
        "f1": metrics.get("f1"),
        "sensitivity_ci95": metrics.get("sensitivity_ci95"),
        "specificity_ci95": metrics.get("specificity_ci95"),
        "agent_to_mcda_exact_run_rate": metrics.get(
            "agent_to_mcda_exact_run_rate"
        ),
        "agent_to_mcda_exact_domain_rate": metrics.get(
            "agent_to_mcda_exact_domain_rate"
        ),
        "mcda_bounded_convergence_rate": metrics.get(
            "mcda_bounded_convergence_rate"
        ),
        "mcda_convergence_direction_distribution": metrics.get(
            "mcda_convergence_direction_distribution"
        ),
        "winner_distribution": metrics.get("winner_distribution"),
        "operational_replication_ready": aggregate.get(
            "operational_replication_ready"
        ),
        "comparative_replication_ready": aggregate.get(
            "comparative_replication_ready"
        ),
        "joint_replication_ready": aggregate.get("joint_replication_ready"),
        "reported_cases": aggregate.get("reported_cases"),
        "passed_cases": aggregate.get("passed_cases"),
    }
    return [{"metric": key, "value": scalar(value)} for key, value in selected.items()]


def svg_bar_chart(
    title: str,
    categories: Sequence[str],
    series: Sequence[Tuple[str, Sequence[float], str]],
    *,
    y_label: str,
) -> str:
    width, height = 920, 520
    left, right, top, bottom = 90, 30, 70, 105
    plot_width = width - left - right
    plot_height = height - top - bottom
    values = [float(value) for _name, rows, _color in series for value in rows]
    maximum = max(values, default=1.0)
    maximum = maximum if maximum > 0 else 1.0
    group_width = plot_width / max(1, len(categories))
    bar_width = min(72.0, group_width * 0.7 / max(1, len(series)))
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width / 2}" y="32" text-anchor="middle" font-family="sans-serif" font-size="20" font-weight="bold">{html.escape(title)}</text>',
        f'<text x="18" y="{top + plot_height / 2}" transform="rotate(-90 18 {top + plot_height / 2})" text-anchor="middle" font-family="sans-serif" font-size="13">{html.escape(y_label)}</text>',
    ]
    for tick in range(6):
        value = maximum * tick / 5
        y = top + plot_height - plot_height * tick / 5
        parts.extend([
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" stroke="#d9dee7" stroke-width="1"/>',
            f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" font-family="sans-serif" font-size="11">{value:.0f}</text>',
        ])
    for category_index, category in enumerate(categories):
        center = left + group_width * (category_index + 0.5)
        total_width = bar_width * len(series)
        for series_index, (name, rows, color) in enumerate(series):
            value = float(rows[category_index])
            bar_height = plot_height * value / maximum
            x = center - total_width / 2 + series_index * bar_width
            y = top + plot_height - bar_height
            parts.extend([
                f'<rect x="{x + 3:.1f}" y="{y:.1f}" width="{bar_width - 6:.1f}" height="{bar_height:.1f}" fill="{color}"/>',
                f'<text x="{x + bar_width / 2:.1f}" y="{max(top + 12, y - 6):.1f}" text-anchor="middle" font-family="sans-serif" font-size="11">{value:.1f}</text>',
            ])
        label = html.escape(category).replace("-&gt;", "→")
        parts.append(
            f'<text x="{center:.1f}" y="{top + plot_height + 25}" text-anchor="middle" font-family="sans-serif" font-size="12">{label}</text>'
        )
    legend_x = left
    legend_y = height - 28
    for name, _rows, color in series:
        parts.extend([
            f'<rect x="{legend_x}" y="{legend_y - 11}" width="14" height="14" fill="{color}"/>',
            f'<text x="{legend_x + 20}" y="{legend_y}" font-family="sans-serif" font-size="12">{html.escape(name)}</text>',
        ])
        legend_x += 190
    parts.append("</svg>\n")
    return "\n".join(parts)


def fmt(value: Any, digits: int = 3) -> str:
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}"
    return "-" if value is None else str(value)


def interval_text(value: Dict[str, Any]) -> str:
    if not isinstance(value, dict) or value.get("estimate") is None:
        return "-"
    return (
        f"{value['estimate']:.3f} "
        f"[{value['low']:.3f}, {value['high']:.3f}]"
    )


def research_report(
    root: Path,
    summary: Dict[str, Any],
    campaign: Dict[str, Any],
    source_count: int,
    source_bytes: int,
) -> str:
    metrics = summary.get("metrics") or {}
    aggregate = summary.get("aggregate") or {}
    design = summary.get("design") or {}
    directions = metrics.get("mcda_convergence_direction_distribution") or {}
    episode = design.get("mcda_episode_definition") or {}
    lines = [
        "# Relatório científico da replicação agentic live",
        "",
        f"Replicação: `{root.name}`  ",
        f"Gerado em: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        "## Perguntas e hipóteses",
        "",
        "- **Hipótese operacional:** os agentes detectam e mitigam ataques sem bloquear os controles benignos, mantendo executor único e protocolo íntegro.",
        "- **Hipótese comparativa:** todos os observadores MCDA convergem com os agentes dentro do episódio temporal v2 previamente congelado.",
        "- **Hipótese conjunta:** as duas condições anteriores são satisfeitas na mesma replicação.",
        "",
        "## Protocolo",
        "",
        f"Foram analisados `{aggregate.get('reported_cases')}` casos em Mininet, distribuídos entre três fluxos cross-domain, com dois domínios SDN. O desenho contém `{design.get('runs_per_scenario')}` ataques e o mesmo número de controles benignos.",
        "",
        f"A definição MCDA `{episode.get('name')}` aceita convergência até `{episode.get('lookback_ms')}` ms antes, no máximo `{episode.get('max_preceding_windows')}` janela precedente, ou `{episode.get('future_convergence_ms')}` ms depois da autoridade agentic.",
        "",
        "## Resultados globais",
        "",
        "| Medida | Resultado |",
        "| --- | ---: |",
        f"| TP / TN / FP / FN | {metrics.get('TP')} / {metrics.get('TN')} / {metrics.get('FP')} / {metrics.get('FN')} |",
        f"| Precisão | {fmt(metrics.get('precision'))} |",
        f"| Sensibilidade observada (IC95% Wilson) | {interval_text(metrics.get('sensitivity_ci95') or {})} |",
        f"| Especificidade observada (IC95% Wilson) | {interval_text(metrics.get('specificity_ci95') or {})} |",
        f"| Concordância exata agente–MCDA por execução | {fmt(metrics.get('agent_to_mcda_exact_run_rate'))} |",
        f"| Concordância exata agente–MCDA por domínio | {fmt(metrics.get('agent_to_mcda_exact_domain_rate'))} |",
        f"| Convergência MCDA no episódio v2 | {fmt(metrics.get('mcda_bounded_convergence_rate'))} |",
        "",
        "### Resultado das hipóteses",
        "",
        f"- Hipótese operacional: **{'confirmada nesta replicação' if aggregate.get('operational_replication_ready') is True else 'não confirmada'}**.",
        f"- Hipótese comparativa: **{'confirmada nesta replicação' if aggregate.get('comparative_replication_ready') is True else 'não confirmada'}**.",
        f"- Hipótese conjunta: **{'confirmada nesta replicação' if aggregate.get('joint_replication_ready') is True else 'não confirmada'}**.",
        "",
        "## Latência por fluxo",
        "",
        "| Fluxo | Detecção média (ms) | Consenso agentic médio (ms) |",
        "| --- | ---: | ---: |",
    ]
    for flow, row in sorted((metrics.get("per_flow") or {}).items()):
        lines.append(
            f"| `{flow}` | {fmt((row.get('detection_latency_ms') or {}).get('mean'))} | {fmt((row.get('agentic_consensus_latency_ms') or {}).get('mean'))} |"
        )
    lines.extend([
        "",
        "![Latência média por fluxo](latency-by-flow.svg)",
        "",
        "## Relação temporal entre MCDA e agentes",
        "",
        f"Foram registrados `{directions.get('BEFORE_AUTHORITY', 0)}` observadores antes da autoridade, `{directions.get('AT_AUTHORITY', 0)}` no mesmo instante, `{directions.get('AFTER_AUTHORITY', 0)}` depois e `{directions.get('NOT_OBSERVED', 0)}` sem convergência no episódio.",
        "",
        "![Ordem temporal MCDA–agentes](mcda-temporal-order.svg)",
        "",
        "## Interpretação",
        "",
        "A replicação sustenta a eficácia operacional da abordagem agentic nas condições avaliadas. Ela não sustenta equivalência temporal universal com o MCDA quando a hipótese comparativa está reprovada. Essa divergência deve ser preservada como resultado, e não removida por alteração posterior dos limites.",
        "",
        "Os valores observados de 100% não devem ser generalizados para outras topologias ou distribuições de tráfego. Os intervalos de Wilson permanecem amplos porque existem apenas nove observações por classe.",
        "",
        "## Ameaças à validade",
        "",
        "- ambiente emulado Mininet e topologia fixa de dois domínios;",
        "- três pares de hosts e nove observações por classe;",
        "- um artefato Holt offline e taxas de tráfego predefinidas;",
        "- resultados ainda não cobrem outros ataques, tráfego de fundo, falhas físicas ou redes de produção;",
        "- pesos e limiares dos agentes e do MCDA são hipóteses de engenharia que exigem ablação e análise de sensibilidade.",
        "",
        "## Rastreabilidade e arquivos",
        "",
        f"O manifesto registra SHA-256 de `{source_count}` arquivos brutos, totalizando `{source_bytes}` bytes, sem copiá-los. Use `artifact-manifest.json` para verificar integridade.",
        "",
        "- `cases.csv`: uma linha por caso experimental;",
        "- `mcda-convergence.csv`: uma linha por observador MCDA em cada ataque;",
        "- `summary-metrics.csv`: métricas globais em formato tabular;",
        "- `latency-by-flow.svg` e `mcda-temporal-order.svg`: figuras vetoriais;",
        "- `artifact-manifest.json`: inventário dos arquivos originais e gerados.",
        "",
    ])
    return "\n".join(lines)


def package_replication(root: Path, output: Optional[Path] = None) -> Dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"diretório de replicação inexistente: {root}")
    summary_path = root / "replication-summary.json"
    summary = read_json(summary_path)
    if summary.get("schema_version") != 3:
        raise ValueError(
            "replication-summary.json schema 3 é obrigatório; reavalie a replicação"
        )
    replication_manifest = read_json(root / "replication-manifest.json")
    campaign_summary_path = find_campaign_summary(root, replication_manifest)
    if campaign_summary_path is None:
        raise ValueError("campaign-summary.json não encontrado")
    campaign = read_json(campaign_summary_path)
    campaign_root = campaign_summary_path.parent
    if output is None:
        output = root / DEFAULT_OUTPUT_NAME
    elif not output.is_absolute():
        output = root / output
    output = output.resolve()
    try:
        output.relative_to(root)
    except ValueError as exc:
        raise ValueError("o pacote precisa permanecer dentro da replicação") from exc
    if output == root:
        raise ValueError("o pacote precisa usar um subdiretório da replicação")
    output.mkdir(parents=True, exist_ok=True)

    cases = case_rows(campaign)
    convergence = convergence_rows(campaign)
    write_csv(
        output / "cases.csv",
        (
            "case_id", "scenario", "flow", "classification", "passed",
            "detection_latency_ms", "mcda_consensus_latency_ms",
            "agentic_consensus_latency_ms", "agentic_matches_mcda",
            "mcda_converged_within_bound", "winner_domains",
            "execution_domains", "flowblocker_requests", "drop_rules",
        ),
        cases,
    )
    write_csv(
        output / "mcda-convergence.csv",
        (
            "case_id", "flow", "cid", "converged", "direction",
            "offset_ms", "lead_ms", "latency_ms", "matches_at_authority",
            "window_relation", "preceding_window_distance",
            "authority_windows",
        ),
        convergence,
    )
    write_csv(output / "summary-metrics.csv", ("metric", "value"), summary_rows(summary))

    per_flow = (summary.get("metrics") or {}).get("per_flow") or {}
    flows = sorted(per_flow)
    detection = [
        float((per_flow[flow].get("detection_latency_ms") or {}).get("mean") or 0)
        for flow in flows
    ]
    agentic = [
        float((per_flow[flow].get("agentic_consensus_latency_ms") or {}).get("mean") or 0)
        for flow in flows
    ]
    (output / "latency-by-flow.svg").write_text(
        svg_bar_chart(
            "Latência média por fluxo",
            flows,
            (
                ("Detecção Holt", detection, "#2563eb"),
                ("Consenso agentic", agentic, "#f97316"),
            ),
            y_label="milissegundos",
        ),
        encoding="utf-8",
    )
    direction_order = (
        "BEFORE_AUTHORITY", "AT_AUTHORITY", "AFTER_AUTHORITY", "NOT_OBSERVED"
    )
    direction_labels = ("antes", "na autoridade", "depois", "não observado")
    direction_counts = (summary.get("metrics") or {}).get(
        "mcda_convergence_direction_distribution"
    ) or {}
    (output / "mcda-temporal-order.svg").write_text(
        svg_bar_chart(
            "Ordem temporal MCDA–agentes",
            direction_labels,
            (("observações de domínio", [
                float(direction_counts.get(key, 0)) for key in direction_order
            ], "#0f766e"),),
            y_label="observações",
        ),
        encoding="utf-8",
    )

    originals = source_files(root, output)
    original_records = [file_record(path, root) for path in originals]
    source_bytes = sum(item["bytes"] for item in original_records)
    (output / "REPORT.md").write_text(
        research_report(
            root, summary, campaign, len(original_records), source_bytes
        ),
        encoding="utf-8",
    )
    generated = sorted(
        path for path in output.iterdir()
        if path.is_file() and path.name != "artifact-manifest.json"
    )
    manifest = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "agentic-live-research-artifact",
        "replication": root.name,
        "source_root": str(root),
        "campaign_summary": campaign_summary_path.relative_to(root).as_posix(),
        "source_files": original_records,
        "source_file_count": len(original_records),
        "source_bytes": source_bytes,
        "generated_files": [file_record(path, root) for path in generated],
        "readiness": {
            key: (summary.get("aggregate") or {}).get(key)
            for key in (
                "operational_replication_ready",
                "comparative_replication_ready",
                "joint_replication_ready",
            )
        },
        "provenance": {
            "git_commit": (replication_manifest.get("runtime") or {}).get(
                "git_commit"
            ),
            "model_sha256": (replication_manifest.get("runtime") or {}).get(
                "model_sha256"
            ),
            "episode_definition": (replication_manifest.get("design") or {}).get(
                "mcda_episode_definition"
            ),
        },
    }
    (output / "artifact-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {
        "output": str(output),
        "source_file_count": len(original_records),
        "source_bytes": source_bytes,
        "generated_file_count": len(generated) + 1,
        "readiness": manifest["readiness"],
    }


def verify_package(root: Path, output: Optional[Path] = None) -> Dict[str, Any]:
    root = root.resolve()
    if output is None:
        output = root / DEFAULT_OUTPUT_NAME
    elif not output.is_absolute():
        output = root / output
    output = output.resolve()
    manifest = read_json(output / "artifact-manifest.json")
    mismatches = []
    checked = 0
    for group in ("source_files", "generated_files"):
        for record in manifest.get(group) or []:
            if not isinstance(record, dict) or not record.get("path"):
                mismatches.append({"path": None, "reason": "registro inválido"})
                continue
            path = (root / str(record["path"])).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                mismatches.append({
                    "path": record["path"], "reason": "caminho fora da replicação"
                })
                continue
            checked += 1
            if not path.is_file():
                mismatches.append({"path": record["path"], "reason": "ausente"})
                continue
            if path.stat().st_size != record.get("bytes"):
                mismatches.append({
                    "path": record["path"], "reason": "tamanho divergente"
                })
                continue
            if sha256_file(path) != record.get("sha256"):
                mismatches.append({
                    "path": record["path"], "reason": "SHA-256 divergente"
                })
    return {
        "valid": not mismatches,
        "checked_files": checked,
        "mismatches": mismatches,
        "manifest": str(output / "artifact-manifest.json"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="diretório agentic-live-replication-*")
    parser.add_argument("--output", type=Path, help="subdiretório de saída opcional")
    parser.add_argument(
        "--verify", action="store_true",
        help="verifica os hashes do pacote existente sem regenerá-lo",
    )
    args = parser.parse_args()
    try:
        result = (
            verify_package(args.root, args.output)
            if args.verify else package_replication(args.root, args.output)
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if result.get("valid", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
