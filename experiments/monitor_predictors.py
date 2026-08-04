#!/usr/bin/env python3
"""Captura uma linha do tempo compacta das APIs do FlowPredictor."""

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


def fetch_json(url: str, timeout_s: float = 2.0) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.loads(response.read().decode("utf-8"))


def flow_anomalies(payload: Dict[str, Any], flow: str) -> List[Dict[str, Any]]:
    src, dst = flow.split("->", 1)
    return [
        row for row in payload.get("anomalies", [])
        if row.get("meta", {}).get("nw_src") == src
        and row.get("meta", {}).get("nw_dst") == dst
    ]


def flow_predictions(payload: Dict[str, Any], flow: str) -> List[Dict[str, Any]]:
    src, dst = flow.split("->", 1)
    return [
        row for row in payload.get("predictions", [])
        if row.get("meta", {}).get("nw_src") == src
        and row.get("meta", {}).get("nw_dst") == dst
    ]


def endpoint_port(endpoint: str) -> str:
    return endpoint.rstrip("/").rsplit(":", 1)[-1]


def capture_endpoint(endpoint: str, flow: str) -> Dict[str, Any]:
    sampled_ns = time.time_ns()
    try:
        status = fetch_json(f"{endpoint}/predictor/status")
        collaboration = fetch_json(f"{endpoint}/predictor/collaboration")
        anomalies = fetch_json(f"{endpoint}/predictor/anomalies?limit=500")
        predictions = fetch_json(f"{endpoint}/predictor/predictions?top=500")
        return {
            "sampled_ns": sampled_ns,
            "endpoint": endpoint,
            "port": endpoint_port(endpoint),
            "status": {
                "cid": status.get("cid"),
                "anomalies_recorded": status.get("anomalies_recorded"),
                "anomalies_suppressed": status.get("anomalies_suppressed"),
                "collaboration": status.get("collaboration"),
            },
            "collaboration": collaboration,
            "anomalies": flow_anomalies(anomalies, flow),
            "predictions": flow_predictions(predictions, flow),
        }
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return {
            "sampled_ns": sampled_ns,
            "endpoint": endpoint,
            "port": endpoint_port(endpoint),
            "error": str(exc),
        }


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def monitor(endpoints: List[str], flow: str, output_dir: Path,
            interval_s: float, duration_s: float,
            stop_file: Optional[Path] = None) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for endpoint in endpoints:
        port = endpoint_port(endpoint)
        for resource in ("status", "model", "collaboration"):
            try:
                write_json(
                    output_dir / f"initial-{resource}-{port}.json",
                    fetch_json(f"{endpoint}/predictor/{resource}"),
                )
            except (OSError, ValueError, urllib.error.URLError) as exc:
                write_json(
                    output_dir / f"initial-{resource}-{port}.json",
                    {"error": str(exc)},
                )

    deadline = time.monotonic() + duration_s
    timeline_path = output_dir / "timeline.ndjson"
    with timeline_path.open("w", encoding="utf-8") as timeline:
        while True:
            for endpoint in endpoints:
                timeline.write(json.dumps(
                    capture_endpoint(endpoint, flow),
                    sort_keys=True,
                    ensure_ascii=False,
                ) + "\n")
            timeline.flush()
            if time.monotonic() >= deadline or (stop_file and stop_file.exists()):
                break
            time.sleep(interval_s)

    for endpoint in endpoints:
        port = endpoint_port(endpoint)
        final = capture_endpoint(endpoint, flow)
        write_json(output_dir / f"final-{port}.json", final)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoints", default="http://127.0.0.1:6060,http://127.0.0.1:6061")
    parser.add_argument("--flow", default="10.0.0.1->10.0.0.8")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-s", type=float, default=0.5)
    parser.add_argument("--duration-s", type=float, default=60.0)
    parser.add_argument("--stop-file", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.interval_s <= 0 or args.duration_s <= 0:
        raise SystemExit("interval-s e duration-s devem ser positivos")
    endpoints = [item.strip().rstrip("/") for item in args.endpoints.split(",") if item.strip()]
    if not endpoints:
        raise SystemExit("ao menos um endpoint é obrigatório")
    if "->" not in args.flow:
        raise SystemExit("flow deve usar o formato src->dst")
    monitor(
        endpoints,
        args.flow,
        args.output,
        args.interval_s,
        args.duration_s,
        args.stop_file,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
