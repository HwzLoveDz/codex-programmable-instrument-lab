#!/usr/bin/env python3
"""Safe USB-serial acquisition helper for tinySA Ultra+ ZS407.

Live access requires pyserial. Parsing, comparison, and unit tests do not.
The helper never enables RF or calibration output.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Protocol


PROMPT_RE = re.compile(r"(?:^|\r?\n)ch>\s*$")
PHASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
QUIRK_NUMBER_RE = re.compile(
    r"^(?P<sign>[+-]?)(?P<lead>[\x3a-\x3f])(?P<tail>\.\d+)(?P<exp>[eE][+-]?\d+)$"
)


class SerialLike(Protocol):
    def reset_input_buffer(self) -> None: ...
    def write(self, data: bytes) -> int: ...
    def read(self, size: int = 1) -> bytes: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class ScanPoint:
    frequency_hz: int
    measured_dbm: float
    firmware_extra_value: float | None = None


def parse_tinysa_number(text: str) -> float:
    """Parse normal floats and the observed ZS407 text-scan formatter quirk.

    Firmware tinySA4_v1.4-217-gc5dd31f emitted ``-:.000000e+01`` for
    -100 dBm. Characters ':' through '?' represent leading values 10..15.
    """
    try:
        value = float(text)
    except ValueError:
        match = QUIRK_NUMBER_RE.fullmatch(text)
        if not match:
            raise ValueError(f"unsupported tinySA numeric value: {text!r}") from None
        lead = ord(match.group("lead")) - ord("0")
        normalized = (
            f"{match.group('sign')}{lead}{match.group('tail')}{match.group('exp')}"
        )
        value = float(normalized)
    if not math.isfinite(value):
        raise ValueError(f"non-finite tinySA numeric value: {text!r}")
    return value


def validate_command(command: str) -> str:
    if not command or "\r" in command or "\n" in command:
        raise ValueError("tinySA command must be one non-empty line")
    return command


def parse_scan_response(
    response: str, *, command: str, expected_points: int
) -> list[ScanPoint]:
    rows: list[ScanPoint] = []
    malformed: list[str] = []
    for source_line in response.splitlines():
        line = source_line.strip()
        if not line or line == command or line.startswith("ch>"):
            continue
        parts = line.split()
        if len(parts) not in (2, 3) or not parts[0].isdigit():
            malformed.append(line)
            continue
        try:
            row = ScanPoint(
                frequency_hz=int(parts[0]),
                measured_dbm=parse_tinysa_number(parts[1]),
                firmware_extra_value=(
                    parse_tinysa_number(parts[2]) if len(parts) == 3 else None
                ),
            )
        except ValueError:
            malformed.append(line)
            continue
        rows.append(row)
    if malformed or len(rows) != expected_points:
        details = " | ".join(malformed[:3])
        raise ValueError(
            f"malformed scan response: parsed {len(rows)}/{expected_points}; {details}"
        )
    frequencies = [row.frequency_hz for row in rows]
    if any(right <= left for left, right in zip(frequencies, frequencies[1:])):
        raise ValueError("scan frequencies are not strictly increasing")
    return rows


class TinySASerial:
    def __init__(
        self,
        port: SerialLike,
        *,
        timeout: float = 20.0,
        max_response_bytes: int = 2_000_000,
    ) -> None:
        self.port = port
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.commands: list[dict[str, object]] = []

    def close(self) -> None:
        self.port.close()

    def command(self, command: str) -> str:
        command = validate_command(command)
        self.port.reset_input_buffer()
        started = time.monotonic()
        self.port.write((command + "\r").encode("ascii"))
        payload = bytearray()
        deadline = started + self.timeout
        while time.monotonic() < deadline:
            chunk = self.port.read(4096)
            if chunk:
                payload.extend(chunk)
                if len(payload) > self.max_response_bytes:
                    raise ValueError("tinySA response exceeded size limit")
                text = payload.decode("ascii", errors="strict")
                if PROMPT_RE.search(text):
                    self.commands.append(
                        {
                            "time_utc": datetime.now(timezone.utc).isoformat(),
                            "command": command,
                            "response_bytes": len(payload),
                            "elapsed_s": round(time.monotonic() - started, 6),
                            "status": "ok",
                        }
                    )
                    return text
        self.commands.append(
            {
                "time_utc": datetime.now(timezone.utc).isoformat(),
                "command": command,
                "response_bytes": len(payload),
                "elapsed_s": round(time.monotonic() - started, 6),
                "status": "timeout",
            }
        )
        raise TimeoutError(f"timeout waiting for prompt after {command!r}")

    def identify(self) -> str:
        return self.command("info")

    def safe_pause(self, *, authorized: bool) -> None:
        if not authorized:
            raise PermissionError("safe_pause requires explicit state-change confirmation")
        for command in ("output off", "caloutput off", "pause"):
            self.command(command)

    def scan(self, start_hz: int, stop_hz: int, points: int) -> tuple[str, list[ScanPoint]]:
        if not (100_000 <= start_hz < stop_hz):
            raise ValueError("scan requires 100000 <= start_hz < stop_hz")
        if not (2 <= points <= 290):
            raise ValueError("text scan supports 2..290 points")
        command = f"scan {start_hz} {stop_hz} {points} 3"
        raw = self.command(command)
        return raw, parse_scan_response(raw, command=command, expected_points=points)


def open_serial(port_name: str, baud: int, timeout: float) -> SerialLike:
    try:
        import serial  # type: ignore
    except ImportError as exc:
        raise RuntimeError("live tinySA access requires pyserial: python -m pip install pyserial") from exc
    return serial.Serial(
        port=port_name,
        baudrate=baud,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=min(timeout, 0.1),
        write_timeout=1.0,
    )


def redact_info(info: str) -> dict[str, str]:
    lines = [line.strip() for line in info.splitlines() if line.strip()]
    model = next((line for line in lines if "tinySA" in line), "unparsed")
    firmware = next(
        (
            line
            for line in lines
            if line.startswith("Firmware Version") or line.startswith("Version:")
        ),
        "unparsed",
    )
    build = next((line for line in lines if line.startswith("Build Time")), "unparsed")
    return {"model": model, "firmware": firmware, "build": build, "identifiers": "[REDACTED]"}


def require_zs407(info: str) -> None:
    """Fail closed before state changes when the attached model is not ZS407."""
    normalized = " ".join(info.upper().split())
    if "TINYSA ULTRA+ ZS407" not in normalized:
        raise RuntimeError("attached instrument did not identify as tinySA ULTRA+ ZS407")


def write_new(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as handle:
        handle.write(data)


def write_json_new(path: Path, data: object) -> None:
    write_new(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def scan_run(args: argparse.Namespace) -> int:
    if not args.port:
        raise ValueError("live ZS407 actions require --port from current enumeration")
    if not args.confirm_input_only:
        raise PermissionError("confirm the probe is connected only to INPUT and RF outputs are disconnected")
    if not args.confirm_state_changes:
        raise PermissionError("confirm output-off, calibration-output-off, and pause state changes")
    if not PHASE_RE.fullmatch(args.phase):
        raise ValueError("phase must be a short filesystem-safe label")
    if not (100_000 <= args.start_hz < args.stop_hz):
        raise ValueError("scan requires 100000 <= start_hz < stop_hz")
    if not (2 <= args.points <= 290):
        raise ValueError("text scan supports 2..290 points")
    if args.warmup_scans < 0 or args.repeats < 1 or args.settle_seconds < 0:
        raise ValueError("warmup scans, repeats, and settle time must be non-negative; repeats must be at least 1")
    if not math.isfinite(args.external_attenuation_db) or args.external_attenuation_db < 0:
        raise ValueError("external attenuation must be a finite non-negative value")
    out = Path(args.out)
    expected = [out / f"{args.phase}-scan-{index:02d}.txt" for index in range(1, args.repeats + 1)]
    expected += [out / f"{args.phase}-traces.csv", out / f"{args.phase}-summary.json"]
    if any(path.exists() for path in expected):
        raise FileExistsError("phase already exists; use a new label instead of overwriting evidence")

    port = open_serial(args.port, args.baud, args.timeout)
    device = TinySASerial(port, timeout=args.timeout)
    started = datetime.now(timezone.utc).isoformat()
    try:
        info = device.identify()
        require_zs407(info)
        device.safe_pause(authorized=True)
        if args.settle_seconds:
            time.sleep(args.settle_seconds)
        for _ in range(args.warmup_scans):
            device.scan(args.start_hz, args.stop_hz, args.points)
        raw_scans: list[str] = []
        scans: list[list[ScanPoint]] = []
        for _ in range(args.repeats):
            raw, points = device.scan(args.start_hz, args.stop_hz, args.points)
            raw_scans.append(raw)
            scans.append(points)
    finally:
        device.close()

    out.mkdir(parents=True, exist_ok=True)
    private_info = out / "tinysa_info_private.txt"
    if not private_info.exists():
        write_new(private_info, info)
    for index, raw in enumerate(raw_scans, 1):
        write_new(out / f"{args.phase}-scan-{index:02d}.txt", raw)

    fieldnames = ["frequency_hz"] + [f"trace{index}_dbm" for index in range(1, args.repeats + 1)]
    fieldnames += ["median_dbm_at_analyzer", "max_dbm_at_analyzer"]
    csv_path = out / f"{args.phase}-traces.csv"
    with csv_path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for point_index in range(args.points):
            levels = [scan[point_index].measured_dbm for scan in scans]
            row: dict[str, object] = {"frequency_hz": scans[0][point_index].frequency_hz}
            row.update({f"trace{index}_dbm": value for index, value in enumerate(levels, 1)})
            row["median_dbm_at_analyzer"] = statistics.median(levels)
            row["max_dbm_at_analyzer"] = max(levels)
            writer.writerow(row)

    summary = {
        "phase": args.phase,
        "operator_declared_dut_state": args.operator_dut_state,
        "invalidates_phase": args.invalidates_phase,
        "started_utc": started,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "instrument": redact_info(info),
        "transport": {"type": "USB serial", "port": args.port, "baud": args.baud},
        "scan": {
            "start_hz": args.start_hz,
            "stop_hz": args.stop_hz,
            "points": args.points,
            "warmup_scans": args.warmup_scans,
            "repeats": args.repeats,
            "screen_state_after_run": "paused",
        },
        "fixture": {
            "external_attenuation_db_nominal": args.external_attenuation_db,
            "probe": args.probe,
            "probe_position": args.probe_position,
        },
        "interpretation": "Relative near-field data only; not regulatory field strength or pass/fail evidence.",
    }
    write_json_new(out / f"{args.phase}-summary.json", summary)
    with (out / "commands.jsonl").open("a", encoding="utf-8", newline="") as handle:
        for entry in device.commands:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def compare_runs(args: argparse.Namespace) -> int:
    def load(path: str) -> list[dict[str, str]]:
        with Path(path).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def level(row: dict[str, str]) -> float:
        value = row.get("median_dbm_at_analyzer")
        if value in (None, ""):
            value = row.get("level_dbm_at_analyzer")
        if value in (None, ""):
            raise ValueError("CSV lacks median_dbm_at_analyzer or level_dbm_at_analyzer")
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("CSV contains a non-finite level")
        return parsed

    off_rows, on_rows = load(args.off_csv), load(args.on_csv)
    if args.exclude_edge_bins < 0:
        raise ValueError("exclude_edge_bins must be non-negative")
    if args.top < 1:
        raise ValueError("top must be at least 1")
    if len(off_rows) != len(on_rows):
        raise ValueError("scan point counts differ")
    if not off_rows:
        raise ValueError("scan files are empty")
    if args.exclude_edge_bins * 2 >= len(off_rows):
        raise ValueError("edge exclusion removes every analysis row")
    output_rows: list[dict[str, object]] = []
    for index, (off, on) in enumerate(zip(off_rows, on_rows)):
        off_frequency, on_frequency = int(off["frequency_hz"]), int(on["frequency_hz"])
        if off_frequency != on_frequency:
            raise ValueError(f"frequency mismatch at row {index}")
        off_level = level(off)
        on_level = level(on)
        output_rows.append(
            {
                "frequency_hz": off_frequency,
                "off_median_dbm": off_level,
                "on_median_dbm": on_level,
                "delta_db": round(on_level - off_level, 6),
            }
        )
    end = len(output_rows) - args.exclude_edge_bins if args.exclude_edge_bins else None
    analysis_rows = output_rows[args.exclude_edge_bins:end]
    top = sorted(analysis_rows, key=lambda row: float(row["delta_db"]), reverse=True)[: args.top]
    out = Path(args.out)
    if out.exists():
        raise FileExistsError("comparison output exists")
    with out.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output_rows[0]))
        writer.writeheader()
        writer.writerows(output_rows)
    summary = {
        "off_csv": args.off_csv,
        "on_csv": args.on_csv,
        "exclude_edge_bins": args.exclude_edge_bins,
        "top_deltas": top,
        "interpretation": "Exploratory relative comparison only; no compliance pass/fail criterion was applied.",
    }
    write_json_new(out.with_suffix(".json"), summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="currently enumerated USB serial port (required for live actions)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=20.0)
    subparsers = parser.add_subparsers(dest="action", required=True)

    identify = subparsers.add_parser("identify", help="read and redact the info response")
    identify.set_defaults(func=lambda args: identify_run(args))

    safe = subparsers.add_parser("safe-pause", help="disable outputs and pause continuous sweeping")
    safe.add_argument("--confirm-state-changes", action="store_true")
    safe.set_defaults(func=lambda args: safe_pause_run(args))

    scan = subparsers.add_parser("scan", help="run an evidence-preserving one-shot scan")
    scan.add_argument("--out", required=True)
    scan.add_argument("--phase", required=True)
    scan.add_argument("--start-hz", type=int, required=True)
    scan.add_argument("--stop-hz", type=int, required=True)
    scan.add_argument("--points", type=int, default=290)
    scan.add_argument("--warmup-scans", type=int, default=1)
    scan.add_argument("--repeats", type=int, default=2)
    scan.add_argument("--settle-seconds", type=float, default=0.0)
    scan.add_argument("--external-attenuation-db", type=float, required=True)
    scan.add_argument("--probe", required=True)
    scan.add_argument("--probe-position", required=True)
    scan.add_argument("--operator-dut-state", required=True)
    scan.add_argument("--invalidates-phase")
    scan.add_argument("--confirm-input-only", action="store_true")
    scan.add_argument("--confirm-state-changes", action="store_true")
    scan.set_defaults(func=scan_run)

    compare = subparsers.add_parser("compare", help="compare matched off/on phase CSV files")
    compare.add_argument("--off-csv", required=True)
    compare.add_argument("--on-csv", required=True)
    compare.add_argument("--out", required=True)
    compare.add_argument("--exclude-edge-bins", type=int, default=1)
    compare.add_argument("--top", type=int, default=20)
    compare.set_defaults(func=compare_runs)
    return parser


def identify_run(args: argparse.Namespace) -> int:
    if not args.port:
        raise ValueError("identify requires --port from current enumeration")
    device = TinySASerial(open_serial(args.port, args.baud, args.timeout), timeout=args.timeout)
    try:
        info = device.identify()
        require_zs407(info)
        print(json.dumps(redact_info(info), indent=2, ensure_ascii=False))
    finally:
        device.close()
    return 0


def safe_pause_run(args: argparse.Namespace) -> int:
    if not args.port:
        raise ValueError("safe-pause requires --port from current enumeration")
    if not args.confirm_state_changes:
        raise PermissionError("safe_pause requires explicit state-change confirmation")
    device = TinySASerial(open_serial(args.port, args.baud, args.timeout), timeout=args.timeout)
    try:
        require_zs407(device.identify())
        device.safe_pause(authorized=args.confirm_state_changes)
    finally:
        device.close()
    print("tinySA outputs disabled; continuous sweep paused")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
