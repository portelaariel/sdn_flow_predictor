#!/usr/bin/env python3
"""Converte CSVs CIC-DDoS2019 em uma série temporal compacta de vazão.

Os arquivos CICFlowMeter contêm um registro por fluxo concluído, enquanto o
FlowPredictor observa a quantidade de bits por janela de polling. Este utilitário
reconstrói essa visão distribuindo os bytes de cada fluxo uniformemente sobre a
sua duração e agregando o resultado em janelas fixas.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass
class Window:
    bits: float = 0.0
    normal_bits: float = 0.0
    attack_bits: float = 0.0
    flow_records: int = 0
    normal_records: int = 0
    attack_records: int = 0


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


def parse_timestamp(raw: str, date_order: str = "day-first") -> float:
    """Converte timestamps numéricos, ISO e formatos usuais do CIC para UTC."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("timestamp vazio")

    try:
        value = float(text)
        if math.isfinite(value):
            return value
    except ValueError:
        pass

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None

    if parsed is None:
        first = "%d/%m/%Y" if date_order == "day-first" else "%m/%d/%Y"
        formats = (
            f"{first} %H:%M:%S.%f",
            f"{first} %H:%M:%S",
            f"{first} %I:%M:%S.%f %p",
            f"{first} %I:%M:%S %p",
        )
        for timestamp_format in formats:
            try:
                parsed = datetime.strptime(text, timestamp_format)
                break
            except ValueError:
                continue

    if parsed is None:
        raise ValueError(f"timestamp inválido: {text!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _finite_nonnegative(raw: str, name: str) -> float:
    try:
        value = float((raw or "").strip())
    except ValueError as exc:
        raise ValueError(f"{name} inválido") from exc
    if not math.isfinite(value) or value < 0.0:
        raise ValueError(f"{name} inválido")
    return value


def _normalized_reader(handle, path: Path) -> csv.DictReader:
    reader = csv.DictReader(handle)
    raw_headers = reader.fieldnames or []
    headers = [header.strip() for header in raw_headers]
    if not headers:
        raise ValueError(f"{path}: cabeçalho CSV ausente")
    if len(headers) != len(set(headers)):
        raise ValueError(f"{path}: cabeçalhos duplicados após remover espaços")
    reader.fieldnames = headers
    return reader


def _add_flow_to_windows(
    windows: Dict[int, Window],
    start_s: float,
    duration_s: float,
    total_bits: float,
    window_s: float,
    is_attack: bool,
) -> None:
    def add(index: int, bits: float) -> None:
        bucket = windows.setdefault(index, Window())
        bucket.bits += bits
        bucket.flow_records += 1
        if is_attack:
            bucket.attack_bits += bits
            bucket.attack_records += 1
        else:
            bucket.normal_bits += bits
            bucket.normal_records += 1

    if duration_s <= 0.0:
        add(math.floor(start_s / window_s), total_bits)
        return

    end_s = start_s + duration_s
    first_index = math.floor(start_s / window_s)
    last_index = math.floor(math.nextafter(end_s, -math.inf) / window_s)
    rate_bps = total_bits / duration_s
    for index in range(first_index, last_index + 1):
        bin_start = index * window_s
        overlap = max(0.0, min(end_s, bin_start + window_s) - max(start_s, bin_start))
        if overlap > 0.0:
            add(index, rate_bps * overlap)


def aggregate_cic_files(
    inputs: Iterable[str],
    window_s: float = 2.0,
    normal_labels: Sequence[str] = ("BENIGN",),
    attack_labels: Sequence[str] = (),
    attack_inbound_only: bool = False,
    series_mode: str = "endpoint-pair",
    date_order: str = "day-first",
    progress_every: int = 1_000_000,
) -> Tuple[Dict[str, Dict[int, Window]], Dict[str, object]]:
    if not math.isfinite(window_s) or window_s <= 0.0:
        raise ValueError("--window-s deve ser positivo e finito")
    normal = {label.strip().casefold() for label in normal_labels if label.strip()}
    if not normal:
        raise ValueError("informe ao menos um --normal-label")
    selected_attacks = {
        label.strip().casefold() for label in attack_labels if label.strip()
    }

    if series_mode not in {"aggregate", "endpoint-pair"}:
        raise ValueError(f"series_mode inválido: {series_mode}")

    paths = _resolve_inputs(inputs)
    series_windows: Dict[str, Dict[int, Window]] = {}
    rows_total = rows_valid = rows_skipped = rows_filtered = 0
    normal_rows = attack_rows = 0
    label_counts: Dict[str, int] = {}
    required = {
        "Timestamp",
        "Flow Duration",
        "Total Length of Fwd Packets",
        "Total Length of Bwd Packets",
        "Label",
    }
    if series_mode == "endpoint-pair":
        required.update(("Source IP", "Destination IP"))
    if attack_inbound_only:
        required.add("Inbound")

    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = _normalized_reader(handle, path)
            headers = set(reader.fieldnames or [])
            missing = sorted(required - headers)
            if missing:
                raise ValueError(f"{path}: colunas ausentes: {missing}")

            for row in reader:
                rows_total += 1
                try:
                    timestamp = parse_timestamp(row.get("Timestamp", ""), date_order)
                    duration_s = _finite_nonnegative(
                        row.get("Flow Duration", ""), "Flow Duration"
                    ) / 1_000_000.0
                    forward_bytes = _finite_nonnegative(
                        row.get("Total Length of Fwd Packets", ""),
                        "Total Length of Fwd Packets",
                    )
                    backward_bytes = _finite_nonnegative(
                        row.get("Total Length of Bwd Packets", ""),
                        "Total Length of Bwd Packets",
                    )
                    raw_label = (row.get("Label") or "").strip()
                    if not raw_label:
                        raise ValueError("Label vazio")
                    if series_mode == "endpoint-pair":
                        source_ip = (row.get("Source IP") or "").strip()
                        destination_ip = (row.get("Destination IP") or "").strip()
                        if not source_ip or not destination_ip:
                            raise ValueError("par de endpoints vazio")
                        series = f"{source_ip}->{destination_ip}"
                    else:
                        series = "aggregate"
                except ValueError:
                    rows_skipped += 1
                    continue

                is_attack = raw_label.casefold() not in normal
                label_counts[raw_label] = label_counts.get(raw_label, 0) + 1
                if is_attack and selected_attacks and raw_label.casefold() not in selected_attacks:
                    rows_filtered += 1
                    continue
                if (is_attack and attack_inbound_only
                        and (row.get("Inbound") or "").strip().casefold()
                        not in {"1", "true", "yes"}):
                    rows_filtered += 1
                    continue
                _add_flow_to_windows(
                    series_windows.setdefault(series, {}),
                    timestamp,
                    duration_s,
                    (forward_bytes + backward_bytes) * 8.0,
                    window_s,
                    is_attack,
                )
                rows_valid += 1
                if is_attack:
                    attack_rows += 1
                else:
                    normal_rows += 1

                if progress_every > 0 and rows_total % progress_every == 0:
                    print(
                        f"processadas {rows_total:,} linhas; "
                        f"séries={len(series_windows):,}; "
                        f"janelas={sum(len(item) for item in series_windows.values()):,}",
                        file=sys.stderr,
                    )

    if not series_windows:
        raise ValueError("nenhuma janela válida foi produzida")

    window_count = sum(len(windows) for windows in series_windows.values())

    summary: Dict[str, object] = {
        "source_files": [path.name for path in paths],
        "source_file_sizes": {path.name: path.stat().st_size for path in paths},
        "source_bytes": sum(path.stat().st_size for path in paths),
        "rows_total": rows_total,
        "rows_valid": rows_valid,
        "rows_skipped": rows_skipped,
        "rows_filtered": rows_filtered,
        "rows_normal": normal_rows,
        "rows_attack": attack_rows,
        "windows": window_count,
        "series": len(series_windows),
        "window_s": window_s,
        "label_counts": label_counts,
    }
    return series_windows, summary


def write_compact_csv(
    output: str,
    series_windows: Dict[str, Dict[int, Window]],
    window_s: float,
    series_key: str,
    label_mode: str = "separate-phases",
    baseline_prefix_windows: int = 10,
    min_series_windows: int = 2,
    min_window_bps: float = 1.0,
    force: bool = False,
) -> Path:
    if baseline_prefix_windows < 1:
        raise ValueError("--baseline-prefix-windows deve ser ao menos 1")
    if min_series_windows < 2:
        raise ValueError("--min-series-windows deve ser ao menos 2")
    if not math.isfinite(min_window_bps) or min_window_bps < 0.0:
        raise ValueError("--min-window-bps deve ser não negativo e finito")

    target = Path(output).expanduser()
    if target.exists() and not force:
        raise ValueError(f"saída já existe: {target}; use --force para substituir")
    target.parent.mkdir(parents=True, exist_ok=True)

    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            writer = csv.DictWriter(
                handle,
                fieldnames=(
                    "timestamp",
                    "datetime_iso",
                    "source_timestamp",
                    "source_datetime_iso",
                    "flow_key",
                    "observed_bps",
                    "label",
                    "flow_records",
                    "normal_records",
                    "attack_records",
                    "phase_source",
                ),
            )
            writer.writeheader()

            normal_rates = [
                bucket.normal_bits / window_s
                for windows in series_windows.values()
                for bucket in windows.values()
                if bucket.normal_records and bucket.normal_bits / window_s >= min_window_bps
            ]
            if label_mode == "separate-phases" and not normal_rates:
                raise ValueError("nenhuma janela benigna passou por --min-window-bps")
            baseline_bps = statistics.median(normal_rates) if normal_rates else 0.0
            rows_written = 0

            def output_key(raw_series: str) -> str:
                return series_key if raw_series == "aggregate" else f"{series_key}:{raw_series}"

            def emit(
                sequence: int,
                key: str,
                observed_bps: float,
                label: str,
                source_index: Optional[int],
                bucket: Optional[Window],
                phase_source: str,
            ) -> None:
                nonlocal rows_written
                timestamp = sequence * window_s
                source_timestamp = (
                    source_index * window_s if source_index is not None else None
                )
                writer.writerow({
                    "timestamp": f"{timestamp:.6f}",
                    "datetime_iso": datetime.fromtimestamp(
                        timestamp, tz=timezone.utc
                    ).isoformat(),
                    "source_timestamp": (
                        "" if source_timestamp is None else f"{source_timestamp:.6f}"
                    ),
                    "source_datetime_iso": (
                        "" if source_timestamp is None else datetime.fromtimestamp(
                            source_timestamp, tz=timezone.utc
                        ).isoformat()
                    ),
                    "flow_key": key,
                    "observed_bps": f"{observed_bps:.6f}",
                    "label": label,
                    "flow_records": "" if bucket is None else (
                        bucket.normal_records if label == "BENIGN"
                        else bucket.attack_records
                    ),
                    "normal_records": "" if bucket is None else bucket.normal_records,
                    "attack_records": "" if bucket is None else bucket.attack_records,
                    "phase_source": phase_source,
                })
                rows_written += 1

            if label_mode == "separate-phases":
                for raw_series, windows in sorted(series_windows.items()):
                    normal_points = [
                        (index, bucket, bucket.normal_bits / window_s)
                        for index, bucket in sorted(windows.items())
                        if (bucket.normal_records
                            and bucket.normal_bits / window_s >= min_window_bps)
                    ]
                    attack_points = [
                        (index, bucket, bucket.attack_bits / window_s)
                        for index, bucket in sorted(windows.items())
                        if (bucket.attack_records
                            and bucket.attack_bits / window_s >= min_window_bps)
                    ]
                    if len(normal_points) < min_series_windows and not attack_points:
                        continue

                    key = output_key(raw_series)
                    sequence = 0
                    if attack_points and len(normal_points) < min_series_windows:
                        for _ in range(baseline_prefix_windows):
                            emit(
                                sequence,
                                key,
                                baseline_bps,
                                "BENIGN",
                                None,
                                None,
                                "synthetic_baseline",
                            )
                            sequence += 1
                    else:
                        for source_index, bucket, rate_bps in normal_points:
                            emit(
                                sequence,
                                key,
                                rate_bps,
                                "BENIGN",
                                source_index,
                                bucket,
                                "observed",
                            )
                            sequence += 1
                    for source_index, bucket, rate_bps in attack_points:
                        emit(
                            sequence,
                            key,
                            rate_bps,
                            "ATTACK",
                            source_index,
                            bucket,
                            "observed",
                        )
                        sequence += 1
            elif label_mode == "mixed-windows":
                for raw_series, windows in sorted(series_windows.items()):
                    sequence = 0
                    key = output_key(raw_series)
                    for source_index, bucket in sorted(windows.items()):
                        rate_bps = bucket.bits / window_s
                        if rate_bps < min_window_bps:
                            continue
                        label = "ATTACK" if bucket.attack_records else "BENIGN"
                        emit(
                            sequence,
                            key,
                            rate_bps,
                            label,
                            source_index,
                            bucket,
                            "observed_mixed",
                        )
                        sequence += 1
            else:
                raise ValueError(f"label_mode inválido: {label_mode}")
            if rows_written == 0:
                raise ValueError("nenhuma série possui janelas suficientes para a saída")
        os.replace(temporary_name, target)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    return target


def write_metadata(output: Path, summary: Dict[str, object], config: Dict[str, object]) -> Path:
    target = Path(f"{output}.metadata.json")
    payload = {
        "schema_version": 1,
        "preparation_tool": "prepare_cicddos2019.py",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "output_file": output.name,
        "summary": summary,
        "config": config,
    }
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_name, target)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Agrega CSVs CIC-DDoS2019 em janelas de vazão compatíveis com o "
            "FlowPredictor, usando memória proporcional ao número de janelas."
        )
    )
    parser.add_argument("inputs", nargs="+", help="arquivos CSV CIC-DDoS2019")
    parser.add_argument("--output", required=True, help="CSV compacto de saída")
    parser.add_argument("--window-s", type=float, default=2.0,
                        help="tamanho da janela em segundos (padrão: 2)")
    parser.add_argument("--normal-label", action="append", dest="normal_labels",
                        help="rótulo benigno; pode ser repetido (padrão: BENIGN)")
    parser.add_argument("--attack-label", action="append", dest="attack_labels",
                        help="inclui somente este ataque; pode ser repetido")
    parser.add_argument(
        "--attack-inbound-only",
        action="store_true",
        help="mantém somente a direção de entrada dos registros de ataque",
    )
    parser.add_argument("--series-key", default="cicddos2019:udp",
                        help="prefixo dos identificadores de série")
    parser.add_argument(
        "--series-mode",
        choices=("endpoint-pair", "aggregate"),
        default="endpoint-pair",
        help="agrupa por par IP (recomendado) ou todo o tráfego em uma série",
    )
    parser.add_argument("--date-order", choices=("day-first", "month-first"),
                        default="day-first", help="ordem de datas com barras")
    parser.add_argument(
        "--label-mode",
        choices=("separate-phases", "mixed-windows"),
        default="separate-phases",
        help=(
            "separate-phases cria baseline seguido de ataque; mixed-windows "
            "preserva a coexistência temporal original"
        ),
    )
    parser.add_argument("--progress-every", type=int, default=1_000_000,
                        help="mostra progresso a cada N linhas; 0 desabilita")
    parser.add_argument("--baseline-prefix-windows", type=int, default=10,
                        help="baseline benigno anteposto a séries somente de ataque")
    parser.add_argument("--min-series-windows", type=int, default=2,
                        help="mínimo de janelas para manter uma série benigna")
    parser.add_argument("--min-window-bps", type=float, default=1.0,
                        help="descarta janelas abaixo desta vazão")
    parser.add_argument("--force", action="store_true",
                        help="substitui o CSV de saída se ele já existir")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        series_windows, summary = aggregate_cic_files(
            args.inputs,
            window_s=args.window_s,
            normal_labels=args.normal_labels or ["BENIGN"],
            attack_labels=args.attack_labels or [],
            attack_inbound_only=args.attack_inbound_only,
            series_mode=args.series_mode,
            date_order=args.date_order,
            progress_every=args.progress_every,
        )
        output = write_compact_csv(
            args.output,
            series_windows,
            args.window_s,
            args.series_key,
            label_mode=args.label_mode,
            baseline_prefix_windows=args.baseline_prefix_windows,
            min_series_windows=args.min_series_windows,
            min_window_bps=args.min_window_bps,
            force=args.force,
        )
        metadata_output = write_metadata(output, summary, {
            "window_s": args.window_s,
            "normal_labels": args.normal_labels or ["BENIGN"],
            "attack_labels": args.attack_labels or [],
            "attack_inbound_only": args.attack_inbound_only,
            "series_mode": args.series_mode,
            "series_key": args.series_key,
            "date_order": args.date_order,
            "label_mode": args.label_mode,
            "baseline_prefix_windows": args.baseline_prefix_windows,
            "min_series_windows": args.min_series_windows,
            "min_window_bps": args.min_window_bps,
        })
    except ValueError as exc:
        parser.error(str(exc))

    print(f"output: {output}")
    print(f"metadata: {metadata_output}")
    print(
        f"rows: {summary['rows_total']} "
        f"(valid={summary['rows_valid']}, skipped={summary['rows_skipped']}, "
        f"filtered={summary['rows_filtered']}, "
        f"normal={summary['rows_normal']}, attack={summary['rows_attack']})"
    )
    print(f"series: {summary['series']} windows: {summary['windows']} "
          f"interval_s={summary['window_s']}")
    print(f"compact_bytes: {output.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
