# tinySA Ultra+ ZS407 USB 串口与板级 EMI 预扫

仅在控制 **tinySA Ultra+ ZS407** 或分析其采集结果时读取。本适配器不是 SCPI；不得把示波器、VISA 或其他频谱仪命令套用到它。

## 已核对的官方边界

- 官方规格与输入限制：[tinySA4 Specification](https://tinysa.org/wiki/pmwiki.php?n=TinySA4.Specification)
- USB 命令格式：[USB Interface](https://tinysa.org/wiki/pmwiki.php?n=Main.USBInterface)
- PC 连接方式：[PC control](https://tinysa.org/wiki/pmwiki.php?n=Main.PCSW)
- 官方固件源码：[erikkaashoek/tinySA](https://github.com/erikkaashoek/tinySA)

ZS407 通过 USB CDC 串口控制，命令以 `\r` 结束，提示符为 `ch>`。官方规格给出：正常输入范围 100 kHz–900 MHz，Ultra 模式最高 7.3 GHz；最大直流输入 ±5 V；内部衰减 0 dB 时绝对最大输入 +6 dBm；内部衰减 30 dB 时短时峰值绝对最大 +20 dBm；自动衰减时建议不超过 0 dBm，最佳测量保持低于 −25 dBm。任何附件、探头或衰减器的更低额定值优先。

`scan` 文本命令最多 290 点；`scanraw` 可输出更多二进制点。USB 文档明确提醒参数错误检查有限，因此适配器必须自行验证范围、点数、完整提示符、频率单调性和返回点数。

## 2026-09-15 实机兼容性证据

本地实机只读识别得到：

- 型号：`tinySA ULTRA+ ZS407`
- 固件：`tinySA4_v1.4-217-gc5dd31f`
- Build Time：`Dec 17 2025 10:50:40`
- Windows 枚举：USB CDC 串口，`VID_0483&PID_5740`；COM 号每次必须重新枚举，不能固化为某次实测端口。

已实机接受的安全命令为 `output off`、`caloutput off`、`pause`。屏幕保持 `Paused` 不表示单次采集无效；`scan ...` 会执行一次扫描并返回数据。不要在适配器中提供或自动调用 `output on`。

该固件的文本 `scan ... 3` 实际每点返回三列：频率、测量电平和一个额外值；本次额外值为 0。保存原文，测量只使用第二列，不猜测第三列语义。

该固件还会把约 −100 dBm 的文本格式写成 `-:.000000e+01`；其中 `:` 表示十位值 10，实际为 −100 dBm。脚本只兼容已观察到的 `:`–`?` 前导值并保留原始行；其他畸形格式必须失败，禁止从残缺字符串中提取局部数字。这个问题属于已观察到的固件文本格式兼容性，不应泛化到其他固件。

全带 `frequencies`/`data 2` 的首点曾返回 0 Hz 与约 +14.6 dBm，而官方有效输入从 100 kHz 起。不得把 0 Hz 首点当作真实输入峰值。显式范围扫描也应默认排除首尾边界点的峰值判断，并用稍微扩大的频段复核边界峰。

## 安全状态与授权

连接探头或 DUT 前：

1. 操作员确认只连接分析输入，普通 RF 输出与 Cal 输出均悬空。
2. 发送 `output off`、`caloutput off`、`pause`；这些仍是状态更改，需操作员授权。
3. 未知近场信号先在仪器输入端串联已确认 50 Ω、带宽合适的 20 dB 外部衰减；观察到充分余量且高能功能关闭后，才能一次减少一档。
4. 外部衰减只是夹具元数据。未校准探头的 dBm 读数不能换算为 dBµV/m，也不能用于法规通过/失败。

若靠近扬声器、D 类功放、天线、功率电感或其他强场源后读数接近 0 dBm，应暂停扫描并增加外部衰减。不能根据远距离或空载基线直接减少保护衰减。

## 近场探头选择和姿态

- 大 H 场环：灵敏度高、空间分辨率低；先找整板或区域级能量。大环中心不等于源头位置。
- 小 H 场环：用于定位电流路径。测 PCB 走线时环面通常竖直于 PCB，并绕垂直轴旋转寻找最大耦合；测功率电感顶部漏磁时可先让环面平行于 PCB，再测电感边缘和相邻开关回路的竖直姿态。
- E 场探头：用于高 `dv/dt` 节点、时钟引脚、FPC 和连接器附近；没有型号或结构证据时，不得只凭外形断言探头类型。

探头需用非导电夹具保持位置、方向和 1–3 mm 高度，不得接触焊盘或裸露导体。用手握持会引入位置误差和人体电容耦合；A/B 采集时应固定探头和线缆。

## 未校准伸缩天线的环境频谱

无型号的 SMA 伸缩鞭状天线用于环境射频探索，不得与板级近场定位混为同一实验。先记录完全伸展长度、方向、底座位置和外置衰减；未知环境先串 20 dB 做安全预扫，确认远离过载且信号接近底噪后，才可单独改为 3 dB 再扫。不要在两次扫描间同时改变长度、方向、位置和衰减。

验证天线是否产生有效接收差异时，可在相同底座和方向下比较完全伸展与完全缩短。时间变化的发射机、室内多径和人体靠近都会改变结果，因此这种 A/B 只能证明探索性接收差异，不能得到天线增益或校准频响。分析仪端 dBm 加上外置衰减的标称值也仍不是场强；没有校准天线系数、线损和测试距离时，不得换算为 dBµV/m。

环境扫描必须加 `--measurement-mode ambient-rf-survey`，使摘要明确写成非校准环境 RF 数据。频点落入广播、航空或其他业务频段不等于已经识别发射源；没有解调、持续时间、位置和监管频率资料等证据时，只报告“候选峰值”。

## 探索性 A/B 流程

1. 记录探头、姿态、位置、外部衰减、频段、点数、DUT 电源和固件功能状态。
2. 操作员明确报告 DUT 状态后才写入 `operator_declared_dut_state`。若后来更正“刚才未关机”，保留原始阶段并用新阶段标记 `invalidates_phase`；禁止覆盖或静默改名。
3. 状态切换后等待稳定，再执行至少一次预热扫描和两次正式扫描；保存每次原始返回。
4. 只比较频率网格完全相同的 off/on CSV；默认排除首尾边界点。
5. 先看绝对幅度、重复稳定性和相对增量，再选窄带复扫；仅因关机底噪很低而得到的大 delta，不自动等于强发射源。
6. 每轮只移动一个位置、改变一种固件功能或一个负载。报告写“候选源/相对变化”，没有校准场强与法规限值时不写 pass/fail。

本次实测中，关闭固件中的扬声器输出后，原先的 2/4/6/8 MHz 分量不再出现，支持把那组低频响应归入扬声器输出相关候选源。小 H 环靠近 DCDC 电感时，约 0.93–1.05 MHz 及其后续分量出现相对增量，但绝对读数接近分析仪底噪，只能列为待复核候选。该位置在 30–90 MHz 的开/关差异同样较弱，不能据此认定它是此前宽带响应的主源。以上均为特定板卡、固件、探头、衰减和位置下的探索性证据，不能写成 ZS407、该产品或所有 DCDC 的通用特性。

## 脚本

需要 Python 3.10+ 与 `pyserial`：

```text
python -m pip install pyserial
python scripts/tinysa_zs407_serial.py --port <current-port> identify
python scripts/tinysa_zs407_serial.py --port <current-port> safe-pause --confirm-state-changes
```

证据保留扫描示例：

```text
python scripts/tinysa_zs407_serial.py --port <current-port> scan \
  --out runs/20260915-dut-nearfield --phase dcdc-board-off \
  --start-hz 900000 --stop-hz 5500000 --points 290 \
  --warmup-scans 1 --repeats 2 --external-attenuation-db 20 \
  --probe "small H loop" --probe-position "DCDC inductor" \
  --operator-dut-state off --confirm-input-only --confirm-state-changes
```

环境天线扫描示例：

```text
python scripts/tinysa_zs407_serial.py --port <current-port> scan \
  --out <new-run-dir> --phase antenna-extended-87p5to108mhz \
  --start-hz 87500000 --stop-hz 108000000 --points 290 \
  --warmup-scans 1 --repeats 2 --external-attenuation-db 3 \
  --probe "30 cm telescopic whip, uncalibrated" \
  --probe-position "vertical, fully extended, fixed" \
  --operator-dut-state "not applicable; ambient RF survey" \
  --measurement-mode ambient-rf-survey \
  --confirm-input-only --confirm-state-changes
```

如果操作员纠正状态，使用新 phase 并显式记录：

```text
--phase dcdc-board-off-corrected --operator-dut-state off \
--invalidates-phase dcdc-board-off
```

对比只处理已保存 CSV，不连接仪器：

```text
python scripts/tinysa_zs407_serial.py compare \
  --off-csv runs/.../dcdc-board-off-traces.csv \
  --on-csv runs/.../dcdc-board-on-traces.csv \
  --out runs/.../dcdc-on-vs-off.csv
```
