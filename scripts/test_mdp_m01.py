"""Offline protocol, safety, and persistent-session tests for mdp_m01."""

from __future__ import annotations

import io
import json
import struct
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import mdp_m01 as mdp


def make_status_packet(channels: list[dict[str, int]], channel_field: int = 0) -> bytes:
    payload = bytearray()
    for index in range(6):
        spec = channels[index] if index < len(channels) else {}
        row = bytearray(24)
        row[0] = index
        struct.pack_into("<HHHHHHH", row, 1,
                         spec.get("voltage_mv", 0), spec.get("current_ma", 0),
                         spec.get("input_voltage_mv", 0), spec.get("input_current_ma", 0),
                         spec.get("set_voltage_mv", 0), spec.get("current_limit_ma", 0),
                         spec.get("temperature_deci_c", 250))
        row[15] = spec.get("online", 0)
        row[16] = spec.get("machine_type", 0)
        row[17] = spec.get("locked", 0)
        row[18] = spec.get("mode", 0)
        row[19] = spec.get("output", 0)
        row[23] = spec.get("error", 0)
        payload.extend(row)
    return mdp._packet(mdp.SYNTHESIZE, channel_field, bytes(payload))


class FakeSerial:
    def __init__(self, *_args, **_kwargs):
        self.rx = bytearray()
        self.closed = False
        self.writes: list[tuple[int, int, bytes]] = []
        self.ignore_output = False
        self.states = [
            {"machine_type": 2, "online": 1, "mode": 2, "set_voltage_mv": 0, "current_limit_ma": 300},
            {"machine_type": 3, "online": 1, "mode": 0, "set_voltage_mv": 0, "current_limit_ma": 0},
        ]

    def reset_input_buffer(self):
        self.rx.clear()

    def write(self, raw: bytes) -> int:
        packet_type, size, channel = raw[2], raw[3], raw[4]
        payload = raw[6:size]
        self.writes.append((packet_type, channel, payload))
        if packet_type in (mdp.SET_VOLTAGE, mdp.SET_CURRENT) and channel < len(self.states):
            voltage_mv, current_ma = struct.unpack("<HH", payload)
            self.states[channel]["set_voltage_mv"] = voltage_mv
            self.states[channel]["current_limit_ma"] = current_ma
        elif packet_type == mdp.SET_OUTPUT and channel < len(self.states) and not self.ignore_output:
            self.states[channel]["output"] = payload[0]
        elif packet_type == mdp.HEARTBEAT:
            # Split each response into small pieces to exercise the incremental parser.
            self.rx.extend(b"noiseZ" + make_status_packet(self.states))
        return len(raw)

    def flush(self):
        return None

    def read(self, size: int) -> bytes:
        if not self.rx:
            time.sleep(0.005)
            return b""
        count = min(size, 13, len(self.rx))
        result = bytes(self.rx[:count])
        del self.rx[:count]
        return result

    def close(self):
        self.closed = True


class MDPAdapterTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSerial()
        self.serial_stub = SimpleNamespace(
            Serial=lambda *args, **kwargs: self.fake,
            EIGHTBITS=8,
            PARITY_NONE="N",
            STOPBITS_ONE=1,
        )
        self.patch_serial = patch.object(mdp, "serial", self.serial_stub)
        self.patch_serial.start()
        self.connection = mdp.MDPConnection("COM-MOCK", timeout_s=0.06)
        self.connection.__enter__()

    def tearDown(self):
        self.connection.close()
        self.patch_serial.stop()

    def arm(self):
        return self.connection.arm(mdp.SafetyEnvelope(5.0, 0.3, 1.5), confirm_wiring=True)

    def test_parser_recovers_from_noise_and_split_magic(self):
        raw = mdp._packet(mdp.SYNTHESIZE, 2, bytes(range(24 * 6)))
        parser = mdp._PacketParser()
        packets = []
        for block in (b"junkZ", raw[:1], raw[1:5], raw[5:79], raw[79:]):
            packets.extend(parser.feed(block))
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0][0:2], (mdp.SYNTHESIZE, 2))

    def test_status_decodes_device_type_mode_and_output_independently(self):
        status = self.connection.read_status()
        self.assertEqual(status.selected_channel, 1)
        self.assertEqual(status.channels[0].machine, "P906")
        self.assertEqual(status.channels[1].machine, "L1060")
        self.assertEqual(status.channels[1].mode, "CC")
        self.assertFalse(status.channels[1].output_enabled)

    def test_p906_and_l1060_setpoints_require_armed_off_channels_and_readback(self):
        self.arm()
        p906 = self.connection.set_p906(1, 5.0, 0.3)
        self.assertAlmostEqual(p906.channels[0].set_voltage_v, 5.0)
        self.assertAlmostEqual(p906.channels[0].current_limit_a, 0.3)
        l1060 = self.connection.set_l1060_cc(2, 0.1)
        self.assertAlmostEqual(l1060.channels[1].current_limit_a, 0.1)
        set_writes = [item for item in self.fake.writes if item[0] in (mdp.SET_VOLTAGE, mdp.SET_CURRENT)]
        self.assertEqual(len(set_writes), 6)  # two documented setpoint frames per value
        self.assertEqual({item[1] for item in set_writes}, {0, 1})

    def test_l1060_current_is_conservatively_capped_at_one_amp(self):
        self.arm()
        with self.assertRaises(mdp.MDPValidationError):
            self.connection.set_l1060_cc(2, 1.001)
        self.assertFalse(any(item[0] == mdp.SET_CURRENT for item in self.fake.writes))

    def test_energy_output_requires_an_explicit_per_command_confirmation(self):
        self.arm()
        self.connection.set_p906(1, 5.0, 0.3)
        with self.assertRaises(mdp.MDPValidationError):
            self.connection.set_output(1, True)
        self.assertFalse(any(item[0] == mdp.SET_OUTPUT for item in self.fake.writes))
        self.connection.set_output(1, True, confirm_energy=True)
        self.assertTrue(self.fake.states[0]["output"])

    def test_arm_refuses_if_any_device_is_already_on(self):
        self.fake.states[0]["output"] = 1
        with self.assertRaises(mdp.MDPStateError):
            self.arm()
        self.assertFalse(any(item[0] in (mdp.SET_OUTPUT, mdp.SET_VOLTAGE, mdp.SET_CURRENT) for item in self.fake.writes))

    def test_off_is_single_write_and_mismatch_is_never_replayed(self):
        self.fake.states[0]["output"] = 1
        self.fake.ignore_output = True
        with self.assertRaises(mdp.MDPTimeoutError):
            self.connection.set_output(1, False)
        output_writes = [item for item in self.fake.writes if item[0] == mdp.SET_OUTPUT]
        self.assertEqual(len(output_writes), 1)
        self.assertEqual(output_writes[0][2], b"\x00")

    def test_output_off_remains_available_when_channel_reports_error(self):
        self.fake.states[0]["output"] = 1
        self.fake.states[0]["error"] = 1
        status = self.connection.set_output(1, False)
        self.assertFalse(status.channels[0].output_enabled)

    def test_connection_stays_open_across_jsonl_session_steps_and_end_is_read_only(self):
        stdin = io.StringIO('{"op":"status"}\n{"op":"end"}\n')
        stdout = io.StringIO()
        result = mdp.run_session(self.connection, stdin, stdout)
        events = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(result, 0)
        self.assertEqual([event["event"] for event in events], ["session_ready", "status", "session_ended"])
        self.assertFalse(self.fake.closed)
        self.assertFalse(any(item[0] in (mdp.SET_OUTPUT, mdp.SET_VOLTAGE, mdp.SET_CURRENT) for item in self.fake.writes))

    def test_json_session_never_replays_failed_output_transition(self):
        self.arm()
        self.fake.ignore_output = True
        stdin = io.StringIO('{"op":"output_on","channel":2,"confirm_energy":true}\n')
        stdout = io.StringIO()
        result = mdp.run_session(self.connection, stdin, stdout)
        events = [json.loads(line) for line in stdout.getvalue().splitlines()]
        writes = [item for item in self.fake.writes if item[0] == mdp.SET_OUTPUT]
        self.assertEqual(result, 2)
        self.assertEqual(len(writes), 1)
        self.assertEqual(events[-1]["event"], "session_stopped")


if __name__ == "__main__":
    unittest.main()
