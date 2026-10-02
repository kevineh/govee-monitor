# govee-monitor

> **[English](./README.md)** | **中文**

Govee H5055 烧烤温度计 → Artisan 咖啡烘焙温度曲线桥接（Windows 原生 Python，无需 WSL2）。

H5055 持续单向广播 BLE 广告（无需连接 / pair / GATT）。本工具连续扫描广告、
解码双探针温度（豆温 BT + 排气温 ET）、通过 WebSocket 喂给 **Artisan**
画曲线 / RoR，同时本地落盘 JSONL/CSV 以防丢失。

解码字节格式参考 [BastiJoe/govee_h5055_mqtt](https://github.com/BastiJoe/govee_h5055_mqtt)。

## 架构

单进程、单 asyncio 事件循环、3 个并发任务，全部读写同一份内存中的
`DeviceState`，无 MQTT、无数据库。

```
BLE适配器 ──► scanner.py (bleak 连续扫描 detection_callback)
                 │ 解码 → state.py (DeviceState: 6通道/BT/ET/battery/rssi)
        ┌────────┼───────────┐
        ▼        ▼           ▼
   server.py   recorder.py
   websockets  CSV/JSONL
   WS服务       持续落盘
        ▼
   Artisan（同机 127.0.0.1 客户端，画曲线/RoR）
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

## 使用方法

完整流程：**发现设备 → 确认探针通道 → 启动 → 配置 Artisan → 开始烘焙**。

### 1. 发现设备

确认 H5055 在蓝牙范围内，运行：

```bash
uv run python -m govee_monitor --list
```

输出示例（新固件 mfg `0x0070` 也能被发现）：

```
a4:c1:38:70:00:83  rssi=-32  battery=100  mask=000001  channels=[1]  name=None
```

- `mask` 是 6 位连接掩码，`channels` 列出已插探针的传感器号。
- 记下 MAC，后续可用 `--mac` 精确过滤，避免同前缀其它设备干扰。

### 2. 确认探针通道

把豆温 / 排气探针插到对应口，运行 `--list` 看 `mask` 的 bit(n-1) 是否对应
你插的传感器号。H5055 共 6 个口、每口一个传感器。本工具用
`--bt-channel`（豆温 BT）和 `--et-channel`（排气温 ET）指定哪个口当 BT / 当 ET。

> 实测：探针插 1 号口、2 号口 → `mask=000011`、`channels=[1,2]`，即可
> `--bt-channel 1 --et-channel 2`。

### 3. 启动（真机）

```bash
# 豆温=1 号口，排气温=2 号口，每 1 秒落盘，WS 127.0.0.1:9090
uv run python -m govee_monitor --mac a4:c1:38:70:00:83 --bt-channel 1 --et-channel 2 --interval 1 --out ./logs
```

启动后：
- 温度实时通过 WebSocket 推给 Artisan（同机 `127.0.0.1:9090`）。
- 同时持续落盘到 `./logs/roast_<UTC时间戳>.jsonl`（每次启动新建一个文件），
  每行写完立即 flush，即使被强杀也不丢数据。
- `Ctrl+C` 退出并关闭日志文件。

### 4. 配置 Artisan（一次性）

见下方「Artisan 侧配置」。连上后手动 **Start** 即可画曲线，无需事件联动。

### 5. 无 BLE 仿真

没有设备时可用仿真跑通整个链路（合成 payload 100→200°C 爬坡）：

```bash
uv run python -m govee_monitor --sim --bt-channel 4 --et-channel 6
```

### 使用提示

- **刷新速度（重要）**：H5055 会**按温度变化快慢自动调节广播频率**。实测同一段
  连续记录（用手给探针加温后自然冷却）：

  | 阶段 | 每 30 秒广播数 | 观察到的探针读数 |
  |---|---|---|
  | 温度正在变化 | **30**（≈1 Hz） | 4 个不同的值 |
  | 温度已稳定 | **2** | 1 个值，之后无 |

  相差约 **15 倍**。因此**烘焙升温时约每秒一个广播、探针值约每 6 秒更新一次**
  —— 正是最需要的时候；温度稳定后更新自然会慢到几十秒一次。
  另外两点：payload 在**三组探针对之间轮转**，每个广播只带 **6 路中的 2 路**；
  测量值量化到 1°C 且会重复发送，所以**读到重复值是正常的**。
  Artisan 采样间隔调小（`Config » Sampling` → 1s）能让它**尽快取到新值**，
  但无法快过广播本身。
- **扫描参数**：`--scan-mode active`（默认）比 `passive` 明显更能抓到广播突发；
  请保持**单个长驻扫描**——实测周期性重启扫描反而会丢包，所以
  `--restart-on-watchdog` 仅作最后的兜底恢复。
  由于探针稳定时本来就会静默，`--reading-watchdog`（默认 60 秒）专门告警
  **探针读数**是否过期，与 `--watchdog`（默认 30 秒，只管设备是否还在广播）
  分开。**稳定期内触发读数告警是正常的，可以忽略**。
  `--dedupe-window`（默认 0.25 秒）会合并 Windows 大量重复的相同广播。
  另外短窗口测量很不可靠：60 秒窗口曾只捕到 2 个包，而 180 秒窗口捕到 69 个。
- **温度读数**：新固件（mfg `0x0070`）已内置 `/100`，读到的是真实的 °C，无需
  `--temp-divisor`。
- **为什么不用 GATT 连接？** 已逆向出 App 的准实时通道（服务
  `494e5445-…4857`、轮询指令 `0x24`，外加 AES-GCM 会话握手），但**第三方客户端
  无法使用**：握手需要 App 从 Govee 账号/云端下发的每设备密钥，没有密钥设备
  不会响应。因此本项目走上面的广播方案。

## Artisan 侧配置

> 以下以实测的 Artisan 4.x 为例；老版本（≥2.4.2）的路径是 `Config » Port` 的
> 最后一个 tab **Web Sockets**，配置字段相同。无需选机器型号。

1. **切到专家模式**（若 `Config` 菜单项不可见）：4.x 有 UI mode，需在
   `View` 或首选项中切换到 Expert，才能看到完整 `Config` 菜单。
2. **选设备类型**：在 ET/BT 配置里，设备类型选 **WebSocket**（本工具只喂温度，
   不需要选机器型号）。
3. **配置连接**：`Config » Port` 里填 host `127.0.0.1`、port `9090`、path `/`
   （或留空），启用。
4. **节点映射**：每个 Input 有 **node** 和 **request** 两个字段：
   - Input 1（temp1，豆温 BT）：node = `BT`，request 留空
   - Input 2（temp2，排气温 ET）：node = `ET`，request 留空
   - 服务端返回 `{"data": {"BT":.., "ET":..}}`，node 名与 `--node-bt/--node-et`
     对齐（默认 BT/ET）。若 ET / BT 反了，互换 node 值即可，无需改本工具。
5. **Data Request**：填 `getData`（单请求取全部，效率更高；Artisan 每次采样
   发一个 `getData`，服务端回最新温度）。
6. **保存并重启 Artisan**：设备连接在启动时建立，改完配置需完全重启才生效。

温度持续推送，Artisan 端手动 **Start / Stop** 即可控制画图，无需事件联动。

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
| `--scan-mode` | active | `active` \| `passive`（active 更能抓到广播突发） |
| `--watchdog` | 30s | 多久无广告则告警（0=关） |
| `--reading-watchdog` | 60s | 多久无**探针读数**则告警（0=关） |
| `--dedupe-window` | 0.25s | 合并该时间窗内完全相同的广播（0=关） |
| `--restart-on-watchdog` | 关 | 连续 3 次看门狗未收到广告后重启扫描（仅兜底） |
| `--sim` | 关 | 合成 payload，无需 BLE |
| `--list` | 关 | 扫描约 5s 并列出发现的 H5055 |
| `-v` | - | 调试日志 |

## 日志文件

程序启动时新建 `roast_<UTC时间戳>.jsonl`（或 .csv），持续记录到退出，
Ctrl+C 时 flush 并关闭当前文件。每次启动对应一个新文件。JSONL 宽格式一行一个快照：

```json
{"t_iso":"2026-08-11T12:30:00.123Z","t_epoch":1789218600.123,"BT":198.0,"ET":185.5,"battery":70,"rssi":-60}
```

`--log-format channels` 会输出 `ch1..ch6` 全部 6 路原始通道。

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

## 首次部署核对

完整步骤见「使用方法」。首次部署时按序核对：
1. `--list` 能看到设备（含新固件 mfg `0x0070`）。
2. 插探针后 `mask` 的 bit(n-1) 与传感器号对应。
3. `--bt-channel` / `--et-channel` 指向正确（默认 4 / 6）。
4. Artisan 连上 WebSocket 后能画曲线。

## 量程交叉核对（`--temp-divisor`）

原始值像整数 °C（室温 23）。若实际读数约为预期的 10 倍，说明固件是 0.1° 分辨率，
加 `--temp-divisor 10`。新固件（mfg `0x0070`）的 `/100` 已在解码器内完成，
无需该参数。

## Windows 注意事项

- BLE 扫描**不需要管理员权限**，只要蓝牙开着、适配器支持 LE。
- 绑定 `127.0.0.1` → WS 端口不触发 Defender 防火墙弹窗。
- 用连续扫描而非反复 `scan()`（WinRT 扫描 start 一次保持稳定）。
- 设备管理器关闭蓝牙适配器「允许计算机关闭此设备以节约电源」；烘焙期间保持机器不睡眠。
- 日志用 `logging` 模块，避免控制台 cp1252 编码报错。

## 测试

```bash
uv run pytest
```

覆盖：两个已知样本的钉死解码（样本 A：battery 70 / 通道4 = 23°C / high 300；
样本 B：battery 59 / 无有效温度）、公司 ID 剥离、负例（battery>100、温度>300、
payload_index>2、过短 payload）、新固件 `0x0070` 解码与分派、WS getData / keepAlive /
未知命令、recorder 持续记录与 JSONL/CSV 内容、sim 驱动的端到端爬坡日志。

## 已知未知项

- **量程**：样本暗示整数 °C，若为 0.1° 分辨率读到 235 也通过校验 → 用 `--temp-divisor`。
- **样本 A 的 23 vs 24 °C**：本 hex 精确解码为 23，单测钉死 23；文档 24 疑为不同抓帧。
- **未知/状态字节**（byte3=00、byte6=06、尾部 0000）：与解码无关，但若固件变体插字节
  会偏移 → 靠防御性校验 + 解码失败时记录原始 hex 定位。
- **广告节奏**：设备**按温度变化快慢节流**——读数在变化时约 1 次/秒，稳定后几乎停发
  （同一段实测为每 30 秒 30 个 vs 2 个）。同时 payload 在三组探针对之间轮转，因此每个
  广播只带 6 路中的 2 路、变化时每路约每 6 秒重新测量一次。空闲速率波动较大，
  测量窗口建议至少 ~2 分钟（见「使用提示」）。GATT 实时协议已逆向，但需要云端
  下发的每设备密钥，无法使用（见「为什么不用 GATT 连接？」）。
