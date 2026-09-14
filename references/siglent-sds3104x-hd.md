# SIGLENT SDS3104X HD 控制参考

资料核对日期：2026-09-14。命令或固件行为有疑问时，以 SIGLENT 当前产品资源页中与实际固件匹配的文档为准。

## 官方资料

- [SDS3000X HD 产品与资源页](https://www.siglent.com/int/products-overview/sds3000x-hd/)
- [SDS3000X HD 用户手册 EN01B](https://int.siglent.com/u_file/document/SDS3000X_HD_UserManual_EN01B.pdf)
- [SDS 系列编程手册（当前直链）](https://int.siglent.com/u_file/document/SDS3000X%20HD_ProgrammingGuide_EN11H.pdf)
- [SIGLENT 被动探头数据表 EN02C](https://siglent.oss-cn-shenzhen.aliyuncs.com/English_content/Document/Oscilloscope/Probe_DataSheet_EN02C.pdf)
- [SP3150A / SP3050A 专用手册](https://siglentna.com/wp-content/uploads/dlm_uploads/2022/08/SP3150ASP3050A-1.pdf)
- [SIGLENT 关于早期 `2 GHz ONLY` SP3050A 的说明](https://siglentna.com/operating-tip/why-do-the-sp3050a-passive-probes-that-come-with-the-sds6000a-have-a-2-ghz-only-label/)

产品资料确认 SDS3104X HD 具备 USB Device (USBTMC)、1000M LAN（VXI-11/Telnet/Socket/LXI）、内置 WebServer 和 SCPI 远控。编程手册的“Supported Models”要求 SDS3000X HD 使用支持新命令的固件；实际测试必须记录 `*IDN?` 中的固件版本。

## 首选 LAN

1. 用 RJ45 网线把示波器和控制电脑接到同一个受信任的路由器或交换机。
2. 在示波器执行 `Utility > Menu > I/O > LAN Config`，或点击屏幕右下角网络图标。
3. 记录屏幕显示的 IPv4 地址；优先在路由器中做 DHCP 地址保留。若采用静态 IP，先确认网段、掩码和地址没有冲突。
4. 电脑浏览器访问 `http://<scope-ip>`。能看到 WebServer 只证明 Web 链路可用。
5. 用 TCP 5025 发送 `*IDN?\n`，得到预期型号后才算 SCPI 链路成立。

原生 Socket 不需要 VISA。官方示例规定 TCP 端口 5025，SCPI 字符串以 LF (`\n`) 结束。Telnet 的交互端口是 5024，适合人工排障，不作为自动化首选。

直连电脑也可行。双方无 DHCP 时可能自动获得 `169.254.0.0/16` 链路本地地址；只有在电脑与示波器均落在该网段且路由、TCP 5025、`*IDN?` 逐层验证通过时才算建链，不要仅凭地址相似判定成功。长期固定工位仍优先使用受控私网的 DHCP 保留地址或静态地址。

## USB 备用

- PC 连接示波器后面的 **USB Device** 口；前/后的 USB Host 口主要用于 U 盘、鼠标键盘或支持的附件，不能当作 PC 的 USBTMC Device 口。
- 官方编程手册要求 USB 控制使用 VISA 库。先在 VISA 工具中发现设备并发送 `*IDN?`，再交给自动化脚本。
- 手册中的资源字符串只是示例，例如 `USB0::0xF4EC::0xEE38::<serial>::INSTR`；实际 VID、PID、序列号和资源名必须从本机枚举获得，不要硬编码示例值。

## 首个物理闭环：前面板 Cal

链路验证完成后，优先用机身自带的探头补偿输出做低风险闭环。官方数据表给出的前面板 Calibration Signal 为 **1 kHz、3 V 方波**。

1. 将被动探头接到 CH1，探头尖端接前面板 `Cal`，地夹接 `Cal` 下方的接地端子。
2. SP3050A 是固定 10X、带自动倍率识别的探头，没有 1X/10X 拨档。先确认探头或补偿盒没有 `2 GHz ONLY` 标签，再查询 `:CHANnel1:PROBe?` 与 `:CHANnel1:IMPedance?`；正常目标为 `1.00E+01` 与 `ONEMeg`。若不符，先停下核对实物与接口，不要用软件倍率掩盖接线或型号问题。
3. 先保存当前截图和 CH1、时基、采样率、存储深度、触发及 Simple 测量源查询结果。用户授权改变示波器设置后，才可使用 `:AUToset`；随后用 `*OPC?` 等待并重新查询实际设置。
4. AutoSet 不保证把 Simple measurement 的全局来源改到 C1。先查询 `:MEASure:SIMPle:SOURce?`，必要时经授权发送 `:MEASure:SIMPle:SOURce C1`，并再次读回。所需项目若未显示，`ITEM <name>,ON` 也是写操作，应记录且不要无条件清空用户已有测量布局。
5. 读取 `FREQ`、`PKPK`、`TOP`、`BASE`、`AMPL`、`PER`、`RISE10T90`、`FALL90T10`、`OVSN`、`OVSP`，并保存最终截图。返回 `****` 表示该测量当前不可用，不是数值 0；允许短暂等待和有限次重试，也可用 `:MEASure:SIMPle:VALue? ALL` 识别当前真正可用的项目。
6. 观察方波平台是否明显上翘、下垂或有肩部；需要补偿时按用户手册使用非金属工具调整探头补偿孔。软件不能代替用户完成物理微调。

这个环路用于证明“网络控制、探头、采集、测量、截图和留痕”能闭环。官方资料未把 Cal 输出定义为精密幅度/频率校准标准，因此不能据此宣称示波器幅度、时基或带宽校准合格。

### SP3050A 资料版本边界

- 当前被动探头数据表 EN02C 将 SP3050A 列为 10X、500 MHz、`10 MΩ || 11 pF`、补偿范围 8–20 pF，并列出与 SDS3104X HD 兼容。
- SP3150A / SP3050A 专用手册则写 `10 MΩ || 10 pF`、补偿范围 10–20 pF、衰减比 `10:1 ±1%`。
- 这是官方资料之间尚未解析的文档或硬件修订差异。记录实物标签、用户报告和所依据文档版本；不要平均为 10.5 pF，也不要在没有容差声明时称二者“在公差内”。
- SIGLENT 曾有带 `2 GHz ONLY` 标签、为 SDS6000A 调整的同名 SP3050A。遇到该标签时停止型号匹配结论，改查具体探头修订。

### 2026-09-14 单机实测经验

以下只是在一台 SDS3104X HD（固件字符串 `6.8.14.1.0.4.2`）上的回归证据，不应硬编码为所有固件的固定结果：

- 电脑与示波器分别取得同一 `169.254.0.0/16` 内的链路本地地址后，TCP 5025、`*IDN?`、`*OPC?` 和 PNG 截图均成功；具体端点不应固化到可复用技能中。
- SP3050A 接 CH1/Cal 后，仪器自动回读 10X，输入为 1 MΩ。
- AutoSet 把 500 ms/div、Roll、400 kSa/s 的不可读显示调整为 500 µs/div、稳定触发、400 MSa/s；但 Simple measurement 来源仍是 C2。显式改为 C1 并读回后，测量才有效。
- `RISE` / `FALL` 返回 `****`，而与屏幕“10–90% / 90–10%”对应的 `RISE10T90` / `FALL90T10` 返回有效值。不要把相似项目名互换。
- 该次 8 组有效读数的均值约为 1000.000032 Hz、3.036618 Vpp，屏幕方波平台平坦、无明显低频欠补偿或过补偿。约 215 ns 的边沿反映 Cal 输出与当前采集条件，不能验证 500 MHz 探头带宽。

## 安全的首轮查询

先执行：

```text
*IDN?
*OPC?
```

随后可按测试需要查询而不改变设置：

```text
:ACQuire:SRATe?
:ACQuire:MDEPth?
:TIMebase:SCALe?
:CHANnel1:SCALe?
:CHANnel1:OFFSet?
```

完整截图查询：

```text
:PRINt? PNG,NORMal
```

官方语法为 `:PRINt? {BMP|PNG}[,{NORMal|INVerted}]`，返回二进制图片。二进制响应不能按普通文本的换行符截断。

## 波形传输要点

波形读取使用 `:WAVeform:SOURce`、`:WAVeform:PREamble?` 和 `:WAVeform:DATA?`。实施前重新核对编程手册对应固件章节，并遵守：

- 数据块使用 `#N<N-digits>` 长度头；波形数据本身可能包含 `0x0A`/`0x0D`，不能用 LF/CR 判断结束。
- 12-bit/大于 8-bit 数据传输应设为 `:WAVeform:WIDTh WORD`，并按手册说明处理字节序、左对齐和缩放。
- 先查询 `:WAVeform:MAXPoint?`，超出单块上限时用 `:WAVeform:STARt`、`:WAVeform:POINt` 分段；不要一开始拉取最大 400 Mpts。
- 保存 preamble、原始码流和解析脚本版本。电压/时间数组是派生数据，不得替代原始块。

## 容易踩坑

- WebServer 可访问不等于 5025 端口可用；分别验证浏览器和 `*IDN?`。
- LAN/USB 远控不会隔离探头地。网线有磁隔离也不能把普通探头变成差分探头。
- `:AUToset` 会改变水平、垂直和触发设置，且官方说明不推荐用于低于 100 Hz 的事件。先存基线，未经同意不使用。
- `:AUToset` 与测量源是两套状态；AutoSet 后必须重新查询 `:MEASure:SIMPle:SOURce?`，不能假定它跟随活动通道。
- 屏幕字段名与 SCPI 项目名可能不同；10–90% 上升时间和 90–10% 下降时间分别使用 `RISE10T90`、`FALL90T10`。
- `****` 是“当前无有效测量”的文本哨兵；解析器应保留为 unavailable/null，不得转成 0 或直接纳入统计。
- 文本响应必须读到 LF 才完整；收到一部分字节后的短暂无数据不是结束条件。总超时或提前 EOF 时，部分文本只能作为错误证据，不能当成功返回。
- 需要“写入 → `*OPC?` → 回读”证明顺序时，三者应在同一持久 TCP 会话中完成。日志要区分“字节已发送”、“`*OPC?` 同步完成”和“可查询目标状态已经观察”，不能把 `sendall()` 或 `*OPC?` 成功直接写成仪器接受了命令。EN11H 未记录错误队列查询，`:AUToset` 与 `:MEASure:SIMPle:ITEM` 也没有直接 query；因此 manifest 必须披露该可观测性限制，并以模式/来源回读及合理 FREQ/PKPK 采集作为结果证据，而不是逐命令接受证明。
- PNG 至少校验 chunk 边界、IHDR/IEND 和 CRC；仅找到 PNG 签名或 IEND 字样不能证明截图文件可解码。
- 深存储、截图或长采集的耗时可能远超 2 s；应按命令类别设超时，不能把一次超时直接判成仪器故障。
- 固件升级可能改变命令覆盖和返回格式；升级前后都要保存 IDN、旧基线和最小回归测试。
