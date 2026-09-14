---
name: codex-scpi-instrument-lab
description: Control programmable bench instruments for closed-loop hardware experiments and evidence-backed validation. Use for SCPI, VISA, socket discovery, oscilloscope acquisition, automated sweeps, pass/fail limits, data capture, or test reporting; initially supports the SIGLENT SDS3104X HD.
---

# 程控仪器闭环实验室

把“测试意图 → 安全接线 → 仪器配置 → 采集 → 判定 → 原始证据与报告”做成可复现闭环。默认用中文沟通，命令、单位和原始返回值保持原样。

## 先选择工作模式

- **连接建链**：只做网络/驱动、端口、`*IDN?` 和截图验证。
- **只读采集**：读取设置、测量值、截图或原始波形，不改变 DUT 激励。
- **闭环实验**：根据事先确认的测试计划改变仪器设置或激励，并按判据自动迭代。
- **新增仪器**：先读厂商针对准确型号/固件的官方编程手册，再按 [references/adapter-contract.md](references/adapter-contract.md) 增加适配器。不要假定不同品牌或系列的 SCPI 方言相同。

控制 SIGLENT SDS3104X HD 时，必须先读 [references/siglent-sds3104x-hd.md](references/siglent-sds3104x-hd.md)。

## 证据分层

始终分开陈述：

1. **计划证据**：脚本和测试方案已准备；不代表已连上仪器。
2. **链路证据**：实时 `*IDN?` 返回了制造商、型号、序列号和固件；不代表探头或 DUT 接线正确。
3. **采集证据**：同一运行目录中有时间戳、配置、测量值、截图/原始波形和命令日志；不自动等于物理结论。
4. **物理结论**：用户确认实际接线、探头/衰减、接地点、DUT 工况和判据后，采集结果才可支持通过/失败结论。

不得把模拟数据、旧截图、Web 页面可见、连接成功或脚本成功冒充实测通过。

## 默认连接策略

- SDS3104X HD 首选 **LAN 原生 Socket：TCP 5025**。SCPI 命令以换行 `\n` 结束；初始超时用 2 s，再按截图或长波形传输实测调整。
- 只有用户提供的完整地址或本机明确枚举出的单一仪器端点才可作为目标。`169.254.x.x` 等遮蔽地址不是可连接地址；不要扫描整个 `/16`、猜测或复用旧端点，应先取得本次准确 IP。
- 将仪器与控制电脑接入同一个受信任的实验室路由器/交换机，使用 DHCP 保留地址或静态地址。不要把仪器端口暴露到公网或做端口转发。
- USBTMC 仅在无可用局域网、需要点对点隔离管理或 LAN 不稳定时作为备用；连接的是示波器 **USB Device** 口，不是 USB Host 口，并通常需要 VISA 后端。
- LAN/USB 只决定控制链路，不改变示波器 BNC 外壳和保护地关系。

## 每次实验的最小闭环

1. 写明 DUT、目的、变量、固定条件、通道/探头、采样窗口、重复次数、通过阈值与停止条件。缺少关键判据时先做探索性采集，不宣称 pass/fail。
2. 让用户完成并确认物理接线。至少确认探头类型和倍率、通道输入阻抗、信号最大/最小值、参考地、DUT 电源与是否存在高侧/市电/浮地节点。
3. 先运行只读识别；实时返回必须包含预期制造商和型号。保存完整 IDN，但向外分享报告时默认遮蔽序列号。
4. 改设置前保存基线截图和相关查询结果。未经用户同意，不发送 `*RST`、`:AUToset`、恢复默认、固件升级、校准或网络配置命令。
5. 每轮只改变一个主要变量；先用小记录长度、低能量/低限值和少量重复验证方向，再扩大数据量或应力。
6. 保存原始返回值，分析产物另存。原始数据不可被图表或清洗结果覆盖。
7. 对照预先定义的判据给出“通过 / 失败 / 证据不足”，同时列出测试条件、异常点和未覆盖边界。

