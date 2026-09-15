#!/usr/bin/env python3
"""Build a host-side LiteVNA one-port OSL plus forward response calibration."""

from __future__ import annotations

import argparse
import cmath
import csv
import json
import math
import statistics
from pathlib import Path


def read_trace(path: Path, prefix: str) -> list[tuple[int, complex]]:
    rows: list[tuple[int, complex]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append(
                (
                    int(row["frequency_hz"]),
                    complex(float(row[f"raw_{prefix}_real"]), float(row[f"raw_{prefix}_imag"])),
                )
            )
    if not rows:
        raise ValueError(f"empty trace: {path}")
    return rows


def solve_one_port(open_m: complex, short_m: complex, load_m: complex) -> tuple[complex, complex, complex]:
    """Return directivity, reflection tracking, and source match error terms."""
    directivity = load_m
    open_delta = open_m - directivity
    short_delta = short_m - directivity
    denominator = open_delta - short_delta
    if abs(denominator) < 1e-15:
        raise ValueError("OPEN and SHORT are numerically indistinguishable")
    source_match = (open_delta + short_delta) / denominator
    reflection_tracking = open_delta * (1 - source_match)
    if abs(reflection_tracking) < 1e-15:
        raise ValueError("reflection tracking collapsed to zero")
    return directivity, reflection_tracking, source_match


def correct_one_port(measured: complex, terms: tuple[complex, complex, complex]) -> complex:
    directivity, reflection_tracking, source_match = terms
    delta = measured - directivity
    denominator = reflection_tracking + delta * source_match
    if abs(denominator) < 1e-15:
        raise ValueError("one-port correction denominator collapsed to zero")
    return delta / denominator


def correct_forward(measured: complex, isolation: complex, thru: complex) -> complex:
    denominator = thru - isolation
    if abs(denominator) < 1e-15:
        raise ValueError("THRU and ISOLATION are numerically indistinguishable")
    return (measured - isolation) / denominator


def db(value: complex) -> float:
    return 20 * math.log10(abs(value)) if value else float("-inf")


def phase_error_from_180(open_m: complex, short_m: complex) -> float:
    separation = abs(math.degrees(cmath.phase(open_m / short_m))) if short_m else 0.0
    return abs(180.0 - separation)


def aligned(*traces: list[tuple[int, complex]]) -> list[tuple[int, tuple[complex, ...]]]:
    frequencies = [[frequency for frequency, _ in trace] for trace in traces]
    if any(item != frequencies[0] for item in frequencies[1:]):
        raise ValueError("calibration traces do not share an identical frequency grid")
    return [
        (frequencies[0][index], tuple(trace[index][1] for trace in traces))
        for index in range(len(frequencies[0]))
    ]


def write_json_new(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def build(args: argparse.Namespace) -> int:
    open_trace = read_trace(Path(args.open), "s11")
    short_trace = read_trace(Path(args.short), "s11")
    load_trace = read_trace(Path(args.load), "s11")
    isolation_trace = read_trace(Path(args.isolation), "s21")
    thru_trace = read_trace(Path(args.thru), "s21")
    samples = aligned(open_trace, short_trace, load_trace, isolation_trace, thru_trace)
    out = Path(args.out)
    coefficients_path = out / "litevna-host-calibration-1mhz-to1ghz.csv"
    summary_path = out / "litevna-host-calibration-summary.json"
    if coefficients_path.exists() or summary_path.exists():
        raise FileExistsError("calibration result already exists; evidence will not be overwritten")

    open_magnitudes: list[float] = []
    short_magnitudes: list[float] = []
    load_return_loss: list[float] = []
    open_short_phase_error: list[float] = []
    isolation_db: list[float] = []
    thru_db: list[float] = []
    tracking_margin_db: list[float] = []
    solved: list[tuple[int, complex, complex, complex, complex, complex]] = []
    for frequency, values in samples:
        open_m, short_m, load_m, isolation_m, thru_m = values
        terms = solve_one_port(open_m, short_m, load_m)
        solved.append((frequency, *terms, isolation_m, thru_m))
        open_magnitudes.append(abs(open_m))
        short_magnitudes.append(abs(short_m))
        load_return_loss.append(-db(load_m))
        open_short_phase_error.append(phase_error_from_180(open_m, short_m))
        isolation_db.append(db(isolation_m))
        thru_db.append(db(thru_m))
        tracking_margin_db.append(db(thru_m - isolation_m) - db(isolation_m))

    quality = {
        "open_raw_magnitude_median": statistics.median(open_magnitudes),
        "short_raw_magnitude_median": statistics.median(short_magnitudes),
        "load_raw_return_loss_db_median": statistics.median(load_return_loss),
        "open_short_raw_phase_error_from_180_deg_median": statistics.median(open_short_phase_error),
        "isolation_raw_db_median": statistics.median(isolation_db),
        "isolation_raw_db_max": max(isolation_db),
        "single_cable_thru_raw_db_median": statistics.median(thru_db),
        "tracking_over_isolation_margin_db_min": min(tracking_margin_db),
    }
    gates = {
        "open_magnitude_gt_0_5": min(open_magnitudes) > 0.5,
        "short_magnitude_gt_0_5": min(short_magnitudes) > 0.5,
        "load_return_loss_median_gt_10_db": quality["load_raw_return_loss_db_median"] > 10.0,
        "open_short_phase_error_median_lt_45_deg": quality["open_short_raw_phase_error_from_180_deg_median"] < 45.0,
        "tracking_over_isolation_margin_min_gt_20_db": quality["tracking_over_isolation_margin_db_min"] > 20.0,
    }
    if not all(gates.values()):
        raise RuntimeError(f"calibration quality gate failed: {gates}; metrics={quality}")

    out.mkdir(parents=True, exist_ok=True)
    with coefficients_path.open("x", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "frequency_hz", "directivity_real", "directivity_imag",
            "reflection_tracking_real", "reflection_tracking_imag",
            "source_match_real", "source_match_imag", "isolation_real", "isolation_imag",
            "thru_real", "thru_imag",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for frequency, directivity, tracking, source_match, isolation, thru in solved:
            writer.writerow({
                "frequency_hz": frequency,
                "directivity_real": directivity.real,
                "directivity_imag": directivity.imag,
                "reflection_tracking_real": tracking.real,
                "reflection_tracking_imag": tracking.imag,
                "source_match_real": source_match.real,
                "source_match_imag": source_match.imag,
                "isolation_real": isolation.real,
                "isolation_imag": isolation.imag,
                "thru_real": thru.real,
                "thru_imag": thru.imag,
            })
    summary = {
        "calibration_type": "host-side one-port OSL plus forward isolation/THRU response calibration",
        "frequency_grid": {"start_hz": samples[0][0], "stop_hz": samples[-1][0], "points": len(samples)},
        "reference_planes": {
            "s11": "LiteVNA PORT1 connector",
            "s21": "PORT1-to-PORT2 path with the selected single male-male cable normalized as THRU",
        },
        "quality_metrics": quality,
        "quality_gates": gates,
        "limitations": [
            "Not a full bidirectional 12-term two-port SOLT calibration.",
            "The OPEN standard was the bare PORT1 connector; its parasitic capacitance is not characterized.",
            "Valid only for the recorded 1 MHz to 1 GHz, 401-point grid and unchanged selected THRU cable path.",
            "LiteVNA USB samples are raw; these coefficients must be applied by host software.",
        ],
        "result": "accepted" if all(gates.values()) else "rejected",
    }
    write_json_new(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--open", required=True)
    result.add_argument("--short", required=True)
    result.add_argument("--load", required=True)
    result.add_argument("--isolation", required=True)
    result.add_argument("--thru", required=True)
    result.add_argument("--out", required=True)
    return result


if __name__ == "__main__":
    raise SystemExit(build(parser().parse_args()))
