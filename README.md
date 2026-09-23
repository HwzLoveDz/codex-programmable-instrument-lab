# Codex Programmable Instrument Lab

[English](README_EN.md)

用于程控台式仪器的 Codex Skill：把测试意图、安全接线、仪器配置、采集、判定和证据留存组织成可复现的硬件实验闭环。

已有完整或受限实机流程覆盖 **SIGLENT SDS3104X HD**、**tinySA Ultra+ ZS407**、**LiteVNA 64 ZN-406** 和 **FLUKE 8845A**。另完成一轮 **MDP-M01 + P906 + L1060 + FLUKE 8845A** 低功率联调。MDP-M01 使用 MINIWARE 专有 USB 二进制协议，不是 SCPI；本仓库目前提供该轮协议与结果参考，尚未提供可直接执行的受维护 MDP CLI/适配器。不同型号不共用未经验证的命令或传输假设。

## 当前能力

- SIGLENT LAN 原生 SCPI Socket（TCP 5025）只读识别、查询和截图；
- FLUKE 8845A LAN（TCP 3490）脱敏识别、配置快照、低压直流采集及本地操作恢复；
- 一轮 MDP-M01 USB 控制的 P906 5 V 电源、L1060 CC 电子负载与 FLUKE 8845A 并行测量联调，记录控制器状态回读与仪表读数差异；
- 严格校验 SCPI 查询头、完整 LF 文本响应、二进制边界及 PNG CRC；
- SDS3104X HD + SP3050A + 前面板 Cal 的受限闭环；
- ZS407 USB CDC 串口识别、安全暂停、一次性窄带扫描及 off/on 对比；
- 大环/小环 H 场探头与疑似 E 场探头的板级 EMI 相对热点定位流程；
- 未校准伸缩天线的独立环境频谱与伸展/缩短 A/B 验证模式；
- 兼容 ZS407 指定固件实测发现的三列 `scan` 返回与约 −100 dBm 文本格式异常；
- LiteVNA SAA2 USB 二进制识别、受限 S11/S21 扫频和 Normal 模式恢复；
- PORT1 单端 OSL + 正向 Isolation/THRU 主机侧校准、质量门槛和参考面记录；
- 写命令固定白名单、双重操作员确认、同一持久会话内同步与回读；
- 原始证据、派生数据和物理结论分层；默认遮蔽日志中的设备序列号。

## 安装

需要 Python 3.10 或更高版本。SIGLENT 和 FLUKE LAN 脚本没有第三方依赖；ZS407 与 LiteVNA 实时 USB 控制需要 `pyserial`。将本仓库目录放入 Codex 的技能目录，并保持目录名为：

```text
codex-programmable-instrument-lab
```

典型位置是 `$CODEX_HOME/skills/codex-programmable-instrument-lab`；未设置 `CODEX_HOME` 时通常为 `~/.codex/skills/codex-programmable-instrument-lab`。

## 最小使用示例

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

ZS407 实时控制需安装 `pyserial`。每次先从设备管理器重新确认端口，不要永久照抄示例 COM 号：

```text
python -m pip install pyserial
python scripts/tinysa_zs407_serial.py --port <current-port> identify
python scripts/tinysa_zs407_serial.py --port <current-port> safe-pause --confirm-state-changes
```

完成输入接线、外置衰减和 DUT 工况确认后，可执行证据保留扫描：

```text
python scripts/tinysa_zs407_serial.py --port <current-port> scan \
  --out runs/20260915-dut-nearfield --phase dcdc-board-off \
  --start-hz 900000 --stop-hz 5500000 --points 290 \
  --external-attenuation-db 20 --probe "small H loop" \
  --probe-position "DCDC inductor" --operator-dut-state off \
  --confirm-input-only --confirm-state-changes
```

完整命令、探头姿态和工况纠错方法见 [ZS407 参考](references/tinysa-ultra-plus-zs407.md)。

使用伸缩天线做环境射频探索时，另加 `--measurement-mode ambient-rf-survey`。该模式与板级近场实验完全分开；输出仅为分析仪端电平和候选峰值，不识别发射台，也不换算场强。

