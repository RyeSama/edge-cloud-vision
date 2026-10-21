# ESP32 传感器节点

这块板子**不跑视觉模型**，只做三件事：

1. 采光强、测距离；
2. 用自己的单调微秒计数器打时间戳；
3. 通过 USB 串口，每 100 ms 打一行 JSON。

视觉仍然在 PC 上跑。这样分工的好处是：板子代码小、好烧、好查；时间戳是板子自己的，不会把 USB 和 Python 的调度延迟混进来。

---

## 1. 你需要什么

| 东西 | 说明 |
|---|---|
| ESP32 DevKit | ESP32-WROOM-32 即可，NodeMCU-32S 也行 |
| BH1750 光照模块 | I2C，地址 0x23 |
| HC-SR04 超声波 | 5V 供电，ECHO 需要分压到 3.3V |
| 按键 | 或直接用杜邦线短接 GPIO4 到 GND |
| LED + 220Ω | 指示 trigger |
| 杜邦线、面包板 | |

> HC-SR04 的 ECHO 是 5V 输出，ESP32 的 GPIO 不耐 5V。用 1kΩ + 2kΩ 分压，或者用 3.3V 兼容的测距模块。不要直接把 ECHO 接 GPIO18。

---

## 2. 接线

| 模块 | 模块引脚 | ESP32 |
|---|---|---|
| BH1750 | VCC | 3V3 |
| BH1750 | GND | GND |
| BH1750 | SDA | GPIO21 |
| BH1750 | SCL | GPIO22 |
| HC-SR04 | VCC | 5V / VIN |
| HC-SR04 | GND | GND |
| HC-SR04 | TRIG | GPIO5 |
| HC-SR04 | ECHO | 分压后接 GPIO18 |
| 按键 | 一端 | GPIO4 |
| 按键 | 另一端 | GND |
| LED | 正极 | GPIO2（串 220Ω） |
| LED | 负极 | GND |

按键用 `INPUT_PULLUP`，按下为 LOW，所以 `trigger=true`。

---

## 3. 烧录

1. 安装 Arduino IDE 2.x。
2. 添加 ESP32 板卡地址：
   `文件 → 首选项 → 附加开发板管理器网址`
   填入：
   ```
   https://espressif.github.io/arduino-esp32/package_esp32_index.json
   ```
3. `工具 → 开发板 → 开发板管理器`，搜索 `esp32`，安装 `esp32 by Espressif Systems`。
4. `工具 → 管理库`，搜索 `BH1750`，安装 **BH1750 by Christopher Laws**。
5. 打开 `firmware/esp32_sensor/esp32_sensor.ino`。
6. 开发板选 `ESP32 Dev Module`，端口选实际 COM 口。
7. 点上传。若一直 `Connecting...`，按住 BOOT 键再点上传，出现 `Writing` 后松开。
8. 打开串口监视器，波特率 **115200**，换行符选 `NL` 或 `Both NL & CR`。

正常会看到：

```text
# boot_id=a1b2c3d4 device_id=esp32-01
{"schema_version":"1.0","kind":"sensor","device_id":"esp32-01","boot_id":"a1b2c3d4","seq":0,"t_us":1234567,"lux":512.3,"distance_mm":803.0,"trigger":false}
{"schema_version":"1.0","kind":"sensor","device_id":"esp32-01","boot_id":"a1b2c3d4","seq":1,"t_us":1335678,"lux":511.8,"distance_mm":802.0,"trigger":false}
```

`#` 开头的是日志，PC 会忽略。JSON 行才是数据。

---

## 4. 在 PC 上收数据

不用接板子也能先试：

```bash
python scripts/serial_monitor.py --replay docs/examples/serial-lines.jsonl
```

接板子：

```bash
# Windows
python scripts/serial_monitor.py --port COM5 --baud 115200

# Linux
python scripts/serial_monitor.py --port /dev/ttyUSB0 --baud 115200
```

如果提示 `pyserial is not installed`：

```bash
pip install -e ".[serial]"
```

输出：

```text
  t_s    seq   lux     distance_mm  trigger  note
  -----  ----  ------  -----------  -------  ----
    1.000     1   512.3        803.0        -
    1.100     2   509.8        802.0        -
    1.200     3   507.1        801.0        -
    1.300     4   505.6        800.0        -
    1.400     5   503.9        799.0      YES
```

`note` 出现 `MISSING n` 表示序号跳了；出现 `duplicate/out-of-order` 表示重复或乱序；`reboots` 增加表示 `boot_id` 变了，板子重启过。

---

## 5. 串口协议

完整字段表和主机解析代码见 [`docs/serial-protocol.md`](../../docs/serial-protocol.md)。

最短版本：

- 115200，8N1，UTF-8，`\n` 结尾；
- 一行一个 JSON，不 pretty print；
- `seq` 从 0 递增；
- `t_us` 是 `esp_timer_get_time()`，板子上电后的微秒数，不是 Unix 时间；
- `boot_id` 每次重启变化；
- `lux`、`distance_mm` 可以是 `null`；
- `trigger` 是按键状态。

板子没有 RTC，所以**不要**在板子上伪造 `ts`。PC 收到行的时候再记 wall clock，这样才知道“板子采样”和“PC 收到”之间差多少。

---

## 6. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 串口监视器乱码 | 波特率不是 115200 | 改成 115200 |
| 找不到 COM 口 | 缺 CP210x/CH340 驱动 | 装对应 USB 转串口驱动 |
| 上传一直 Connecting | ESP32 没进下载模式 | 按住 BOOT 再点上传 |
| `BH1750 not found` | SDA/SCL 接反或地址不对 | 检查接线，地址 0x23 |
| `distance_mm` 总是 null | ECHO 没分压/没接/超量程 | 检查 5V、分压和遮挡 |
| 每行都 `MISSING` | `seq` 没递增或板子重启 | 看 `boot_id` 是否变化 |
| PC 端 `pyserial` 报错 | 没装可选依赖 | `pip install -e ".[serial]"` |

---

## 7. STM32 怎么办

协议一样，不需要改 PC 端。把 `.ino` 里的采样换成 HAL：

```c
uint32_t seq = 0;
uint64_t t_us = 0;  // HAL_GetTick() * 1000 + 微秒级补偿
char line[256];
int n = snprintf(line, sizeof(line),
    "{\"schema_version\":\"1.0\",\"kind\":\"sensor\",\"device_id\":\"stm32-01\","
    "\"boot_id\":\"%08lx\",\"seq\":%lu,\"t_us\":%llu,\"lux\":%.1f,"
    "\"distance_mm\":%.1f,\"trigger\":%s}\n",
    boot_id, seq++, t_us, lux, distance_mm, trigger ? "true" : "false");
HAL_UART_Transmit(&huart1, (uint8_t *)line, n, 100);
```

注意 `printf` 浮点支持、`%llu` 支持和 UART 发送阻塞时间；这些本身就是嵌入式测试点。
