# Codex SCPI Instrument Lab

[中文](README.md)

A Codex skill for programmable bench instruments. It turns test intent, safe wiring, instrument configuration, acquisition, acceptance criteria, and evidence retention into a reproducible closed-loop hardware workflow.

This is an unofficial community tool. It is not affiliated with, endorsed by, or sponsored by SIGLENT Technologies.

The first hardware-validated target is the **SIGLENT SDS3104X HD**. The repository also defines an adapter contract for future signal generators, power supplies, electronic loads, multimeters, and other SCPI/VISA instruments. It does not assume that different models share identical commands.

## Current capabilities

- Native SCPI over LAN sockets (TCP 5025) for read-only identification, queries, and screenshots;
- strict validation of SCPI query headers, complete LF-terminated text responses, binary boundaries, and PNG CRCs;
- a constrained SDS3104X HD + SP3050A + front-panel Cal loop;
- allowlisted write commands, two independent operator confirmations, synchronization, and readback in one persistent session;
- separation of raw evidence, derived data, and physical conclusions, with device serial numbers redacted from logs by default.

## Installation

Python 3.10 or later is required. The runtime scripts have no third-party Python dependencies. Place this repository in the Codex skills directory and keep the directory name as:

```text
codex-scpi-instrument-lab
```

A typical path is `$CODEX_HOME/skills/codex-scpi-instrument-lab`, or `~/.codex/skills/codex-scpi-instrument-lab` when `CODEX_HOME` is not set.

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

## Safety boundaries

- A standard benchtop oscilloscope probe ground clip is normally connected to protective earth. Never attach it to mains live, a half-bridge switching node, or another non-ground high-side node. Use an appropriately rated differential probe or isolated measurement method when required.
- `--confirm-cal-wiring` and `--confirm-scope-state-changes` are separate permissions and cannot replace one another.
- The Cal loop performs no implicit restore. It finishes by directly reading back Simple Measurement/C1. No separate query is available to prove acceptance of AutoSet, so the report describes only the observable post-command state.
- The official EN11H programming guide does not document an error-queue query, and AutoSet and Simple ITEM have no independent query. `*OPC?` proves synchronization only. `cal_loop_acquired` means that the target mode/source and plausible FREQ/PKPK results were observed; it does not independently prove acceptance of every write command.
- The front-panel Cal output is a nominal 1 kHz, 3 V square wave, not a traceable precision standard. This test does not prove oscilloscope amplitude/time-base calibration or 500 MHz probe bandwidth.
- Future adapters for sources, supplies, loads, or relays must keep energy output OFF until the operator confirms wiring, polarity, limits, and stop conditions.

## Privacy and evidence

The generated `idn_private.txt` contains the full device IDN and may include a serial number. Do not commit or publish it. `commands.jsonl` redacts the IDN serial number by default, but run directories still require manual review before sharing.

The `identify` command and the general `query --command "*IDN?"` mode intentionally print the full IDN to the local terminal. Do not paste raw terminal transcripts into public issues, logs, or chats. Store measurement outputs under the repository's `runs/` directory whenever possible.

The repository `.gitignore` excludes common local run directories, private IDN files, and command logs. Never store instrument credentials, network passwords, or device serial numbers in source files, tests, issues, or commit history.

## Offline validation

```text
python -B -m unittest discover -s scripts -p "test_*.py"
```

The tests use only a simulated instrument on `127.0.0.1`. They do not scan the LAN or connect to a real oscilloscope.

## Official references

- [SDS3000X HD product page](https://www.siglent.com/int/products-overview/sds3000x-hd/)
- [SDS3000X HD Programming Guide](https://int.siglent.com/u_file/document/SDS3000X%20HD_ProgrammingGuide_EN11H.pdf)
- [SIGLENT Passive Probe Datasheet](https://siglent.oss-cn-shenzhen.aliyuncs.com/English_content/Document/Oscilloscope/Probe_DataSheet_EN02C.pdf)
- [SP3150A / SP3050A Manual](https://siglentna.com/wp-content/uploads/dlm_uploads/2022/08/SP3150ASP3050A-1.pdf)

## License

This project is licensed under the [MIT License](LICENSE). Copyright (c) 2026 HwzLoveDz.
