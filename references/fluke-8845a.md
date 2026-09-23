# FLUKE 8845A：LAN 建链与低压直流采集

## 支持范围与资料

已实机验证 FLUKE 8845A 的身份、配置、前端选择、积分时间、自动零点、数学功能、触发查询、固定 10 V 量程写入／回读、短接和约 3.3 V 电源 READ?、SYST:LOC + *OPC?。不把这些扩展成电流、电阻、交流、完整校准或其他型号的实机支持。

依据 [Fluke 8845A/8846A Programmers Manual，Rev. 3](https://media.fluke.com/8f58fba8-10bb-438b-a91b-b10800c2bbc4_original%20file.pdf)：纸面页 4（接口）、12–17（LAN）、42（READ?）、44–45（量程／积分）、51（端子）、52（数学功能）、65–66（本地／远程）。这是编程手册，不以外形相近或兼容模式推断命令支持。

参考 [脱敏实机摘要](fluke-8845a-validated-session.json)。实测采集最初通过受限 PowerShell 会话完成；随后提取 Python 适配器并做离线测试。具体脚本分支是否经过实机执行，应以对应运行目录为准，不能把命令已验证等同于所有新代码都已实机验证。

此前 Python 适配器的 17 项离线测试及当时全仓库 61 项回归通过；随后尝试实机验证 Python 本地恢复分支时，TCP 端口拒绝连接，未进入命令阶段。此前 PowerShell 的本地恢复已获得 *OPC?=1；不能把那次未建链的尝试标成 Python 分支实机通过。

2026-09-23 增加前台 `session` 模式后，全仓库 69 项离线测试通过，并完成一次有限实机验证：首次新连接的 `*IDN?` 被重置，显式一次身份重试成功；成功的 Socket 上连续完成两次配置快照、3 次 `READ?`、再一次快照，期间未重新建链。`connection_count=2` 包含失败的初连。`end` 后 `SYST:LOC` 与 `*OPC?` 返回完成，会话报告本地恢复和 Socket 关闭成功。3 点约 13 µV 的均值仅用于检查采集链路，不能作为精度或校准结论；初连重置的根因仍未确定。

## 建链：先确认网络层，再发命令

- LAN 为原生 TCP Socket，默认 **3490**，命令以 LF 结束；CRLF 也被手册接受。不使用 SIGLENT 的 5025。
- `INSTR SETUP → PORT IF → SELECT PORT → LAN` 选择活动远程接口；进入 LAN 地址设置页不等于已经选择 LAN。仪器只允许一个远程客户端，不强占未知会话。
- 电脑直连、没有 DHCP 服务器时，可以使用静态地址；DHCP 不必为“能联网”而勾选。确认双方实际地址和掩码、路由及网卡 Up 状态。若双方使用链路本地地址，通常使用匹配的 `/16`；不要仅凭地址前两段相同就忽略掩码。
- 修改 IP／掩码／网关后，按 ENTER 保存，再按手册使用**后面板电源开关**重新上电生效；不能把前面板待机按钮视为同一操作。不要未经用户操作确认就假定已重启。
- 每次重新枚举网卡及路由。网卡拔插会使旧的“100 Mbps / 可达”结论失效。ARP 中保留的 Stale／Probe 条目、MAC 匹配或链路灯，不证明 TCP 服务当前可用。
- 模糊照片中的 3／9 等数字容易看错。以操作员明确确认的地址为准，不能因低置信 OCR 就让用户改 IP，更不能探测误识别的公网地址。
- 已知地址连接超时就分层排查，不扫网段、不改 Wi-Fi、防火墙或仪器网络配置来碰运气。

2026-09-23 实机多次在新建 TCP 连接后的首条 `*IDN?` 遇到 `WinError 10054`；一次成功连接中的配置快照连续完成 11 条查询。频繁建断链可能相关，但现有证据不能确定它就是重置根因。允许显式 `--retry-identity-once` 做一次**尚未发生设置写入／采集之前**的连接／身份查询重试；再次失败则停止。任何设置或 `READ?` 结果不确定后，都不能自动重连重放。该日示波器未接入，不能据此判断示波器链路。

## 读配置与测量的区别

| 操作 | 语义及限制 |
| --- | --- |
| `*IDN?` | 核对原生 FLUKE / 8845A；固件字段原样保留，序列号默认脱敏。异常或仿真身份不绕过核对。 |
| `CONF?`, `ROUT:TERM?` | 当前配置、前／后机械输入选择（FRON / REAR）；不证明表笔确实接到目标。 |
| `VOLT:DC:NPLC?`, `ZERO:AUTO?`, `CALC:STAT?` | 记录积分、自动零点和数学功能；相对值／NULL 未排除时，不把返回值当绝对电压。 |
| `TRIG:SOUR?`, `TRIG:COUN?`, `SAMP:COUN?` | 脚本只接受 IMM、一次触发／一次采样；遇到 EXT/BUS/多样本就停止，不暗改触发。 |
| `VOLT:DC:RANG 10` | 设置 10 V 固定档，并分别回读范围及 AUTO=0。低压脚本只暴露 0.1、1、10 V。 |
| `READ?` | 按现有触发条件采集。每条命令必须读完 LF 终止的完整响应再发下一条；不是纯配置查询。 |
| `MEAS?`, `CONF:…` | 会改变配置；本脚本不提供，不能当只读查询的替代。 |
| `SYST:REM`, `SYST:LOC` | 进入远程测量的会话在整场实验收尾时恢复本地操作并关闭连接；只读身份／配置快照不无条件发送 `SYST:LOC`。不使用锁死 LOCAL 键的 RWLock。 |
| `*OPC?` | 同步完成，不是通用“所有命令无错”或物理按键已验证的证据。 |
| `SYST:ERR?` | 读取会弹出错误队列；不是无副作用快照。本脚本不暗中清队列，故不能宣称错误队列为空。 |

## 受限脚本

Python 3.10+ 标准库，无第三方依赖。`--out` 必须是新目录，失败也留日志；完整 IDN 不落盘、不输出到终端；本机地址不写入摘要。不要把实例占位地址当作实际端点。

独立的 `identify`、`snapshot`、`dcv` CLI 调用仍各自建链／断联；单次 `dcv` 的多点采样在同一连接内完成。跨多个阶段时使用前台 `session` 模式：核对 `*IDN?` 后从标准输入逐行接收 JSON 操作，并跨人工步骤保持同一 Socket。`end` 或输入流关闭时执行收尾；仅在会话进入远程测量状态后尝试 `SYST:LOC`，然后关闭 Socket。LAN 一次只接纳一个远程客户端，退出后回监听；会话管理必须尊重仪器空闲／超时限制。有限实机验证仅覆盖上述快照／3 点 DCV／正常 `end` 路径。

```text
python scripts/fluke_8845a.py --host <current-dmm-ip> --out runs/dmm-identify identify
python scripts/fluke_8845a.py --host <current-dmm-ip> --out runs/dmm-config snapshot
python scripts/fluke_8845a.py --host <current-dmm-ip> --out runs/dmm-local restore-local --confirm-state-changes
```

用户确认前面板 **INPUT HI / INPUT LO** 已跨接已知低压电源与参考地（不是电流孔、SENSE 端或高侧未知浮地节点），并授权测量和必要量程改变后：

```text
python scripts/fluke_8845a.py --host <current-dmm-ip> --out runs/dmm-3v3 dcv --range-v 10 --count 10 --phase rail-3v3 --operator-state "INPUT HI to confirmed low-voltage rail; INPUT LO to its reference; powered and connected" --confirm-wiring --confirm-state-changes
```

需要跨阶段使用同一连接时，启动一次前台会话（`--out` 仍用全新目录），随后在**该进程的标准输入**逐行输入 JSON；每个 `dcv` 的 `phase` 必须唯一：

```text
python scripts/fluke_8845a.py --host <current-dmm-ip> --out runs/dmm-session session
{"op":"snapshot"}
{"op":"dcv","range_v":10,"count":3,"phase":"rail-3v3","operator_state":"INPUT HI/LO connected to confirmed low-voltage rail/reference; powered","confirm_wiring":true,"confirm_state_changes":true}
{"op":"end"}
```

`session_ready` 报告进程 PID 和建链次数；每步完成后保存清单与采样。收尾的 `session_ended` 报告本地恢复及 Socket 关闭结果。操作员步骤之间可保持该前台会话，但要保留对进程及超时的监控；输入流意外关闭时同样收尾，并把实验标为未完成。一次 `session` 不支持任意写命令或绕过量程、接线与状态改变确认。

不要让用户重新授权已经明确请求的正常测量步骤。用户未确认接线或信号上限时先停下；量程不是硬件输入额定值的替代。本脚本只接受原本已处于前端 DCV 的仪器，不会把连接中设备从电阻／电流档自动切过来。

发生接触断开后，让用户确认重测状态，新目录、新 phase，必要时加 `--supersedes-phase <old-phase>`；保留旧阶段并附操作员更正。未提供精确断开时刻时，不强行把原因对应到某几个点，也不能只截取好看的尾段作为稳态结论。

## 完成与恢复

- 采完立即告诉用户可以移动／换线，不等报告生成，也不继续背景采集。
- 进入远程测量状态的 `session` 在整场实验结束时执行 `SYST:LOC`，随后确认命令处理、关闭 Socket；纯只读身份／配置快照不无条件执行该写入。独立的 `dcv` CLI 仅覆盖单次调用内的采集，结束该调用就会恢复本地并断联。`SYST:LOC` 不复原全部设置，也不是恢复出厂或校准。
- 若仍可能接着 3.3 V 信号，保留适用的 DCV 10 V 档，不盲目恢复到短接试验的 100 mV 档或原先的通断档。若用户要求精确恢复基线，先核对当前接线，说明哪些原始设置确实留过快照。
- 若连接已丢失，报告“已关闭客户端，仪器本地恢复未验证”；不能只因为运行到了 finally 就写“已恢复”。
- 有限数值、10 个点或 *OPC? 成功都不等于校准合格。无 DUT 容差时报告实际值和偏差，不自行设通过门槛。
- 低速 DC 样本跨度不等于纹波峰峰值，不能据此评价开关纹波或瞬态。短接微伏读数也不能自动当补偿值写回。

## 本次实机经验

短接 10 次返回 +2.24～+2.72 µV，均值 +2.48 µV（该轮未查询数学状态，不作计量结论）。首次约 3.3 V 采集混有明显低点；操作员之后明确说测量中意外断开，旧阶段保留且不作稳态依据。重新采集 10 次为 3.319780～3.320330 V，均值 3.3201085 V；该轮确认数学功能关闭。最终本地恢复后保留 DCV 10 V，未更改网络、校准或 DUT 状态。
