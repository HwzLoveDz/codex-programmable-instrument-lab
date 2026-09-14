#!/usr/bin/env python3

import socketserver
import json
import struct
import tempfile
import threading
import time
import unittest
import zlib
from contextlib import contextmanager
from pathlib import Path

import siglent_socket


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def make_png() -> bytes:
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    scanline = zlib.compress(b"\x00\x00\x00\x00")
    return (
        siglent_socket.PNG_SIGNATURE
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", scanline)
        + png_chunk(b"IEND", b"")
    )


def make_bmp() -> bytes:
    pixel_data = b"\x00\x00\x00\x00"
    pixel_offset = 14 + 40
    file_size = pixel_offset + len(pixel_data)
    file_header = b"BM" + struct.pack("<IHHI", file_size, 0, 0, pixel_offset)
    dib_header = struct.pack(
        "<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, len(pixel_data), 2835, 2835, 0, 0
    )
    return file_header + dib_header + pixel_data


PNG = make_png()
BMP = make_bmp()


class QuietThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        with self.server.state_lock:
            self.server.connection_count += 1
        pending = bytearray()
        while True:
            while b"\n" not in pending:
                chunk = self.request.recv(1024)
                if not chunk:
                    return
                pending.extend(chunk)
            newline = pending.index(10)
            command = bytes(pending[: newline + 1])
            del pending[: newline + 1]
            with self.server.state_lock:
                self.server.commands.append(command)
            if command == b"*IDN?\n":
                self.request.sendall(b"Siglent Technologies,SDS3104XHD,TESTSERIAL,1.0.4.2\n")
            elif command == b"*OPC?\n":
                self.request.sendall(b"1\n")
            elif command == b":PAIR?\n":
                self.request.sendall(b"first\nsecond\n")
            elif command == b":NOOP?\n":
                pass
            elif command == b":PRINt? PNG,NORMal\n":
                self.request.sendall(PNG + b"\r\n")


class DelayedTextHandler(socketserver.BaseRequestHandler):
    def handle(self):
        command = bytearray()
        while not command.endswith(b"\n"):
            chunk = self.request.recv(1024)
            if not chunk:
                return
            command.extend(chunk)
        self.request.sendall(b"Siglent Technologies,")
        time.sleep(0.08)
        self.request.sendall(b"SDS3104XHD,SERIAL,FW\n")


class PartialEofHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.recv(1024)
        self.request.sendall(b"partial")


class PartialTimeoutHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.recv(1024)
        self.request.sendall(b"partial")
        time.sleep(0.3)


class OversizeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.recv(1024)
        self.request.sendall(b"0123456789ABCDEF")


