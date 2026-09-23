#!/usr/bin/env python3
"""Restricted FLUKE 8845A LAN client: identity, snapshot, local restore, low-voltage DC."""

from __future__ import annotations

import argparse
import csv
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import socket
import statistics
import sys
import time
from datetime import datetime, timezone

VERSION = "1.1.0"
QUERIES = frozenset({
    "*IDN?", "*OPC?", "CONF?", "ROUT:TERM?", "VOLT:DC:NPLC?",
    "ZERO:AUTO?", "CALC:STAT?", "TRIG:SOUR?", "TRIG:COUN?", "SAMP:COUN?",
    "VOLT:DC:RANG?", "VOLT:DC:RANG:AUTO?",
})
BASELINE = (
    "CONF?", "ROUT:TERM?", "VOLT:DC:NPLC?", "ZERO:AUTO?", "CALC:STAT?",
    "TRIG:SOUR?", "TRIG:COUN?", "SAMP:COUN?", "VOLT:DC:RANG?", "VOLT:DC:RANG:AUTO?",
)
RANGES = {0.1: "0.1", 1.0: "1", 10.0: "10"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def number(text):
    value = float(text)
    if not math.isfinite(value) or abs(value) >= 1e30:
        raise ValueError("Non-finite or overload/sentinel reading")
    return value


def redact_identity(text):
    parts = text.split(",", 3)
    if len(parts) != 4:
        raise ValueError("Malformed identity; raw response withheld")
    return ",".join([parts[0].strip(), parts[1].strip(), "[REDACTED]", parts[3].strip()])


class Fluke8845A:
    def __init__(self, host, port=3490, timeout=5.0, log_path=None):
        # One explicitly supplied unicast endpoint, never a discovery/subnet scan.
        address = ipaddress.IPv4Address(host)
        if address.is_multicast or address.is_unspecified or int(address) == 0xFFFFFFFF:
            raise ValueError("An explicit unicast IPv4 address is required")
        if not 1 <= port <= 65535 or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError("Invalid port or timeout (0 < timeout <= 30 seconds)")
        self.host, self.port, self.timeout = str(address), port, timeout
        self.log_path = Path(log_path) if log_path else None
        self.sock = None
        self.identified = False
        self.remote_restore_pending = False
        self.connection_count = 0
        self.log = []

    def close(self):
        sock, self.sock = self.sock, None
        self.identified = False
        if sock is not None:
            try:
                sock.close()
            finally:
                self._record({"utc": utc_now(), "event": "socket_closed",
                              "connection_number": self.connection_count})

    def _record(self, entry):
        self.log.append(entry)
        if self.log_path:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _exchange(self, command, reply=True):
        if self.sock is None:
            raise RuntimeError("No connected instrument")
        entry = {"utc": utc_now(), "command": command}
        started = time.monotonic()
        try:
            self.sock.settimeout(self.timeout)
            self.sock.sendall(command.encode("ascii") + b"\n")
            if not reply:
                entry["response"] = None
                return None
            data = bytearray()
            deadline = started + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Incomplete response within total query deadline")
                self.sock.settimeout(remaining)
                chunk = self.sock.recv(1)
                if not chunk:
                    raise EOFError("Peer closed before LF")
                data.extend(chunk)
                if len(data) > 4096:
                    raise ValueError("Response exceeds 4096 bytes")
                if chunk == b"\n":
                    break
            text = data.decode("ascii").strip()
            if command == "*IDN?":
                text = redact_identity(text)
            entry["response"] = text
            return text
        except BaseException as exc:
            # A partial reply cannot safely be reused by a subsequent query.
            entry["error"] = type(exc).__name__
            self.close()
            raise
        finally:
            entry["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
            self._record(entry)

    def identify(self, retry_identity_once=False):
        if self.sock is not None:
            raise RuntimeError("identify requires a fresh, unowned connection")
        for attempt in range(2 if retry_identity_once else 1):
            try:
                self.sock = socket.create_connection((self.host, self.port), self.timeout)
                self.connection_count += 1
                self._record({"utc": utc_now(), "event": "connected",
                              "connection_number": self.connection_count})
                identity = self._exchange("*IDN?")
                parts = identity.split(",", 3)
                if parts[:2] != ["FLUKE", "8845A"]:
                    raise ValueError("Identity is not native FLUKE 8845A; no state changes sent")
                self.identified = True
                return identity
            except (OSError, EOFError):
                self.close()
                if attempt == 0 and retry_identity_once:
                    self._record({"utc": utc_now(), "event": "one_pre_mutation_identity_retry"})
                    time.sleep(0.2)
                    continue
                raise
            except Exception:
                self.close()
                raise

    def query(self, command):
        if not self.identified or command not in QUERIES:
            raise ValueError("Only allowlisted queries on a verified instrument are permitted")
        return self._exchange(command)

    def _write(self, command):
        allowed = {"SYST:REM", "SYST:LOC"} | {"VOLT:DC:RANG " + r for r in RANGES.values()}
        if not self.identified or command not in allowed:
            raise ValueError("Write not allowed on this verified instrument")
        if command == "SYST:REM":
            # Once attempted, restoration is required even if the send fails ambiguously.
            self.remote_restore_pending = True
        self._exchange(command, reply=False)

    def snapshot(self):
        return {command: self.query(command) for command in BASELINE}

    def restore_local(self):
        self._write("SYST:LOC")
        if self.query("*OPC?") != "1":
            raise ValueError("Local restore completion response was not 1")
        self.remote_restore_pending = False
        # Completion is not physical evidence that every front-panel key works.
        return {"local_command_processed": True, "front_panel_keys_physically_verified": False}

    def acquire_dcv(self, result, range_v, count, wiring_confirmed, state_changes_confirmed,
                    restore_on_finish=True):
        if not wiring_confirmed or not state_changes_confirmed:
            raise ValueError("Operator wiring and state-change authorization are required")
        if (isinstance(range_v, bool) or range_v not in RANGES or isinstance(count, bool)
                or not isinstance(count, int) or not 1 <= count <= 100):
            raise ValueError("Use range 0.1/1/10 V and 1..100 samples")
        # Identity must already be verified. No implicit change of measurement function.
        result["baseline"] = self.snapshot()
        before = result["baseline"]
        if not before["CONF?"].strip('"').startswith("VOLT ") or before["ROUT:TERM?"] != "FRON":
            raise ValueError("Expected front-input DC voltage; measurement function not changed")
        if before["CALC:STAT?"] != "0":
            raise ValueError("Math/relative mode active; no offset disabled or overwritten")
        if before["TRIG:SOUR?"] != "IMM" or number(before["TRIG:COUN?"]) != 1 or number(before["SAMP:COUN?"]) != 1:
            raise ValueError("Requires one sample per immediate trigger; trigger settings not changed")
        result["measurements"] = []
        result["local_restore"] = ({"local_command_processed": False} if restore_on_finish
                                   else {"deferred_until_session_end": True})
        try:
            if not self.remote_restore_pending:
                self._write("SYST:REM")
            self._write("VOLT:DC:RANG " + RANGES[range_v])
            if number(self.query("VOLT:DC:RANG?")) != range_v or self.query("VOLT:DC:RANG:AUTO?") != "0":
                raise ValueError("Requested fixed range not verified")
            deadline = time.monotonic() + 60
            for index in range(count):
                if time.monotonic() >= deadline:
                    raise TimeoutError("60-second acquisition budget exceeded")
                raw = self._exchange("READ?")
                value = number(raw)
                result["measurements"].append({"sample": index + 1, "utc": self.log[-1]["utc"], "raw": raw, "volts": value})
                if abs(value) > range_v * 1.2:
                    raise ValueError("Reading outside configured range; acquisition stopped")
            result["after"] = self.snapshot()
            if (number(result["after"]["VOLT:DC:RANG?"]) != range_v
                    or result["after"]["ROUT:TERM?"] != "FRON"
                    or result["after"]["CALC:STAT?"] != "0"):
                raise ValueError("Post-acquisition configuration changed; retain data for review")
            values = [m["volts"] for m in result["measurements"]]
            result["statistics"] = {
                "count": len(values), "mean_V": statistics.mean(values),
                "min_V": min(values), "max_V": max(values), "span_V": max(values) - min(values),
                "sample_stddev_V": statistics.stdev(values) if len(values) > 1 else None,
            }
            result["status"] = "acquired_not_an_accuracy_or_ripple_test"
        finally:
            if restore_on_finish:
                # Do not reconnect or replay writes after uncertain execution.
                try:
                    if self.sock is None:
                        raise ConnectionError("Link lost; local mode restoration unverified")
                    result["local_restore"] = self.restore_local()
                except (OSError, EOFError, ValueError, RuntimeError) as exc:
                    result["local_restore"] = {"local_command_processed": False, "error": type(exc).__name__}
                    result["status"] = "incomplete_local_restore_unverified"
                    raise


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", required=True, help="Operator-confirmed IPv4 address")
    p.add_argument("--port", type=int, default=3490)
    p.add_argument("--timeout", type=float, default=5)
    p.add_argument("--retry-identity-once", action="store_true", help="One retry only before any state change")
    p.add_argument("--out", type=Path, required=True, help="New evidence directory; existing paths rejected")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("identify")
    sub.add_parser("snapshot")
    local = sub.add_parser("restore-local")
    local.add_argument("--confirm-state-changes", action="store_true")
    dcv = sub.add_parser("dcv")
    dcv.add_argument("--range-v", type=float, choices=tuple(RANGES), required=True)
    dcv.add_argument("--count", type=int, default=10)
    dcv.add_argument("--phase", required=True)
    dcv.add_argument("--operator-state", required=True, help="Confirmed fixture, supply/reference and DUT state")
    dcv.add_argument("--supersedes-phase", help="Previous phase not suitable for the new physical claim")
    dcv.add_argument("--confirm-wiring", action="store_true")
    dcv.add_argument("--confirm-state-changes", action="store_true")
    sub.add_parser("session", help="One foreground TCP connection; newline JSON on stdin, end to restore/close")
    return p


def _session_request(raw):
    request = json.loads(raw)
    if not isinstance(request, dict):
        raise ValueError("Session request must be a JSON object")
    operation = request.get("op")
    if operation in {"snapshot", "end"}:
        if set(request) != {"op"}:
            raise ValueError("This operation accepts only op")
    elif operation == "dcv":
        required = {"op", "range_v", "count", "phase", "operator_state",
                    "confirm_wiring", "confirm_state_changes"}
        optional = {"supersedes_phase"}
        if not required <= set(request) or set(request) - required - optional:
            raise ValueError("DCV requires range_v, count, phase, operator_state and both confirmations")
        if (not isinstance(request["phase"], str) or not request["phase"].strip()
                or not isinstance(request["operator_state"], str) or not request["operator_state"].strip()
                or type(request["confirm_wiring"]) is not bool
                or type(request["confirm_state_changes"]) is not bool
                or ("supersedes_phase" in request and request["supersedes_phase"] is not None
                    and not isinstance(request["supersedes_phase"], str))):
            raise ValueError("Invalid DCV phase, operator state or confirmation types")
    else:
        raise ValueError("Session op must be snapshot, dcv or end")
    return request


def run_session(client, args, result, input_stream=None, output_stream=None):
    """Keep one verified connection across all foreground experiment steps."""
    input_stream = sys.stdin if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream
    result.update(session_pid=os.getpid(), steps=[], connection_count=0)

    def emit(event):
        output_stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        output_stream.flush()

    def persist():
        result["connection_count"] = client.connection_count
        (args.out / "manifest.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        rows = [{"phase": step["phase"], **measurement}
                for step in result["steps"] if step.get("action") == "dcv"
                for measurement in step.get("measurements", [])]
        if rows:
            with (args.out / "measurements.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=("phase", "sample", "utc", "raw", "volts"))
                writer.writeheader()
                writer.writerows(rows)

    code = 2
    try:
        result["identity"] = client.identify(args.retry_identity_once)
        result["status"] = "session_ready"
        persist()
        emit({"event": "session_ready", "pid": result["session_pid"], "identity": result["identity"],
              "connection_count": client.connection_count,
              "stop": "Send {\"op\":\"end\"} then newline; Ctrl+C also runs cleanup"})
        for raw in input_stream:
            if not raw.strip():
                continue
            step = {"index": len(result["steps"]) + 1, "started_utc": utc_now(), "status": "incomplete"}
            try:
                request = _session_request(raw)
                step["action"] = request["op"]
                if request["op"] == "snapshot":
                    step["baseline"] = client.snapshot()
                    step["status"] = "configuration_read_only"
                elif request["op"] == "dcv":
                    phase = request["phase"]
                    if any(previous.get("phase") == phase for previous in result["steps"]):
                        raise ValueError("Phase must be unique within a session")
                    step.update(phase=phase, operator_state=request["operator_state"],
                                supersedes_phase=request.get("supersedes_phase"),
                                physical_confirmation_source="operator", requested_count=request["count"],
                                requested_range_V=request["range_v"])
                    client.acquire_dcv(step, request["range_v"], request["count"],
                                       request["confirm_wiring"], request["confirm_state_changes"],
                                       restore_on_finish=False)
                else:
                    step["status"] = "end_requested"
            except BaseException as exc:
                step["error"] = type(exc).__name__ + ": " + str(exc)
                result["steps"].append(step)
                persist()
                raise
            result["steps"].append(step)
            persist()
            emit({"event": "step_complete", "index": step["index"], "action": step["action"],
                  "status": step["status"], "statistics": step.get("statistics"),
                  "connection_count": client.connection_count})
            if request["op"] == "end":
                result["status"] = "session_completed"
                code = 0
                break
        else:
            result["status"] = "incomplete_unexpected_stdin_eof"
            result["error"] = "Session input closed before end"
    except KeyboardInterrupt:
        result["status"] = "incomplete_interrupted"
        result["error"] = "KeyboardInterrupt"
        code = 130
    except Exception as exc:
        result["status"] = "incomplete"
        result["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        if client.remote_restore_pending:
            try:
                if client.sock is None:
                    raise ConnectionError("Link lost; local mode restoration unverified")
                result["local_restore"] = client.restore_local()
            except (OSError, EOFError, ValueError, RuntimeError) as exc:
                result["local_restore"] = {"local_command_processed": False,
                                           "error": type(exc).__name__}
                result["status"] = "incomplete_local_restore_unverified"
                code = 2
        else:
            result["local_restore"] = {"required": False}
        client.close()
        result["socket_closed"] = True
        result["finished_utc"] = utc_now()
        persist()
        emit({"event": "session_ended", "pid": result["session_pid"],
              "status": result["status"], "local_restore": result["local_restore"],
              "socket_closed": True, "connection_count": client.connection_count})
    return code


def main(argv=None):
    args = parser().parse_args(argv)
    # Validate authorization before any connection or directory creation.
    if args.action in {"dcv", "restore-local"} and not args.confirm_state_changes:
        raise ValueError("State-change authorization required")
    if args.action == "dcv" and (not args.confirm_wiring or not 1 <= args.count <= 100):
        raise ValueError("Confirmed wiring and 1..100 samples required")
    client = Fluke8845A(args.host, args.port, args.timeout, args.out / "commands.jsonl")
    args.out.mkdir(parents=True, exist_ok=False)
    result = {"script_version": VERSION, "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "started_utc": utc_now(), "action": args.action, "status": "incomplete", "transport": "tcp_socket",
              "port": args.port, "serial_redacted": True}
    if args.action == "dcv":
        result.update(phase=args.phase, operator_state=args.operator_state, supersedes_phase=args.supersedes_phase,
                      physical_confirmation_source="operator", requested_count=args.count, requested_range_V=args.range_v)
    if args.action == "session":
        return run_session(client, args, result)
    code = 0
    try:
        result["identity"] = client.identify(args.retry_identity_once)
        if args.action == "identify":
            result["status"] = "identity_verified"
        elif args.action == "snapshot":
            result["baseline"] = client.snapshot()
            result["status"] = "configuration_read_only"
        elif args.action == "restore-local":
            result["baseline"] = {q: client.query(q) for q in ("CONF?", "ROUT:TERM?")}
            result["local_restore"] = client.restore_local()
            result["status"] = "local_command_processed_settings_preserved"
        else:
            client.acquire_dcv(result, args.range_v, args.count, args.confirm_wiring, args.confirm_state_changes)
    except (OSError, EOFError, ValueError, RuntimeError) as exc:
        result["status"] = "incomplete"
        result["error"] = type(exc).__name__ + ": " + str(exc)
        code = 2
    finally:
        client.close()
        result["socket_closed"] = True
        result["finished_utc"] = utc_now()
        (args.out / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if "measurements" in result:
            with (args.out / "measurements.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=("sample", "utc", "raw", "volts"))
                writer.writeheader()
                writer.writerows(result["measurements"])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
