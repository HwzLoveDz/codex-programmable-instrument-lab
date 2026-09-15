#!/usr/bin/env python3
"""Constrained SAA2 USB adapter for LiteVNA 64 ZN-406 bring-up."""

from __future__ import annotations

import argparse
import csv
import json
import math
import struct
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


READ1 = 0x10
READFIFO = 0x18
WRITE1 = 0x20
WRITE2 = 0x21
WRITE8 = 0x23
INDICATE = 0x0D

REG_SWEEP_START = 0x00
REG_SWEEP_STEP = 0x10
REG_SWEEP_POINTS = 0x20
REG_VALUES_PER_FREQUENCY = 0x22
REG_RAW_SAMPLES_MODE = 0x26
REG_VALUES_FIFO = 0x30
REG_LOW_POWER = 0x41
REG_HIGH_POWER = 0x42
REG_CHANNEL_SELECT = 0x44
REG_DEVICE_VARIANT = 0xF0
REG_PROTOCOL_VERSION = 0xF1
REG_HARDWARE_REVISION = 0xF2
REG_FIRMWARE_MAJOR = 0xF3
REG_FIRMWARE_MINOR = 0xF4

VALUE_STRUCT = struct.Struct("<iiiiiiH6x")


class SerialLike(Protocol):
    def reset_input_buffer(self) -> None: ...
    def write(self, data: bytes) -> int: ...
    def read(self, size: int = 1) -> bytes: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class Identity:
    device_variant: int
    protocol_version: int
    hardware_revision: int
    firmware_major: int
    firmware_minor: int
    low_frequency_power_code: int
    high_frequency_power_code: int


@dataclass(frozen=True)
class RawValue:
    fwd0_re: int
    fwd0_im: int
    rev0_re: int
    rev0_im: int
    rev1_re: int
    rev1_im: int
    frequency_index: int

    def s11(self) -> complex:
        forward = complex(self.fwd0_re, self.fwd0_im)
        if forward == 0:
            raise ValueError(f"zero forward reference at index {self.frequency_index}")
        return complex(self.rev0_re, self.rev0_im) / forward

    def s21(self) -> complex:
        forward = complex(self.fwd0_re, self.fwd0_im)
        if forward == 0:
            raise ValueError(f"zero forward reference at index {self.frequency_index}")
        return complex(self.rev1_re, self.rev1_im) / forward


