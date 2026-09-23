#!/usr/bin/env python3
"""Constrained MDP-M01 USB adapter for paired MINIWARE P906/L1060 devices.

The M01 uses a proprietary binary protocol, not SCPI. ``session`` keeps one
USB-serial connection open for a complete operator-guided experiment and only
exposes a small allowlist of status, setpoint, and output operations.

Runtime dependency for hardware access: ``python -m pip install pyserial``.
Offline tests use a fake serial port and never connect to hardware.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
import time
from dataclasses import asdict, dataclass
from functools import reduce
from operator import xor
from typing import Any, Callable, Iterable, TextIO

try:
    import serial
    from serial.tools import list_ports
except ImportError as exc:  # pragma: no cover - depends on the runtime host
    serial = None
    list_ports = None
    _SERIAL_IMPORT_ERROR: ImportError | None = exc
else:
    _SERIAL_IMPORT_ERROR = None


BAUD_RATE = 115_200
MAGIC = b"ZZ"
HEADER_SIZE = 6
CHANNEL_COUNT = 6
SYNTHESIZE = 0x11
SET_OUTPUT = 0x16
SET_VOLTAGE = 0x1A
SET_CURRENT = 0x1B
HEARTBEAT = 0x22
MACHINE_NAMES = {0: "unconfigured", 1: "P905", 2: "P906", 3: "L1060"}
P906_MODES = {0: "OFF", 1: "CC", 2: "CV", 3: "ON"}
L1060_MODES = {0: "CC", 1: "CV", 2: "CR", 3: "CP"}
# Conservative software ceiling: this initial adapter has only been exercised
# at 100 mA and deliberately does not expose the L1060's full rating yet.
L1060_CC_MAX_A = 1.0


class MDPError(Exception):
    """Base exception for adapter errors."""


class MDPDependencyError(MDPError):
    """pySerial is unavailable."""


class MDPConnectionError(MDPError):
    """USB serial connection failed or was lost."""


class MDPProtocolError(MDPError):
    """A packet could not be parsed or had an unsupported format."""


class MDPTimeoutError(MDPError):
    """A fresh state packet or requested readback did not arrive in time."""


class MDPStateError(MDPError):
    """The requested action does not match the observed device state."""


class MDPValidationError(MDPError):
    """An input is malformed or exceeds the armed safety envelope."""


@dataclass(frozen=True)
class ChannelStatus:
    channel: int
    machine: str
    machine_type: int
    online: bool
    output_enabled: bool
    mode: str
    locked: bool
    error: bool
    voltage_v: float
    current_a: float
    power_w: float
    set_voltage_v: float
    current_limit_a: float
    input_voltage_v: float
    input_current_a: float
    temperature_c: float


@dataclass(frozen=True)
class MDPStatus:
    selected_channel: int
    channels: tuple[ChannelStatus, ...]


@dataclass(frozen=True)
class SafetyEnvelope:
    max_voltage_v: float
    max_current_a: float
    max_power_w: float


class _PacketParser:
    """Incremental packet parser with header resynchronization and XOR checks."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def reset(self) -> None:
        self.buffer.clear()

    def feed(self, data: bytes) -> list[tuple[int, int, bytes]]:
        self.buffer.extend(data)
        packets: list[tuple[int, int, bytes]] = []
        while True:
            start = self.buffer.find(MAGIC)
            if start < 0:
                self.buffer[:] = self.buffer[-1:] if self.buffer.endswith(b"Z") else b""
                break
            if start:
                del self.buffer[:start]
            if len(self.buffer) < 4:
                break
            size = self.buffer[3]
            if size < HEADER_SIZE:
                del self.buffer[0]
                continue
            if len(self.buffer) < size:
                break
            raw = bytes(self.buffer[:size])
            if _xor(raw[HEADER_SIZE:]) != raw[5]:
                del self.buffer[0]
                continue
            del self.buffer[:size]
            packets.append((raw[2], raw[4], raw[HEADER_SIZE:]))
        return packets


