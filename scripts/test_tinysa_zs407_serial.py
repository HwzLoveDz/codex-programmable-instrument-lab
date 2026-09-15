#!/usr/bin/env python3

import argparse
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tinysa_zs407_serial as tinysa


class FakeSerial:
    def __init__(self, responses):
        self.responses = [response.encode("ascii") for response in responses]
        self.pending = b""
        self.writes = []
        self.closed = False

    def reset_input_buffer(self):
        self.pending = b""

    def write(self, data):
        self.writes.append(data)
        if not self.responses:
            raise AssertionError("unexpected command")
        self.pending = self.responses.pop(0)
        return len(data)

    def read(self, size=1):
        result, self.pending = self.pending[:size], self.pending[size:]
        return result

    def close(self):
        self.closed = True


def scan_response(start=100000, step=100000, levels=(-90.0, -100.0, -95.5)):
    lines = [f"scan {start} {start + step * (len(levels) - 1)} {len(levels)} 3"]
    for index, level in enumerate(levels):
        encoded = "-:.000000e+01" if level == -100.0 else f"{level:.6e}"
        lines.append(f"{start + step * index} {encoded} 0.000000000")
    return "\r\n".join(lines) + "\r\nch> \r\n"


class TinySANumberTest(unittest.TestCase):
    def test_normal_and_firmware_quirk_numbers(self):
        self.assertEqual(tinysa.parse_tinysa_number("-9.350000e+01"), -93.5)
        self.assertEqual(tinysa.parse_tinysa_number("-:.000000e+01"), -100.0)
        self.assertEqual(tinysa.parse_tinysa_number("-:.500000e+01"), -105.0)
        self.assertEqual(tinysa.parse_tinysa_number("-;.000000e+01"), -110.0)
        with self.assertRaises(ValueError):
            tinysa.parse_tinysa_number("not-a-level")
        with self.assertRaises(ValueError):
            tinysa.parse_tinysa_number("nan")

    def test_scan_parser_accepts_live_three_column_shape(self):
        command = "scan 100000 300000 3 3"
        rows = tinysa.parse_scan_response(
            scan_response(), command=command, expected_points=3
        )
        self.assertEqual([row.frequency_hz for row in rows], [100000, 200000, 300000])
        self.assertEqual(rows[1].measured_dbm, -100.0)
        self.assertEqual(rows[1].firmware_extra_value, 0.0)

    def test_scan_parser_fails_closed(self):
        command = "scan 100000 300000 3 3"
        malformed = scan_response().replace("200000 -:.000000e+01", "200000 broken")
        with self.assertRaisesRegex(ValueError, "malformed"):
            tinysa.parse_scan_response(malformed, command=command, expected_points=3)
        descending = (
            f"{command}\r\n100000 -90 0\r\n300000 -91 0\r\n200000 -92 0\r\nch> \r\n"
        )
        with self.assertRaisesRegex(ValueError, "increasing"):
            tinysa.parse_scan_response(descending, command=command, expected_points=3)


