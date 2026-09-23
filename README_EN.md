# Codex Programmable Instrument Lab

[中文](README.md)

A Codex skill for programmable bench instruments. It turns test intent, safe wiring, instrument configuration, acquisition, acceptance criteria, and evidence retention into a reproducible closed-loop hardware workflow.

Complete or constrained hardware workflows cover the **SIGLENT SDS3104X HD**, **tinySA Ultra+ ZS407**, **LiteVNA 64 ZN-406**, and **FLUKE 8845A**. One low-power integration run also covered **MDP-M01 + P906 + L1060 + FLUKE 8845A**. MDP-M01 uses MINIWARE's proprietary USB binary protocol, not SCPI; this repository now includes an initial constrained MDP CLI/adapter, but it has only passed offline fake-serial tests and is not yet hardware-validated. Commands and transports are never assumed to be portable across models.

## Current capabilities

- SIGLENT native SCPI over LAN sockets (TCP 5025) for read-only identification, queries, and screenshots;
- FLUKE 8845A LAN control (TCP 3490) for redacted identification, configuration snapshots, low-voltage DC acquisition, and local-control restoration;
- one low-power integration run using MDP-M01 USB control for a P906 5 V supply and L1060 CC load, measured in parallel with a FLUKE 8845A and documented with status readback and meter discrepancies;
- persistent MDP-M01 USB sessions, status reads, constrained P906/L1060-CC setpoints and readback, and per-command-confirmed output control (fake-serial tested; hardware validation pending);
- strict validation of SCPI query headers, complete LF-terminated text responses, binary boundaries, and PNG CRCs;
- a constrained SDS3104X HD + SP3050A + front-panel Cal loop;
- ZS407 USB CDC identification, safe pause, one-shot narrow scans, and matched off/on comparisons;
- board-level relative EMI hotspot localization with large/small H-field probes and a probable E-field probe;
- a separate ambient-spectrum and extended/collapsed A/B mode for uncalibrated telescopic antennas;
- compatibility with the three-column `scan` response and approximately -100 dBm text-format quirk observed on the validated ZS407 firmware;
- LiteVNA SAA2 USB binary identification, constrained S11/S21 sweeps, and Normal-mode restoration;
- host-side PORT1 one-port OSL plus forward Isolation/THRU calibration with quality gates and explicit reference planes;
- allowlisted write commands, two independent operator confirmations, synchronization, and readback in one persistent session;
- separation of raw evidence, derived data, and physical conclusions, with device serial numbers redacted from logs by default.

## Installation

Python 3.10 or later is required. The SIGLENT and FLUKE LAN scripts have no third-party dependencies; live ZS407, LiteVNA, and MDP-M01 USB control requires `pyserial`. Place this repository in the Codex skills directory and keep the directory name as:

```text
codex-programmable-instrument-lab
```

A typical path is `$CODEX_HOME/skills/codex-programmable-instrument-lab`, or `~/.codex/skills/codex-programmable-instrument-lab` when `CODEX_HOME` is not set.

## Quick start

Establish a read-only connection:

```text
python scripts/siglent_socket.py --host <scope-ip> identify --expect-model "SDS3104X HD"
python scripts/siglent_socket.py --host <scope-ip> smoke --out <new-run-dir>
```

Run the constrained Cal loop only after the operator confirms that the SP3050A connects CH1 to the front-panel Cal output and explicitly accepts AutoSet and measurement-screen state changes:

```text
python scripts/siglent_cal_check.py --host <scope-ip> --out <new-run-dir> --confirm-cal-wiring --confirm-scope-state-changes
```

If the probe compensation box has been inspected and does not carry the early `2 GHz ONLY` label, also add:

```text
--confirm-no-2ghz-only-label
```

Live ZS407 control requires `pyserial`. Re-enumerate the port for each session instead of permanently copying the example COM number:

```text
python -m pip install pyserial
python scripts/tinysa_zs407_serial.py --port <current-port> identify
python scripts/tinysa_zs407_serial.py --port <current-port> safe-pause --confirm-state-changes
```