LiteVNA 的 USB 采样是原始未校准复数数据。每次源扫频前必须先确认实际接线，再独立授权本次 RF 输出：

```text
python scripts/litevna_zn406.py --port <current-port> acquire-cal-standard \
  --out runs/litevna-cal --phase port1-open --standard open \
  --start-hz 1000000 --stop-hz 1000000000 --points 401 \
  --confirm-standard-wiring --confirm-source-sweep
```

完整的 OPEN、SHORT、LOAD、ISOLATION、THRU 接线顺序、参考面限制与离线求解命令见 [LiteVNA 64 ZN-406 参考](references/litevna-64-zn406.md)。当前实现是一端口 OSL + 正向响应校准，不是双向 12 项 SOLT。

### FLUKE 8845A 万用表

先选择仪器的活动 LAN 接口；编辑网络配置页不等于启用 LAN。默认端口为 **3490**。修改 IP／掩码后须用后面板电源开关重新上电。每次使用当前操作员确认的地址，不固定本机地址或扫描网段。

```text
python scripts/fluke_8845a.py --host <current-dmm-ip> --out runs/dmm-identify identify
python scripts/fluke_8845a.py --host <current-dmm-ip> --out runs/dmm-config snapshot
```

用户已确认前面板 INPUT HI／LO 接到低压直流信号与参考地，并要求测量后，可以记录该授权并采集：

```text
python scripts/fluke_8845a.py --host <current-dmm-ip> --out runs/dmm-3v3 dcv --range-v 10 --count 10 --phase rail-3v3 --operator-state "3.3 V rail connected to front INPUT HI/LO" --confirm-wiring --confirm-state-changes
```

多阶段实验使用一个前台会话，跨阶段复用同一 TCP 连接；在会话标准输入中逐行发送 JSON，最后用 `end` 收尾：

```text
python scripts/fluke_8845a.py --host <current-dmm-ip> --retry-identity-once --out runs/dmm-session session
{"op":"snapshot"}
{"op":"dcv","range_v":10,"count":3,"phase":"rail-3v3","operator_state":"front INPUT HI/LO on the confirmed low-voltage rail","confirm_wiring":true,"confirm_state_changes":true}
{"op":"end"}
```

`identify`、`snapshot`、`dcv` 单次命令仍适合独立诊断，每次调用结束会断联；完整实验应使用 `session`。适配器只接受已处于前面板 DCV 的仪器，核对数学功能、触发方式和量程回读。进入远程测量后，会话结束时尝试恢复本地操作，保留适用量程并关闭连接；它不会执行调零、复位或校准。短接、3.3 V 采集和持久会话已分别做过有限实机验证；证据边界见 [FLUKE 参考](references/fluke-8845a.md)。

### MDP-M01、P906 与 L1060 联调记录

已用 M01 的专有 USB 二进制链路确认配对的 P906/L1060 状态并完成一轮 5 V、100 mA CC 轻载测试；Fluke 8845A 跨三个阶段保持一个 LAN 会话。外部测得空载均值 5.006109 V、接入负载均值 5.001783 V（变化 −4.326 mV）。当时没有预先定义电源准确度阈值，所以这是探索性结果，不是合格判定。M01 内部状态回报的 L1060 端电压约 4.938 V，与并联 Fluke 约 5.002 V 不一致，原因未查明。结束时远程请求关闭 P906 未获回读确认；操作者随后手动关机，新的 M01 状态读取确认 P906 和 L1060 均 OFF。协议与完整证据边界见 [MDP 联调参考](references/mdp-m01-p906-l1060.md)。该记录不是一个可直接调用的 MDP 控制 CLI。

## 安全边界

