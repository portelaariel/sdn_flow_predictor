#!/usr/bin/env python3
"""Execute a benchmark workload through the Mininet Python API."""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eMSN_ENV.setup_mininet import build_network  # noqa: E402


LOSS_PATTERN = re.compile(r"([0-9]+(?:\.[0-9]+)?)% packet loss")


class WorkloadError(RuntimeError):
    """Raised when the workload cannot produce a trustworthy measurement."""


def packet_loss(text: str) -> Optional[float]:
    match = LOSS_PATTERN.search(text)
    return float(match.group(1)) if match else None


def write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def run_host(host: Any, command: list[str]) -> Tuple[str, str, int]:
    """Run a command inside a Mininet host and preserve stdout/stderr/status."""
    stdout, stderr, status = host.pexec(command)
    return stdout or "", stderr or "", int(status)


def wait_for_switches(net: Any, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    disconnected = [switch.name for switch in net.switches]
    while time.monotonic() < deadline:
        disconnected = [switch.name for switch in net.switches
                        if not switch.connected()]
        if not disconnected:
            return
        time.sleep(0.5)
    raise WorkloadError(
        "switches sem conexão OpenFlow: " + ", ".join(disconnected)
    )


def wait_for_data_plane(source: Any, destination_ip: str, output: Path,
                        attempts: int, interval_s: float) -> float:
    records = []
    last_loss = None
    for attempt in range(1, attempts + 1):
        stdout, stderr, status = run_host(
            source, ["ping", "-c", "3", "-W", "2", destination_ip]
        )
        combined = stdout + stderr
        loss = packet_loss(combined)
        last_loss = loss
        records.append(
            f"=== tentativa {attempt} status={status} ===\n{combined}"
        )
        if loss is not None and loss < 100.0:
            write_text(output, "\n".join(records))
            return loss
        time.sleep(interval_s)
    write_text(output, "\n".join(records))
    raise WorkloadError(
        "plano de dados indisponível após "
        f"{attempts} tentativas (última perda={last_loss})"
    )


def run_iperf(source: Any, destination_ip: str, rate: str, duration_s: int,
              output_json: Path, output_stderr: Path) -> Tuple[int, Optional[float]]:
    stdout, stderr, status = run_host(source, [
        "iperf3", "-c", destination_ip, "-u", "-b", rate,
        "-t", str(duration_s), "-J",
    ])
    write_text(output_json, stdout)
    write_text(output_stderr, stderr)
    bits_per_second = None
    try:
        payload = json.loads(stdout)
        end = payload.get("end", {})
        for key in ("sum", "sum_received", "sum_sent"):
            value = end.get(key, {}).get("bits_per_second")
            if isinstance(value, (int, float)):
                bits_per_second = float(value)
                break
    except (TypeError, ValueError):
        pass
    return status, bits_per_second


def dump_flows(net: Any, output: Path) -> None:
    for switch in net.switches:
        result = subprocess.run(
            ["ovs-ofctl", "-O", "OpenFlow10", "dump-flows", switch.name],
            check=False,
            capture_output=True,
            text=True,
        )
        write_text(
            output / f"ovs-flows-{switch.name}.txt",
            result.stdout + result.stderr,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csets", type=int, required=True)
    parser.add_argument("--sper", type=int, required=True)
    parser.add_argument("--source-host", required=True)
    parser.add_argument("--destination-host", required=True)
    parser.add_argument("--destination-ip", required=True)
    parser.add_argument("--scenario", choices=("benign", "ddos"), required=True)
    parser.add_argument("--baseline-rate", required=True)
    parser.add_argument("--attack-rate", required=True)
    parser.add_argument("--baseline-duration-s", type=int, required=True)
    parser.add_argument("--attack-duration-s", type=int, required=True)
    parser.add_argument("--settle-s", type=int, required=True)
    parser.add_argument("--controller-timeout-s", type=float, default=30.0)
    parser.add_argument("--connectivity-attempts", type=int, default=8)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    status: Dict[str, Any] = {
        "valid": False,
        "reason": None,
        "switches_connected": False,
        "ping_before_loss_percent": None,
        "baseline_exit_code": None,
        "baseline_bps": None,
        "attack_exit_code": None,
        "attack_bps": None,
        "ping_after_loss_percent": None,
    }
    net = None
    server = None
    server_log = None
    try:
        net = build_network(args.csets, args.sper)
        wait_for_switches(net, args.controller_timeout_s)
        status["switches_connected"] = True

        source = net.get(args.source_host)
        destination = net.get(args.destination_host)
        status["ping_before_loss_percent"] = wait_for_data_plane(
            source,
            args.destination_ip,
            args.output / "ping_before.txt",
            args.connectivity_attempts,
            2.0,
        )

        server_log = (args.output / "iperf-server.log").open("wb")
        server = destination.popen(
            ["iperf3", "-s"], stdout=server_log, stderr=subprocess.STDOUT
        )
        time.sleep(1.0)
        if server.poll() is not None:
            raise WorkloadError("servidor iperf3 encerrou antes do baseline")

        write_text(args.output / "baseline_start_ns.txt", f"{time.time_ns()}\n")
        baseline_status, baseline_bps = run_iperf(
            source,
            args.destination_ip,
            args.baseline_rate,
            args.baseline_duration_s,
            args.output / "baseline.json",
            args.output / "baseline.stderr",
        )
        write_text(args.output / "baseline_end_ns.txt", f"{time.time_ns()}\n")
        status["baseline_exit_code"] = baseline_status
        status["baseline_bps"] = baseline_bps
        if baseline_status != 0 or baseline_bps is None:
            raise WorkloadError(
                f"baseline iperf3 inválido (status={baseline_status})"
            )

        if args.scenario == "ddos":
            time.sleep(2.0)
            write_text(args.output / "attack_start_ns.txt", f"{time.time_ns()}\n")
            attack_status, attack_bps = run_iperf(
                source,
                args.destination_ip,
                args.attack_rate,
                args.attack_duration_s,
                args.output / "attack.json",
                args.output / "attack.stderr",
            )
            write_text(args.output / "attack_end_ns.txt", f"{time.time_ns()}\n")
            status["attack_exit_code"] = attack_status
            status["attack_bps"] = attack_bps
            if attack_status != 0 or attack_bps is None:
                raise WorkloadError(
                    f"ataque iperf3 inválido (status={attack_status})"
                )

        time.sleep(args.settle_s)
        stdout, stderr, ping_status = run_host(
            source, ["ping", "-c", "5", "-W", "2", args.destination_ip]
        )
        ping_output = stdout + stderr
        write_text(args.output / "ping_after.txt", ping_output)
        status["ping_after_loss_percent"] = packet_loss(ping_output)
        if status["ping_after_loss_percent"] is None:
            raise WorkloadError(
                f"ping final sem métrica de perda (status={ping_status})"
            )

        status["valid"] = True
        return_code = 0
    except Exception as exc:  # keep artifacts from partial experimental runs
        status["reason"] = str(exc)
        print(f"workload inválido: {exc}", file=sys.stderr)
        return_code = 1
    finally:
        if net is not None:
            dump_flows(net, args.output)
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=3)
            except subprocess.TimeoutExpired:
                server.kill()
        if server_log is not None:
            server_log.close()
        if net is not None:
            net.stop()
        write_text(
            args.output / "workload_status.json",
            json.dumps(status, indent=2, sort_keys=True) + "\n",
        )
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
