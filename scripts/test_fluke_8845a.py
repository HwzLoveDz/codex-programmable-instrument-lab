import io
import json
import socketserver
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import fluke_8845a as f


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = False
    block_on_close = True


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        self.server.state["connections"] += 1
        try:
            for raw in self.rfile:
                command = raw.decode("ascii").strip()
                state = self.server.state
                state["commands"].append(command)
                if command == "VOLT:DC:RANG 10":
                    state["range"] = "10"
                    continue
                if command in {"SYST:REM", "SYST:LOC"}:
                    continue
                responses = {
                    "*IDN?": "FLUKE,8845A,SYNTHETIC-SERIAL,TEST-FW",
                    "*OPC?": "1", "CONF?": '"VOLT ' + state["range"] + ',1E-5"',
                    "ROUT:TERM?": "FRON", "VOLT:DC:NPLC?": "10", "ZERO:AUTO?": "1",
                    "CALC:STAT?": "0", "TRIG:SOUR?": "IMM", "TRIG:COUN?": "1", "SAMP:COUN?": "1",
                    "VOLT:DC:RANG?": state["range"], "VOLT:DC:RANG:AUTO?": "0", "READ?": "3.320100",
                }
                response = state["overrides"].get(command, responses.get(command))
                if response is None:
                    return
                if isinstance(response, bytes):
                    self.request.sendall(response)
                    return
                payload = (response + "\r\n").encode("ascii")
                # Exercise TCP fragmentation instead of assuming one receive per line.
                self.request.sendall(payload[:3])
                self.request.sendall(payload[3:])
        except (OSError, TimeoutError):
            return


@contextmanager
def server(overrides=None):
    srv = Server(("127.0.0.1", 0), Handler)
    srv.state = {"commands": [], "connections": 0, "range": "0.1", "overrides": overrides or {}}
    thread = threading.Thread(target=srv.serve_forever)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(3)
        if thread.is_alive():
            raise RuntimeError("Test server thread did not stop")


@contextmanager
def client(srv, **kwargs):
    c = f.Fluke8845A("127.0.0.1", srv.server_address[1], **kwargs)
    try:
        c.identify()
        yield c
    finally:
        c.close()