- 普通台式示波器探头地夹通常连接保护地。禁止夹到市电火线、半桥开关节点或其他非地高侧节点；需要时使用额定值合适的差分探头或隔离测量方案。
- `--confirm-cal-wiring` 与 `--confirm-scope-state-changes` 是两个独立授权，不能互相代替。
- Cal 闭环不做隐式恢复；结束时直接回读确认 Simple Measurement/C1。AutoSet 是否被单独接受没有可用 query，报告只描述可观察到的后命令状态。
- 官方 EN11H 编程手册没有记录错误队列查询，AutoSet 与 Simple ITEM 也没有独立 query。脚本中的 `*OPC?` 只证明同步完成；`cal_loop_acquired` 表示已观察到目标模式/来源和合理的 FREQ/PKPK 采集结果，不表示每条写命令都获得了独立接受证明。
- 前面板 Cal 是标称 1 kHz、3 V 方波，不是可溯源精密标准；该测试不能证明示波器幅度/时基校准或探头 500 MHz 带宽。
- 对未来能输出能量的电源、负载、信号源或继电器，默认输出必须保持 OFF，直至操作员确认接线、极性、限值与停止条件。
- ZS407 适配器不提供 `output on`。`safe-pause` 会关闭普通 RF 输出与 Cal 输出并暂停连续扫频；屏幕显示 `Paused` 时，一次性 `scan` 仍会执行并返回数据。
- 未知近场源先使用带宽和阻抗合适的外置衰减器。未校准近场探头只能做热点与相对 A/B 比较，不能换算为法规场强或直接判定 EMC 合格。
- 操作员更正开机/关机状态时，旧阶段保留并标记无效，新建 corrected phase；禁止覆盖原始证据。
- 涉及现实世界换线、探头位置或 DUT 状态时，脚本不能自行感知完成：必须给出一个明确动作、等待操作员确认、再采集，并在采集结束后立即说明可以移动。
- LiteVNA 的准确 `ZN-406` 型号来自操作员核对机身标签；USB 电子读回只证明兼容的 LiteVNA variant/protocol。机内校准不会自动应用到 USB 原始数据。
- FLUKE 的 `READ?` 会触发测量，不能混入只读配置查询；数学／相对值功能开启、非立即单样本触发时，受限脚本停止而不暗改设置。低速 DC 样本跨度不是纹波，未知容差时不做合格判定。
- MDP-M01 的串口写入不是设备回执。设置或输出切换必须等状态帧核对；远程 OFF 未能确认时立即停止，不重复发送以碰运气，并让操作者物理确认后再做只读回读。
- 操作员报告意外断开后，保留原始阶段并记录更正，新建阶段重测。仪器连接被重置时最多显式重试一次身份建链；不在写入或采集结果不确定后自动重放。

## 隐私与证据

运行产物中的 `idn_private.txt` 保存完整设备 IDN，可能包含序列号，不应提交或直接公开。`commands.jsonl` 默认对 IDN 序列号脱敏；仍应在分享运行目录前进行人工检查。仓库提交建议使用 GitHub noreply 邮箱，避免把私人邮箱写入公开提交元数据。

SIGLENT 的 `identify` 以及通用 `query --command "*IDN?"` 会按设计把完整 IDN 输出到本地终端；不要把终端记录原样粘贴到公开 Issue、日志或聊天中。FLUKE 脚本则对终端和文件中的序列号同时脱敏，不生成私有 IDN 文件。建议始终把实测输出目录放在仓库根目录的 `runs/` 下。

仓库的 `.gitignore` 默认排除常见本地运行目录、私有 IDN 和命令日志。不要在源码、测试、Issue 或提交历史中保存仪器凭据、网络密码或设备序列号。

## 离线验证

```text
python -B -m unittest discover -s scripts -p "test_*.py"
```

离线测试使用本地模拟 Socket 与模拟串口，不扫描局域网，也不会连接真实仪器。ZS407 的实时连接依赖 `pyserial`，但离线解析和测试不依赖它。

## 官方参考

- [SDS3000X HD product page / 产品页](https://www.siglent.com/int/products-overview/sds3000x-hd/)
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

## 许可证

本项目采用 [MIT License](LICENSE)，Copyright (c) 2026 HwzLoveDz。
