#!/usr/bin/env python3

import struct
import unittest

import litevna_zn406 as litevna


class FakeSerial:
    def __init__(self, response=b""):
        self.response = bytearray(response)
        self.writes = []
        self.closed = False

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def read(self, size=1):
        data = self.response[:size]
        del self.response[:size]
        return bytes(data)

    def close(self):
        self.closed = True


class LiteVNAProtocolTest(unittest.TestCase):
    def test_cli_separates_reference_and_dut_confirmations(self):
        parser = litevna.build_parser()
        reference = parser.parse_args(
            ["--port", "TESTPORT", "acquire-s21-reference", "--out", "run", "--phase", "ref"]
        )
        dut = parser.parse_args(
            ["--port", "TESTPORT", "acquire-s21-dut", "--out", "run", "--phase", "dut"]
        )
        self.assertEqual(reference.fixture_kind, "thru-reference")
        self.assertFalse(reference.confirm_dut_wiring)
        self.assertEqual(dut.fixture_kind, "dut")
        self.assertFalse(dut.confirm_thru_wiring)

    def test_identity_is_read_only_and_validated(self):
        fake = FakeSerial(b"2" + bytes([2, 1, 2, 2, 2, 1, 3]))
        device = litevna.LiteVNA(fake, timeout=0.01)
        identity = device.identify()
        self.assertEqual(identity.device_variant, 2)
        self.assertEqual(identity.protocol_version, 1)
        self.assertTrue(all(write[0] in (litevna.INDICATE, litevna.READ1) for write in fake.writes))

    def test_s21_math_and_zero_forward_rejection(self):
        value = litevna.RawValue(100, 0, 0, 0, 50, 0, 7)
        self.assertEqual(value.s21(), 0.5 + 0j)
        self.assertEqual(litevna.RawValue(100, 0, -25, 0, 0, 0, 7).s11(), -0.25 + 0j)
        with self.assertRaises(ValueError):
            litevna.RawValue(0, 0, 0, 0, 1, 0, 0).s21()

    def test_cli_exposes_separate_calibration_wiring_confirmations(self):
        parser = litevna.build_parser()
        standard = parser.parse_args([
            "--port", "TESTPORT", "acquire-cal-standard", "--out", "run", "--phase", "open",
            "--standard", "open",
        ])
        isolation = parser.parse_args([
            "--port", "TESTPORT", "acquire-s21-isolation", "--out", "run", "--phase", "isolation",
        ])
        self.assertEqual(standard.standard, "open")
        self.assertFalse(standard.confirm_standard_wiring)
        self.assertEqual(isolation.fixture_kind, "isolation")
        self.assertFalse(isolation.confirm_isolation_wiring)

    def test_configure_has_bounded_allowlist(self):
        fake = FakeSerial(b"2")
        device = litevna.LiteVNA(fake, timeout=0.01)
        step = device.configure_s21_sweep(1_000_000, 1_000_000_000, 401)
        self.assertEqual(step, 2_497_500)
        addresses = [payload[1] for payload in fake.writes if payload[0] in (litevna.WRITE1, litevna.WRITE2, litevna.WRITE8)]
        self.assertEqual(addresses, [0x44, 0x00, 0x10, 0x20, 0x22])
        with self.assertRaises(ValueError):
            device.configure_s21_sweep(49_999, 1_000_000, 10)

    def test_fifo_requires_complete_ordered_sweep(self):
        records = []
        for index in range(3):
            records.append(litevna.VALUE_STRUCT.pack(100, 0, 0, 0, 50, 0, index))
        fake = FakeSerial(b"2" + b"".join(records))
        device = litevna.LiteVNA(fake, timeout=0.01)
        values = device.acquire_one_sweep(3)
        self.assertEqual([value.frequency_index for value in values], [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
