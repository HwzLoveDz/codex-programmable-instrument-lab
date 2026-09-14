# Codex SCPI Instrument Lab

[中文](#中文) | [English](#english)

**检索主题 / Search topics:** `codex-skill` · `scpi` · `instrument-control` · `oscilloscope` · `siglent` · `sds3104x-hd` · `hardware-testing`

面向程控仪器与闭环硬件实验的 Codex Skill，首个实机目标为 **SIGLENT SDS3104X HD**。

A Codex skill for programmable instruments and closed-loop hardware experiments, with **SIGLENT SDS3104X HD** as the first hardware-validated target.

这是非官方社区工具，与 SIGLENT Technologies 没有隶属关系，也不代表其认可或背书。

This is an unofficial community tool. It is not affiliated with, endorsed by, or sponsored by SIGLENT Technologies.

## 中文

本 Skill 把测试意图、安全接线、仪器配置、采集、判定和证据留存组织成可复现的硬件实验闭环。仓库同时保留适配器约定，方便后续增加信号源、电源、电子负载、万用表和其他 SCPI/VISA 仪器，但不会假定不同型号共享同一套命令。

### 当前能力

- LAN 原生 SCPI Socket（TCP 5025）只读识别、查询和截图；
- 严格校验 SCPI 查询头、完整 LF 文本响应、二进制边界及 PNG CRC；
- SDS3104X HD + SP3050A + 前面板 Cal 的受限闭环；
- 写命令固定白名单、双重操作员确认、同一持久会话内同步与回读；
- 原始证据、派生数据和物理结论分层；默认遮蔽日志中的设备序列号。

### 安装

需要 Python 3.10 或更高版本；运行脚本无第三方 Python 依赖。将本仓库目录放入 Codex 的技能目录，并保持目录名为：

```text
codex-scpi-instrument-lab
```

典型位置是 `$CODEX_HOME/skills/codex-scpi-instrument-lab`；未设置 `CODEX_HOME` 时通常为 `~/.codex/skills/codex-scpi-instrument-lab`。

### 最小使用示例

只读建链：

```text
python scripts/siglent_socket.py --host <scope-ip> identify --expect-model "SDS3104X HD"
python scripts/siglent_socket.py --host <scope-ip> smoke --out <new-run-dir>
```

只有操作员已经确认 SP3050A 从 CH1 接到前面板 Cal，并明确接受 AutoSet 与测量界面状态改变时，才运行受限闭环：

```text
python scripts/siglent_cal_check.py --host <scope-ip> --out <new-run-dir> --confirm-cal-wiring --confirm-scope-state-changes
```

如果已经检查探头补偿盒且确认没有早期 `2 GHz ONLY` 标签，可再加：

```text
--confirm-no-2ghz-only-label
```

### 安全边界

- 普通台式示波器探头地夹通常连接保护地。禁止夹到市电火线、半桥开关节点或其他非地高侧节点；需要时使用额定值合适的差分探头或隔离测量方案。
- `--confirm-cal-wiring` 与 `--confirm-scope-state-changes` 是两个独立授权，不能互相代替。
- Cal 闭环不做隐式恢复；结束时直接回读确认 Simple Measurement/C1。AutoSet 是否被单独接受没有可用 query，报告只描述可观察到的后命令状态。
- 官方 EN11H 编程手册没有记录错误队列查询，AutoSet 与 Simple ITEM 也没有独立 query。脚本中的 `*OPC?` 只证明同步完成；`cal_loop_acquired` 表示已观察到目标模式/来源和合理的 FREQ/PKPK 采集结果，不表示每条写命令都获得了独立接受证明。
- 前面板 Cal 是标称 1 kHz、3 V 方波，不是可溯源精密标准；该测试不能证明示波器幅度/时基校准或探头 500 MHz 带宽。
- 对未来能输出能量的电源、负载、信号源或继电器，默认输出必须保持 OFF，直至操作员确认接线、极性、限值与停止条件。

### 隐私与证据

运行产物中的 `idn_private.txt` 保存完整设备 IDN，可能包含序列号，不应提交或直接公开。`commands.jsonl` 默认对 IDN 序列号脱敏；仍应在分享运行目录前进行人工检查。

`identify` 以及通用 `query --command "*IDN?"` 会按设计把完整 IDN 输出到本地终端；不要把终端记录原样粘贴到公开 Issue、日志或聊天中。建议始终把实测输出目录放在仓库根目录的 `runs/` 下。

仓库的 `.gitignore` 默认排除常见本地运行目录、私有 IDN 和命令日志。不要在源码、测试、Issue 或提交历史中保存仪器凭据、网络密码或设备序列号。

### 离线验证

```text
python -B -m unittest discover -s scripts -p "test_*.py"
```

当前测试仅使用 `127.0.0.1` 模拟仪器，不扫描局域网，也不会连接真实示波器。

## English

This skill organizes test intent, safe wiring, instrument configuration, acquisition, acceptance criteria, and evidence retention into a reproducible closed-loop hardware workflow. Its adapter contract is designed to support future signal generators, power supplies, electronic loads, multimeters, and other SCPI/VISA instruments without assuming that different models share identical commands.

### Current capabilities

- Native SCPI over LAN sockets (TCP 5025) for read-only identification, queries, and screenshots;
- strict validation of SCPI query headers, complete LF-terminated text responses, binary boundaries, and PNG CRCs;
- a constrained SDS3104X HD + SP3050A + front-panel Cal loop;
- allowlisted write commands, two independent operator confirmations, synchronization, and readback in one persistent session;
- separation of raw evidence, derived data, and physical conclusions, with device serial numbers redacted from logs by default.

### Installation

Python 3.10 or later is required. The runtime scripts have no third-party Python dependencies. Place this repository in the Codex skills directory and keep the directory name as:

```text
codex-scpi-instrument-lab
```

A typical path is `$CODEX_HOME/skills/codex-scpi-instrument-lab`, or `~/.codex/skills/codex-scpi-instrument-lab` when `CODEX_HOME` is not set.

### Quick start

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

### Safety boundaries

- A standard benchtop oscilloscope probe ground clip is normally connected to protective earth. Never attach it to mains live, a half-bridge switching node, or another non-ground high-side node. Use an appropriately rated differential probe or isolated measurement method when required.
- `--confirm-cal-wiring` and `--confirm-scope-state-changes` are separate permissions and cannot replace one another.
- The Cal loop performs no implicit restore. It finishes by directly reading back Simple Measurement/C1. No separate query is available to prove acceptance of AutoSet, so the report describes only the observable post-command state.
- The official EN11H programming guide does not document an error-queue query, and AutoSet and Simple ITEM have no independent query. `*OPC?` proves synchronization only. `cal_loop_acquired` means that the target mode/source and plausible FREQ/PKPK results were observed; it does not independently prove acceptance of every write command.
- The front-panel Cal output is a nominal 1 kHz, 3 V square wave, not a traceable precision standard. This test does not prove oscilloscope amplitude/time-base calibration or 500 MHz probe bandwidth.
- Future adapters for sources, supplies, loads, or relays must keep energy output OFF until the operator confirms wiring, polarity, limits, and stop conditions.

### Privacy and evidence

The generated `idn_private.txt` contains the full device IDN and may include a serial number. Do not commit or publish it. `commands.jsonl` redacts the IDN serial number by default, but run directories still require manual review before sharing.

The `identify` command and the general `query --command "*IDN?"` mode intentionally print the full IDN to the local terminal. Do not paste raw terminal transcripts into public issues, logs, or chats. Store measurement outputs under the repository's `runs/` directory whenever possible.

The repository `.gitignore` excludes common local run directories, private IDN files, and command logs. Never store instrument credentials, network passwords, or device serial numbers in source files, tests, issues, or commit history.

### Offline validation

```text
python -B -m unittest discover -s scripts -p "test_*.py"
```

The tests use only a simulated instrument on `127.0.0.1`. They do not scan the LAN or connect to a real oscilloscope.

## Official references / 官方参考

- [SDS3000X HD product page / 产品页](https://www.siglent.com/int/products-overview/sds3000x-hd/)
- [SDS3000X HD Programming Guide](https://int.siglent.com/u_file/document/SDS3000X%20HD_ProgrammingGuide_EN11H.pdf)
- [SIGLENT Passive Probe Datasheet](https://siglent.oss-cn-shenzhen.aliyuncs.com/English_content/Document/Oscilloscope/Probe_DataSheet_EN02C.pdf)
- [SP3150A / SP3050A Manual](https://siglentna.com/wp-content/uploads/dlm_uploads/2022/08/SP3150ASP3050A-1.pdf)

## License / 许可证

MIT License. Copyright (c) 2026 HwzLoveDz.
