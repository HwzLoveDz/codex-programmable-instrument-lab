#!/usr/bin/env python3
"""Dependency-free, read-only SCPI client for SIGLENT oscilloscopes over TCP 5025."""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import struct
import sys
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_IEND = b"IEND\xaeB\x60\x82"
SCRIPT_VERSION = "0.3.0"
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
RECV_CHUNK_BYTES = 1024 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_read_only_query(command: str) -> str:
    if not isinstance(command, str):
        raise ValueError("SCPI query must be text.")
    if any(char in command for char in (";", "\r", "\n")):
        raise ValueError("Multiple commands and embedded line endings are not allowed.")
    command = command.strip(" \t")
    if not command:
        raise ValueError("Only SCPI queries are accepted.")
    if any(ord(char) < 32 or ord(char) == 127 for char in command):
        raise ValueError("SCPI query contains a control character.")
    if not command.startswith(("*", ":")):
        raise ValueError("SCPI query must start with '*' or ':'.")
    if command.count("?") != 1:
        raise ValueError("A single SCPI query must contain exactly one query marker.")
    header = command.split(maxsplit=1)[0]
    if not header.endswith("?"):
        raise ValueError("The first SCPI header token must end with '?'.")
    try:
        command.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("SCPI command must contain ASCII characters only.") from exc
    return command


def strip_ieee_block(data: bytes) -> bytes:
    if not data.startswith(b"#"):
        return data
    if len(data) < 2:
        raise ValueError("Truncated IEEE 488.2 binary block header.")
    digits_byte = data[1:2]
    if not digits_byte.isdigit():
        raise ValueError("Invalid IEEE 488.2 binary block header.")
    digits = int(digits_byte)
    if digits == 0:
        raise ValueError("Indefinite-length IEEE 488.2 blocks are not supported.")
    if len(data) < 2 + digits:
        raise ValueError("Truncated IEEE 488.2 binary block length.")
    length_bytes = data[2 : 2 + digits]
    if not length_bytes.isdigit():
        raise ValueError("Invalid IEEE 488.2 binary block length.")
    payload_len = int(length_bytes)
    start = 2 + digits
    end = start + payload_len
    if len(data) < end:
        raise ValueError("Truncated IEEE 488.2 binary block.")
    if data[end:].strip(b"\r\n"):
        raise ValueError("Unexpected bytes after IEEE 488.2 binary block.")
    return data[start:end]