@contextmanager
def running_server(handler):
    server = QuietThreadingTCPServer(("127.0.0.1", 0), handler)
    server.connection_count = 0
    server.commands = []
    server.state_lock = threading.Lock()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class ClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_context = running_server(Handler)
        cls.server = cls.server_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.server_context.__exit__(None, None, None)

    def client(self, **kwargs):
        return siglent_socket.ScpiSocket(
            "127.0.0.1", self.server.server_address[1], timeout=1, **kwargs
        )

    def test_identify_and_screenshot(self):
        idn = self.client().query("*IDN?")
        siglent_socket.ensure_expected_idn(idn, "SDS3104X HD")
        self.assertEqual(self.client().screenshot("PNG"), PNG)

    def test_read_only_query_parser_accepts_only_header_query(self):
        for command in ("*IDN?", ":MEASure:SIMPle:VALue? FREQ", ":PRINt? PNG,NORMal"):
            self.assertEqual(siglent_socket.validate_read_only_query(command), command)
        invalid = (
            ":RUN",
            ":RUN;*IDN?",
            ":RUN\n*IDN?",
            ':DISPlay:TEXT "?"',
            ":RUN value?",
            ":FOO??",
            ":FOO? BAR?",
            ':FOO "argument?"',
        )
        for command in invalid:
            with self.subTest(command=command), self.assertRaises(ValueError):
                siglent_socket.validate_read_only_query(command)

    def test_idn_checks_only_manufacturer_and_model_fields(self):
        siglent_socket.ensure_expected_idn(
            "Siglent Technologies,SDS3104XHD,SERIAL,FW", "SDS3104X HD"
        )
        siglent_socket.ensure_expected_idn("SIGLENT,SDS3104X HD,SERIAL,FW", "SDS3104XHD")
        rejected = (
            "Other,SDS3104XHD,SIGLENT,FW",
            "Siglent Technologies,OTHER,SDS3104XHD,FW",
            "Siglent Technologies",
        )
        for idn in rejected:
            with self.subTest(idn=idn), self.assertRaises(RuntimeError):
                siglent_socket.ensure_expected_idn(idn, "SDS3104X HD")

    def test_bmp_and_ieee_block_parsing(self):
        length = str(len(BMP)).encode("ascii")
        block = b"#" + str(len(length)).encode("ascii") + length + BMP + b"\r\n"
        self.assertEqual(siglent_socket.extract_image(block, "BMP"), BMP)

    def test_png_crc_and_structure_validation(self):
        self.assertEqual(siglent_socket.extract_image(PNG, "PNG"), PNG)
        corrupt = bytearray(PNG)
        corrupt[-1] ^= 0x01
        with self.assertRaisesRegex(ValueError, "CRC"):
            siglent_socket.extract_image(bytes(corrupt), "PNG")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            siglent_socket.extract_image(PNG[:-12], "PNG")

    def test_bmp_structure_validation(self):
        self.assertEqual(siglent_socket.extract_image(BMP, "BMP"), BMP)
        corrupt = bytearray(BMP)
        struct.pack_into("<I", corrupt, 10, 1)
        with self.assertRaisesRegex(ValueError, "pixel offset"):
            siglent_socket.extract_image(bytes(corrupt), "BMP")

    def test_delayed_packets_outlive_idle_timeout(self):
        with running_server(DelayedTextHandler) as server:
            client = siglent_socket.ScpiSocket(
                "127.0.0.1", server.server_address[1], timeout=0.5, idle_timeout=0.02
            )
            self.assertEqual(
                client.query("*IDN?"), "Siglent Technologies,SDS3104XHD,SERIAL,FW"
            )

    def test_partial_eof_is_not_success(self):
        with running_server(PartialEofHandler) as server:
            client = siglent_socket.ScpiSocket(
                "127.0.0.1", server.server_address[1], timeout=0.5, idle_timeout=0.02
            )
            with self.assertRaises(ConnectionError):
                client.query("*IDN?")

    def test_partial_timeout_is_not_success(self):
        with running_server(PartialTimeoutHandler) as server:
            client = siglent_socket.ScpiSocket(
                "127.0.0.1", server.server_address[1], timeout=0.08, idle_timeout=0.01
            )
            with self.assertRaises(TimeoutError):
                client.query("*IDN?")

    def test_response_size_limit(self):
        with running_server(OversizeHandler) as server:
            client = siglent_socket.ScpiSocket(
                "127.0.0.1",
                server.server_address[1],
                timeout=0.5,
                idle_timeout=0.02,
                max_response_bytes=8,
            )
            with self.assertRaisesRegex(ValueError, "limit"):
                client.query("*IDN?")

    def test_persistent_session_write_order_text_residual_and_binary_tail(self):
        before = self.server.connection_count
        client = self.client(idle_timeout=0.02)
        with client as session:
            session._write_line(":AUToset")
            self.assertEqual(session.query("*OPC?"), "1")
            self.assertEqual(session.query(":PAIR?"), "first")
            self.assertEqual(session.query(":NOOP?"), "second")
            self.assertEqual(session.screenshot("PNG"), PNG)
            self.assertEqual(session.query("*OPC?"), "1")
        self.assertIsNone(client._sock)
        self.assertEqual(self.server.connection_count - before, 1)
        self.assertEqual(
            self.server.commands[-6:],
            [
                b":AUToset\n",
                b"*OPC?\n",
                b":PAIR?\n",
                b":NOOP?\n",
                b":PRINt? PNG,NORMal\n",
                b"*OPC?\n",
            ],
        )

    def test_protected_write_requires_connection_and_rejects_unsafe_lines(self):
        client = self.client()
        with self.assertRaises(RuntimeError):
            client._write_line(":AUToset")
        with client:
            for command in ("AUToset", ":FOO?", ":FOO;:BAR", ":FOO\r", ":FOO\n"):
                with self.subTest(command=command), self.assertRaises(ValueError):
                    client._write_line(command)

    def test_smoke_creates_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out = Path(temp_dir) / "run"
            rc = siglent_socket.main([
                "--host", "127.0.0.1",
                "--port", str(self.server.server_address[1]),
                "smoke", "--out", str(out),
            ])
            self.assertEqual(rc, 0)
            self.assertTrue((out / "manifest.json").exists())
            self.assertTrue((out / "idn_private.txt").exists())
            self.assertFalse((out / "idn.txt").exists())
            self.assertEqual((out / "screen.png").read_bytes(), PNG)
            manifest_text = (out / "manifest.json").read_text(encoding="utf-8")
            self.assertNotIn("TESTSERIAL", manifest_text)
            manifest = json.loads(manifest_text)
            self.assertEqual(manifest["instrument"]["serial"], "[REDACTED]")
            self.assertIn("TESTSERIAL", (out / "idn_private.txt").read_text(encoding="utf-8"))
            commands_text = (out / "commands.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("TESTSERIAL", commands_text)
            self.assertIn("[REDACTED]", commands_text)
            self.assertGreaterEqual(
                len(commands_text.splitlines()), 3
            )


if __name__ == "__main__":
    unittest.main()