class LiteVNA:
    def __init__(self, port: SerialLike, *, timeout: float = 3.0) -> None:
        self.port = port
        self.timeout = timeout
        self.commands: list[dict[str, object]] = []

    def close(self) -> None:
        self.port.close()

    def _write(self, payload: bytes, name: str) -> None:
        written = self.port.write(payload)
        if written != len(payload):
            raise IOError(f"short serial write for {name}")
        self.commands.append({"time_utc": datetime.now(timezone.utc).isoformat(), "name": name})

    def _read_exact(self, length: int) -> bytes:
        deadline = time.monotonic() + self.timeout
        data = bytearray()
        while len(data) < length and time.monotonic() < deadline:
            chunk = self.port.read(length - len(data))
            if chunk:
                data.extend(chunk)
        if len(data) != length:
            raise TimeoutError(f"short LiteVNA response: {len(data)}/{length} bytes")
        return bytes(data)

    def indicate(self) -> None:
        self._write(bytes([INDICATE]), "indicate")
        if self._read_exact(1) != b"2":
            raise ValueError("LiteVNA INDICATE response mismatch")

    def read_u8(self, address: int, name: str) -> int:
        self._write(bytes([READ1, address]), f"read {name}")
        return self._read_exact(1)[0]

    def write_u8(self, address: int, value: int, name: str) -> None:
        self._write(bytes([WRITE1, address, value]), f"write {name}")

    def write_u16(self, address: int, value: int, name: str) -> None:
        self._write(bytes([WRITE2, address]) + struct.pack("<H", value), f"write {name}")

    def write_u64(self, address: int, value: int, name: str) -> None:
        self._write(bytes([WRITE8, address]) + struct.pack("<Q", value), f"write {name}")

    def identify(self) -> Identity:
        self.port.reset_input_buffer()
        self.indicate()
        identity = Identity(
            device_variant=self.read_u8(REG_DEVICE_VARIANT, "device variant"),
            protocol_version=self.read_u8(REG_PROTOCOL_VERSION, "protocol version"),
            hardware_revision=self.read_u8(REG_HARDWARE_REVISION, "hardware revision"),
            firmware_major=self.read_u8(REG_FIRMWARE_MAJOR, "firmware major"),
            firmware_minor=self.read_u8(REG_FIRMWARE_MINOR, "firmware minor"),
            low_frequency_power_code=self.read_u8(REG_LOW_POWER, "low-frequency power code"),
            high_frequency_power_code=self.read_u8(REG_HIGH_POWER, "high-frequency power code"),
        )
        if identity.device_variant != 2 or identity.protocol_version != 1:
            raise RuntimeError("attached device is not a supported LiteVNA SAA2 variant")
        if identity.low_frequency_power_code not in (1, 2, 3):
            raise RuntimeError("LiteVNA low-frequency power code is outside documented range")
        if identity.high_frequency_power_code not in (1, 2, 3):
            raise RuntimeError("LiteVNA high-frequency power code is outside documented range")
        return identity

    def configure_s21_sweep(self, start_hz: int, stop_hz: int, points: int) -> int:
        return self.configure_sweep(start_hz, stop_hz, points, channel=2)

    def configure_sweep(self, start_hz: int, stop_hz: int, points: int, *, channel: int) -> int:
        if not (50_000 <= start_hz < stop_hz <= 6_300_000_000):
            raise ValueError("LiteVNA sweep must remain within 50 kHz to 6.3 GHz")
        if not (2 <= points <= 1024):
            raise ValueError("constrained adapter supports 2..1024 USB points")
        step_hz = (stop_hz - start_hz) // (points - 1)
        if step_hz < 1:
            raise ValueError("sweep step rounded below 1 Hz")
        if channel not in (1, 2):
            raise ValueError("channel must be 1 (S11) or 2 (S21)")
        self.write_u8(REG_CHANNEL_SELECT, channel, f"channel S{channel}1")
        self.write_u64(REG_SWEEP_START, start_hz, "sweep start")
        self.write_u64(REG_SWEEP_STEP, step_hz, "sweep step")
        self.write_u16(REG_SWEEP_POINTS, points, "sweep points")
        self.write_u16(REG_VALUES_PER_FREQUENCY, 1, "values per frequency")
        self.indicate()
        return step_hz

    def acquire_one_sweep(self, points: int) -> list[RawValue]:
        self.write_u8(REG_VALUES_FIFO, 0, "clear values FIFO")
        self.indicate()
        payload = bytearray()
        remaining = points
        while remaining:
            count = min(255, remaining)
            self._write(bytes([READFIFO, REG_VALUES_FIFO, count]), f"read FIFO {count}")
            payload.extend(self._read_exact(count * VALUE_STRUCT.size))
            remaining -= count
        values = [RawValue(*VALUE_STRUCT.unpack_from(payload, offset)) for offset in range(0, len(payload), VALUE_STRUCT.size)]
        indexes = [value.frequency_index for value in values]
        if indexes != list(range(points)):
            raise ValueError(f"LiteVNA FIFO did not return one ordered complete sweep: {indexes[:4]}..{indexes[-4:]}")
        return values

    def restore_normal_mode(self) -> None:
        self.write_u8(REG_RAW_SAMPLES_MODE, 2, "normal mode")
        self.indicate()


def open_serial(port_name: str, timeout: float) -> SerialLike:
    try:
        import serial  # type: ignore
    except ImportError as exc:
        raise RuntimeError("live LiteVNA access requires pyserial") from exc
    return serial.Serial(port=port_name, timeout=min(timeout, 0.1), write_timeout=1.0)