def _xor(values: Iterable[int]) -> int:
    return reduce(xor, values, 0)


def _packet(packet_type: int, channel: int = 0xEE, payload: bytes = b"") -> bytes:
    size = HEADER_SIZE + len(payload)
    if not 0 <= packet_type <= 0xFF or not 0 <= channel <= 0xFF or size > 0xFF:
        raise MDPValidationError("invalid MDP packet header or payload length")
    return MAGIC + bytes((packet_type, size, channel, _xor(payload))) + payload


def _parse_status(channel_field: int, payload: bytes) -> MDPStatus:
    if len(payload) % CHANNEL_COUNT:
        raise MDPProtocolError(f"status payload length {len(payload)} is not divisible by six")
    record_size = len(payload) // CHANNEL_COUNT
    if record_size < 24:
        raise MDPProtocolError(f"status record size {record_size} is shorter than the known 24-byte layout")

    channels: list[ChannelStatus] = []
    for index in range(CHANNEL_COUNT):
        row = payload[index * record_size : (index + 1) * record_size]
        voltage_mv, current_ma = struct.unpack_from("<HH", row, 1)
        input_voltage_mv, input_current_ma = struct.unpack_from("<HH", row, 5)
        set_voltage_mv, current_limit_ma = struct.unpack_from("<HH", row, 9)
        temperature_deci_c = struct.unpack_from("<H", row, 13)[0]
        machine_type = row[16]
        mode_table = L1060_MODES if machine_type == 3 else P906_MODES
        voltage_v = voltage_mv / 1000.0
        current_a = current_ma / 1000.0
        channels.append(
            ChannelStatus(
                channel=index + 1,
                machine=MACHINE_NAMES.get(machine_type, f"unknown({machine_type})"),
                machine_type=machine_type,
                online=row[15] == 1,
                output_enabled=row[19] != 0,
                mode=mode_table.get(row[18], f"UNKNOWN({row[18]})"),
                locked=row[17] == 1,
                error=row[23] == 1,
                voltage_v=voltage_v,
                current_a=current_a,
                power_w=voltage_v * current_a,
                set_voltage_v=set_voltage_mv / 1000.0,
                current_limit_a=current_limit_ma / 1000.0,
                input_voltage_v=input_voltage_mv / 1000.0,
                input_current_a=input_current_ma / 1000.0,
                temperature_c=temperature_deci_c / 10.0,
            )
        )
    selected_channel = channel_field + 1 if channel_field < CHANNEL_COUNT else channel_field
    return MDPStatus(selected_channel=selected_channel, channels=tuple(channels))


def find_mdp_port() -> str:
    """Discover a Miniware M01 CDC port, rejecting ambiguous candidates."""
    if serial is None or list_ports is None:
        detail = f" ({_SERIAL_IMPORT_ERROR})" if _SERIAL_IMPORT_ERROR else ""
        raise MDPDependencyError(f"pyserial is required for hardware access{detail}; install with python -m pip install pyserial")
    try:
        ports = list_ports.comports()
    except Exception as exc:  # pragma: no cover - operating-system dependent
        raise MDPConnectionError(f"could not enumerate serial ports: {exc}") from exc
    candidates = []
    for item in ports:
        description = " ".join(
            str(value or "")
            for value in (item.manufacturer, item.product, item.description, item.hwid)
        ).lower()
        if "miniware" in description or (item.vid == 0x0416 and item.pid == 0xDC01):
            candidates.append(item.device)
    if len(candidates) == 0:
        raise MDPConnectionError("no MDP-M01 serial port found; connect the M01 by USB or pass --port")
    if len(candidates) > 1:
        raise MDPConnectionError("multiple possible MDP-M01 ports found; pass --port to select the current port")
    return candidates[0]