After the operator confirms the analyzer-input wiring, external attenuation, probe fixture, and DUT state, run an evidence-preserving scan:

```text
python scripts/tinysa_zs407_serial.py --port <current-port> scan \
  --out runs/20260915-dut-nearfield --phase dcdc-board-off \
  --start-hz 900000 --stop-hz 5500000 --points 290 \
  --external-attenuation-db 20 --probe "small H loop" \
  --probe-position "DCDC inductor" --operator-dut-state off \
  --confirm-input-only --confirm-state-changes
```

See the [ZS407 reference](references/tinysa-ultra-plus-zs407.md) for command details, probe orientation, and operator-state corrections.

Add `--measurement-mode ambient-rf-survey` for exploratory reception with a telescopic antenna. This mode is separate from board-level near-field work; it reports analyzer-input levels and candidate peaks only, without identifying transmitters or converting readings to field strength.

LiteVNA USB samples are raw, uncalibrated complex data. Confirm the physical fixture and separately authorize every RF source sweep:

```text
python scripts/litevna_zn406.py --port <current-port> acquire-cal-standard \
  --out runs/litevna-cal --phase port1-open --standard open \
  --start-hz 1000000 --stop-hz 1000000000 --points 401 \
  --confirm-standard-wiring --confirm-source-sweep
```

See the [LiteVNA 64 ZN-406 reference](references/litevna-64-zn406.md) for the complete OPEN, SHORT, LOAD, ISOLATION, and THRU fixture sequence, reference-plane limitations, and offline solver. This implementation is a one-port OSL plus forward-response calibration, not a bidirectional 12-term SOLT calibration.

### FLUKE 8845A multimeter

Select LAN as the active remote interface first: opening the LAN settings page does not enable it. The default socket port is **3490**. IP-address and subnet-mask changes require a power cycle using the rear-panel switch. Always use the address confirmed for the current session, without hard-coded bench endpoints or subnet scanning.

```text
python scripts/fluke_8845a.py --host <current-dmm-ip> --out runs/dmm-identify identify
python scripts/fluke_8845a.py --host <current-dmm-ip> --out runs/dmm-config snapshot
```

Once the operator confirms that the front INPUT HI/LO terminals connect to a known low-voltage DC source and its reference, and requests a measurement, record that authorization and acquire:

```text
python scripts/fluke_8845a.py --host <current-dmm-ip> --out runs/dmm-3v3 dcv --range-v 10 --count 10 --phase rail-3v3 --operator-state "3.3 V rail connected to front INPUT HI/LO" --confirm-wiring --confirm-state-changes
```

For a multi-phase experiment, keep one foreground session and send one JSON object per line on standard input. Finish with `end`:

```text
python scripts/fluke_8845a.py --host <current-dmm-ip> --retry-identity-once --out runs/dmm-session session
{"op":"snapshot"}
{"op":"dcv","range_v":10,"count":3,"phase":"rail-3v3","operator_state":"front INPUT HI/LO on the confirmed low-voltage rail","confirm_wiring":true,"confirm_state_changes":true}
{"op":"end"}
```

Standalone `identify`, `snapshot`, and `dcv` commands remain useful for isolated diagnostics; each invocation closes its connection. Use `session` for a full experiment. The adapter requires an existing front-input DCV configuration and checks math mode, triggering, and range readback. After remote measurement, session shutdown attempts to return control to the front panel, retains the appropriate range, and closes the connection; it never zeros, resets, or calibrates the meter. Shorted-input acquisition, nominal 3.3 V acquisition, and the persistent session have each received limited hardware validation. See the [FLUKE reference](references/fluke-8845a.md) for the evidence boundaries.

### MDP-M01, P906, and L1060 adapter and integration record