def write_json_new(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def acquire_s21(args: argparse.Namespace) -> int:
    if args.fixture_kind == "thru-reference" and not args.confirm_thru_wiring:
        raise PermissionError("confirm PORT1 and PORT2 are joined only through cables and THRU")
    if args.fixture_kind == "dut" and not args.confirm_dut_wiring:
        raise PermissionError("confirm the passive DUT is connected between the two fixed test cables")
    if args.fixture_kind == "isolation" and not args.confirm_isolation_wiring:
        raise PermissionError("confirm LOAD is on PORT1 and PORT2 is open")
    if not args.confirm_source_sweep:
        raise PermissionError("confirm authorization for the VNA source sweep and USB-mode state changes")
    out = Path(args.out)
    summary_path = out / f"{args.phase}-summary.json"
    csv_path = out / f"{args.phase}-raw-s21.csv"
    commands_path = out / f"{args.phase}-commands.jsonl"
    if any(path.exists() for path in (summary_path, csv_path, commands_path)):
        raise FileExistsError("phase already exists; evidence will not be overwritten")

    device = LiteVNA(open_serial(args.port, args.timeout), timeout=args.timeout)
    configured = False
    started = datetime.now(timezone.utc).isoformat()
    try:
        identity = device.identify()
        step_hz = device.configure_s21_sweep(args.start_hz, args.stop_hz, args.points)
        configured = True
        values = device.acquire_one_sweep(args.points)
    finally:
        try:
            if configured:
                device.restore_normal_mode()
        finally:
            device.close()

    out.mkdir(parents=True, exist_ok=True)
    with csv_path.open("x", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "frequency_hz", "frequency_index", "fwd0_re", "fwd0_im", "rev0_re", "rev0_im",
            "rev1_re", "rev1_im", "raw_s21_real", "raw_s21_imag", "raw_s21_db", "raw_s21_phase_deg",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for value in values:
            s21 = value.s21()
            magnitude = abs(s21)
            writer.writerow(
                {
                    "frequency_hz": args.start_hz + value.frequency_index * step_hz,
                    **asdict(value),
                    "raw_s21_real": s21.real,
                    "raw_s21_imag": s21.imag,
                    "raw_s21_db": 20 * math.log10(magnitude) if magnitude else "-inf",
                    "raw_s21_phase_deg": math.degrees(math.atan2(s21.imag, s21.real)),
                }
            )
    summary = {
        "phase": args.phase,
        "fixture_kind": args.fixture_kind,
        "purpose": (
            "uncalibrated THRU reference for a later relative S21 ratio"
            if args.fixture_kind == "thru-reference"
            else (
                "raw isolation evidence for later host-side transmission correction"
                if args.fixture_kind == "isolation"
                else "uncalibrated passive-DUT trace for a relative S21 ratio against matched THRU"
            )
        ),
        "started_utc": started,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "instrument": {**asdict(identity), "operator_model_label": "LiteVNA 64 ZN-406"},
        "transport": {"type": "USB CDC / SAA2 binary", "port": args.port},
        "sweep": {
            "requested_start_hz": args.start_hz,
            "requested_stop_hz": args.stop_hz,
            "actual_start_hz": args.start_hz,
            "actual_stop_hz": args.start_hz + step_hz * (args.points - 1),
            "step_hz": step_hz,
            "points": args.points,
            "channel": "S21",
        },
        "calibration": "none; raw complex S21 only",
        "state_after_run": "normal mode requested and serial port closed",
        "interpretation": (
            "Reference trace only; not a calibrated insertion-loss result."
            if args.fixture_kind == "thru-reference"
            else (
                "Isolation trace only; use with a matched THRU trace before correcting S21."
                if args.fixture_kind == "isolation"
                else "Passive-DUT trace only; calculate relative S21 against the matched THRU reference."
            )
        ),
    }
    write_json_new(summary_path, summary)
    with commands_path.open("x", encoding="utf-8", newline="") as handle:
        for command in device.commands:
            handle.write(json.dumps(command, ensure_ascii=False) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def acquire_cal_standard(args: argparse.Namespace) -> int:
    if args.standard not in ("open", "short", "load"):
        raise ValueError("this command is restricted to one-port OPEN/SHORT/LOAD acquisition")
    if not args.confirm_standard_wiring:
        expected = "PORT1 is bare open" if args.standard == "open" else f"the {args.standard.upper()} standard is connected at PORT1"
        raise PermissionError(f"confirm {expected}")
    if not args.confirm_source_sweep:
        raise PermissionError("confirm authorization for the VNA source sweep and USB-mode state changes")
    out = Path(args.out)
    summary_path = out / f"{args.phase}-summary.json"
    csv_path = out / f"{args.phase}-raw-s11.csv"
    commands_path = out / f"{args.phase}-commands.jsonl"
    if any(path.exists() for path in (summary_path, csv_path, commands_path)):
        raise FileExistsError("phase already exists; evidence will not be overwritten")

    device = LiteVNA(open_serial(args.port, args.timeout), timeout=args.timeout)
    configured = False
    started = datetime.now(timezone.utc).isoformat()
    try:
        identity = device.identify()
        step_hz = device.configure_sweep(args.start_hz, args.stop_hz, args.points, channel=1)
        configured = True
        values = device.acquire_one_sweep(args.points)
    finally:
        try:
            if configured:
                device.restore_normal_mode()
        finally:
            device.close()

    out.mkdir(parents=True, exist_ok=True)
    with csv_path.open("x", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "frequency_hz", "frequency_index", "fwd0_re", "fwd0_im", "rev0_re", "rev0_im",
            "rev1_re", "rev1_im", "raw_s11_real", "raw_s11_imag", "raw_s11_db", "raw_s11_phase_deg",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for value in values:
            s11 = value.s11()
            magnitude = abs(s11)
            writer.writerow({
                "frequency_hz": args.start_hz + value.frequency_index * step_hz,
                **asdict(value),
                "raw_s11_real": s11.real,
                "raw_s11_imag": s11.imag,
                "raw_s11_db": 20 * math.log10(magnitude) if magnitude else "-inf",
                "raw_s11_phase_deg": math.degrees(math.atan2(s11.imag, s11.real)),
            })
    summary = {
        "phase": args.phase,
        "standard": args.standard,
        "purpose": "raw one-port calibration-standard evidence for later host-side OSL correction",
        "started_utc": started,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "instrument": {**asdict(identity), "operator_model_label": "LiteVNA 64 ZN-406"},
        "transport": {"type": "USB CDC / SAA2 binary", "port": args.port},
        "sweep": {
            "actual_start_hz": args.start_hz,
            "actual_stop_hz": args.start_hz + step_hz * (args.points - 1),
            "step_hz": step_hz,
            "points": args.points,
            "channel": "S11",
        },
        "calibration": "standard acquisition only; correction not yet applied",
        "state_after_run": "normal mode requested and serial port closed",
    }
    write_json_new(summary_path, summary)
    with commands_path.open("x", encoding="utf-8", newline="") as handle:
        for command in device.commands:
            handle.write(json.dumps(command, ensure_ascii=False) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--timeout", type=float, default=3.0)
    subparsers = parser.add_subparsers(dest="action", required=True)
    def add_acquire_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--out", required=True)
        command.add_argument("--phase", required=True)
        command.add_argument("--start-hz", type=int, default=1_000_000)
        command.add_argument("--stop-hz", type=int, default=1_000_000_000)
        command.add_argument("--points", type=int, default=401)
        command.add_argument("--confirm-source-sweep", action="store_true")

    reference = subparsers.add_parser("acquire-s21-reference")
    add_acquire_arguments(reference)
    reference.add_argument("--confirm-thru-wiring", action="store_true")
    reference.set_defaults(
        func=acquire_s21,
        fixture_kind="thru-reference",
        confirm_dut_wiring=False,
        confirm_isolation_wiring=False,
    )

    dut = subparsers.add_parser("acquire-s21-dut")
    add_acquire_arguments(dut)
    dut.add_argument("--confirm-dut-wiring", action="store_true")
    dut.set_defaults(
        func=acquire_s21,
        fixture_kind="dut",
        confirm_thru_wiring=False,
        confirm_isolation_wiring=False,
    )

    isolation = subparsers.add_parser("acquire-s21-isolation")
    add_acquire_arguments(isolation)
    isolation.add_argument("--confirm-isolation-wiring", action="store_true")
    isolation.set_defaults(
        func=acquire_s21,
        fixture_kind="isolation",
        confirm_thru_wiring=False,
        confirm_dut_wiring=False,
    )

    standard = subparsers.add_parser("acquire-cal-standard")
    add_acquire_arguments(standard)
    standard.add_argument("--standard", required=True, choices=("open", "short", "load"))
    standard.add_argument("--confirm-standard-wiring", action="store_true")
    standard.set_defaults(func=acquire_cal_standard)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
