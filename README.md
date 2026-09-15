# Codex SCPI Instrument Lab

[English](README_EN.md)

用于程控台式仪器的 Codex Skill：把测试意图、安全接线、仪器配置、采集、判定和证据留存组织成可复现的硬件实验闭环。

这是非官方社区工具，与 SIGLENT Technologies、tinySA 或 Zeenko/LiteVNA 项目没有隶属关系，也不代表其认可或背书。

当前经过实机流程验证的目标包括 **SIGLENT SDS3104X HD**、**tinySA Ultra+ ZS407** 和 **LiteVNA 64 ZN-406**。后两者分别使用 USB 文本命令和 USB 二进制协议，而不是 SCPI。仓库同时保留适配器约定，方便后续增加信号源、电源、电子负载、万用表和其他程控仪器，但不会假定不同型号共享命令或传输协议。

## 当前能力

- LAN 原生 SCPI Socket（TCP 5025）只读识别、查询和截图；
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

需要 Python 3.10 或更高版本。SIGLENT LAN 脚本没有第三方依赖；ZS407 与 LiteVNA 实时 USB 控制需要 `pyserial`。将本仓库目录放入 Codex 的技能目录，并保持目录名为：

```text
codex-scpi-instrument-lab
```

典型位置是 `$CODEX_HOME/skills/codex-scpi-instrument-lab`；未设置 `CODEX_HOME` 时通常为 `~/.codex/skills/codex-scpi-instrument-lab`。

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

## 隐私与证据

运行产物中的 `idn_private.txt` 保存完整设备 IDN，可能包含序列号，不应提交或直接公开。`commands.jsonl` 默认对 IDN 序列号脱敏；仍应在分享运行目录前进行人工检查。

`identify` 以及通用 `query --command "*IDN?"` 会按设计把完整 IDN 输出到本地终端；不要把终端记录原样粘贴到公开 Issue、日志或聊天中。建议始终把实测输出目录放在仓库根目录的 `runs/` 下。

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

## 许可证

本项目采用 [MIT License](LICENSE)，Copyright (c) 2026 HwzLoveDz。
