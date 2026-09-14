#!/usr/bin/env python3

import csv
import json
import socketserver
import struct
import tempfile
import threading
import unittest
import zlib
from pathlib import Path

import siglent_cal_check


def png_chunk(chunk_type, data):
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


PNG = (
    b"\x89PNG\r\n\x1a\n"
    + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    + png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
    + png_chunk(b"IEND", b"")
)


class FakeScopeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        responses = {
            "*IDN?": self.server.idn_response,
            "*OPC?": "1",
            ":CHANnel1:IMPedance?": self.server.impedance_response,
            ":CHANnel1:PROBe?": self.server.probe_response,
            ":CHANnel1:SCALe?": "5.00E-01",
            ":CHANnel1:OFFSet?": "-1.48E+00",
            ":TIMebase:SCALe?": "5.00E-04",
            ":ACQuire:SRATe?": "4.00E+08",
            ":ACQuire:MDEPth?": "2.00E+06",
            ":TRIGger:MODE?": "AUTO",
            ":TRIGger:STATus?": "Trig'd",
            ":TRIGger:TYPE?": "EDGE",
            ":TRIGger:FREQuency?": "1.00E+03",
            ":MEASure:SIMPle:VALue? TOP": "2.978627E+00",
            ":MEASure:SIMPle:VALue? BASE": "2.157E-03",
            ":MEASure:SIMPle:VALue? AMPL": "2.976471E+00",
            ":MEASure:SIMPle:VALue? PER": "9.999997E-04",
            ":MEASure:SIMPle:VALue? RISE10T90": "2.146E-07",
            ":MEASure:SIMPle:VALue? FALL90T10": "2.153E-07",
            ":MEASure:SIMPle:VALue? OVSN": "8.70E-01",
            ":MEASure:SIMPle:VALue? OVSP": "****",
        }
        buffer = b""
        while True:
            chunk = self.request.recv(1024)
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                command, buffer = buffer.split(b"\n", 1)
                text = command.decode("ascii").strip()
                with self.server.commands_lock:
                    self.server.commands.append(text)
                if text in siglent_cal_check.FIXED_WRITES:
                    with self.server.state_lock:
                        if not self.server.ignore_all_writes:
                            if text == siglent_cal_check.AUTOSET_COMMAND:
                                if not self.server.ignore_autoset_write:
                                    self.server.autoset_outcome_ready = True
                            elif text == siglent_cal_check.MEASUREMENT_MODE_COMMAND:
                                self.server.measurement_mode = "SIMPle"
                            elif text == siglent_cal_check.SOURCE_COMMAND:
                                self.server.measurement_source = "C1"
                            elif not self.server.ignore_item_writes:
                                item = text.split("ITEM ", 1)[1].split(",", 1)[0]
                                self.server.enabled_items.add(item)
                    continue
                if text == siglent_cal_check.SCREEN_QUERY:
                    self.request.sendall(PNG[:9])
                    self.request.sendall(PNG[9:])
                elif text == ":MEASure:MODE?":
                    with self.server.state_lock:
                        response = self.server.measurement_mode
                    self.request.sendall(response.encode("ascii") + b"\n")
                elif text == siglent_cal_check.SOURCE_QUERY:
                    with self.server.state_lock:
                        response = self.server.measurement_source
                    self.request.sendall(response.encode("ascii") + b"\n")
                elif text.startswith(":MEASure:SIMPle:VALue? "):
                    item = text.rsplit(" ", 1)[1]
                    with self.server.state_lock:
                        ready = (
                            self.server.autoset_outcome_ready
                            and self.server.measurement_mode == "SIMPle"
                            and self.server.measurement_source == "C1"
                        )
                        core_item_ready = item not in {"FREQ", "PKPK"} or item in self.server.enabled_items
                    if not ready or not core_item_ready:
                        response = "****"
                    elif item == "FREQ":
                        response = self.server.freq_response
                    elif item == "PKPK":
                        response = self.server.pkpk_response
                    else:
                        response = responses.get(text, "****")
                    self.request.sendall(response.encode("ascii") + b"\n")
                elif text in responses:
                    self.request.sendall(responses[text].encode("ascii") + b"\n")


class FakeScopeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        probe_response="1.00E+01",
        impedance_response="ONEMeg",
        freq_response="1.000000E+03",
        pkpk_response="3.036471E+00",
        idn_response="Siglent Technologies,SDS3104XHD,TESTSERIAL,6.8.14.1.0.4.2",
        ignore_all_writes=False,
        ignore_autoset_write=False,
        ignore_item_writes=False,
    ):
        super().__init__(("127.0.0.1", 0), FakeScopeHandler)
        self.probe_response = probe_response
        self.impedance_response = impedance_response
        self.freq_response = freq_response
        self.pkpk_response = pkpk_response
        self.idn_response = idn_response
        self.ignore_all_writes = ignore_all_writes
        self.ignore_autoset_write = ignore_autoset_write
        self.ignore_item_writes = ignore_item_writes
        self.autoset_outcome_ready = False
        self.measurement_mode = "ADVanced"
        self.measurement_source = "C2"
        self.enabled_items = set()
        self.commands = []
        self.commands_lock = threading.Lock()
        self.state_lock = threading.Lock()


class RunningFakeScope:
    def __init__(self, **kwargs):
        self.server = FakeScopeServer(**kwargs)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.server

    def __exit__(self, exc_type, exc, traceback):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def valid_args(server, out):
    return [
        "--host", "127.0.0.1",
        "--port", str(server.server_address[1]),
        "--out", str(out),
        "--timeout", "1",
        "--idle-timeout", "0.05",
        "--samples", "2",
        "--settle-seconds", "0",
        "--sample-interval", "0",
        "--confirm-cal-wiring",
        "--confirm-scope-state-changes",
    ]


