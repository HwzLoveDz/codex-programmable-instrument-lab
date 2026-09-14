#!/usr/bin/env python3
"""Run a narrowly scoped SDS3104X HD CH1/front-panel-Cal verification.

This is deliberately not a general SCPI writer. It only performs Auto Setup
and a fixed Simple Measurement setup for C1, after an explicit wiring
confirmation and read-back safety checks for 1 Mohm input and a 10X probe.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from siglent_socket import (
    ScpiSocket,
    append_jsonl,
    ensure_expected_idn,
    redacted_idn_summary,
    response_record,
    utc_now,
)


SCRIPT_VERSION = "0.2.0"
SUPPORTED_MODEL = "SDS3104X HD"
SCREEN_QUERY = ":PRINt? PNG,NORMal"
AUTOSET_COMMAND = ":AUToset"
MEASUREMENT_MODE_COMMAND = ":MEASure:MODE SIMPle"
SOURCE_COMMAND = ":MEASure:SIMPle:SOURce C1"
SOURCE_QUERY = ":MEASure:SIMPle:SOURce?"
CORE_ITEM_COMMANDS = (
    ":MEASure:SIMPle:ITEM FREQ,ON",
    ":MEASure:SIMPle:ITEM PKPK,ON",
)
FIXED_WRITES = (AUTOSET_COMMAND, MEASUREMENT_MODE_COMMAND, SOURCE_COMMAND, *CORE_ITEM_COMMANDS)
CAL_FREQUENCY_SANITY_HZ = (100.0, 10_000.0)
CAL_PKPK_SANITY_V = (0.1, 20.0)

BASELINE_QUERIES = (
    ("channel1_impedance", ":CHANnel1:IMPedance?"),
    ("channel1_probe_factor", ":CHANnel1:PROBe?"),
    ("channel1_scale_v_per_div", ":CHANnel1:SCALe?"),
    ("channel1_offset_v", ":CHANnel1:OFFSet?"),
    ("timebase_scale_s_per_div", ":TIMebase:SCALe?"),
    ("sample_rate_sa_per_s", ":ACQuire:SRATe?"),
    ("memory_depth_points", ":ACQuire:MDEPth?"),
    ("trigger_mode", ":TRIGger:MODE?"),
    ("trigger_status", ":TRIGger:STATus?"),
    ("trigger_type", ":TRIGger:TYPE?"),
    ("trigger_frequency_hz", ":TRIGger:FREQuency?"),
    ("measurement_mode", ":MEASure:MODE?"),
    ("simple_measurement_source", SOURCE_QUERY),
)

MEASUREMENT_ITEMS = (
    ("frequency_hz", "FREQ", "Hz"),
    ("pkpk_v", "PKPK", "V"),
    ("top_v", "TOP", "V"),
    ("base_v", "BASE", "V"),
    ("amplitude_v", "AMPL", "V"),
    ("period_s", "PER", "s"),
    ("rise_10_90_s", "RISE10T90", "s"),
    ("fall_90_10_s", "FALL90T10", "s"),
    ("negative_overshoot_percent", "OVSN", "%"),
    ("positive_overshoot_percent", "OVSP", "%"),
)


class SafetyGateError(RuntimeError):
    """Raised before scope writes when the physical/configuration gate fails."""


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _log_query(client: ScpiSocket, command: str, log_path: Path, phase: str) -> str:
    started = utc_now()
    tick = time.perf_counter()
    try:
        response = client.query(command)
    except Exception as exc:
        record = {
            "timestamp_utc": started,
            "phase": phase,
            "operation": "query",
            "command": command,
            "success": False,
            "elapsed_ms": round((time.perf_counter() - tick) * 1000, 3),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        append_jsonl(log_path, record)
        raise
    elapsed_ms = (time.perf_counter() - tick) * 1000
    payload = response.encode("utf-8")
    record = response_record(command, started, elapsed_ms, payload)
    record.update(
        {
            "phase": phase,
            "operation": "query",
            "success": True,
            "response_text": (
                ",".join(redacted_idn_summary(response).values())
                if command.strip().upper() == "*IDN?"
                else response
            ),
        }
    )
    if command.strip().upper() == "*IDN?":
        record["response_redacted"] = True
        record["full_response_location"] = "idn_private.txt"
    append_jsonl(log_path, record)
    return response


def _capture_screen(client: ScpiSocket, path: Path, log_path: Path, phase: str) -> None:
    started = utc_now()
    tick = time.perf_counter()
    try:
        image = client.screenshot("PNG")
    except Exception as exc:
        append_jsonl(
            log_path,
            {
                "timestamp_utc": started,
                "phase": phase,
                "operation": "screenshot",
                "command": SCREEN_QUERY,
                "success": False,
                "elapsed_ms": round((time.perf_counter() - tick) * 1000, 3),
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        raise
    path.write_bytes(image)
    record = response_record(SCREEN_QUERY, started, (time.perf_counter() - tick) * 1000, image)
    record.update(
        {
            "phase": phase,
            "operation": "screenshot",
            "success": True,
            "artifact": path.name,
        }
    )
    append_jsonl(log_path, record)


def _send_fixed_write(client: ScpiSocket, command: str, log_path: Path, phase: str) -> None:
    if command not in FIXED_WRITES:
        raise ValueError("Internal safety error: command is not in the fixed Cal-check allowlist.")
    started = utc_now()
    tick = time.perf_counter()
    try:
        client._write_line(command)
    except Exception as exc:
        append_jsonl(
            log_path,
            {
                "timestamp_utc": started,
                "phase": phase,
                "operation": "fixed_write",
                "command": command,
                "success": False,
                "elapsed_ms": round((time.perf_counter() - tick) * 1000, 3),
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        raise
    append_jsonl(
        log_path,
        {
            "timestamp_utc": started,
            "phase": phase,
            "operation": "fixed_write_sent",
            "command": command,
            "success": True,
            "delivery_status": "sent_on_persistent_session; effect_requires_following observable synchronization/readback",
            "elapsed_ms": round((time.perf_counter() - tick) * 1000, 3),
            "response_bytes": 0,
            "response_sha256": hashlib.sha256(b"").hexdigest(),
            "authorization": ["--confirm-cal-wiring", "--confirm-scope-state-changes"],
        },
    )


def _is_one_megohm(response: str) -> bool:
    normalized = "".join(char for char in response.upper() if char.isalnum() or char in ".+-")
    if normalized in {"ONEMEG", "1MEG", "1MOHM", "1M"}:
        return True
    try:
        value = float(response.strip())
    except ValueError:
        return False
    return math.isclose(value, 1_000_000.0, rel_tol=0.001)


def _is_ten_x(response: str) -> bool:
    normalized = response.strip().upper()
    if normalized.endswith("X"):
        normalized = normalized[:-1].strip()
    try:
        value = float(normalized)
    except ValueError:
        return False
    return math.isclose(value, 10.0, rel_tol=0.001)


def _is_channel1(response: str) -> bool:
    normalized = "".join(char for char in response.upper() if char.isalnum())
    return normalized in {"C1", "CH1", "CHAN1", "CHANNEL1"}


def parse_measurement(response: str, field: str | None = None) -> float | None:
    text = response.strip()
    if not text or set(text) == {"*"}:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value) or abs(value) > 1e12:
        return None
    if field in {"frequency_hz", "period_s", "rise_10_90_s", "fall_90_10_s"} and value <= 0:
        return None
    if field in {
        "pkpk_v",
        "amplitude_v",
        "negative_overshoot_percent",
        "positive_overshoot_percent",
    } and value < 0:
        return None
    return value


def _core_cal_sample_is_sane(row: dict[str, Any]) -> bool:
    frequency = row.get("frequency_hz")
    pkpk = row.get("pkpk_v")
    return (
        frequency is not None
        and pkpk is not None
        and CAL_FREQUENCY_SANITY_HZ[0] <= frequency <= CAL_FREQUENCY_SANITY_HZ[1]
        and CAL_PKPK_SANITY_V[0] <= pkpk <= CAL_PKPK_SANITY_V[1]
    )


def _is_simple_mode(response: str) -> bool:
    return "".join(char for char in response.upper() if char.isalnum()) == "SIMPLE"


def _write_result(path: Path, manifest: dict[str, Any]) -> None:
    instrument = manifest.get("instrument", {})
    lines = [
        "# SDS3104X HD 前面板 Cal 受限闭环",
        "",
        f"- 状态：`{manifest.get('status', 'unknown')}`",
        f"- 仪器：`{instrument.get('manufacturer', 'unknown')} {instrument.get('model', 'unknown')}`",
        f"- 接线确认：`{manifest.get('wiring_confirmed_by_operator', False)}`",
        f"- 示波器状态改变授权：`{manifest.get('scope_state_changes_confirmed_by_operator', False)}`",
        f"- 已确认无 `2 GHz ONLY` 标签：`{manifest.get('probe_no_2ghz_only_label_confirmed', False)}`",
        f"- 运行后状态：{manifest.get('scope_left_in', '未确定；检查前面板与 commands.jsonl')}",
        "",
        "此运行只验证受限控制、探头识别、采集和证据闭环；前面板 Cal 不是可溯源标准，",
        "结果不能证明示波器幅度/时基校准或 SP3050A 的 500 MHz 带宽。",
        "完整 IDN 只保存在受控的 idn_private.txt；commands.jsonl 默认遮蔽序列号。",
        "分享整个运行目录前仍应排除或脱敏 idn_private.txt。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _idn_summary(idn: str) -> dict[str, Any]:
    fields = [part.strip() for part in idn.split(",")]
    return {
        "manufacturer": fields[0] if fields else None,
        "model": fields[1] if len(fields) > 1 else None,
        "serial": "REDACTED" if len(fields) > 2 and fields[2] else None,
        "firmware": fields[3] if len(fields) > 3 else None,
        "idn_sha256": hashlib.sha256(idn.encode("utf-8")).hexdigest(),
        "full_idn_location": "idn_private.txt",
    }


def _measurement_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field, scpi_item, unit in MEASUREMENT_ITEMS:
        values = [row[field] for row in rows if row[field] is not None]
        entry: dict[str, Any] = {
            "scpi_item": scpi_item,
            "unit": unit,
            "available_samples": len(values),
            "unavailable_samples": len(rows) - len(values),
        }
        if values:
            entry.update(
                {
                    "mean": statistics.fmean(values),
                    "min": min(values),
                    "max": max(values),
                    "population_stdev": statistics.pstdev(values) if len(values) > 1 else 0.0,
                }
            )
        result[field] = entry
    return result


def _write_measurements_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = ["sample_index", "timestamp_utc"] + [field for field, _, _ in MEASUREMENT_ITEMS]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _read_scope_state(client: ScpiSocket, log_path: Path, phase: str) -> dict[str, str]:
    return {
        name: _log_query(client, command, log_path, phase)
        for name, command in BASELINE_QUERIES
    }


def _validate_args(args: argparse.Namespace) -> None:
    if not args.confirm_cal_wiring:
        raise ValueError(
            "Refusing scope writes: physically connect the SP3050A from CH1 to front-panel Cal, "
            "then pass --confirm-cal-wiring."
        )
    if not args.confirm_scope_state_changes:
        raise ValueError(
            "Refusing scope writes: Auto Setup and Simple Measurement settings change scope state; "
            "obtain operator authorization, then pass --confirm-scope-state-changes."
        )
    if not (1 <= args.port <= 65535):
        raise ValueError("Port must be between 1 and 65535.")
    if args.timeout <= 0 or args.idle_timeout <= 0:
        raise ValueError("Timeouts must be positive.")
    if not (1 <= args.samples <= 100):
        raise ValueError("Samples must be between 1 and 100.")
    if args.settle_seconds < 0 or args.sample_interval < 0:
        raise ValueError("Settle time and sample interval must not be negative.")


def run_cal_check(args: argparse.Namespace) -> Path:
    _validate_args(args)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=False)
    log_path = out / "commands.jsonl"
    client = ScpiSocket(args.host, args.port, args.timeout, args.idle_timeout)
    created_utc = utc_now()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "script_version": SCRIPT_VERSION,
        "created_utc": created_utc,
        "transport": "tcp_raw_scpi",
        "host": args.host,
        "port": args.port,
        "expected_model": SUPPORTED_MODEL,
        "scope_channel": "C1",
        "signal_source": "front_panel_cal",
        "expected_signal": "nominal 1 kHz, 3 V square wave; not a traceable calibrator",
        "expected_probe": "SP3050A, 10X",
        "probe_no_2ghz_only_label_confirmed": args.confirm_no_2ghz_only_label,
        "wiring_confirmed_by_operator": True,
        "scope_state_changes_confirmed_by_operator": True,
        "writes_allowlist": list(FIXED_WRITES),
        "verification_semantics": {
            "opc": "*OPC? proves synchronization only; it does not report a command error queue.",
            "mode_source": "Only the separately queried Simple mode and C1 source states are directly read back.",
            "autoset_items": (
                "The published SDS3000X HD command guide documents no error-queue query, no AutoSet query, "
                "and no Simple ITEM query. Plausible FREQ/PKPK acquisition is observable outcome evidence, "
                "not proof that each write command was accepted."
            ),
        },
        "scope_state_may_have_changed": False,
        "privacy": {
            "idn_private_contains_serial": True,
            "commands_jsonl_idn_redacted": True,
            "share_warning": "Do not publish idn_private.txt without redacting the serial.",
        },
        "status": "running",
    }

    try:
        client.connect()
        idn = _log_query(client, "*IDN?", log_path, "baseline")
        (out / "idn_private.txt").write_text(idn + "\n", encoding="utf-8")
        ensure_expected_idn(idn, SUPPORTED_MODEL)
        manifest["instrument"] = _idn_summary(idn)

        baseline = _read_scope_state(client, log_path, "baseline")
        _capture_screen(client, out / "before.png", log_path, "baseline")
        impedance_ok = _is_one_megohm(baseline["channel1_impedance"])
        probe_ok = _is_ten_x(baseline["channel1_probe_factor"])
        baseline["safety_gate"] = {
            "one_megohm_input": impedance_ok,
            "ten_x_probe": probe_ok,
        }
        _write_json(out / "baseline.json", baseline)
        manifest["baseline_safety_gate"] = baseline["safety_gate"]
        if not impedance_ok or not probe_ok:
            manifest["status"] = "safety_gate_failed_before_writes"
            raise SafetyGateError(
                "CH1 safety read-back failed; required 1 Mohm input and 10X probe. "
                f"Observed impedance={baseline['channel1_impedance']!r}, "
                f"probe={baseline['channel1_probe_factor']!r}. No scope writes were sent."
            )

        manifest["scope_state_may_have_changed"] = True
        _send_fixed_write(client, AUTOSET_COMMAND, log_path, "autoset")
        opc = _log_query(client, "*OPC?", log_path, "autoset")
        if opc.strip() != "1":
            raise RuntimeError(f"Synchronization after Auto Setup did not return 1; got {opc!r}.")
        manifest["autoset_opc"] = opc

        manifest["simple_measurement_source_after_autoset"] = _log_query(
            client, SOURCE_QUERY, log_path, "autoset_readback"
        )
        _send_fixed_write(client, MEASUREMENT_MODE_COMMAND, log_path, "measurement_setup")
        _send_fixed_write(client, SOURCE_COMMAND, log_path, "measurement_source")
        for command in CORE_ITEM_COMMANDS:
            _send_fixed_write(client, command, log_path, "measurement_setup")
        setup_opc = _log_query(client, "*OPC?", log_path, "measurement_setup")
        if setup_opc.strip() != "1":
            raise RuntimeError(f"Synchronization after measurement setup did not return 1; got {setup_opc!r}.")
        manifest["measurement_setup_opc"] = setup_opc
        mode_readback = _log_query(client, ":MEASure:MODE?", log_path, "measurement_setup")
        if not _is_simple_mode(mode_readback):
            raise RuntimeError(f"Measurement mode read-back is not Simple: {mode_readback!r}")
        manifest["measurement_mode_readback"] = mode_readback
        source_readback = _log_query(client, SOURCE_QUERY, log_path, "measurement_source")
        if not _is_channel1(source_readback):
            raise RuntimeError(f"Simple Measurement source read-back is not C1: {source_readback!r}")
        manifest["simple_measurement_source_readback"] = source_readback

        if args.settle_seconds:
            time.sleep(args.settle_seconds)

        rows: list[dict[str, Any]] = []
        for sample_index in range(1, args.samples + 1):
            row: dict[str, Any] = {
                "sample_index": sample_index,
                "timestamp_utc": utc_now(),
            }
            for field, scpi_item, _ in MEASUREMENT_ITEMS:
                response = _log_query(
                    client,
                    f":MEASure:SIMPle:VALue? {scpi_item}",
                    log_path,
                    "measurement",
                )
                row[field] = parse_measurement(response, field)
            rows.append(row)
            if args.sample_interval and sample_index < args.samples:
                time.sleep(args.sample_interval)

        measurements_path = out / "measurements.csv"
        _write_measurements_csv(measurements_path, rows)
        sane_core_samples = sum(_core_cal_sample_is_sane(row) for row in rows)
        manifest["core_cal_sanity"] = {
            "purpose": "broad wiring/acquisition sanity only; not a calibration tolerance",
            "frequency_hz_inclusive": list(CAL_FREQUENCY_SANITY_HZ),
            "pkpk_v_inclusive": list(CAL_PKPK_SANITY_V),
            "sane_samples": sane_core_samples,
            "core_measurement_outcome_evidence": (
                "FREQ and PKPK returned finite in-range values after setup synchronization; this proves "
                "the required acquisition outcome, not per-command acceptance."
            ),
        }
        if sane_core_samples == 0:
            _capture_screen(client, out / "after.png", log_path, "measurement_unavailable")
            manifest["status"] = "core_measurements_unavailable"
            raise RuntimeError(
                "No sample contained plausible positive FREQ and PKPK values for the nominal Cal source; evidence was retained, "
                "but the Cal acquisition loop is not complete."
            )
        final_state = _read_scope_state(client, log_path, "final_state")
        final_state["simple_measurement_source"] = _log_query(
            client, SOURCE_QUERY, log_path, "final_state"
        )
        _write_json(out / "final_state.json", final_state)
        _capture_screen(client, out / "after.png", log_path, "final")

        post_impedance_ok = _is_one_megohm(final_state["channel1_impedance"])
        post_probe_ok = _is_ten_x(final_state["channel1_probe_factor"])
        post_source_ok = _is_channel1(final_state["simple_measurement_source"])
        manifest["final_safety_readback"] = {
            "one_megohm_input": post_impedance_ok,
            "ten_x_probe": post_probe_ok,
            "simple_measurement_source_c1": post_source_ok,
        }
        if not (post_impedance_ok and post_probe_ok and post_source_ok):
            raise RuntimeError("Final scope read-back did not preserve 1 Mohm / 10X / C1 conditions.")

        manifest["measurement_samples"] = args.samples
        manifest["measurement_summary"] = _measurement_summary(rows)
        manifest["status"] = "cal_loop_acquired"
        manifest["scope_left_in"] = (
            "Post-command state; Simple Measurement mode/source C1 observed. "
            "AutoSet acceptance is not independently queryable."
        )
        _write_result(out / "result.md", manifest)
        artifact_names = (
            "commands.jsonl",
            "idn_private.txt",
            "baseline.json",
            "before.png",
            "measurements.csv",
            "final_state.json",
            "after.png",
            "result.md",
        )
        manifest["artifacts_sha256"] = {
            name: _sha256_file(out / name) for name in artifact_names
        }
        _write_json(out / "manifest.json", manifest)
        return out
    except (Exception, KeyboardInterrupt) as exc:
        if manifest["status"] in {"running", "cal_loop_acquired"}:
            manifest["status"] = "failed"
        if manifest.get("scope_state_may_have_changed"):
            manifest["scope_left_in"] = (
                "Potentially partial Auto Setup/Simple Measurement state; inspect the front panel "
                "and commands.jsonl before continuing."
            )
        else:
            manifest["scope_left_in"] = "No scope write was sent by this run."
        manifest["error_type"] = type(exc).__name__
        manifest["error"] = str(exc)
        manifest["failed_utc"] = utc_now()
        existing_artifacts = (
            "commands.jsonl",
            "idn_private.txt",
            "baseline.json",
            "before.png",
            "measurements.csv",
            "final_state.json",
            "after.png",
            "result.md",
        )
        _write_result(out / "result.md", manifest)
        manifest["artifacts_sha256"] = {
            name: _sha256_file(out / name)
            for name in existing_artifacts
            if (out / name).is_file()
        }
        _write_json(out / "manifest.json", manifest)
        raise
    finally:
        client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="SDS3104X HD IPv4 address or host name")
    parser.add_argument("--port", type=int, default=5025, help="Raw SCPI socket port (default: 5025)")
    parser.add_argument("--timeout", type=float, default=20.0, help="Per-command timeout in seconds")
    parser.add_argument("--idle-timeout", type=float, default=0.3, help="Idle gap after response data")
    parser.add_argument("--out", type=Path, required=True, help="New evidence directory (must not exist)")
    parser.add_argument("--samples", type=int, default=5, help="Measurement repetitions (1-100)")
    parser.add_argument("--settle-seconds", type=float, default=1.0)
    parser.add_argument("--sample-interval", type=float, default=0.1)
    parser.add_argument(
        "--confirm-cal-wiring",
        action="store_true",
        help="Confirm SP3050A is connected from CH1 to the front-panel Cal terminal",
    )
    parser.add_argument(
        "--confirm-scope-state-changes",
        action="store_true",
        help="Confirm the operator authorizes Auto Setup and fixed Simple Measurement state changes",
    )
    parser.add_argument(
        "--confirm-no-2ghz-only-label",
        action="store_true",
        help="Record that the SP3050A compensation box has no early '2 GHz ONLY' label",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = run_cal_check(args)
    print(str(out.resolve()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TimeoutError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