class MDPConnection:
    """One persistent USB serial connection and a limited, readback-based API."""

    def __init__(self, port: str | None = None, timeout_s: float = 3.0) -> None:
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (float, int)) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise MDPValidationError("timeout must be a finite positive number")
        self.port = port or find_mdp_port()
        self.timeout_s = float(timeout_s)
        self._serial: Any = None
        self._parser = _PacketParser()
        self._safety_envelope: SafetyEnvelope | None = None

    def __enter__(self) -> "MDPConnection":
        if serial is None:
            raise MDPDependencyError("pyserial is required for hardware access; install with python -m pip install pyserial")
        kwargs: dict[str, Any] = {}
        if os.name == "posix":
            kwargs["exclusive"] = True
        try:
            self._serial = serial.Serial(
                self.port, BAUD_RATE, bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                timeout=0.1, write_timeout=1.0, xonxoff=False,
                rtscts=False, dsrdtr=False, **kwargs,
            )
            self._serial.reset_input_buffer()
        except Exception as exc:
            self._serial = None
            raise MDPConnectionError(f"could not open MDP-M01 port {self.port}: {exc}") from exc
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        device, self._serial = self._serial, None
        if device is not None:
            try:
                device.close()
            except Exception as exc:
                raise MDPConnectionError(f"could not close MDP-M01 port {self.port}: {exc}") from exc

    def _write(self, packet_type: int, channel: int = 0xEE, payload: bytes = b"") -> None:
        if self._serial is None:
            raise MDPConnectionError("connection is closed")
        raw = _packet(packet_type, channel, payload)
        try:
            written = self._serial.write(raw)
            self._serial.flush()
        except Exception as exc:
            raise MDPConnectionError(f"write to MDP-M01 failed; delivery may be uncertain: {exc}") from exc
        if written is not None and written != len(raw):
            raise MDPConnectionError("short write to MDP-M01; delivery may be uncertain; do not replay the command")

    def read_status(
        self,
        *,
        timeout_s: float | None = None,
        predicate: Callable[[MDPStatus], bool] | None = None,
    ) -> MDPStatus:
        """Request a fresh status and optionally wait for matching readback."""
        if self._serial is None:
            raise MDPConnectionError("connection is closed")
        wait_s = self.timeout_s if timeout_s is None else timeout_s
        if isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)) or not math.isfinite(wait_s) or wait_s <= 0:
            raise MDPValidationError("status timeout must be a finite positive number")
        deadline = time.monotonic() + wait_s
        next_heartbeat = 0.0
        latest: MDPStatus | None = None
        first_request = True
        while time.monotonic() < deadline:
            now = time.monotonic()
            if first_request or now >= next_heartbeat:
                try:
                    self._serial.reset_input_buffer()
                except Exception as exc:
                    raise MDPConnectionError(f"could not clear stale status bytes: {exc}") from exc
                self._parser.reset()
                self._write(HEARTBEAT)
                first_request = False
                next_heartbeat = now + 0.5
            try:
                data = self._serial.read(4096)
            except Exception as exc:
                raise MDPConnectionError(f"read from MDP-M01 failed: {exc}") from exc
            for packet_type, channel_field, payload in self._parser.feed(data or b""):
                if packet_type != SYNTHESIZE:
                    continue
                latest = _parse_status(channel_field, payload)
                if predicate is None or predicate(latest):
                    return latest
        if latest is None:
            raise MDPTimeoutError(f"no fresh MDP-M01 status received within {wait_s:.2f}s")
        raise MDPTimeoutError(f"MDP-M01 status arrived, but requested state was not confirmed within {wait_s:.2f}s")

    @staticmethod
    def _channel(status: MDPStatus, number: int, expected: str, *, allow_locked: bool = False) -> ChannelStatus:
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= CHANNEL_COUNT:
            raise MDPValidationError("channel must be an integer from 1 through 6")
        state = status.channels[number - 1]
        if not state.online or state.machine != expected:
            raise MDPStateError(f"CH{number} is {state.machine} / online={state.online}, expected online {expected}")
        if state.error:
            raise MDPStateError(f"CH{number} reports an instrument error; stop and inspect the device")
        if state.locked and not allow_locked:
            raise MDPStateError(f"CH{number} is locked on the MDP-M01")
        return state

    def arm(self, envelope: SafetyEnvelope, *, confirm_wiring: bool) -> MDPStatus:
        """Set an in-session energy envelope after operator wiring confirmation."""
        if confirm_wiring is not True:
            raise MDPValidationError("arming requires confirm_wiring=true after the operator checks polarity, wiring, load, and stop conditions")
        values = (envelope.max_voltage_v, envelope.max_current_a, envelope.max_power_w)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 for value in values):
            raise MDPValidationError("safety-envelope voltage, current, and power must be finite positive numbers")
        # Conservative software bounds derived from the supported P906 range;
        # the operator must use a lower envelope appropriate to the DUT/load.
        if envelope.max_voltage_v > 30.0 or envelope.max_current_a > 10.0 or envelope.max_power_w > 300.0:
            raise MDPValidationError("requested envelope exceeds the P906 software limits (30 V, 10 A, 300 W)")
        status = self.read_status()
        for state in status.channels:
            if state.online and state.output_enabled:
                raise MDPStateError(f"CH{state.channel} ({state.machine}) is already ON; do not arm while any MDP output is enabled")
        self._safety_envelope = envelope
        return status

    def _require_envelope(self) -> SafetyEnvelope:
        if self._safety_envelope is None:
            raise MDPStateError("session is not armed; first read status and arm with an operator-confirmed safety envelope")
        return self._safety_envelope

    @staticmethod
    def _milli(value: float, label: str, maximum: float, *, allow_zero: bool = False) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise MDPValidationError(f"{label} must be a finite number")
        minimum = 0.0 if allow_zero else 0.001
        if value < minimum or value > maximum:
            raise MDPValidationError(f"{label} must be in the range {minimum:g} to {maximum:g}")
        result = int(value * 1000 + 0.5)
        if not allow_zero and result == 0:
            raise MDPValidationError(f"{label} is below one supported milli-unit")
        return result

    def set_p906(self, channel: int, voltage_v: float, current_limit_a: float) -> MDPStatus:
        """Set both P906 values while its output is OFF; never enables output."""
        envelope = self._require_envelope()
        voltage_mv = self._milli(voltage_v, "voltage", envelope.max_voltage_v, allow_zero=True)
        current_ma = self._milli(current_limit_a, "current limit", envelope.max_current_a)
        if (voltage_mv / 1000.0) * (current_ma / 1000.0) > envelope.max_power_w:
            raise MDPValidationError("P906 setpoint power exceeds the armed envelope")
        before = self.read_status()
        state = self._channel(before, channel, "P906")
        if state.locked:
            raise MDPStateError(f"CH{channel} is locked on the MDP-M01")
        if state.output_enabled:
            raise MDPStateError("P906 setpoints may only be changed while its output is OFF")
        payload = struct.pack("<HH", voltage_mv, current_ma)
        self._send_setpoint_pair(SET_VOLTAGE, channel - 1, payload)
        self._send_setpoint_pair(SET_CURRENT, channel - 1, payload)
        return self.read_status(predicate=lambda snap: self._p906_matches(snap, channel, voltage_mv, current_ma))

    def set_l1060_cc(self, channel: int, current_a: float) -> MDPStatus:
        """Set only L1060 CC current; mode selection and output control are separate."""
        envelope = self._require_envelope()
        current_ma = self._milli(current_a, "L1060 CC current", min(envelope.max_current_a, L1060_CC_MAX_A))
        if envelope.max_voltage_v * (current_ma / 1000.0) > envelope.max_power_w:
            raise MDPValidationError("L1060 CC setpoint at the armed maximum voltage exceeds the power envelope")
        before = self.read_status()
        state = self._channel(before, channel, "L1060")
        if state.mode != "CC":
            raise MDPStateError(f"CH{channel} is in {state.mode}, not CC; this adapter will not change mode")
        if state.output_enabled:
            raise MDPStateError("L1060 CC setpoint may only be changed while its input is OFF")
        payload = struct.pack("<HH", self._milli(state.set_voltage_v, "L1060 voltage preset", 65.535, allow_zero=True), current_ma)
        self._send_setpoint_pair(SET_CURRENT, channel - 1, payload)
        return self.read_status(predicate=lambda snap: self._l1060_matches(snap, channel, current_ma))

    def set_output(self, channel: int, enabled: bool, *, confirm_energy: bool = False) -> MDPStatus:
        """Turn one paired device ON/OFF once and verify it by status readback."""
        if not isinstance(enabled, bool):
            raise MDPValidationError("enabled must be true or false")
        before = self.read_status()
        state = before.channels[channel - 1] if isinstance(channel, int) and not isinstance(channel, bool) and 1 <= channel <= CHANNEL_COUNT else None
        if state is None:
            raise MDPValidationError("channel must be an integer from 1 through 6")
        if not state.online or state.machine not in ("P906", "L1060"):
            raise MDPStateError(f"CH{channel} is not an online supported P906/L1060 device")
        if not enabled and not state.output_enabled:
            return before
        if enabled:
            if state.error:
                raise MDPStateError(f"CH{channel} reports an instrument error")
            if state.locked:
                raise MDPStateError(f"CH{channel} is locked on the MDP-M01")
            envelope = self._require_envelope()
            if confirm_energy is not True:
                raise MDPValidationError("output ON requires confirm_energy=true for this specific command")
            if state.output_enabled:
                raise MDPStateError(f"CH{channel} is already ON; refusing to replay an output command")
            if state.machine == "P906":
                if state.set_voltage_v > envelope.max_voltage_v or state.current_limit_a > envelope.max_current_a:
                    raise MDPValidationError("observed P906 setpoint exceeds the armed voltage/current envelope")
                if state.set_voltage_v * state.current_limit_a > envelope.max_power_w:
                    raise MDPValidationError("observed P906 setpoint exceeds the armed power envelope")
            else:
                if state.mode != "CC":
                    raise MDPStateError(f"CH{channel} is in {state.mode}, not CC; supported L1060 operation is CC only")
                if state.current_limit_a > min(envelope.max_current_a, L1060_CC_MAX_A) or envelope.max_voltage_v * state.current_limit_a > envelope.max_power_w:
                    raise MDPValidationError("observed L1060 CC setpoint exceeds the armed envelope")
        self._write(SET_OUTPUT, channel - 1, bytes((1 if enabled else 0,)))
        # The binary protocol has no per-command ACK. Send exactly once and
        # rely on a new M01 status frame; a mismatch is an uncertain outcome.
        return self.read_status(predicate=lambda snap: snap.channels[channel - 1].output_enabled is enabled)

    def _send_setpoint_pair(self, packet_type: int, channel: int, payload: bytes) -> None:
        # MINIWARE's PC source sends each setpoint frame twice (~30 ms apart)
        # to improve delivery over the M01 wireless hop. This is not applied to
        # output state transitions, which must never be blindly replayed.
        self._write(packet_type, channel, payload)
        time.sleep(0.03)
        self._write(packet_type, channel, payload)
        time.sleep(0.03)

    @staticmethod
    def _p906_matches(status: MDPStatus, channel: int, voltage_mv: int, current_ma: int) -> bool:
        state = status.channels[channel - 1]
        return state.online and state.machine == "P906" and not state.error and not state.locked and (
            round(state.set_voltage_v * 1000) == voltage_mv and round(state.current_limit_a * 1000) == current_ma and not state.output_enabled
        )

    @staticmethod
    def _l1060_matches(status: MDPStatus, channel: int, current_ma: int) -> bool:
        state = status.channels[channel - 1]
        return state.online and state.machine == "L1060" and state.mode == "CC" and not state.error and not state.locked and (
            round(state.current_limit_a * 1000) == current_ma and not state.output_enabled
        )