class CalCheckTest(unittest.TestCase):
    def test_confirmation_is_required_before_network_or_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out = Path(temp_dir) / "run"
            with self.assertRaisesRegex(ValueError, "--confirm-cal-wiring"):
                siglent_cal_check.main([
                    "--host", "127.0.0.1",
                    "--port", "1",
                    "--out", str(out),
                ])
            self.assertFalse(out.exists())

    def test_state_change_confirmation_is_separate_and_precedes_network(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out = Path(temp_dir) / "run"
            with self.assertRaisesRegex(ValueError, "--confirm-scope-state-changes"):
                siglent_cal_check.main([
                    "--host", "127.0.0.1",
                    "--port", "1",
                    "--out", str(out),
                    "--confirm-cal-wiring",
                ])
            self.assertFalse(out.exists())

    def test_full_loop_creates_evidence_and_uses_only_fixed_writes(self):
        with RunningFakeScope() as server, tempfile.TemporaryDirectory() as temp_dir:
            out = Path(temp_dir) / "run"
            self.assertEqual(siglent_cal_check.main(valid_args(server, out)), 0)

            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "cal_loop_acquired")
            self.assertTrue(manifest["scope_state_changes_confirmed_by_operator"])
            self.assertTrue(manifest["scope_state_may_have_changed"])
            self.assertEqual(manifest["measurement_setup_opc"], "1")
            self.assertEqual(manifest["measurement_samples"], 2)
            self.assertEqual(
                manifest["measurement_summary"]["positive_overshoot_percent"]["available_samples"],
                0,
            )
            self.assertEqual((out / "before.png").read_bytes(), PNG)
            self.assertEqual((out / "after.png").read_bytes(), PNG)

            with (out / "measurements.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["frequency_hz"], "1000.0")
            self.assertEqual(rows[0]["positive_overshoot_percent"], "")

            commands = server.commands
            autoset_index = commands.index(siglent_cal_check.AUTOSET_COMMAND)
            self.assertLess(commands.index("*IDN?"), autoset_index)
            self.assertLess(commands.index(":CHANnel1:IMPedance?"), autoset_index)
            self.assertLess(commands.index(":CHANnel1:PROBe?"), autoset_index)
            self.assertLess(commands.index(siglent_cal_check.SCREEN_QUERY), autoset_index)
            self.assertLess(autoset_index, commands.index("*OPC?"))
            source_write_index = commands.index(siglent_cal_check.SOURCE_COMMAND)
            source_query_indices = [
                index
                for index, command in enumerate(commands)
                if command == siglent_cal_check.SOURCE_QUERY
            ]
            self.assertTrue(any(index < autoset_index for index in source_query_indices))
            self.assertTrue(any(index > source_write_index for index in source_query_indices))
            writes = [command for command in commands if "?" not in command]
            self.assertEqual(
                writes,
                list(siglent_cal_check.FIXED_WRITES),
            )
            self.assertEqual(commands.count(siglent_cal_check.SCREEN_QUERY), 2)
            self.assertEqual(commands.count("*OPC?"), 2)
            last_item_index = max(commands.index(command) for command in siglent_cal_check.CORE_ITEM_COMMANDS)
            setup_opc_index = [index for index, command in enumerate(commands) if command == "*OPC?"][1]
            mode_readback_index = next(
                index
                for index, command in enumerate(commands)
                if command == ":MEASure:MODE?" and index > setup_opc_index
            )
            self.assertLess(last_item_index, setup_opc_index)
            self.assertLess(setup_opc_index, mode_readback_index)
            commands_text = (out / "commands.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("TESTSERIAL", commands_text)
            self.assertIn("[REDACTED]", commands_text)
            self.assertIn("TESTSERIAL", (out / "idn_private.txt").read_text(encoding="utf-8"))

    def test_other_siglent_model_stops_before_any_write(self):
        with RunningFakeScope(
            idn_response="Siglent Technologies,SDS2104X Plus,TESTSERIAL,1.0.0"
        ) as server, tempfile.TemporaryDirectory() as temp_dir:
            out = Path(temp_dir) / "run"
            with self.assertRaisesRegex(RuntimeError, "Expected model"):
                siglent_cal_check.main(valid_args(server, out))

            self.assertEqual(server.commands, ["*IDN?"])
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["expected_model"], siglent_cal_check.SUPPORTED_MODEL)
            self.assertEqual(manifest["scope_left_in"], "No scope write was sent by this run.")

    def test_ignored_all_writes_cannot_be_reported_as_acquired(self):
        with RunningFakeScope(ignore_all_writes=True) as server, tempfile.TemporaryDirectory() as temp_dir:
            out = Path(temp_dir) / "run"
            with self.assertRaisesRegex(RuntimeError, "Measurement mode read-back"):
                siglent_cal_check.main(valid_args(server, out))

            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertNotEqual(manifest["status"], "cal_loop_acquired")

    def test_ignored_autoset_write_cannot_produce_a_cal_outcome(self):
        with RunningFakeScope(ignore_autoset_write=True) as server, tempfile.TemporaryDirectory() as temp_dir:
            out = Path(temp_dir) / "run"
            with self.assertRaisesRegex(RuntimeError, "No sample contained plausible"):
                siglent_cal_check.main(valid_args(server, out))

            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "core_measurements_unavailable")

    def test_ignored_item_writes_cannot_produce_a_cal_outcome(self):
        with RunningFakeScope(ignore_item_writes=True) as server, tempfile.TemporaryDirectory() as temp_dir:
            out = Path(temp_dir) / "run"
            with self.assertRaisesRegex(RuntimeError, "No sample contained plausible"):
                siglent_cal_check.main(valid_args(server, out))

            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "core_measurements_unavailable")

    def test_probe_mismatch_stops_before_any_write(self):
        with RunningFakeScope(probe_response="1.00E+00") as server, tempfile.TemporaryDirectory() as temp_dir:
            out = Path(temp_dir) / "run"
            with self.assertRaisesRegex(siglent_cal_check.SafetyGateError, "No scope writes"):
                siglent_cal_check.main(valid_args(server, out))

            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "safety_gate_failed_before_writes")
            self.assertTrue((out / "before.png").is_file())
            self.assertNotIn(siglent_cal_check.AUTOSET_COMMAND, server.commands)
            self.assertNotIn(siglent_cal_check.SOURCE_COMMAND, server.commands)

    def test_measurement_star_response_is_unavailable(self):
        self.assertIsNone(siglent_cal_check.parse_measurement("****"))
        self.assertIsNone(siglent_cal_check.parse_measurement("9.91E+37"))
        self.assertIsNone(siglent_cal_check.parse_measurement(""))
        self.assertIsNone(siglent_cal_check.parse_measurement("not-a-number"))
        self.assertIsNone(siglent_cal_check.parse_measurement("NaN"))
        self.assertIsNone(siglent_cal_check.parse_measurement("Inf"))
        self.assertIsNone(siglent_cal_check.parse_measurement("-Inf"))
        self.assertIsNone(siglent_cal_check.parse_measurement("-1", "frequency_hz"))
        self.assertIsNone(siglent_cal_check.parse_measurement("-3", "pkpk_v"))
        self.assertEqual(siglent_cal_check.parse_measurement("2.146E-07"), 2.146e-7)

    def test_missing_core_measurements_retains_failure_evidence(self):
        with RunningFakeScope(freq_response="****", pkpk_response="9.91E+37") as server, tempfile.TemporaryDirectory() as temp_dir:
            out = Path(temp_dir) / "run"
            with self.assertRaisesRegex(RuntimeError, "Cal acquisition loop is not complete"):
                siglent_cal_check.main(valid_args(server, out))

            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "core_measurements_unavailable")
            self.assertTrue((out / "measurements.csv").is_file())
            self.assertTrue((out / "after.png").is_file())


if __name__ == "__main__":
    unittest.main()