The adapter supports dynamic status discovery, changing both P906 voltage/current limit while its output is OFF, setting L1060 current only when already in CC mode and its input is OFF, and explicit per-command output ON/OFF. It does not expose arbitrary packet passthrough or L1060 mode switching; the initial software ceiling for L1060 CC is 1 A. Before hardware use, inspect the current device/channel mapping, wiring, output states, and safety envelope. Do not run MINIWARE's PC software as a second controller. The `session` command retains one USB serial connection until `end`:

```text
python -m pip install pyserial
python scripts/mdp_m01.py status
python scripts/mdp_m01.py --port <current MDP-M01 serial port> session
{"op":"status"}
{"op":"arm","max_voltage_v":5,"max_current_a":0.3,"max_power_w":1.5,"confirm_wiring":true}
{"op":"set_p906","channel":1,"voltage_v":5,"current_limit_a":0.3}
{"op":"set_l1060_cc","channel":2,"current_a":0.1}
{"op":"output_on","channel":2,"confirm_energy":true}
{"op":"output_on","channel":1,"confirm_energy":true}
{"op":"output_off","channel":2}
{"op":"output_off","channel":1}
{"op":"end"}
```

Channel numbers and values above are examples only. First use `status` to identify each device, then check real wiring, polarity, load, and limits before acting. `arm` requires all online outputs to be OFF; setpoints can only be changed while the respective output is OFF. Wait for a new status frame after every state change. Never replay an output command after a timeout or mismatch; have the operator inspect the physical state. `end` reads the final state and closes USB but does not switch outputs off. Explicitly turn off and verify each relevant output before ending the experiment. The adapter has only been tested with a fake serial port; hardware validation is pending. See the [MDP integration reference](references/mdp-m01-p906-l1060.md) for protocol and evidence boundaries.

The earlier low-power hardware run used a temporary program to confirm paired P906/L1060 status and run a 5 V, 100 mA CC light-load check; one FLUKE 8845A LAN session was retained across all three phases. The DMM means were 5.006109 V with the load off and 5.001783 V with it on (a −4.326 mV change). No supply-accuracy acceptance limit was defined, so this remains exploratory, not a pass/fail result. M01 telemetry reported about 4.938 V at the L1060 input, inconsistent with the parallel FLUKE reading of about 5.002 V; the cause has not been determined. The remote P906 OFF request was not confirmed by readback; the operator then switched it off locally, and a subsequent M01 status read confirmed both P906 and L1060 OFF. That run does not count as hardware validation of the new adapter. See the [MDP integration reference](references/mdp-m01-p906-l1060.md) for details.

## Safety boundaries

- A standard benchtop oscilloscope probe ground clip is normally connected to protective earth. Never attach it to mains live, a half-bridge switching node, or another non-ground high-side node. Use an appropriately rated differential probe or isolated measurement method when required.
- `--confirm-cal-wiring` and `--confirm-scope-state-changes` are separate permissions and cannot replace one another.
- The Cal loop performs no implicit restore. It finishes by directly reading back Simple Measurement/C1. No separate query is available to prove acceptance of AutoSet, so the report describes only the observable post-command state.
- The official EN11H programming guide does not document an error-queue query, and AutoSet and Simple ITEM have no independent query. `*OPC?` proves synchronization only. `cal_loop_acquired` means that the target mode/source and plausible FREQ/PKPK results were observed; it does not independently prove acceptance of every write command.
- The front-panel Cal output is a nominal 1 kHz, 3 V square wave, not a traceable precision standard. This test does not prove oscilloscope amplitude/time-base calibration or 500 MHz probe bandwidth.
- Future adapters for sources, supplies, loads, or relays must keep energy output OFF until the operator confirms wiring, polarity, limits, and stop conditions.
- The ZS407 adapter exposes no `output on` operation. `safe-pause` disables normal RF and calibration outputs and pauses continuous sweeping; one-shot `scan` acquisition remains valid while the screen shows `Paused`.
- Start unknown near-field work with a correctly rated external attenuator. An uncalibrated near-field probe supports relative hotspot and A/B analysis only, not regulatory field strength or EMC pass/fail claims.
- If the operator corrects an on/off state label, preserve and invalidate the original phase and create a new corrected phase instead of overwriting evidence.
- For any real-world cable, probe, or DUT-state change, give one explicit action, wait for operator confirmation, acquire only afterward, and immediately state when the fixture may be moved.
- The exact `ZN-406` model comes from the operator's chassis-label check. USB electronic identity proves only a compatible LiteVNA variant/protocol, and on-device calibration is not automatically applied to raw USB samples.
- FLUKE `READ?` initiates acquisition and is not a configuration-only query. The constrained adapter stops on active math/relative mode or non-immediate, multi-sample triggering instead of silently reconfiguring them. Slow DC sample spread is not ripple; unspecified tolerances do not justify a pass/fail verdict.
- MDP-M01 serial writes are not device acknowledgements. Wait for and inspect a status frame after every setpoint or output transition. If remote OFF cannot be confirmed, stop; do not resend blindly. Have the operator verify the physical state before a read-only status retry.
- After an operator reports an accidental disconnection, retain and annotate the original phase and acquire a new one. A connection reset permits at most one explicitly enabled identity-stage retry; uncertain writes and acquisitions are never replayed automatically.