class FlukeTest(unittest.TestCase):
    def test_identify_fragmented_and_redacted(self):
        with server() as s, client(s) as c:
            self.assertEqual(c.query("*IDN?"), "FLUKE,8845A,[REDACTED],TEST-FW")
            self.assertNotIn("SYNTHETIC-SERIAL", json.dumps(c.log))

    def test_snapshot_has_no_writes_or_trigger(self):
        with server() as s, client(s) as c:
            self.assertEqual(c.snapshot()["ROUT:TERM?"], "FRON")
            self.assertTrue(all(x.endswith("?") for x in s.state["commands"]))
            self.assertNotIn("READ?", s.state["commands"])

    def test_wrong_model_never_writes(self):
        with server({"*IDN?": "OTHER,MODEL,SYNTHETIC,FW"}) as s:
            c = f.Fluke8845A("127.0.0.1", s.server_address[1])
            with self.assertRaises(ValueError):
                c.identify(True)
            self.assertIsNone(c.sock)
            self.assertEqual(s.state["commands"], ["*IDN?"])

    def test_reject_query_injection_and_trigger(self):
        with server() as s, client(s) as c:
            for command in ("READ?", "MEAS?", "*RST", "CONF?;*RST", "CONF?\n*RST", "SYST:ERR?"):
                with self.subTest(command=command), self.assertRaises(ValueError):
                    c.query(command)
            with self.assertRaises(ValueError):
                c._write("CONF:RES")

    def test_authorization_precedes_transport(self):
        c = f.Fluke8845A("127.0.0.1")
        for wiring, state in ((False, True), (True, False)):
            with self.assertRaises(ValueError):
                c.acquire_dcv({}, 10, 10, wiring, state)
        self.assertEqual(c.log, [])

    def test_success_readback_and_restore(self):
        with server() as s, client(s) as c:
            result = {}
            c.acquire_dcv(result, 10, 3, True, True)
            self.assertEqual(result["statistics"]["count"], 3)
            self.assertAlmostEqual(result["statistics"]["mean_V"], 3.3201)
            self.assertEqual(result["after"]["VOLT:DC:RANG?"], "10")
            self.assertTrue(result["local_restore"]["local_command_processed"])
            self.assertEqual(s.state["commands"][-2:], ["SYST:LOC", "*OPC?"])

    def test_unsafe_baseline_stops_without_writes(self):
        for query, reply in (("ROUT:TERM?", "REAR"), ("CALC:STAT?", "1"), ("TRIG:SOUR?", "EXT"),
                             ("TRIG:COUN?", "2"), ("SAMP:COUN?", "2"), ("CONF?", '"RES 1000,0.1"')):
            with self.subTest(query=query), server({query: reply}) as s, client(s) as c:
                with self.assertRaises(ValueError):
                    c.acquire_dcv({}, 10, 1, True, True)
                self.assertNotIn("SYST:REM", s.state["commands"])
                self.assertNotIn("READ?", s.state["commands"])

    def test_failed_range_readback_does_not_read(self):
        with server({"VOLT:DC:RANG?": "0.1"}) as s, client(s) as c:
            result = {}
            with self.assertRaises(ValueError):
                c.acquire_dcv(result, 10, 1, True, True)
            self.assertNotIn("READ?", s.state["commands"])
            self.assertTrue(result["local_restore"]["local_command_processed"])

    def test_overload_retained_in_log_and_local_restored(self):
        with server({"READ?": "+9.9E37"}) as s, client(s) as c:
            result = {}
            with self.assertRaises(ValueError):
                c.acquire_dcv(result, 10, 3, True, True)
            self.assertEqual(s.state["commands"].count("READ?"), 1)
            self.assertTrue(result["local_restore"]["local_command_processed"])
            self.assertIn("+9.9E37", json.dumps(c.log))

    def test_partial_read_never_replays_and_restore_is_unverified(self):
        with server({"READ?": b"3.32"}) as s, client(s) as c:
            result = {}
            with self.assertRaises(ConnectionError):
                c.acquire_dcv(result, 10, 3, True, True)
            self.assertIsNone(c.sock)
            self.assertFalse(result["local_restore"]["local_command_processed"])
            self.assertEqual(s.state["commands"].count("READ?"), 1)

    def test_restore_preserves_range_and_does_not_enter_remote(self):
        with server() as s, client(s) as c:
            self.assertTrue(c.restore_local()["local_command_processed"])
            self.assertEqual(s.state["range"], "0.1")
            self.assertNotIn("SYST:REM", s.state["commands"])

    def test_single_identity_retry_only(self):
        with server() as s:
            c = f.Fluke8845A("127.0.0.1", s.server_address[1])
            original = f.socket.create_connection
            calls = []
            def connect(*args, **kwargs):
                calls.append(1)
                if len(calls) == 1:
                    raise ConnectionResetError("synthetic reset")
                return original(*args, **kwargs)
            try:
                with patch.object(f.socket, "create_connection", side_effect=connect):
                    c.identify(True)
                self.assertEqual(len(calls), 2)
                self.assertEqual(s.state["commands"], ["*IDN?"])
            finally:
                c.close()

    def test_identity_retry_exhausted(self):
        c = f.Fluke8845A("127.0.0.1")
        with patch.object(f.socket, "create_connection", side_effect=ConnectionResetError()) as mocked:
            with self.assertRaises(ConnectionResetError):
                c.identify(True)
            self.assertEqual(mocked.call_count, 2)
            self.assertIsNone(c.sock)

    def test_total_receive_deadline(self):
        class SlowSocket:
            closed = False
            def settimeout(self, _): pass
            def sendall(self, _): pass
            def recv(self, _):
                time.sleep(0.02)
                return b"x"
            def close(self): self.closed = True
        c = f.Fluke8845A("127.0.0.1", timeout=0.03)
        sock = SlowSocket()
        c.sock = sock
        with self.assertRaises(TimeoutError):
            c._exchange("*IDN?")
        self.assertTrue(sock.closed)

    def test_interrupt_during_exchange_discards_partial_connection(self):
        class InterruptSocket:
            closed = False
            def settimeout(self, _): pass
            def sendall(self, _): pass
            def recv(self, _): raise KeyboardInterrupt()
            def close(self): self.closed = True
        c = f.Fluke8845A("127.0.0.1")
        sock = InterruptSocket()
        c.sock = sock
        c.identified = True
        c.remote_restore_pending = True
        with self.assertRaises(KeyboardInterrupt):
            c._exchange("READ?")
        self.assertTrue(sock.closed)
        self.assertIsNone(c.sock)
        self.assertTrue(c.remote_restore_pending)
        self.assertEqual(c.log[-1]["error"], "KeyboardInterrupt")

    def test_malformed_identity_never_logs_raw(self):
        with server({"*IDN?": "PRIVATE-UNPARSEABLE"}) as s:
            c = f.Fluke8845A("127.0.0.1", s.server_address[1])
            with self.assertRaises(ValueError):
                c.identify()
            self.assertNotIn("PRIVATE-UNPARSEABLE", json.dumps(c.log))

    def test_cli_evidence_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp, server() as s:
            out = Path(tmp) / "new-run"
            args = ["--host", "127.0.0.1", "--port", str(s.server_address[1]), "--out", str(out), "dcv",
                    "--range-v", "10", "--count", "2", "--phase", "synthetic", "--operator-state", "synthetic fixture",
                    "--confirm-wiring", "--confirm-state-changes"]
            with patch("builtins.print"):
                self.assertEqual(f.main(args), 0)
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["socket_closed"])
            self.assertEqual(len(manifest["measurements"]), 2)
            self.assertNotIn("SYNTHETIC-SERIAL", (out / "commands.jsonl").read_text(encoding="utf-8"))
            with self.assertRaises(FileExistsError):
                f.main(args)

    def test_session_reuses_one_connection_for_multiple_phases_then_restores(self):
        with tempfile.TemporaryDirectory() as tmp, server() as s:
            out = Path(tmp) / "session-run"
            requests = [
                {"op": "snapshot"},
                {"op": "dcv", "range_v": 10, "count": 2, "phase": "before", "operator_state": "synthetic before",
                 "confirm_wiring": True, "confirm_state_changes": True},
                {"op": "snapshot"},
                {"op": "dcv", "range_v": 10, "count": 1, "phase": "after", "operator_state": "synthetic after",
                 "confirm_wiring": True, "confirm_state_changes": True},
                {"op": "end"},
            ]
            stdin = io.StringIO("".join(json.dumps(x) + "\n" for x in requests))
            stdout = io.StringIO()
            with patch.object(f.sys, "stdin", stdin), patch.object(f.sys, "stdout", stdout):
                code = f.main(["--host", "127.0.0.1", "--port", str(s.server_address[1]),
                               "--out", str(out), "session"])
            self.assertEqual(code, 0)
            self.assertEqual(s.state["connections"], 1)
            self.assertEqual(s.state["commands"].count("*IDN?"), 1)
            self.assertEqual(s.state["commands"].count("SYST:REM"), 1)
            self.assertEqual(s.state["commands"].count("READ?"), 3)
            self.assertEqual(s.state["commands"].count("SYST:LOC"), 1)
            self.assertEqual(s.state["commands"][-2:], ["SYST:LOC", "*OPC?"])
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["script_version"], "1.1.0")
            self.assertEqual(manifest["connection_count"], 1)
            self.assertEqual(manifest["status"], "session_completed")
            self.assertEqual([step["action"] for step in manifest["steps"]],
                             ["snapshot", "dcv", "snapshot", "dcv", "end"])
            self.assertTrue(manifest["local_restore"]["local_command_processed"])
            self.assertEqual((out / "measurements.csv").read_text(encoding="utf-8").count("before,"), 2)
            self.assertEqual((out / "measurements.csv").read_text(encoding="utf-8").count("after,"), 1)
            events = [json.loads(line) for line in stdout.getvalue().splitlines()]
            self.assertEqual(events[0]["event"], "session_ready")
            self.assertIn("pid", events[0])
            self.assertIn('"end"', events[0]["stop"])
            self.assertEqual(events[-1]["event"], "session_ended")
            self.assertTrue(events[-1]["socket_closed"])
            logs = [json.loads(line) for line in (out / "commands.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([entry["event"] for entry in logs if "event" in entry],
                             ["connected", "socket_closed"])
            self.assertNotIn("SYNTHETIC-SERIAL", json.dumps(manifest) + json.dumps(logs))

    def test_read_only_session_does_not_write_local_command(self):
        with tempfile.TemporaryDirectory() as tmp, server() as s:
            out = Path(tmp) / "read-only"
            with patch.object(f.sys, "stdin", io.StringIO('{"op":"snapshot"}\n{"op":"end"}\n')):
                with patch.object(f.sys, "stdout", io.StringIO()):
                    self.assertEqual(f.main(["--host", "127.0.0.1", "--port", str(s.server_address[1]),
                                             "--out", str(out), "session"]), 0)
            self.assertEqual(s.state["connections"], 1)
            self.assertTrue(all(command.endswith("?") for command in s.state["commands"]))
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["local_restore"], {"required": False})

    def test_session_explicit_identity_retry_before_any_step(self):
        with tempfile.TemporaryDirectory() as tmp, server() as s:
            out = Path(tmp) / "retry"
            original = f.socket.create_connection
            attempts = []
            def connect(*args, **kwargs):
                attempts.append(1)
                if len(attempts) == 1:
                    raise ConnectionResetError("synthetic first connect reset")
                return original(*args, **kwargs)
            with patch.object(f.socket, "create_connection", side_effect=connect):
                with patch.object(f.sys, "stdin", io.StringIO('{"op":"snapshot"}\n{"op":"end"}\n')):
                    with patch.object(f.sys, "stdout", io.StringIO()):
                        self.assertEqual(f.main(["--host", "127.0.0.1", "--port", str(s.server_address[1]),
                                                 "--out", str(out), "--retry-identity-once", "session"]), 0)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(s.state["connections"], 1)
            self.assertEqual(s.state["commands"].count("*IDN?"), 1)
            self.assertNotIn("SYST:LOC", s.state["commands"])
            logs = (out / "commands.jsonl").read_text(encoding="utf-8")
            self.assertIn("one_pre_mutation_identity_retry", logs)

    def test_session_eof_restores_local_and_reports_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp, server() as s:
            out = Path(tmp) / "eof"
            request = {"op": "dcv", "range_v": 10, "count": 1, "phase": "before",
                       "operator_state": "synthetic", "confirm_wiring": True, "confirm_state_changes": True}
            with patch.object(f.sys, "stdin", io.StringIO(json.dumps(request) + "\n")):
                with patch.object(f.sys, "stdout", io.StringIO()):
                    self.assertEqual(f.main(["--host", "127.0.0.1", "--port", str(s.server_address[1]),
                                             "--out", str(out), "session"]), 2)
            self.assertEqual(s.state["connections"], 1)
            self.assertEqual(s.state["commands"][-2:], ["SYST:LOC", "*OPC?"])
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "incomplete_unexpected_stdin_eof")
            self.assertTrue(manifest["local_restore"]["local_command_processed"])

    def test_session_keyboard_interrupt_while_idle_restores_and_closes(self):
        class InterruptedInput:
            def __init__(self, first): self.first = first
            def __iter__(self): return self
            def __next__(self):
                if self.first is not None:
                    line, self.first = self.first, None
                    return line
                raise KeyboardInterrupt()
        with tempfile.TemporaryDirectory() as tmp, server() as s:
            out = Path(tmp) / "interrupt"
            request = {"op": "dcv", "range_v": 10, "count": 1, "phase": "before",
                       "operator_state": "synthetic", "confirm_wiring": True, "confirm_state_changes": True}
            with patch.object(f.sys, "stdin", InterruptedInput(json.dumps(request) + "\n")):
                with patch.object(f.sys, "stdout", io.StringIO()):
                    self.assertEqual(f.main(["--host", "127.0.0.1", "--port", str(s.server_address[1]),
                                             "--out", str(out), "session"]), 130)
            self.assertEqual(s.state["connections"], 1)
            self.assertEqual(s.state["commands"][-2:], ["SYST:LOC", "*OPC?"])
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "incomplete_interrupted")
            self.assertTrue(manifest["socket_closed"])

    def test_session_partial_read_never_reconnects_or_replays(self):
        with tempfile.TemporaryDirectory() as tmp, server({"READ?": b"3.32"}) as s:
            out = Path(tmp) / "partial"
            request = {"op": "dcv", "range_v": 10, "count": 2, "phase": "partial",
                       "operator_state": "synthetic", "confirm_wiring": True, "confirm_state_changes": True}
            with patch.object(f.sys, "stdin", io.StringIO(json.dumps(request) + "\n"
                                                         + json.dumps({"op": "end"}) + "\n")):
                with patch.object(f.sys, "stdout", io.StringIO()):
                    self.assertEqual(f.main(["--host", "127.0.0.1", "--port", str(s.server_address[1]),
                                             "--out", str(out), "session"]), 2)
            self.assertEqual(s.state["connections"], 1)
            self.assertEqual(s.state["commands"].count("READ?"), 1)
            self.assertNotIn("SYST:LOC", s.state["commands"])
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "incomplete_local_restore_unverified")
            self.assertFalse(manifest["local_restore"]["local_command_processed"])

    def test_session_rejects_invalid_operation_without_state_change(self):
        with tempfile.TemporaryDirectory() as tmp, server() as s:
            out = Path(tmp) / "invalid"
            with patch.object(f.sys, "stdin", io.StringIO('{"op":"READ?"}\n')):
                with patch.object(f.sys, "stdout", io.StringIO()):
                    self.assertEqual(f.main(["--host", "127.0.0.1", "--port", str(s.server_address[1]),
                                             "--out", str(out), "session"]), 2)
            self.assertEqual(s.state["connections"], 1)
            self.assertEqual(s.state["commands"], ["*IDN?"])
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["steps"][0]["action"] if "action" in manifest["steps"][0] else None, None)
            self.assertIn("ValueError", manifest["steps"][0]["error"])

    def test_validation_without_network(self):
        for host in ("<IP>", "169.254.x.x", "0.0.0.0", "224.0.0.1", "255.255.255.255"):
            with self.assertRaises(ValueError):
                f.Fluke8845A(host)
        for value in ("nan", "inf", "9.9E37", "1,2"):
            with self.assertRaises(ValueError):
                f.number(value)


if __name__ == "__main__":
    unittest.main()