## 硬件安全闸门

- 台式示波器普通探头地夹通常连接保护地。禁止把它夹到市电火线、半桥开关节点、浮地电源高侧等非地节点；使用额定值合适的差分探头或隔离测量方案。禁止断开示波器保护地来“浮地测量”。
- 不依据软件量程保护硬件；先核对仪器、探头、附件、连接器和 DUT 中最低的额定值及 CAT/瞬态条件。
- 对电源、电子负载、信号源、继电器、运动平台等能施加能量或改变接线状态的仪器，默认输出为 OFF。只有在用户确认接线、极性、限压/限流/功率/温度和紧急停止方式后，才可授权一次明确的上电或输出动作。
- 出现过压、过流、过温、失锁、波形异常、通信错帧或超时重复时，先停止激励并保存证据；不要无限重试。
- 同一仪器同一时刻只允许一个控制者，避免前面板、Web UI 和脚本相互覆盖设置。

## 配套脚本

`scripts/siglent_socket.py` 是无第三方依赖的 LAN 只读客户端，支持：

```text
python scripts/siglent_socket.py --host <IP> identify
python scripts/siglent_socket.py --host <IP> query --command "*IDN?"
python scripts/siglent_socket.py --host <IP> screenshot --output screen.png
python scripts/siglent_socket.py --host <IP> smoke --out <run-dir> --expect-model "SDS3104X HD"
```

优先用 `smoke` 建立首份链路证据。脚本不提供通用写命令；闭环实验的写操作应在已确认的测试计划中以明确命令实现，并记录每条命令、响应、时间、固件和脚本版本。运行完成后关闭 socket，不留下后台服务。

在操作员明确确认“SP3050A 已从 CH1 接到前面板 Cal”并接受 AutoSet、Simple measurement 模式/来源与 FREQ/PKPK 显示项改变后，可运行受限闭环脚本：

```text
python scripts/siglent_cal_check.py --host <IP> --out <new-run-dir> --confirm-cal-wiring --confirm-scope-state-changes
```

`--confirm-cal-wiring` 只证明操作员确认了物理接线；`--confirm-scope-state-changes` 独立证明操作员接受 AutoSet、测量模式/来源和显示项改变，不得互相代替。`scripts/siglent_cal_check.py` 不是通用 SCPI 写入器。它先核对准确型号、CH1 1 MΩ 和自动识别的 10X，再保存基线；写命令固定白名单仅含 AutoSet、Simple 模式、C1 测量源及 FREQ/PKPK 两个显示项。它使用同一持久会话完成写入、同步和回读，使用 `RISE10T90` / `FALL90T10`，把 `****` 和无效哨兵保留为 unavailable，并要求至少一组 FREQ+PKPK 落入仅用于接线/采集健全性检查的宽范围后才标记采集完成。运行后不做隐式恢复；报告只把回读到的 Simple Measurement/C1 及其他可观察状态写成事实，并注明 AutoSet 接受无法由独立 query 证明。

若操作员已检查探头补偿盒且确认没有早期 `2 GHz ONLY` 标签，可另加 `--confirm-no-2ghz-only-label` 记录该事实；未提供时仍可做 1 kHz 低频闭环，但报告必须把具体探头修订记为未确认。

## 运行目录约定

每次实测使用独立目录：`YYYYMMDD-HHMMSS_<dut>_<test>`。至少包含：

- `manifest.json`：仪器 IDN、固件、传输方式、时间、DUT/探头/环境和脚本版本；
- `commands.jsonl`：按顺序记录命令、响应摘要、耗时和错误；
- `baseline.*`、`screen.*`、`waveform_raw.*`、`measurements.csv`：按实际采集类型保存；
- `result.md`：判据、结果、异常、边界和未验证项。

日志、截图或导出的报告不得保存账号密码、网络凭据或不必要的设备序列号。
