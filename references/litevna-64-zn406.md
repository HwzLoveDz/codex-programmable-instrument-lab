# LiteVNA 64 ZN-406 程控与校准边界

## 已验证设备与链路

- 操作员机身标签：LiteVNA 64 ZN-406。
- Windows 枚举：USB CDC 串口；COM 号每次重新枚举，不固化。
- 实机协议标识：LiteVNA variant 2、protocol 1、hardware revision 2、firmware registers 2.2。
- USB 标识不包含 `ZN-406` 销售型号，因此报告必须分开记录“操作员标签”和“电子读回”。
- 传输协议是 SAA2 二进制寄存器/FIFO，不是 SCPI，也不是 tinySA 文本命令。

官方资料：

- [LiteVNA 产品页](https://www.zeenko.tech/litevna)
- [LiteVNA User Guide / USB data interface](https://nanovna.com/wp-content/uploads/2021/11/LiteVNA_User-Guide.pdf)

## 非显然协议事实

- `0x0D` INDICATE 应答 ASCII `2`；variant/protocol 寄存器应分别为 `2`/`1`。
- 扫频范围限制为 50 kHz–6.3 GHz；当前适配器再限制为 2–1024 点。
- `channel select=1` 采 S11，`=2` 采 S21；每个 FIFO value 是 32 bytes。
- 原始比值：`S11 = rev0 / fwd0`，`S21 = rev1 / fwd0`；前向参考为零必须失败，不能制造无穷值后继续。
- 写 sweep 参数会进入 USB 数据模式。无论成功或失败，脚本都应尝试写 `rawSamplesMode=2` 恢复 Normal 模式并关闭串口。
- USB 返回原始未校准复数数据；机内用户校准不能通过该 USB 接口读取或自动应用。主机侧校准必须保存标准原始 trace 与派生系数。

## 每次实测的操作员握手

1. 明确告诉操作员此刻需要连接、拆除、固定或开关什么；不要说含糊的“保持不动”。
2. 等待操作员明确确认实际动作完成。
3. 在每次 VNA 源扫频前说明频段、点数、端口和输出性质，并取得该次明确授权。
4. 采集期间说明不要触碰夹具；采集完成后立即告知现在可以移动或换线。
5. 操作员更正接线或状态时保留旧 phase，新建 corrected phase；不得覆盖原始证据。

## 参考面与连接器

校准件的公母头决定能否把参考面移到线缆末端。若 OPEN/SHORT/LOAD 都不能直接连接到测试线末端，就不能声称完成了线缆末端 SOLT。不要用额外转接头“凑上”后忽略其电气影响。

本次已验证配置中：SHORT 与 LOAD 为 SMA 公头，可直接连接仪器母座；两根测试线为公对公；因此：

- S11 OSL 参考面在 PORT1 仪器母座；
- OPEN 使用裸 PORT1，未对边缘电容建模；
- S21 THRU 使用一根选定的公对公线直接连接 PORT1/PORT2，并把该路径归一化；
- 两线加双母 THRU 的衰减器夹具与单线 THRU 不是同一参考面，不能混用校准系数。

## 受限校准流程

在固定频率网格上逐项采集，不覆盖文件：

1. OPEN：PORT1 裸开路，PORT2 空载，采 S11。
2. SHORT：SHORT 直接接 PORT1，PORT2 空载，采 S11。
3. LOAD：50 Ω LOAD 直接接 PORT1，PORT2 空载，采 S11。
4. ISOLATION：保持 LOAD 在 PORT1、PORT2 空载，采 S21 本底。
5. THRU：取下 LOAD，用一根选定公对公线直连 PORT1/PORT2，采 S21。

主机侧一端口三项误差模型：

```text
m = e00 + (e10e01 * Γ) / (1 - e11 * Γ)
```

由理想 OPEN `Γ=+1`、SHORT `Γ=-1`、LOAD `Γ=0` 求 `e00`、`e10e01`、`e11`。正向传输响应校正为：

```text
S21_corrected = (measurement - isolation) / (thru - isolation)
```

这是一端口 OSL + 正向响应校准，不是双向 12 项两端口 SOLT。它不能消除 PORT2 匹配误差、反向路径误差或未表征标准件的不确定度。

## 本次实机验收基线

2026-09-15、1 MHz–1 GHz、401 点的实机数据得到：

- OPEN 原始幅度中位 1.0159；SHORT 原始幅度中位 1.0883；
- LOAD 原始回波损耗中位 20.79 dB；
- OPEN/SHORT 原始相位分离相对 180° 的误差中位 7.43°；
- ISOLATION 原始中位 −95.31 dB，最大 −84.29 dB；
- 单线 THRU 原始中位 +0.97 dB；THRU 相对隔离最差裕量 84.32 dB。

这些指标只证明本次采集数值可用于该主机侧模型，不证明仪器计量校准合格，也不是校准件可溯源证书。

## 命令示例

实时访问需要 `pyserial`：

```text
python -m pip install pyserial
python scripts/litevna_zn406.py --port <current-port> acquire-cal-standard \
  --out <new-run-dir> --phase port1-open --standard open \
  --start-hz 1000000 --stop-hz 1000000000 --points 401 \
  --confirm-standard-wiring --confirm-source-sweep
```

其余标准必须使用唯一 phase。完成五次采集后运行离线求解器；输入文件必须处于完全一致的频率网格：

```text
python scripts/litevna_calibration.py \
  --open <open-raw-s11.csv> --short <short-raw-s11.csv> \
  --load <load-raw-s11.csv> --isolation <isolation-raw-s21.csv> \
  --thru <thru-raw-s21.csv> --out <new-calibration-dir>
```