def _png_frame_end(data: bytes, max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES) -> int | None:
    if len(data) < len(PNG_SIGNATURE):
        if PNG_SIGNATURE.startswith(data):
            return None
        raise ValueError("Response is not a PNG image.")
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("Response is not a PNG image.")
    offset = len(PNG_SIGNATURE)
    saw_ihdr = False
    saw_idat = False
    while True:
        if len(data) < offset + 8:
            return None
        length = struct.unpack_from(">I", data, offset)[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > max_bytes:
            raise ValueError("PNG image exceeds the configured response limit.")
        if len(data) < chunk_end:
            return None
        if not all((65 <= value <= 90) or (97 <= value <= 122) for value in chunk_type):
            raise ValueError("PNG contains an invalid chunk type.")
        chunk_data = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise ValueError(f"PNG chunk {chunk_type!r} has an invalid CRC.")
        if not saw_ihdr:
            if chunk_type != b"IHDR" or length != 13:
                raise ValueError("PNG must begin with one 13-byte IHDR chunk.")
            width, height, _, _, compression, filter_method, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
            if width == 0 or height == 0 or compression != 0 or filter_method != 0 or interlace not in (0, 1):
                raise ValueError("PNG IHDR fields are invalid.")
            saw_ihdr = True
        elif chunk_type == b"IHDR":
            raise ValueError("PNG contains more than one IHDR chunk.")
        if chunk_type == b"IDAT":
            saw_idat = True
        if chunk_type == b"IEND":
            if length != 0 or not saw_idat:
                raise ValueError("PNG IEND or IDAT structure is invalid.")
            return chunk_end
        offset = chunk_end


def _validate_bmp(image: bytes) -> None:
    if len(image) < 26 or not image.startswith(b"BM"):
        raise ValueError("Response does not contain a complete BMP header.")
    declared_size = struct.unpack_from("<I", image, 2)[0]
    pixel_offset = struct.unpack_from("<I", image, 10)[0]
    dib_size = struct.unpack_from("<I", image, 14)[0]
    if declared_size != len(image) or dib_size < 12 or 14 + dib_size > len(image):
        raise ValueError("BMP size or DIB header is invalid.")
    if dib_size == 12:
        width, height, planes, bits_per_pixel = struct.unpack_from("<HHHH", image, 18)
    elif dib_size >= 40:
        width, height, planes, bits_per_pixel = struct.unpack_from("<iiHH", image, 18)
    else:
        raise ValueError("Unsupported BMP DIB header.")
    if width <= 0 or height == 0 or planes != 1 or bits_per_pixel == 0:
        raise ValueError("BMP dimensions or pixel format are invalid.")
    if pixel_offset < 14 + dib_size or pixel_offset >= len(image):
        raise ValueError("BMP pixel offset is invalid.")


def extract_image(data: bytes, expected: str) -> bytes:
    expected = expected.upper()
    framed = data.lstrip(b"\r\n")
    payload = strip_ieee_block(framed).lstrip(b"\r\n")
    if expected == "PNG":
        end = _png_frame_end(payload)
        if end is None:
            raise ValueError("Response contains an incomplete PNG image.")
        if payload[end:].strip(b"\r\n"):
            raise ValueError("Unexpected bytes after PNG image.")
        return payload[:end]
    if expected != "BMP":
        raise ValueError("Image format must be PNG or BMP.")
    if len(payload) < 6 or not payload.startswith(b"BM"):
        raise ValueError("Response does not contain a BMP header.")
    size = struct.unpack_from("<I", payload, 2)[0]
    if size > DEFAULT_MAX_RESPONSE_BYTES or len(payload) < size:
        raise ValueError("Response contains an incomplete or oversized BMP image.")
    image = payload[:size]
    _validate_bmp(image)
    if payload[size:].strip(b"\r\n"):
        raise ValueError("Unexpected bytes after BMP image.")
    return image


def _binary_frame_end(data: bytes, max_bytes: int) -> int | None:
    if data.startswith(b"#") and len(data) >= 2 and data[1:2].isdigit():
        digits = int(data[1:2])
        if digits == 0:
            raise ValueError("Indefinite-length IEEE 488.2 blocks are not supported.")
        if len(data) < 2 + digits:
            return None
        length_bytes = data[2 : 2 + digits]
        if not length_bytes.isdigit():
            raise ValueError("Invalid IEEE 488.2 binary block length.")
        frame_end = 2 + digits + int(length_bytes)
        if frame_end > max_bytes:
            raise ValueError("Binary response exceeds the configured response limit.")
        return frame_end if len(data) >= frame_end else None
    if data.startswith(b"#"):
        return None
    if PNG_SIGNATURE.startswith(data) or data.startswith(PNG_SIGNATURE):
        return _png_frame_end(data, max_bytes)
    if b"BM".startswith(data) or data.startswith(b"BM"):
        if len(data) < 6:
            return None
        frame_end = struct.unpack_from("<I", data, 2)[0]
        if frame_end < 26 or frame_end > max_bytes:
            raise ValueError("BMP response has an invalid or oversized file length.")
        return frame_end if len(data) >= frame_end else None
    if data:
        preview = bytes(data[:64]).decode("ascii", errors="replace")
        raise ValueError(f"Binary response has an unexpected prefix: {preview!r}")
    return None


def _binary_response_complete(data: bytes) -> bool:
    try:
        return _binary_frame_end(data.lstrip(b"\r\n"), DEFAULT_MAX_RESPONSE_BYTES) is not None
    except ValueError:
        return False


class ScpiSocket:
    def __init__(
        self,
        host: str,
        port: int = 5025,
        timeout: float = 2.0,
        idle_timeout: float = 0.2,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.idle_timeout = idle_timeout
        self.max_response_bytes = max_response_bytes
        self._sock: socket.socket | None = None
        self._rx_buffer = bytearray()

    def connect(self) -> ScpiSocket:
        if self._sock is None:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._rx_buffer.clear()
        return self

    def close(self) -> None:
        sock, self._sock = self._sock, None
        self._rx_buffer.clear()
        if sock is not None:
            sock.close()

    def __enter__(self) -> ScpiSocket:
        return self.connect()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _write_line(self, command: str) -> None:
        if self._sock is None:
            raise RuntimeError("A persistent SCPI connection is required for writes; call connect() first.")
        if (
            not isinstance(command, str)
            or not command
            or not command.startswith(("*", ":"))
            or any(char in command for char in ("?", ";", "\r", "\n"))
            or any(ord(char) < 32 or ord(char) == 127 for char in command)
        ):
            raise ValueError("Protected writes require one ASCII SCPI line without '?', ';', CR, or LF.")
        try:
            payload = command.encode("ascii") + b"\n"
        except UnicodeEncodeError as exc:
            raise ValueError("SCPI command must contain ASCII characters only.") from exc
        try:
            self._sock.sendall(payload)
        except OSError:
            self.close()
            raise

    def _exchange(self, command: str, binary: bool = False) -> bytes:
        deadline = time.monotonic() + self.timeout
        persistent = self._sock is not None
        sock = self._sock if persistent else socket.create_connection(
            (self.host, self.port), timeout=self.timeout
        )
        assert sock is not None
        data = bytearray(self._rx_buffer if persistent else b"")
        if persistent:
            self._rx_buffer.clear()
        try:
            sock.sendall(command.encode("ascii") + b"\n")
            while True:
                while data and data[0] in (10, 13):
                    del data[0]
                if len(data) > self.max_response_bytes:
                    raise ValueError("SCPI response exceeds the configured response limit.")
                if binary:
                    frame_end = _binary_frame_end(bytes(data), self.max_response_bytes)
                    if frame_end is not None:
                        response = bytes(data[:frame_end])
                        if persistent:
                            self._rx_buffer.extend(data[frame_end:])
                        return response
                else:
                    newline = data.find(b"\n")
                    if newline >= 0:
                        response = bytes(data[: newline + 1])
                        if persistent:
                            self._rx_buffer.extend(data[newline + 1 :])
                        return response
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    detail = "incomplete response" if data else "no response"
                    raise TimeoutError(f"SCPI {detail} within {self.timeout:.3f} s")
                sock.settimeout(min(self.idle_timeout, remaining))
                try:
                    chunk = sock.recv(RECV_CHUNK_BYTES)
                except socket.timeout:
                    continue
                if not chunk:
                    detail = "before response completion" if data else "before any response"
                    raise ConnectionError(f"SCPI peer closed the connection {detail}.")
                data.extend(chunk)
        except (OSError, TimeoutError, ValueError):
            if persistent:
                self.close()
            raise
        finally:
            if not persistent:
                sock.close()

    def query(self, command: str) -> str:
        command = validate_read_only_query(command)
        data = self._exchange(command)
        try:
            return data.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise ValueError("SCPI text response is not valid UTF-8.") from exc

    def screenshot(self, image_format: str = "PNG") -> bytes:
        image_format = image_format.upper()
        if image_format not in {"PNG", "BMP"}:
            raise ValueError("Image format must be PNG or BMP.")
        data = self._exchange(f":PRINt? {image_format},NORMal", binary=True)
        return extract_image(data, image_format)


def response_record(command: str, started: str, elapsed_ms: float, payload: bytes) -> dict[str, Any]:
    return {
        "timestamp_utc": started,
        "command": command,
        "elapsed_ms": round(elapsed_ms, 3),
        "response_bytes": len(payload),
        "response_sha256": hashlib.sha256(payload).hexdigest(),
    }


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_text_query(client: ScpiSocket, command: str, log_path: Path | None = None) -> str:
    started = utc_now()
    tick = time.perf_counter()
    response = client.query(command)
    elapsed_ms = (time.perf_counter() - tick) * 1000
    if log_path:
        record = response_record(command, started, elapsed_ms, response.encode("utf-8"))
        if command.strip().upper() == "*IDN?":
            summary = redacted_idn_summary(response)
            record["response_text"] = ",".join(summary.values())
            record["response_redacted"] = True
            record["full_response_location"] = "idn_private.txt when produced by smoke"
        else:
            record["response_text"] = response
        append_jsonl(log_path, record)
    return response


def ensure_expected_idn(idn: str, expected_model: str | None) -> None:
    fields = [field.strip() for field in idn.strip().split(",")]
    if len(fields) < 2:
        raise RuntimeError(f"Malformed *IDN? response: {idn!r}")

    def normalize(value: str) -> str:
        return "".join(char for char in value.upper() if char.isalnum())

    manufacturer = normalize(fields[0])
    if manufacturer not in {"SIGLENT", "SIGLENTTECHNOLOGIES"}:
        raise RuntimeError(f"Unexpected manufacturer in *IDN? response: {fields[0]!r}")
    normalized_model = normalize(expected_model or "")
    if normalized_model and normalize(fields[1]) != normalized_model:
        raise RuntimeError(f"Expected model {expected_model!r}, got model field {fields[1]!r}")


def redacted_idn_summary(idn: str) -> dict[str, str]:
    fields = [field.strip() for field in idn.strip().split(",")]
    if len(fields) < 2:
        raise RuntimeError(f"Malformed *IDN? response: {idn!r}")
    return {
        "manufacturer": fields[0],
        "model": fields[1],
        "serial": "[REDACTED]",
        "firmware": fields[3] if len(fields) >= 4 else "",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Oscilloscope IPv4 address or host name")
    parser.add_argument("--port", type=int, default=5025, help="SCPI raw-socket port (default: 5025)")
    parser.add_argument("--timeout", type=float, default=2.0, help="Total response timeout in seconds")
    parser.add_argument("--idle-timeout", type=float, default=0.2, help="Idle gap after received data")
    sub = parser.add_subparsers(dest="action", required=True)

    identify = sub.add_parser("identify", help="Run read-only *IDN? identification")
    identify.add_argument("--expect-model", help="Require this model text in the IDN response")
    identify.add_argument("--log", type=Path, help="Append command evidence to JSONL")

    query = sub.add_parser("query", help="Run one read-only SCPI query")
    query.add_argument("--command", required=True, help="Single SCPI query; writes and ';' are rejected")
    query.add_argument("--log", type=Path, help="Append command evidence to JSONL")

    screenshot = sub.add_parser("screenshot", help="Capture the current screen without changing settings")
    screenshot.add_argument("--format", choices=("PNG", "BMP"), default="PNG")
    screenshot.add_argument("--output", type=Path, required=True)
    screenshot.add_argument("--log", type=Path, help="Append command evidence to JSONL")

    smoke = sub.add_parser("smoke", help="Save IDN, OPC, screenshot, log, and manifest in a run directory")
    smoke.add_argument("--out", type=Path, required=True)
    smoke.add_argument("--expect-model", default="SDS3104X HD")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not (1 <= args.port <= 65535):
        raise ValueError("Port must be between 1 and 65535.")
    if args.timeout <= 0 or args.idle_timeout <= 0:
        raise ValueError("Timeouts must be positive.")
    client = ScpiSocket(args.host, args.port, args.timeout, args.idle_timeout)

    if args.action == "identify":
        idn = run_text_query(client, "*IDN?", args.log)
        ensure_expected_idn(idn, args.expect_model)
        print(idn)
        return 0

    if args.action == "query":
        print(run_text_query(client, args.command, args.log))
        return 0

    if args.action == "screenshot":
        started = utc_now()
        tick = time.perf_counter()
        image = client.screenshot(args.format)
        elapsed_ms = (time.perf_counter() - tick) * 1000
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(image)
        if args.log:
            append_jsonl(args.log, response_record(f":PRINt? {args.format},NORMal", started, elapsed_ms, image))
        print(str(args.output.resolve()))
        return 0

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=False)
    log_path = out / "commands.jsonl"
    started = utc_now()
    idn = run_text_query(client, "*IDN?", log_path)
    ensure_expected_idn(idn, args.expect_model)
    opc = run_text_query(client, "*OPC?", log_path)
    image_started = utc_now()
    tick = time.perf_counter()
    image = client.screenshot("PNG")
    elapsed_ms = (time.perf_counter() - tick) * 1000
    (out / "screen.png").write_bytes(image)
    append_jsonl(log_path, response_record(":PRINt? PNG,NORMal", image_started, elapsed_ms, image))
    (out / "idn_private.txt").write_text(idn + "\n", encoding="utf-8")
    (out / "opc.txt").write_text(opc + "\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "script_version": SCRIPT_VERSION,
        "created_utc": started,
        "transport": "tcp_raw_scpi",
        "host": args.host,
        "port": args.port,
        "instrument": redacted_idn_summary(idn),
        "expected_model": args.expect_model,
        "status": "transport_and_screenshot_verified",
        "physical_wiring_verified": False,
        "privacy": {
            "warning": "idn_private.txt contains the full device serial; exclude or redact it before sharing the run directory.",
            "manifest_serial_redacted": True,
            "commands_jsonl_idn_redacted": True,
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(str(out.resolve()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TimeoutError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