class AdapterTest(unittest.TestCase):
    def test_model_check_fails_closed(self):
        tinysa.require_zs407("tinySA ULTRA+ ZS407\r\nFirmware Version: test")
        with self.assertRaisesRegex(RuntimeError, "did not identify"):
            tinysa.require_zs407("some other instrument")

    def test_redact_info_accepts_live_version_label(self):
        redacted = tinysa.redact_info(
            "tinySA ULTRA+ ZS407\r\nVersion: tinySA4_v1.4-test\r\n"
            "Build Time: test\r\nDevice ID: SECRET123"
        )
        self.assertEqual(redacted["firmware"], "Version: tinySA4_v1.4-test")
        self.assertNotIn("SECRET123", json.dumps(redacted))

    def test_safe_pause_requires_confirmation_and_never_enables_output(self):
        fake = FakeSerial(["output off\r\nch> \r\n", "caloutput off\r\nch> \r\n", "pause\r\nch> \r\n"])
        device = tinysa.TinySASerial(fake, timeout=0.1)
        with self.assertRaises(PermissionError):
            device.safe_pause(authorized=False)
        device.safe_pause(authorized=True)
        self.assertEqual(fake.writes, [b"output off\r", b"caloutput off\r", b"pause\r"])
        self.assertNotIn(b"output on\r", fake.writes)

    def test_command_rejects_multiline_and_scan_bounds(self):
        device = tinysa.TinySASerial(FakeSerial([]), timeout=0.01)
        for command in ("", "info\routput on", "info\noutput on"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                device.command(command)
        with self.assertRaises(ValueError):
            device.scan(99999, 1000000, 10)
        with self.assertRaises(ValueError):
            device.scan(100000, 1000000, 291)

    def test_scan_run_preserves_evidence_and_redacts_summary(self):
        info = (
            "info\r\ntinySA ULTRA+ ZS407\r\n"
            "Firmware Version: tinySA4_v1.4-217-gc5dd31f\r\n"
            "Build Time: Dec 17 2025\r\nDevice ID: SECRET123\r\nch> \r\n"
        )
        responses = [
            info,
            "output off\r\nch> \r\n",
            "caloutput off\r\nch> \r\n",
            "pause\r\nch> \r\n",
            scan_response(),
            scan_response(levels=(-89.0, -100.0, -94.0)),
            scan_response(levels=(-91.0, -99.0, -96.0)),
        ]
        fake = FakeSerial(responses)
        with tempfile.TemporaryDirectory() as temp_dir:
            args = argparse.Namespace(
                port="TESTPORT",
                baud=115200,
                timeout=0.1,
                out=temp_dir,
                phase="board-off",
                start_hz=100000,
                stop_hz=300000,
                points=3,
                warmup_scans=1,
                repeats=2,
                settle_seconds=0,
                external_attenuation_db=20.0,
                probe="small H loop",
                probe_position="DCDC inductor",
                operator_dut_state="off",
                invalidates_phase=None,
                confirm_input_only=True,
                confirm_state_changes=True,
            )
            with mock.patch.object(tinysa, "open_serial", return_value=fake):
                self.assertEqual(tinysa.scan_run(args), 0)
            self.assertTrue(fake.closed)
            summary_text = (Path(temp_dir) / "board-off-summary.json").read_text(encoding="utf-8")
            self.assertNotIn("SECRET123", summary_text)
            self.assertIn("[REDACTED]", summary_text)
            self.assertIn("SECRET123", (Path(temp_dir) / "tinysa_info_private.txt").read_text(encoding="utf-8"))
            self.assertTrue((Path(temp_dir) / "board-off-scan-01.txt").exists())
            with (Path(temp_dir) / "board-off-traces.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(float(rows[1]["median_dbm_at_analyzer"]), -99.5)
            commands = (Path(temp_dir) / "commands.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("SECRET123", commands)
            self.assertNotIn("output on", commands)

    def test_scan_run_rejects_missing_confirmations_and_overwrite(self):
        base = dict(
            port="TESTPORT", baud=115200, timeout=0.1, out="unused", phase="test",
            start_hz=100000, stop_hz=300000, points=3, warmup_scans=1, repeats=2,
            settle_seconds=0, external_attenuation_db=20.0, probe="H", probe_position="P",
            operator_dut_state="off", invalidates_phase=None,
            confirm_input_only=False, confirm_state_changes=True,
        )
        with self.assertRaises(PermissionError):
            tinysa.scan_run(argparse.Namespace(**base))


class CompareTest(unittest.TestCase):
    def test_compare_excludes_edge_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name, level_field, levels in (
                ("off", "level_dbm_at_analyzer", (-80, -100, -100, -80)),
                ("on", "median_dbm_at_analyzer", (-60, -90, -70, -60)),
            ):
                fieldnames = ["frequency_hz", level_field]
                with (root / f"{name}.csv").open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fieldnames)
                    writer.writeheader()
                    for frequency, level in zip((1, 2, 3, 4), levels):
                        writer.writerow({"frequency_hz": frequency, level_field: level})
            args = argparse.Namespace(
                off_csv=str(root / "off.csv"), on_csv=str(root / "on.csv"),
                out=str(root / "comparison.csv"), exclude_edge_bins=1, top=5,
            )
            self.assertEqual(tinysa.compare_runs(args), 0)
            summary = json.loads((root / "comparison.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["top_deltas"][0]["frequency_hz"], 3)
            with self.assertRaises(FileExistsError):
                tinysa.compare_runs(args)

            invalid = argparse.Namespace(**vars(args))
            invalid.out = str(root / "invalid.csv")
            invalid.exclude_edge_bins = 2
            with self.assertRaisesRegex(ValueError, "every analysis row"):
                tinysa.compare_runs(invalid)


if __name__ == "__main__":
    unittest.main()