## Privacy and evidence

The generated `idn_private.txt` contains the full device IDN and may include a serial number. Do not commit or publish it. `commands.jsonl` redacts the IDN serial number by default, but run directories still require manual review before sharing. Use a GitHub noreply address for commits to avoid exposing a personal email in public commit metadata.

The SIGLENT `identify` command and general `query --command "*IDN?"` mode intentionally print the full IDN to the local terminal. Do not paste raw terminal transcripts into public issues, logs, or chats. The FLUKE adapter instead redacts serial numbers in both terminal output and files and does not create a private IDN file. Store measurement outputs under the repository's `runs/` directory whenever possible.

The repository `.gitignore` excludes common local run directories, private IDN files, and command logs. Never store instrument credentials, network passwords, or device serial numbers in source files, tests, issues, or commit history.

## Offline validation

```text
python -B -m unittest discover -s scripts -p "test_*.py"
```

Offline tests use a local simulated socket and simulated serial transport. They do not scan the LAN or connect to real instruments. Live ZS407 access requires `pyserial`; offline parsing and tests do not.

## Official references

- [SDS3000X HD product page](https://www.siglent.com/int/products-overview/sds3000x-hd/)
- [SDS3000X HD Programming Guide](https://int.siglent.com/u_file/document/SDS3000X%20HD_ProgrammingGuide_EN11H.pdf)
- [SIGLENT Passive Probe Datasheet](https://siglent.oss-cn-shenzhen.aliyuncs.com/English_content/Document/Oscilloscope/Probe_DataSheet_EN02C.pdf)
- [SP3150A / SP3050A Manual](https://siglentna.com/wp-content/uploads/dlm_uploads/2022/08/SP3150ASP3050A-1.pdf)
- [tinySA Ultra+ ZS407 Specification](https://tinysa.org/wiki/pmwiki.php?n=TinySA4.Specification)
- [tinySA USB Interface](https://tinysa.org/wiki/pmwiki.php?n=Main.USBInterface)
- [tinySA PC control](https://tinysa.org/wiki/pmwiki.php?n=Main.PCSW)
- [Zeenko LiteVNA product page](https://www.zeenko.tech/litevna)
- [LiteVNA User Guide](https://nanovna.com/wp-content/uploads/2021/11/LiteVNA_User-Guide.pdf)
- [FLUKE 8845A/8846A Programmers Manual](https://media.fluke.com/8f58fba8-10bb-438b-a91b-b10800c2bbc4_original%20file.pdf)
- [MINIWARE MDP firmware, upper-computer, and source-code downloads](https://forum.minidso.com/forum.php?mod=viewthread&tid=3685)

## License

This project is licensed under the [MIT License](LICENSE). Copyright (c) 2026 HwzLoveDz.