def _status_payload(status: MDPStatus) -> dict[str, Any]:
    return {"selected_channel": status.selected_channel, "channels": [asdict(ch) for ch in status.channels]}


def _emit(stream: TextIO, event: dict[str, Any]) -> None:
    stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()


def _handle_session_request(connection: MDPConnection, request: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(request, dict) or not isinstance(request.get("op"), str):
        raise MDPValidationError("each JSON line must be an object with a string 'op'")
    op = request["op"]
    if op == "status":
        return {"event": "status", "status": _status_payload(connection.read_status())}, False
    if op == "arm":
        envelope = SafetyEnvelope(
            max_voltage_v=request.get("max_voltage_v"),
            max_current_a=request.get("max_current_a"),
            max_power_w=request.get("max_power_w"),
        )
        status = connection.arm(envelope, confirm_wiring=request.get("confirm_wiring") is True)
        return {"event": "armed", "envelope": asdict(envelope), "status": _status_payload(status)}, False
    if op == "set_p906":
        status = connection.set_p906(request.get("channel"), request.get("voltage_v"), request.get("current_limit_a"))
        return {"event": "readback_confirmed", "operation": op, "status": _status_payload(status)}, False
    if op == "set_l1060_cc":
        status = connection.set_l1060_cc(request.get("channel"), request.get("current_a"))
        return {"event": "readback_confirmed", "operation": op, "status": _status_payload(status)}, False
    if op == "output_on":
        status = connection.set_output(request.get("channel"), True, confirm_energy=request.get("confirm_energy") is True)
        return {"event": "readback_confirmed", "operation": op, "status": _status_payload(status)}, False
    if op == "output_off":
        status = connection.set_output(request.get("channel"), False)
        return {"event": "readback_confirmed", "operation": op, "status": _status_payload(status)}, False
    if op == "end":
        status = connection.read_status()
        return {"event": "session_ended", "status": _status_payload(status), "outputs_changed_by_end": False}, True
    raise MDPValidationError("allowed operations: status, arm, set_p906, set_l1060_cc, output_on, output_off, end")


def run_session(connection: MDPConnection, input_stream: TextIO, output_stream: TextIO) -> int:
    """Run JSONL commands without disconnecting between operator phases."""
    try:
        initial = connection.read_status()
        _emit(output_stream, {"event": "session_ready", "pid": os.getpid(), "port": connection.port, "status": _status_payload(initial)})
        for line_number, raw in enumerate(input_stream, start=1):
            if not raw.strip():
                continue
            try:
                request = json.loads(raw)
                event, done = _handle_session_request(connection, request)
                _emit(output_stream, event)
                if done:
                    return 0
            except (json.JSONDecodeError, MDPError, TypeError, ValueError) as exc:
                _emit(output_stream, {"event": "session_stopped", "line": line_number, "error": str(exc), "next_action": "Do not replay a write whose result is uncertain; inspect the instrument physically and reconnect for read-only status."})
                return 2
        _emit(output_stream, {"event": "session_incomplete", "reason": "stdin_eof_before_end", "outputs_changed_by_close": False})
        return 2
    except MDPError as exc:
        _emit(output_stream, {"event": "session_failed", "error": str(exc)})
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Constrained MINIWARE MDP-M01 USB adapter (not SCPI).")
    parser.add_argument("--port", help="current USB serial port; auto-detected when omitted")
    parser.add_argument("--timeout", type=float, default=3.0, help="bounded status/readback timeout in seconds (default: 3)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="one-shot, read-only status snapshot")
    sub.add_parser("session", help="persistent foreground session; JSON commands are read from stdin")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        with MDPConnection(args.port, timeout_s=args.timeout) as connection:
            if args.command == "status":
                status = connection.read_status()
                print(json.dumps({"event": "status", "port": connection.port, "status": _status_payload(status)}, ensure_ascii=False, indent=2))
                return 0
            return run_session(connection, sys.stdin, sys.stdout)
    except MDPError as exc:
        print(f"MDP error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised as a process by operators
    raise SystemExit(main())
