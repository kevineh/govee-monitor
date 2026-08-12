# govee-monitor

Govee H5055 烧烤温度计 → Artisan 咖啡烘焙温度曲线桥接（Windows 原生 Python，无需 WSL2）。

H5055 持续单向广播 BLE 广告（无需连接 / pair / GATT）。本工具连续扫描广告、
解码双探针温度（豆温 BT + 排气温 ET）、通过 WebSocket 喂给 **Artisan**
画曲线 / RoR / 事件按钮，同时本地落盘 JSONL/CSV 以防丢失。

解码字节格式参考 [BastiJoe/govee_h5055_mqtt](https://github.com/BastiJoe/govee_h5055_mqtt)。

## 架构

单进程、单 asyncio 事件循环、3 个并发任务，全部读写同一份内存中的
`DeviceState`，无 MQTT、无数据库。

```
BLE适配器 ──► scanner.py (bleak 连续扫描 detection_callback)
                 │ 解码 → state.py (DeviceState: 6通道/BT/ET/battery/rssi)
        ┌────────┼──────────────┐
        ▼        ▼              ▼
   server.py   recorder.py    events.py
   websockets  CSV/JSONL      c=CHARGE/d=DROP
   WS服务       落盘/轮转       键盘线程/自动触发
        │ push {"Message":"CHARGE"/"DROP"}
        ▼
   Artisan（同机 127.0.0.1 客户端，画曲线/RoR/事件按钮）
```

## 安装

```bash
# Windows 原生 Python（或本仓库任意平台）
uv venv --python 3.11
uv pip install -e ".[dev]"

# 查看帮助
uv run python -m govee_monitor --help
```

依赖：`bleak`（Windows 上自动拉取 winrt 运行时）、`websockets`。**不用**
paho-mqtt / bluepy / numpy。

## 快速开始

```bash
# 完整运行：豆温=传感器4，排气温=传感器6，WS 127.0.0.1:9090，日志落盘 ./logs
uv run python -m govee_monitor --bt-channel 4 --et-channel 6 --port 9090 --out ./logs

# 烘焙前发现设备 / 看连接掩码
uv run python -m govee_monitor --list

# 无 BLE 仿真（合成 payload 跑通 Artisan 接线）
uv run python -m govee_monitor --sim --bt-channel 4 --et-channel 6

# 自动在 60°C 开始记录（下豆 CHARGE）
uv run python -m govee_monitor --bt-channel 4 --et-channel 6 --auto-charge-temp 60
```

运行后控制台可按键：`c` = CHARGE（下豆，新建烘焙文件）、`d` = DROP（出锅，关闭文件）、`q` = 退出。

## Artisan 侧配置

1. Artisan 打开 `Config » Port`，切到最后一个 tab **Web Sockets**。
2. Endpoint 填 `ws://127.0.0.1:9090/`。
3. Node 值：`temp1` = `BT`（豆温）、`temp2` = `ET`（排气温）——与 `--node-bt/--node-et`
   对齐（默认 BT/ET）。
4. 若 ET / BT 反了，互换 node 值即可，无需改本工具。
5. Data Request 保留单个 `getData`（单请求取全部，效率更高）。
6. 可选开启 "START on CHARGE" / "OFF on DROP" 联动事件按钮。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--mac` | 前缀 `a4:c1:38` | 精确过滤指定 MAC |
| `--bt-channel` / `--et-channel` | 4 / 6 | 豆温 / 排气温对应的传感器号（1–6） |
| `--host` / `--port` | 127.0.0.1 / 9090 | WS 绑定地址/端口（本机回环不触发防火墙弹窗） |
| `--ws-path` | `/` | WS 路径（说明性，所有路径都接受） |
| `--node-bt` / `--node-et` | BT / ET | 喂给 Artisan 的 node 名 |
| `--interval` | 2s | 落盘快照间隔 |
| `--out` | ./logs | 烘焙日志目录 |
| `--log-format` | jsonl | `jsonl` \| `csv` \| `channels`（channels 为 6 通道 CSV） |
| `--temp-divisor` | 1 | 原始读数除以该值（见「量程交叉核对」） |
| `--scan-mode` | active | `active` \| `passive` |
| `--watchdog` | 30s | 多久无广告则告警（0=关） |
| `--restart-on-watchdog` | 关 | 连续 3 次看门狗未收到广告后重启扫描 |
| `--sim` | 关 | 合成 payload，无需 BLE |
| `--list` | 关 | 扫描约 5s 并列出发现的 H5055 |
| `--auto-charge-temp` | 无 | BT 首次达到该温度自动发 CHARGE（DROP 后重新武装） |
| `--control-file` | 无 | 可选控制文件：写入 CHARGE/DROP 触发事件 |
| `-v` | - | 调试日志 |

## 烘焙日志

每次 CHARGE 新建 `roast_<UTC时间戳>.jsonl`（或 .csv），DROP 写入事件行并关闭，
SIGINT/Ctrl+C 时 flush 当前文件。JSONL 宽格式一行一个快照：

```json
{"t_iso":"2026-08-11T12:30:00.123Z","t_epoch":1789218600.123,"BT":198.0,"ET":185.5,"battery":70,"rssi":-60,"event":""}
```

`event` 字段：`CHARGE` / `DROP` / 普通快照为空。`--log-format channels` 会输出
`ch1..ch6` 全部 6 路原始通道。

## 解码格式

22 字节 payload（Windows bleak 已剥离公司 ID `0xEC88`，代码仍保留防御性剥离）。

| 偏移(字节) | 含义 |
| --- | --- |
| 4 | 电池电量（0–100） |
| 5 | 通道字节：`payload_index=(b>>6)&3`、`connection_mask=b&0x3F` |
| 7–8 | A 槽值（传感器 `base=2*payload_index+1`，小端） |
| 9–10 / 11–12 | A 槽 low / high 报警阈值 |
| 14–15 | B 槽值（传感器 `base+1`，小端） |
| 16–17 / 18–19 | B 槽 low / high 报警阈值 |

`0xFFFF` = 该槽未接探针 / 未设值。每个广告只携带一对传感器槽位，扫描器跨
3 种 payload 聚合出完整 6 通道。

### 新固件（mfg `0x0070`，20 字节，大端，值已内置 `/100`）

较新固件的 H5055 改广播 manufacturer data（mfg 键 `0x0070`，service
`00005550-…`），20 字节、**大端**、温度原始值除以 100 才是 °C —— 解码器已
内置 `/100`，**无需 `--temp-divisor`**。

| 偏移(字节) | 含义 |
| --- | --- |
| 5 | 电池电量（`& 0x7F`，最高位是标志位，0–100） |
| 6 | 通道字节：`payload_index=(b>>6)&3`、`connection_mask=b&0x3F` |
| 8–9 | A 槽值（传感器 `base=2*payload_index+1`，大端 `/100`） |
| 10–11 / 12–13 | A 槽 low / high 报警阈值（大端） |
| 14–15 | B 槽值（传感器 `base+1`，大端 `/100`） |
| 16–17 / 18–19 | B 槽 low / high 报警阈值（大端） |

同样 `0xFFFF` = 未接探针 / 未设值。扫描器会同时识别 `0xEC88`（旧）与 `0x0070`
（新）两种格式。

## 真机首测清单

1. `uv run python -m govee_monitor --list` 确认 MAC 出现（前缀 `a4:c1:38`；新固件 mfg `0x0070` 也能被发现）。
2. 插豆温 / 排气探针，看 mask 的 bit(n-1) 是否对应该传感器号。
3. 据此设 `--bt-channel` / `--et-channel`（默认 4 / 6）。
4. 开 Artisan，配好 Web Sockets，看到曲线即可。

## 量程交叉核对（`--temp-divisor`）

原始值像整数 °C（室温 23）。若实际读数约为预期的 10 倍，说明固件是 0.1° 分辨率，
加 `--temp-divisor 10`。新固件（mfg `0x0070`）的 `/100` 已在解码器内完成，
无需该参数。

## Windows 注意事项

- BLE 扫描**不需要管理员权限**，只要蓝牙开着、适配器支持 LE。
- 绑定 `127.0.0.1` → WS 端口不触发 Defender 防火墙弹窗。
- 用连续扫描而非反复 `scan()`（WinRT 扫描 start 一次保持稳定）。
- 设备管理器关闭蓝牙适配器「允许计算机关闭此设备以节约电源」；烘焙期间保持机器不睡眠。
- 键盘输入走后台线程 `input()` + `call_soon_threadsafe`（Windows Proactor 不支持 `add_reader(stdin)`）。
- 日志用 `logging` 模块，避免控制台 cp1252 编码报错。

## 测试

```bash
uv run pytest
```

覆盖：两个已知样本的钉死解码（样本 A：battery 70 / 通道4 = 23°C / high 300；
样本 B：battery 59 / 无有效温度）、公司 ID 剥离、负例（battery>100、温度>300、
payload_index>2、过短 payload）、WS getData / keepAlive / 未知命令 / CHARGE 推送、
recorder 轮转与 JSONL/CSV 内容、sim 驱动的端到端爬坡日志。

## 已知未知项

- **量程**：样本暗示整数 °C，若为 0.1° 分辨率读到 235 也通过校验 → 用 `--temp-divisor`。
- **样本 A 的 23 vs 24 °C**：本 hex 精确解码为 23，单测钉死 23；文档 24 疑为不同抓帧。
- **未知/状态字节**（byte3=00、byte6=06、尾部 0000）：与解码无关，但若固件变体插字节
  会偏移 → 靠防御性校验 + 解码失败时记录原始 hex 定位。
- **广告节奏 / 三种 payload 轮转速度**：假设 ~1–2s 内全部出现，实际慢则调低 `--interval`。
