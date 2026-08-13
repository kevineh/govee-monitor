# govee-monitor

> **English** | [中文](./README.zh-CN.md)

Govee H5055 BBQ thermometer → Artisan coffee roasting temperature curve bridge
(native Windows Python, no WSL2 required).

The H5055 broadcasts BLE advertisements continuously — no connect / pair / GATT
needed. This tool scans the advertisements, decodes the probe temperatures
(bean temperature **BT** + exhaust temperature **ET**), feeds them to
**Artisan** over WebSocket for curves / RoR, and logs locally to JSONL/CSV so
no data is lost.

The byte layout is reverse-engineered from
[BastiJoe/govee_h5055_mqtt](https://github.com/BastiJoe/govee_h5055_mqtt).

## Architecture

Single process, single asyncio event loop, three concurrent tasks, all reading
and writing one in-memory `DeviceState`. No MQTT, no database.

```
BLE adapter ──► scanner.py (bleak continuous scan detection_callback)
                 │ decode → state.py (DeviceState: 6 channels / BT / ET / battery / rssi)
        ┌────────┼───────────┐
        ▼        ▼           ▼
   server.py   recorder.py
   websockets  CSV/JSONL
   WS server    continuous logging
        ▼
   Artisan (localhost 127.0.0.1 client, curves/RoR)
```

## Installation

```bash
# Native Windows Python (or any platform)
uv venv --python 3.11
uv pip install -e ".[dev]"

# View help
uv run python -m govee_monitor --help
```

Dependencies: `bleak` (pulls in the winrt runtime on Windows), `websockets`.
**No** paho-mqtt / bluepy / numpy.

## Usage

Full workflow: **discover device → confirm probe channels → start → configure
Artisan → start roasting**.

### 1. Discover the device

Make sure the H5055 is within Bluetooth range, then run:

```bash
uv run python -m govee_monitor --list
```

Example output (new-firmware mfg `0x0070` devices are found too):

```
a4:c1:38:70:00:83  rssi=-32  battery=100  mask=000001  channels=[1]  name=None
```

- `mask` is the 6-bit connection mask; `channels` lists the sensor numbers
  that have probes plugged in.
- Note the MAC so you can filter with `--mac` and avoid other devices sharing
  the same prefix.

### 2. Confirm the probe channels

Plug your bean / exhaust probes into the ports, run `--list`, and check that
`mask`'s bit(n-1) matches the sensor number you plugged in. The H5055 has 6
ports, one sensor per port. Use `--bt-channel` (bean temp BT) and
`--et-channel` (exhaust temp ET) to pick which port maps to BT / ET.

> Verified in the field: probes in ports 1 and 2 → `mask=000011`,
> `channels=[1,2]`, so use `--bt-channel 1 --et-channel 2`.

### 3. Start (with the real device)

```bash
# bean temp = port 1, exhaust temp = port 2, log every 1s, WS 127.0.0.1:9090
uv run python -m govee_monitor --mac a4:c1:38:70:00:83 --bt-channel 1 --et-channel 2 --interval 1 --out ./logs
```

While running:
- Temperatures are pushed to Artisan in real time over WebSocket
  (`127.0.0.1:9090`).
- Data is continuously logged to `./logs/roast_<UTC-timestamp>.jsonl` (a new
  file per run). Every row is flushed immediately, so a hard kill never loses
  data.
- `Ctrl+C` exits and closes the log file.

### 4. Configure Artisan (one-time)

See the "Artisan configuration" section below. Once connected, press
**Start** manually to draw curves — no event linkage is required.

### 5. Simulate without BLE

When you have no device, run a simulation of the whole pipeline (synthetic
payload ramping 100→200 °C):

```bash
uv run python -m govee_monitor --sim --bt-channel 4 --et-channel 6
```

### Usage notes

- **Refresh rate (important)**: the H5055 delivers data by broadcast when
  idle, measured at roughly **one advertisement every 15–30 seconds**
  (broadcast is the official data channel — Home Assistant's govee_ble and
  Theengs both parse it from broadcasts only). So temperatures update in
  15–30 s steps, not as a smooth continuous curve.
  The Govee app is near-real-time (<5s) because it opens a **GATT
  connection** that exclusively uses the device (the device stops
  broadcasting while connected) and streams via a private notify protocol,
  which has no public reverse-engineering yet. This tool therefore uses
  broadcast mode, and **the refresh ceiling is the device's own broadcast
  rate**.
  Lower Artisan's sampling interval (`Config » Sampling` → 1s) so it pulls a
  new value right after each broadcast, but it cannot be faster than the
  broadcasts themselves.
- **Temperature values**: new firmware (mfg `0x0070`) already divides by 100
  internally, so you read real °C and don't need `--temp-divisor`.

## Artisan configuration

> The steps below are for Artisan 4.x (tested). On older versions (≥2.4.2)
> the path is the last **Web Sockets** tab under `Config » Port` with the
> same fields. No machine model is required.

1. **Switch to Expert mode** (if the `Config` menu is not visible): 4.x has
   UI modes — switch to Expert via `View` or preferences to see the full
   `Config` menu.
2. **Choose the device type**: in the ET/BT configuration, set the device
   type to **WebSocket** (this tool only feeds temperature; no machine model
   is needed).
3. **Configure the connection**: in `Config » Port`, set host `127.0.0.1`,
   port `9090`, path `/` (or leave empty), and enable it.
4. **Node mapping**: each Input has **node** and **request** fields:
   - Input 1 (temp1, bean temp BT): node = `BT`, request leave empty
   - Input 2 (temp2, exhaust temp ET): node = `ET`, request leave empty
   - The server returns `{"data": {"BT":.., "ET":..}}`; the node names match
     `--node-bt/--node-et` (defaults BT/ET). If ET and BT look swapped, just
     swap the node values — no tool changes needed.
5. **Data Request**: set it to `getData` (one request fetches everything,
     more efficient; Artisan sends one `getData` per sample and the server
     replies with the latest temperature).
6. **Save and restart Artisan**: the device connection is established at
   startup, so you must fully restart Artisan after changing the config.

Temperatures are pushed continuously; use Artisan's manual **Start / Stop**
to control plotting. No event linkage.

## Command line options

| Option | Default | Description |
| --- | --- | --- |
| `--mac` | prefix `a4:c1:38` | Filter by exact MAC |
| `--bt-channel` / `--et-channel` | 4 / 6 | Sensor number (1–6) for bean temp BT / exhaust temp ET |
| `--host` / `--port` | 127.0.0.1 / 9090 | WS bind address/port (localhost loopback avoids the firewall prompt) |
| `--ws-path` | `/` | WS path (informational; all paths accepted) |
| `--node-bt` / `--node-et` | BT / ET | Node names fed to Artisan |
| `--interval` | 2s | Log snapshot interval |
| `--out` | ./logs | Output directory for log files |
| `--log-format` | jsonl | `jsonl` \| `csv` \| `channels` (channels = 6-column CSV) |
| `--temp-divisor` | 1 | Divide raw readings by this (see "Range cross-check") |
| `--scan-mode` | active | `active` \| `passive` |
| `--watchdog` | 30s | Warn after this many seconds without advertisements (0=off) |
| `--restart-on-watchdog` | off | Restart the scan after 3 missed watchdog intervals |
| `--sim` | off | Synthetic payloads, no BLE needed |
| `--list` | off | Scan ~5s and list discovered H5055 devices |
| `-v` | - | Debug logging |

## Log files

A new `roast_<UTC-timestamp>.jsonl` (or .csv) file is created at startup and
written continuously until exit; `Ctrl+C` flushes and closes it. Each run gets
a new file. JSONL wide format, one snapshot per line:

```json
{"t_iso":"2026-08-11T12:30:00.123Z","t_epoch":1789218600.123,"BT":198.0,"ET":185.5,"battery":70,"rssi":-60}
```

`--log-format channels` outputs all 6 raw channels as `ch1..ch6`.

## Decode format

22-byte payload (Windows bleak strips the company ID `0xEC88`; the code keeps
a defensive strip).

| Offset (byte) | Meaning |
| --- | --- |
| 4 | Battery (0–100) |
| 5 | Channel byte: `payload_index=(b>>6)&3`, `connection_mask=b&0x3F` |
| 7–8 | A-slot value (sensor `base=2*payload_index+1`, little-endian) |
| 9–10 / 11–12 | A-slot low / high alarm thresholds |
| 14–15 | B-slot value (sensor `base+1`, little-endian) |
| 16–17 / 18–19 | B-slot low / high alarm thresholds |

`0xFFFF` = no probe connected / value not set. Each advertisement carries only
one sensor pair; the scanner aggregates the 3 payload variants to assemble the
full 6 channels.

### New firmware (mfg `0x0070`, 20 bytes, big-endian, `/100` built in)

Newer H5055 firmware broadcasts a different manufacturer data block (mfg key
`0x0070`, service `00005550-…`): 20 bytes, **big-endian**, temperature raw
value divided by 100 to get °C — the decoder already applies `/100`, so **no
`--temp-divisor` is needed**.

| Offset (byte) | Meaning |
| --- | --- |
| 5 | Battery (`& 0x7F`, top bit is a flag, 0–100) |
| 6 | Channel byte: `payload_index=(b>>6)&3`, `connection_mask=b&0x3F` |
| 8–9 | A-slot value (sensor `base=2*payload_index+1`, big-endian `/100`) |
| 10–11 / 12–13 | A-slot low / high alarm thresholds (big-endian) |
| 14–15 | B-slot value (sensor `base+1`, big-endian `/100`) |
| 16–17 / 18–19 | B-slot low / high alarm thresholds (big-endian) |

Same `0xFFFF` = no probe / value not set. The scanner recognizes both `0xEC88`
(old) and `0x0070` (new) formats.

## First-time deployment checklist

Full steps are in "Usage". For first-time deployment, verify in order:
1. `--list` sees the device (including new firmware mfg `0x0070`).
2. With probes plugged in, `mask`'s bit(n-1) matches the sensor number.
3. `--bt-channel` / `--et-channel` point to the right ports (defaults 4 / 6).
4. Artisan draws curves once connected over WebSocket.

## Range cross-check (`--temp-divisor`)

Raw values look like whole °C (room temp 23). If a reading is roughly 10× the
expected value, the firmware is 0.1° resolution — add `--temp-divisor 10`.
New firmware (mfg `0x0070`) already divides by 100 in the decoder, so this
option isn't needed there.

## Windows notes

- BLE scanning **does not require admin rights**, just Bluetooth enabled and
  an LE-capable adapter.
- Binding `127.0.0.1` means the WS port doesn't trigger a Defender firewall
  prompt.
- Use a continuous scan rather than repeated `scan()` (a single WinRT scan
  start stays stable).
- In Device Manager, disable "Allow the computer to turn off this device to
  save power" for the Bluetooth adapter; keep the machine awake during
  roasting.
- Logging uses the `logging` module to avoid console cp1252 encoding errors.

## Testing

```bash
uv run pytest
```

Coverage: pinned decoding of two known samples (sample A: battery 70 /
channel 4 = 23 °C / high 300; sample B: battery 59 / no valid temperature),
company-ID stripping, negative cases (battery > 100, temp > 300,
payload_index > 2, too-short payload), new-firmware `0x0070` decoding and
dispatch, WS getData / keepAlive / unknown commands, continuous recorder
JSONL/CSV content, and sim-driven end-to-end ramp logs.

## Known unknowns

- **Range**: samples suggest whole °C; if a 0.1° device reads 235 it still
  passes validation → use `--temp-divisor`.
- **Sample A's 23 vs 24 °C**: this hex decodes to exactly 23 (pinned in the
  test); the 24 in some docs is likely a different capture.
- **Unknown/status bytes** (byte3=00, byte6=06, trailing 0000): not relevant
  to decoding, but a firmware variant that inserts bytes would shift the
  offsets → mitigated by defensive validation and logging the raw hex on
  decode failure.
- **Broadcast cadence**: new firmware measured at roughly one broadcast every
  15–30 s idle, with the 3 payload variants rotating even more sparsely;
  refresh is limited by this (see "Usage notes"). A future GATT real-time
  protocol could break past it.
