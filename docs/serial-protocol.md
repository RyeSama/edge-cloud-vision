# 串口协议：板子到 PC

这是一条**单向、文本、按行**的链路。板子只负责采样和打自己的单调时间戳；PC 负责 wall clock、校验、序号跟踪和显示。

## 1. 物理层

| 项目 | 值 |
|---|---|
| 接口 | USB 转 UART（ESP32 板载 CP210x/CH340） |
| 波特率 | 115200 |
| 数据位/校验/停止位 | 8 / None / 1（8N1） |
| 编码 | UTF-8 |
| 行尾 | `\n`（Arduino `Serial.printf` 自带） |
| 方向 | 板子 → PC；PC 不发命令 |

## 2. 帧格式

一行一个 JSON 对象，不要 pretty print。空行和 `#` 开头的日志行忽略。

```json
{"schema_version":"1.0","kind":"sensor","device_id":"esp32-01","boot_id":"a1b2c3d4","seq":42,"t_us":12345678,"lux":512.3,"distance_mm":803.0,"trigger":false}
```

| 字段 | 类型 | 必填 | 含义 |
|---|---|---:|---|
| `schema_version` | string | 是 | 当前固定 `"1.0"` |
| `kind` | string | 是 | 固定 `"sensor"`，给未来扩展留位 |
| `device_id` | string | 是 | 设备名，如 `esp32-01`，只允许字母数字 `_.:-` |
| `boot_id` | string | 是 | 每次上电/复位随机生成的 8 位十六进制 |
| `seq` | int ≥0 | 是 | 采样序号，每次采样 +1 |
| `t_us` | int ≥0 | 是 | 板子上电后的单调微秒数，**不是 Unix 时间** |
| `lux` | float/null | 否 | 光照，单位 lx；读不到为 `null` |
| `distance_mm` | float/null | 否 | 距离，单位 mm；无回波为 `null` |
| `trigger` | bool | 否 | 按键/外部触发状态，默认 `false` |

约束：

- `t_us` 用 64 位，不要用 32 位 `micros()`，否则约 71 分钟回绕。
- `seq` 用 `uint32_t` 也会回绕，但 10 Hz 下约 13 年，演示够用；生产可换成 64 位。
- 板子**没有 RTC**，不要在板子上填 `ts`。PC 收到行时记 `time.time_ns()`，这样“采样时刻”和“到达时刻”是分开的。
- 一行超过 2048 字节视为错误；正常一行约 180–220 字节。

## 3. 板子端怎么写

Arduino/ESP32：

```c
const int64_t t_us = esp_timer_get_time();
Serial.printf(
  "{\"schema_version\":\"1.0\",\"kind\":\"sensor\",\"device_id\":\"%s\","
  "\"boot_id\":\"%s\",\"seq\":%lu,\"t_us\":%lld,\"lux\":%s,"
  "\"distance_mm\":%s,\"trigger\":%s}\n",
  DEVICE_ID, boot_id, (unsigned long)seq++, (long long)t_us,
  lux_text, distance_text, trigger ? "true" : "false");
```

STM32：

```c
int n = snprintf(line, sizeof(line),
  "{\"schema_version\":\"1.0\",\"kind\":\"sensor\",\"device_id\":\"stm32-01\","
  "\"boot_id\":\"%08lx\",\"seq\":%lu,\"t_us\":%llu,\"lux\":%.1f,"
  "\"distance_mm\":%.1f,\"trigger\":%s}\n",
  boot_id, seq++, t_us, lux, distance_mm, trigger ? "true" : "false");
HAL_UART_Transmit(&huart1, (uint8_t *)line, n, 100);
```

注意：`snprintf` 的浮点支持、`%llu` 支持、UART 阻塞时间，都是实际测试点，不要假设工具链默认打开。

## 4. PC 端怎么写

核心就三步：读一行、`json.loads`、Pydantic 校验。

```python
from ecvision.transport.serial_json import parse_sensor_line

with open("capture.jsonl", encoding="utf-8") as f:
    for raw in f:
        try:
            reading = parse_sensor_line(raw)
        except ValueError as exc:
            print("bad line:", exc)
            continue
        print(reading.seq, reading.t_us, reading.lux, reading.distance_mm)
```

真实串口：

```python
from ecvision.transport.serial_json import iter_serial, iter_sensor_lines

for reading in iter_sensor_lines(iter_serial("COM5", baud=115200)):
    print(reading.model_dump_json())
```

没有硬件时用回放：

```bash
python scripts/serial_monitor.py --replay docs/examples/serial-lines.jsonl
```

## 5. 序号、重启和迟到

`SensorSequenceTracker` 做三件事：

- `seq` 比上次大 1：正常；
- `seq` 跳号：`gaps += n`，说明丢行；
- `seq` 重复/变小：`duplicates += 1`；
- `boot_id` 变了：`reboots += 1`，从新序号重新开始，不把重启误判成丢包。

```python
from ecvision.transport.serial_json import SensorSequenceTracker

tracker = SensorSequenceTracker()
missing = tracker.observe(reading)
if missing > 0:
    print(f"missing {missing} samples")
```

PC 收到行时记 `received_ns = time.time_ns()`。如果要估计“板子采样到 PC 收到”的延迟，需要在实验开始时让板子和 PC 对齐一次（例如板子发一个已知时刻的脉冲，PC 同时记时间），不能直接拿 `t_us` 和 Unix 时间相减。

## 6. 错误处理约定

| 情况 | PC 行为 |
|---|---|
| 空行 / `#` 注释 | 跳过，不计数 |
| JSON 语法错误 | 计一次 parse error，继续读下一行 |
| 字段缺失/类型错/多余字段 | Pydantic 拒绝，计一次 parse error |
| 一行超 2048 字节 | 拒绝，通常是波特率或接线问题 |
| 连续大量错误 | 提示检查波特率、共地、USB 线 |

不静默把坏行改成 0 或 `null`。测量系统里，“坏数据”必须显式可见。

## 7. 为什么不用二进制/CRC

这是教学和第一次联调用的协议，优先可读、可抓、可手写。JSON 的代价是每行约 200 字节，10 Hz 只有 2 KB/s，USB 串口完全够。

如果现场干扰大，再加一层：

- 帧头 `0xAA55` + 长度 + CRC16；
- 或者每行末尾加 `*AB` 的 NMEA 风格校验；
- 或者改用 Modbus RTU。

这些是 v0.7 的候选，不是现在的前置条件。
